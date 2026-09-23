"""
Backward-compatible entry point.

The MCP server now lives in the installable `crypto_sentiment_x402_mcp`
package (see pyproject.toml and MCP.md) so it can be published to PyPI and
the MCP Registry. Running this file directly still works for anyone who
clones the repo instead of `pip install`-ing the package -- Python adds this
script's own directory to `sys.path`, and `crypto_sentiment_x402_mcp/` is
right next to it.
"""

from crypto_sentiment_x402_mcp import main

if __name__ == "__main__":
    main()
