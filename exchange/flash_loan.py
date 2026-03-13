"""
exchange/flash_loan.py — Python integration for the FlashLoanArb Solidity contract.

This module is the Python side of the flash loan system.
The contract lives on Base mainnet at the address in FLASH_LOAN_CONTRACT (.env).

Responsibilities:
    1. Scan for arb opportunities by comparing quotes across Uniswap V3 and Aerodrome.
    2. Estimate net profit after Aave fee + gas.
    3. If profitable, ABI-encode TradeParams and call contract.executeArb().
    4. Monitor and log outcomes.
    5. Withdraw accumulated profit to owner wallet on command.

Flow:
    scanner/dexscreener.py  →  finds token pairs with price discrepancies
    flash_loan.py           →  checks if the spread is wide enough to arb
                               → if yes, calls the on-chain contract
    contract (Solidity)     →  borrows, swaps, repays, keeps profit atomically

Key env vars:
    BASE_RPC_URL            — Base mainnet RPC (Coinbase public or Alchemy)
    BASE_PRIVATE_KEY        — Wallet that owns the contract
    FLASH_LOAN_CONTRACT     — Deployed FlashLoanArb address (set after deploy)

Usage:
    from exchange.flash_loan import FlashLoanArb

    async with FlashLoanArb() as arb:
        result = await arb.find_and_execute()
        # or
        profit = await arb.check_opportunity(token_mid=WETH_ADDRESS, borrow_usdc=5000)
"""

import os
import asyncio
from typing import Optional
from web3 import AsyncWeb3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_account import Account
from loguru import logger

# ── Token + contract addresses (Base mainnet) ─────────────────────────────────
USDC_ADDRESS    = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH_ADDRESS    = "0x4200000000000000000000000000000000000006"
USDC_DECIMALS   = 6

# DEX identifiers — must match the Solidity constants
DEX_UNISWAP     = 0
DEX_AERODROME   = 1

# Aave V3 flash loan fee: 5 bps = 0.05%
AAVE_PREMIUM_BPS = 5

# Default slippage for arb swaps (tighter than meme trades — arb is capital-certain)
ARB_SLIPPAGE_BPS = 50   # 0.5%

# ── Contract ABI (only the functions the bot calls) ───────────────────────────

FLASH_LOAN_ABI = [
    # executeArb(address tokenBorrow, uint256 amount, bytes params)
    {
        "inputs": [
            {"name": "tokenBorrow", "type": "address"},
            {"name": "amount",      "type": "uint256"},
            {"name": "params",      "type": "bytes"},
        ],
        "name": "executeArb",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # withdrawProfit(address token, uint256 amount)
    {
        "inputs": [
            {"name": "token",  "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "withdrawProfit",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # tokenBalance(address token) view returns (uint256)
    {
        "inputs": [{"name": "token", "type": "address"}],
        "name": "tokenBalance",
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    # setMinProfit(uint256 newMin)
    {
        "inputs": [{"name": "newMin", "type": "uint256"}],
        "name": "setMinProfit",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # minProfitDefault() view returns (uint256)
    {
        "inputs": [],
        "name": "minProfitDefault",
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    # ArbExecuted event
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "name": "tokenBorrow", "type": "address"},
            {"indexed": True,  "name": "tokenMid",    "type": "address"},
            {"indexed": False, "name": "borrowed",    "type": "uint256"},
            {"indexed": False, "name": "premium",     "type": "uint256"},
            {"indexed": False, "name": "profit",      "type": "uint256"},
        ],
        "name": "ArbExecuted",
        "type": "event",
    },
]

# TradeParams ABI type (for eth_abi encoding)
TRADE_PARAMS_TYPE = "(uint8,uint8,address,uint24,uint24,bool,bool,uint256,uint256)"


class FlashLoanArb:
    """
    Python interface to the deployed FlashLoanArb Solidity contract.

    Handles opportunity scanning, profit estimation, transaction construction,
    and result logging. Used by main.py's scheduler and by Telegram /arb commands.
    """

    def __init__(self):
        self._w3:              Optional[AsyncWeb3] = None
        self._account:         Optional[Account]   = None
        self._address:         str                 = ""
        self._contract:        Optional[object]    = None
        self._contract_address: str                = ""

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def connect(self):
        rpc_url          = os.getenv("BASE_RPC_URL",         "https://mainnet.base.org").strip()
        private_key      = os.getenv("BASE_PRIVATE_KEY",     "").strip()
        contract_address = os.getenv("FLASH_LOAN_CONTRACT",  "").strip()

        if not private_key:
            raise ValueError("BASE_PRIVATE_KEY not set in .env")
        if not contract_address:
            raise ValueError(
                "FLASH_LOAN_CONTRACT not set in .env. "
                "Deploy the contract first: cd hardhat && npm run deploy:mainnet"
            )

        if not private_key.startswith("0x"):
            private_key = "0x" + private_key

        self._w3      = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))
        self._w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self._account = Account.from_key(private_key)
        self._address = self._account.address

        connected = await self._w3.is_connected()
        if not connected:
            raise ConnectionError(f"Cannot connect to Base RPC: {rpc_url}")

        self._contract_address = AsyncWeb3.to_checksum_address(contract_address)
        self._contract = self._w3.eth.contract(
            address=self._contract_address,
            abi=FLASH_LOAN_ABI
        )

        logger.info(f"FlashLoanArb ready | contract={self._contract_address} | wallet={self._address}")

    async def close(self):
        logger.info("FlashLoanArb connector closed")

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()

    # ── Opportunity scanner ────────────────────────────────────────────────

    async def check_opportunity(
        self,
        token_mid:      str   = WETH_ADDRESS,
        borrow_usdc:    float = 5000.0,
        dex_a:          int   = DEX_UNISWAP,
        dex_b:          int   = DEX_AERODROME,
        fee_tier_a:     int   = 500,
        fee_tier_b:     int   = 3000,
        stable_a:       bool  = False,
        stable_b:       bool  = False,
    ) -> dict:
        """
        Simulate the arb off-chain to check profitability BEFORE sending a transaction.

        Calls Uniswap/Aerodrome quoter contracts (read-only, no gas).
        Returns a dict with profitability data.

        Args:
            token_mid:    Intermediate token for the two-leg path (default: WETH)
            borrow_usdc:  USDC amount to simulate borrowing
            dex_a / dex_b: DEX for each leg
            fee_tier_a/b: Uniswap fee tier (ignored for Aerodrome legs)
            stable_a/b:   Aerodrome stable flag (ignored for Uniswap legs)

        Returns:
            {
              "profitable":    bool,
              "borrow_usdc":   float,
              "aave_fee_usdc": float,
              "gas_cost_usdc": float,
              "gross_profit":  float,
              "net_profit":    float,
              "profit_pct":    float,
              "params_encoded": bytes,  -- ready to pass to execute_arb()
              "error":         str | None,
            }
        """
        from exchange.base import BaseConnector, USDC_DECIMALS, USDC_ADDRESS

        result = {
            "profitable":     False,
            "borrow_usdc":    borrow_usdc,
            "aave_fee_usdc":  0.0,
            "gas_cost_usdc":  0.0,
            "gross_profit":   0.0,
            "net_profit":     0.0,
            "profit_pct":     0.0,
            "params_encoded": b"",
            "error":          None,
        }

        try:
            async with BaseConnector() as base:
                borrow_wei = int(borrow_usdc * 10 ** USDC_DECIMALS)

                # Leg A quote: USDC -> tokenMid
                quote_a = await base.get_quote_uniswap(USDC_ADDRESS, token_mid, borrow_wei, fee_tier_a) \
                          if dex_a == DEX_UNISWAP else \
                          await base.get_quote_aerodrome(USDC_ADDRESS, token_mid, borrow_wei, stable_a)

                if quote_a.get("amount_out", 0) == 0:
                    result["error"] = "Leg A: no route"
                    return result

                mid_amount_wei = quote_a["amount_out"]

                # Leg B quote: tokenMid -> USDC
                quote_b = await base.get_quote_uniswap(token_mid, USDC_ADDRESS, mid_amount_wei, fee_tier_b) \
                          if dex_b == DEX_UNISWAP else \
                          await base.get_quote_aerodrome(token_mid, USDC_ADDRESS, mid_amount_wei, stable_b)

                if quote_b.get("amount_out", 0) == 0:
                    result["error"] = "Leg B: no route"
                    return result

                final_usdc_wei = quote_b["amount_out"]

                # Aave fee (0.05% of borrow)
                aave_fee_wei  = int(borrow_wei * AAVE_PREMIUM_BPS / 10000)
                repay_wei     = borrow_wei + aave_fee_wei

                # Estimate gas cost in USDC (rough: ~300k gas * current gas price)
                gas_price_wei = await self._w3.eth.gas_price
                gas_units     = 300_000
                gas_eth       = gas_price_wei * gas_units / 1e18
                eth_price_usdc = await self._get_eth_price_usdc()
                gas_cost_usdc = gas_eth * eth_price_usdc

                gross_profit = (final_usdc_wei - repay_wei) / 10 ** USDC_DECIMALS
                net_profit   = gross_profit - gas_cost_usdc
                profit_pct   = (net_profit / borrow_usdc) * 100 if borrow_usdc > 0 else 0

                # Encode params for on-chain call
                from eth_abi import encode as abi_encode
                min_profit_wei = max(0, int(net_profit * 0.8 * 10 ** USDC_DECIMALS))
                encoded_params = abi_encode(
                    [TRADE_PARAMS_TYPE],
                    [(dex_a, dex_b, AsyncWeb3.to_checksum_address(token_mid),
                      fee_tier_a, fee_tier_b, stable_a, stable_b,
                      min_profit_wei, ARB_SLIPPAGE_BPS)]
                )

                result.update({
                    "profitable":     net_profit > 0,
                    "aave_fee_usdc":  aave_fee_wei / 10 ** USDC_DECIMALS,
                    "gas_cost_usdc":  gas_cost_usdc,
                    "gross_profit":   gross_profit,
                    "net_profit":     net_profit,
                    "profit_pct":     profit_pct,
                    "params_encoded": encoded_params,
                    "dex_a":          dex_a,
                    "dex_b":          dex_b,
                    "token_mid":      token_mid,
                    "fee_tier_a":     fee_tier_a,
                    "fee_tier_b":     fee_tier_b,
                    "stable_a":       stable_a,
                    "stable_b":       stable_b,
                })

                logger.info(
                    f"Arb check | borrow=${borrow_usdc:.0f} "
                    f"gross={gross_profit:.4f} gas={gas_cost_usdc:.4f} "
                    f"net={net_profit:.4f} USDC | profitable={net_profit > 0}"
                )

        except Exception as e:
            result["error"] = str(e)
            logger.error(f"check_opportunity failed: {e}")

        return result

    # ── Execute arb ────────────────────────────────────────────────────────

    async def execute_arb(
        self,
        borrow_token:    str   = USDC_ADDRESS,
        borrow_amount:   float = 5000.0,
        params_encoded:  bytes = b"",
        token_decimals:  int   = USDC_DECIMALS,
    ) -> dict:
        """
        Send the executeArb transaction to the deployed contract.

        Always call check_opportunity() first and only call this if profitable=True.

        Args:
            borrow_token:   Token to borrow (usually USDC).
            borrow_amount:  Human-readable amount (e.g. 5000.0 for $5,000 USDC).
            params_encoded: ABI-encoded TradeParams bytes from check_opportunity().
            token_decimals: Decimals of borrow_token.

        Returns:
            {"success": bool, "tx_hash": str, "profit_usdc": float, "error": str | None}
        """
        result = {"success": False, "tx_hash": "", "profit_usdc": 0.0, "error": None}

        try:
            borrow_wei = int(borrow_amount * 10 ** token_decimals)
            nonce      = await self._w3.eth.get_transaction_count(self._address)
            gas_price  = await self._w3.eth.gas_price

            tx = await self._contract.functions.executeArb(
                AsyncWeb3.to_checksum_address(borrow_token),
                borrow_wei,
                params_encoded,
            ).build_transaction({
                "from":     self._address,
                "nonce":    nonce,
                "gasPrice": int(gas_price * 1.15),
            })
            tx["gas"] = int(await self._w3.eth.estimate_gas(tx) * 1.25)

            signed  = self._account.sign_transaction(tx)
            tx_hash = await self._w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = await self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt.status != 1:
                result["error"] = f"Transaction reverted: {tx_hash.hex()}"
                logger.error(result["error"])
                return result

            # Parse ArbExecuted event for profit
            profit_usdc = await self._parse_profit_from_receipt(receipt, borrow_token)

            result.update({
                "success":     True,
                "tx_hash":     tx_hash.hex(),
                "profit_usdc": profit_usdc,
            })

            logger.info(f"Arb executed | tx={tx_hash.hex()} profit=${profit_usdc:.4f}")
            logger.info(f"Track: https://basescan.org/tx/{tx_hash.hex()}")

        except Exception as e:
            result["error"] = str(e)
            logger.error(f"execute_arb failed: {e}")

        return result

    # ── Convenience: scan all route combinations ───────────────────────────

    async def find_best_opportunity(
        self,
        borrow_usdc: float = 5000.0,
        token_mids: list   = None,
    ) -> Optional[dict]:
        """
        Run check_opportunity() across all DEX combinations and token paths.
        Returns the most profitable result, or None if nothing is profitable.

        Called by main.py's arb scheduler every cycle.
        """
        if token_mids is None:
            token_mids = [WETH_ADDRESS]

        routes = [
            # Uniswap A / Aerodrome B (most common arb direction)
            {"dex_a": DEX_UNISWAP,   "dex_b": DEX_AERODROME, "fee_tier_a": 500,  "fee_tier_b": 3000, "stable_b": False},
            {"dex_a": DEX_UNISWAP,   "dex_b": DEX_AERODROME, "fee_tier_a": 3000, "fee_tier_b": 3000, "stable_b": False},
            # Aerodrome A / Uniswap B (reverse direction)
            {"dex_a": DEX_AERODROME, "dex_b": DEX_UNISWAP,   "fee_tier_a": 3000, "fee_tier_b": 500,  "stable_a": False},
            {"dex_a": DEX_AERODROME, "dex_b": DEX_UNISWAP,   "fee_tier_a": 3000, "fee_tier_b": 3000, "stable_a": False},
        ]

        checks = []
        for token_mid in token_mids:
            for route in routes:
                checks.append(self.check_opportunity(
                    token_mid=token_mid,
                    borrow_usdc=borrow_usdc,
                    **route,
                ))

        results = await asyncio.gather(*checks, return_exceptions=True)

        # Filter to profitable results only, sort by net_profit descending
        profitable = [
            r for r in results
            if isinstance(r, dict) and r.get("profitable") and r.get("net_profit", 0) > 0
        ]
        if not profitable:
            return None

        best = max(profitable, key=lambda r: r["net_profit"])
        logger.info(
            f"Best arb: net=${best['net_profit']:.4f} "
            f"dexA={best['dex_a']} dexB={best['dex_b']} mid={best['token_mid'][:8]}…"
        )
        return best

    # ── Withdraw profit ────────────────────────────────────────────────────

    async def withdraw_profit(self, token: str = USDC_ADDRESS, amount: float = 0.0) -> str:
        """
        Withdraw accumulated profit from the contract to the owner wallet.
        Pass amount=0 to sweep the full balance.

        Returns tx hash.
        """
        amount_wei = int(amount * 10 ** USDC_DECIMALS) if amount > 0 else 0
        nonce      = await self._w3.eth.get_transaction_count(self._address)
        gas_price  = await self._w3.eth.gas_price

        tx = await self._contract.functions.withdrawProfit(
            AsyncWeb3.to_checksum_address(token),
            amount_wei,
        ).build_transaction({
            "from":     self._address,
            "nonce":    nonce,
            "gasPrice": int(gas_price * 1.1),
        })
        tx["gas"] = int(await self._w3.eth.estimate_gas(tx) * 1.2)

        signed  = self._account.sign_transaction(tx)
        tx_hash = await self._w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = await self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        if receipt.status != 1:
            raise RuntimeError(f"withdrawProfit failed: {tx_hash.hex()}")

        logger.info(f"Profit withdrawn | tx={tx_hash.hex()}")
        return tx_hash.hex()

    async def get_contract_balance(self, token: str = USDC_ADDRESS) -> float:
        """Return accumulated profit (USDC) sitting in the contract."""
        raw = await self._contract.functions.tokenBalance(
            AsyncWeb3.to_checksum_address(token)
        ).call()
        return raw / 10 ** USDC_DECIMALS

    # ── Internal helpers ───────────────────────────────────────────────────

    async def _parse_profit_from_receipt(self, receipt, borrow_token: str) -> float:
        """Parse the ArbExecuted event from a transaction receipt to get profit."""
        try:
            logs = self._contract.events.ArbExecuted().process_receipt(receipt)
            if logs:
                profit_wei = logs[0]["args"]["profit"]
                token_addr = AsyncWeb3.to_checksum_address(borrow_token)
                # Get decimals of borrow token (USDC = 6)
                decimals = USDC_DECIMALS if token_addr == AsyncWeb3.to_checksum_address(USDC_ADDRESS) else 18
                return profit_wei / 10 ** decimals
        except Exception as e:
            logger.warning(f"Could not parse ArbExecuted event: {e}")
        return 0.0

    async def _get_eth_price_usdc(self) -> float:
        """
        Get approximate ETH/USDC price by querying the Uniswap V3 USDC/WETH pool.
        Used only for gas cost estimation — does not need to be perfectly accurate.
        Falls back to $3000 if the call fails.
        """
        try:
            from exchange.base import BaseConnector, USDC_DECIMALS
            async with BaseConnector() as base:
                # Quote 1 WETH -> USDC
                weth_wei = int(1e18)
                quote    = await base.get_quote_uniswap(WETH_ADDRESS, USDC_ADDRESS, weth_wei, fee=500)
                if quote.get("amount_out_ui", 0) > 0:
                    return float(quote["amount_out_ui"])
        except Exception:
            pass
        return 3000.0  # fallback estimate


# ── Module-level quick check (run directly for debugging) ─────────────────────

async def _main():
    """
    Quick profitability check across all routes.
    Run with: python3 -m exchange.flash_loan
    """
    async with FlashLoanArb() as arb:
        print("\nScanning arb opportunities on Base mainnet…\n")
        best = await arb.find_best_opportunity(borrow_usdc=5000.0)
        if best:
            print(f"  ✅ PROFITABLE ARB FOUND")
            print(f"     Net profit:  ${best['net_profit']:.4f} USDC")
            print(f"     Gross:       ${best['gross_profit']:.4f}")
            print(f"     Aave fee:    ${best['aave_fee_usdc']:.4f}")
            print(f"     Gas est:     ${best['gas_cost_usdc']:.4f}")
            print(f"     Route:       DEX {best['dex_a']} -> DEX {best['dex_b']}")
        else:
            print("  ℹ  No profitable arb at this moment.")
        balance = await arb.get_contract_balance()
        print(f"\n  Contract USDC balance: ${balance:.4f}")


if __name__ == "__main__":
    asyncio.run(_main())
