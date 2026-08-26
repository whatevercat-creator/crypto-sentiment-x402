import asyncio
from cdp import CdpClient
from cdp.evm_local_account import EvmLocalAccount
from x402 import x402Client
from x402.http.clients import x402HttpxClient
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact import ExactEvmScheme

async def main() -> None:
    async with CdpClient() as cdp:
        account = await cdp.evm.get_or_create_account(name="x402-client-wallet-1")
        signer = EthAccountSigner(EvmLocalAccount(account))
        print(f"Paying from {signer.address}")

        balances = await cdp.evm.list_token_balances(address=signer.address, network="base")
        usdc_balance = next(
            (b.amount.amount for b in balances.balances if b.token.symbol == "USDC"), 0
        )
        eth_balance = next(
            (b.amount.amount for b in balances.balances if b.token.symbol == "ETH"), 0
        )
        print(f"USDC={usdc_balance} ETH={eth_balance}")

        payment_client = x402Client()
        payment_client.register("eip155:8453", ExactEvmScheme(signer))

        async with x402HttpxClient(payment_client) as http:
            response = await http.get("https://crypto-sentiment-x402.onrender.com/sentiment/BTC")
            await response.aread()

        print(f"HTTP {response.status_code}")
        print(response.text)

asyncio.run(main())
