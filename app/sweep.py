"""
Automated hot-wallet -> cold-wallet USDC sweep.

Every SWEEP_INTERVAL_SECONDS, checks the hot wallet's USDC balance and, if it is
above SWEEP_THRESHOLD_USD, transfers everything above SWEEP_BUFFER_USD to
SWEEP_TO_ADDRESS. Signing goes through the CDP server wallet API, so the hot
wallet must be a CDP-managed EVM account under the configured CDP credentials
(CDP_API_KEY_ID / CDP_API_KEY_SECRET / CDP_WALLET_SECRET). An externally-held
address (MetaMask, Coinbase Wallet, ...) cannot be swept from here.

Env vars (see .env.example):
  SWEEP_TO_ADDRESS        - cold wallet that receives sweeps. Unset = sweep disabled.
  SWEEP_FROM_ADDRESS      - hot wallet to sweep from (default: PAY_TO_ADDRESS)
  SWEEP_THRESHOLD_USD     - only sweep when USDC balance exceeds this (default 1.00)
  SWEEP_BUFFER_USD        - USDC left behind in the hot wallet (default 0.10)
  SWEEP_MIN_ETH           - skip the sweep if ETH is below this, to cover gas (default 0.00005)
  SWEEP_INTERVAL_SECONDS  - how often to check (default 1800)
  SWEEP_DRY_RUN           - defaults to ON. While on, every check runs (including
                            confirming the hot wallet is CDP-managed) and what WOULD be
                            swept is logged, but transfer() is never called. Set it to
                            "0"/"false" explicitly to move real funds.
"""

import asyncio
import logging
import os
from decimal import Decimal, InvalidOperation

from cdp import CdpClient
from eth_utils import is_address, to_checksum_address

logger = logging.getLogger("app.sweep")

# Balances are matched by contract address, never by symbol -- wallets receive
# airdropped scam tokens that spoof the "USDC" ticker.
USDC_CONTRACTS = {
    "base": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "base-sepolia": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
}
ETH_PLACEHOLDER = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"  # EIP-7528 native ETH
USDC_DECIMALS = 6
ETH_DECIMALS = 18


def _decimal_env(name: str, default: str) -> Decimal:
    raw = os.environ.get(name, default)
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise RuntimeError(f"{name}={raw!r} is not a valid number")
    if value < 0:
        raise RuntimeError(f"{name} must not be negative")
    return value


def _atomic(amount: Decimal, decimals: int) -> int:
    return int(amount * (10**decimals))


def _fmt_usdc(atomic: int) -> str:
    return f"{Decimal(atomic) / 10**USDC_DECIMALS:.6f}"


def _fmt_eth(atomic: int) -> str:
    return f"{Decimal(atomic) / 10**ETH_DECIMALS:.8f}"


def _load_config() -> dict | None:
    """Returns the sweep config, or None (with a log line saying why) if disabled."""
    to_address = os.environ.get("SWEEP_TO_ADDRESS", "").strip()
    if not to_address:
        logger.info("[sweep] disabled: SWEEP_TO_ADDRESS is not set")
        return None

    from_address = (
        os.environ.get("SWEEP_FROM_ADDRESS") or os.environ.get("PAY_TO_ADDRESS") or ""
    ).strip()

    for label, addr in (("SWEEP_TO_ADDRESS", to_address), ("SWEEP_FROM_ADDRESS", from_address)):
        if not is_address(addr):
            logger.error("[sweep] disabled: %s=%r is not a valid address", label, addr)
            return None
    to_address, from_address = to_checksum_address(to_address), to_checksum_address(from_address)
    if to_address == from_address:
        logger.error("[sweep] disabled: destination and source are the same address")
        return None

    if not os.environ.get("CDP_WALLET_SECRET"):
        logger.error("[sweep] disabled: CDP_WALLET_SECRET is not set (needed to sign transfers)")
        return None

    mainnet = os.environ.get("X402_NETWORK", "testnet") == "mainnet"
    try:
        return {
            "to": to_address,
            "from": from_address,
            "network": "base" if mainnet else "base-sepolia",
            "threshold": _atomic(_decimal_env("SWEEP_THRESHOLD_USD", "1.00"), USDC_DECIMALS),
            "buffer": _atomic(_decimal_env("SWEEP_BUFFER_USD", "0.10"), USDC_DECIMALS),
            "min_eth": _atomic(_decimal_env("SWEEP_MIN_ETH", "0.00005"), ETH_DECIMALS),
            "interval": int(os.environ.get("SWEEP_INTERVAL_SECONDS", "1800")),
            # Fail safe: only an explicit "0"/"false"/"no" enables real transfers.
            "dry_run": os.environ.get("SWEEP_DRY_RUN", "1").strip().lower()
            not in ("0", "false", "no"),
        }
    except (RuntimeError, ValueError) as e:
        logger.error("[sweep] disabled: bad config: %s", e)
        return None


async def _sweep_once(cfg: dict) -> None:
    usdc_contract = USDC_CONTRACTS[cfg["network"]].lower()

    async with CdpClient() as cdp:
        # The result is paginated and the wallet may hold many airdropped spam tokens,
        # so walk every page -- otherwise USDC/ETH can be missed and read as zero.
        usdc = eth = 0
        page_token = None
        while True:
            page = await cdp.evm.list_token_balances(
                address=cfg["from"], network=cfg["network"], page_token=page_token
            )
            for b in page.balances:
                contract = b.token.contract_address.lower()
                if contract == usdc_contract:
                    usdc += b.amount.amount
                elif contract == ETH_PLACEHOLDER:
                    eth += b.amount.amount
            page_token = page.next_page_token
            if not page_token:
                break

        if usdc <= cfg["threshold"]:
            logger.info(
                "[sweep] no sweep: USDC %s <= threshold %s (ETH %s)",
                _fmt_usdc(usdc), _fmt_usdc(cfg["threshold"]), _fmt_eth(eth),
            )
            return

        amount = usdc - cfg["buffer"]
        if amount <= 0:
            return

        if eth < cfg["min_eth"]:
            logger.warning(
                "[sweep] SKIPPED, LOW ETH FOR GAS: hot wallet %s has %s ETH, needs >= %s. "
                "%s USDC is waiting to be swept -- top up ETH on Base manually.",
                cfg["from"], _fmt_eth(eth), _fmt_eth(cfg["min_eth"]), _fmt_usdc(usdc),
            )
            return

        # get_account raises (404) if the hot wallet isn't CDP-managed under these creds.
        # Done before the dry-run exit so a dry run also proves the wallet is signable.
        account = await cdp.evm.get_account(address=cfg["from"])

        if cfg["dry_run"]:
            logger.info(
                "[sweep] DRY RUN, NO TRANSFER MADE: would sweep %s USDC %s -> %s "
                "(balance %s, buffer %s, ETH %s, network %s; wallet is CDP-managed)",
                _fmt_usdc(amount), cfg["from"], cfg["to"],
                _fmt_usdc(usdc), _fmt_usdc(cfg["buffer"]), _fmt_eth(eth), cfg["network"],
            )
            return

        logger.info(
            "[sweep] attempting: %s USDC %s -> %s (balance %s, buffer %s, ETH %s, network %s)",
            _fmt_usdc(amount), cfg["from"], cfg["to"],
            _fmt_usdc(usdc), _fmt_usdc(cfg["buffer"]), _fmt_eth(eth), cfg["network"],
        )
        tx_hash = await account.transfer(
            to=cfg["to"],
            amount=amount,
            token=USDC_CONTRACTS[cfg["network"]],
            network=cfg["network"],
        )
        # The hash means CDP accepted and broadcast the tx, not that it has been
        # mined; the next cycle's balance check is the confirmation.
        logger.info(
            "[sweep] SUBMITTED: %s USDC -> %s tx=%s", _fmt_usdc(amount), cfg["to"], tx_hash
        )


async def sweep_loop() -> None:
    cfg = _load_config()
    if cfg is None:
        return
    logger.info(
        "[sweep] enabled (%s): %s -> %s on %s every %ss (threshold %s USDC, buffer %s USDC)",
        "DRY RUN" if cfg["dry_run"] else "LIVE, WILL MOVE REAL FUNDS",
        cfg["from"], cfg["to"], cfg["network"], cfg["interval"],
        _fmt_usdc(cfg["threshold"]), _fmt_usdc(cfg["buffer"]),
    )
    while True:
        try:
            await _sweep_once(cfg)
        except Exception:
            # Never let a bad round kill the loop or the API -- retry next interval.
            logger.exception("[sweep] FAILED this cycle, will retry in %ss", cfg["interval"])
        await asyncio.sleep(cfg["interval"])
