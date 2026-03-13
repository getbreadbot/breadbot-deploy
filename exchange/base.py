"""
exchange/base.py — DEX execution on Base chain via Uniswap V3 and Aerodrome.

Uniswap V3 is the primary router for most token pairs.
Aerodrome is Base-native and often has better liquidity for newer tokens.

The module auto-selects the better route by comparing quotes from both DEXs.

Methods:
    get_quote_uniswap(token_in, token_out, amount_in)   -> quote dict
    get_quote_aerodrome(token_in, token_out, amount_in) -> quote dict
    best_quote(token_in, token_out, amount_in)          -> quote dict
    execute_swap(quote)                                 -> tx hash str
    get_token_balance(token_address)                    -> float
    get_eth_balance()                                   -> float (ETH)
    verify_connectivity()                               -> health dict

Key addresses (Base mainnet):
    USDC:           0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
    WETH:           0x4200000000000000000000000000000000000006
    Uniswap V3 Router: 0x2626664c2603336E57B271c5C0b26F421741e481
    Aerodrome Router:  0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43
"""

import os
import asyncio
from typing import Optional
from web3 import AsyncWeb3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_account import Account
from loguru import logger

# ── Token addresses (Base mainnet) ────────────────────────────────────────────
USDC_ADDRESS  = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH_ADDRESS  = "0x4200000000000000000000000000000000000006"
USDC_DECIMALS = 6
ETH_DECIMALS  = 18

# ── Router addresses ──────────────────────────────────────────────────────────
UNISWAP_V3_ROUTER    = "0x2626664c2603336E57B271c5C0b26F421741e481"
UNISWAP_V3_QUOTER    = "0x3d4e44Eb1374240CE5F1B136CFeFD1d31bd7E3b8"
AERODROME_ROUTER     = "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"

# ── Pool fees (Uniswap V3) ────────────────────────────────────────────────────
FEE_LOW    = 500    # 0.05% — stable pairs
FEE_MEDIUM = 3000   # 0.30% — standard
FEE_HIGH   = 10000  # 1.00% — exotic/new tokens

# ── Slippage ──────────────────────────────────────────────────────────────────
DEFAULT_SLIPPAGE_BPS = 50   # 0.5%
MEME_SLIPPAGE_BPS    = 300  # 3.0% — new/thin liquidity tokens

# ── Minimal ABIs ──────────────────────────────────────────────────────────────

ERC20_ABI = [
    {"inputs": [{"name": "account", "type": "address"}],
     "name": "balanceOf", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"},
                {"name": "amount", "type": "uint256"}],
     "name": "approve", "outputs": [{"type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"},
                {"name": "spender", "type": "address"}],
     "name": "allowance", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals",
     "outputs": [{"type": "uint8"}],
     "stateMutability": "view", "type": "function"},
]

UNISWAP_V3_ROUTER_ABI = [
    {"inputs": [{"components": [
        {"name": "tokenIn",  "type": "address"},
        {"name": "tokenOut", "type": "address"},
        {"name": "fee",      "type": "uint24"},
        {"name": "recipient","type": "address"},
        {"name": "amountIn", "type": "uint256"},
        {"name": "amountOutMinimum", "type": "uint256"},
        {"name": "sqrtPriceLimitX96","type": "uint160"},
    ], "name": "params", "type": "tuple"}],
     "name": "exactInputSingle",
     "outputs": [{"name": "amountOut", "type": "uint256"}],
     "stateMutability": "payable", "type": "function"},
]

UNISWAP_V3_QUOTER_ABI = [
    {"inputs": [
        {"name": "tokenIn",  "type": "address"},
        {"name": "tokenOut", "type": "address"},
        {"name": "amountIn", "type": "uint256"},
        {"name": "fee",      "type": "uint24"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"},
    ], "name": "quoteExactInputSingle",
     "outputs": [
        {"name": "amountOut",               "type": "uint256"},
        {"name": "sqrtPriceX96After",        "type": "uint160"},
        {"name": "initializedTicksCrossed",  "type": "uint32"},
        {"name": "gasEstimate",              "type": "uint256"},
     ],
     "stateMutability": "nonpayable", "type": "function"},
]

AERODROME_ROUTER_ABI = [
    {"inputs": [
        {"name": "amountIn",  "type": "uint256"},
        {"name": "routes", "type": "tuple[]",
         "components": [
            {"name": "from",   "type": "address"},
            {"name": "to",     "type": "address"},
            {"name": "stable", "type": "bool"},
            {"name": "factory","type": "address"},
         ]},
    ], "name": "getAmountsOut",
     "outputs": [{"name": "amounts", "type": "uint256[]"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [
        {"name": "amountIn",    "type": "uint256"},
        {"name": "amountOutMin","type": "uint256"},
        {"name": "routes", "type": "tuple[]",
         "components": [
            {"name": "from",   "type": "address"},
            {"name": "to",     "type": "address"},
            {"name": "stable", "type": "bool"},
            {"name": "factory","type": "address"},
         ]},
        {"name": "to",       "type": "address"},
        {"name": "deadline", "type": "uint256"},
    ], "name": "swapExactTokensForTokens",
     "outputs": [{"name": "amounts", "type": "uint256[]"}],
     "stateMutability": "nonpayable", "type": "function"},
]

AERODROME_FACTORY = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"


class BaseConnector:
    """
    DEX execution on Base chain via Uniswap V3 and Aerodrome.

    Auto-selects the better quote between both DEXs before executing.
    Loads wallet from BASE_PRIVATE_KEY and RPC from BASE_RPC_URL in .env.
    """

    def __init__(self):
        self._w3:       Optional[AsyncWeb3] = None
        self._account:  Optional[Account]   = None
        self._address:  str                 = ""

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def connect(self):
        rpc_url     = os.getenv("BASE_RPC_URL", "https://mainnet.base.org").strip()
        private_key = os.getenv("BASE_PRIVATE_KEY", "").strip()

        if not private_key:
            raise ValueError("BASE_PRIVATE_KEY not set in .env")

        # Ensure 0x prefix
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key

        self._w3      = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))
        self._w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self._account = Account.from_key(private_key)
        self._address = self._account.address

        connected = await self._w3.is_connected()
        if not connected:
            raise ConnectionError(f"Cannot connect to Base RPC: {rpc_url}")

        chain_id = await self._w3.eth.chain_id
        if chain_id != 8453:
            raise ValueError(f"Wrong chain — expected Base (8453), got {chain_id}")

        logger.info(f"Base connector ready | wallet: {self._address} | chain: {chain_id}")

    async def close(self):
        logger.info("Base connector closed")

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()


    # ── Approval helper ────────────────────────────────────────────────────

    async def _ensure_approval(self, token_address: str, spender: str, amount_wei: int):
        """Approve a spender to spend tokens if current allowance is insufficient."""
        token = self._w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(token_address),
            abi=ERC20_ABI
        )
        allowance = await token.functions.allowance(self._address, spender).call()
        if allowance >= amount_wei:
            return  # Already approved

        logger.info(f"Approving {spender[:10]}… to spend {token_address[:10]}…")
        nonce   = await self._w3.eth.get_transaction_count(self._address)
        gas_price = await self._w3.eth.gas_price

        tx = await token.functions.approve(spender, 2**256 - 1).build_transaction({
            "from":     self._address,
            "nonce":    nonce,
            "gasPrice": int(gas_price * 1.1),
        })
        tx["gas"] = await self._w3.eth.estimate_gas(tx)
        signed  = self._account.sign_transaction(tx)
        tx_hash = await self._w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = await self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        if receipt.status != 1:
            raise RuntimeError(f"Approval transaction failed: {tx_hash.hex()}")
        logger.info(f"Approval confirmed: {tx_hash.hex()}")

    # ── Uniswap V3 quote ───────────────────────────────────────────────────

    async def get_quote_uniswap(
        self,
        token_in:       str,
        token_out:      str,
        amount_in_wei:  int,
        fee:            int = FEE_MEDIUM,
    ) -> dict:
        """
        Get a Uniswap V3 quote. Tries FEE_MEDIUM, falls back to FEE_HIGH.
        Returns dict with keys: dex, amount_out, amount_out_ui, fee, token_in, token_out
        """
        quoter = self._w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(UNISWAP_V3_QUOTER),
            abi=UNISWAP_V3_QUOTER_ABI
        )

        best_out = 0
        best_fee = fee

        for try_fee in [FEE_LOW, FEE_MEDIUM, FEE_HIGH]:
            try:
                result = await quoter.functions.quoteExactInputSingle(
                    AsyncWeb3.to_checksum_address(token_in),
                    AsyncWeb3.to_checksum_address(token_out),
                    amount_in_wei,
                    try_fee,
                    0
                ).call()
                amount_out = result[0]
                if amount_out > best_out:
                    best_out = amount_out
                    best_fee = try_fee
            except Exception:
                continue

        if best_out == 0:
            return {"dex": "uniswap_v3", "amount_out": 0, "error": "no_route"}

        decimals_out = await self._get_decimals(token_out)
        return {
            "dex":          "uniswap_v3",
            "amount_out":   best_out,
            "amount_out_ui": best_out / (10 ** decimals_out),
            "fee":          best_fee,
            "token_in":     token_in,
            "token_out":    token_out,
            "amount_in":    amount_in_wei,
        }


    # ── Aerodrome quote ────────────────────────────────────────────────────

    async def get_quote_aerodrome(
        self,
        token_in:      str,
        token_out:     str,
        amount_in_wei: int,
        stable:        bool = False,
    ) -> dict:
        """
        Get an Aerodrome quote. Tries both stable=False and stable=True pools.
        Returns the better of the two.
        """
        router = self._w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(AERODROME_ROUTER),
            abi=AERODROME_ROUTER_ABI
        )
        best_out = 0
        best_stable = False

        for try_stable in [False, True]:
            try:
                route = [{
                    "from":    AsyncWeb3.to_checksum_address(token_in),
                    "to":      AsyncWeb3.to_checksum_address(token_out),
                    "stable":  try_stable,
                    "factory": AsyncWeb3.to_checksum_address(AERODROME_FACTORY),
                }]
                amounts = await router.functions.getAmountsOut(amount_in_wei, route).call()
                out = amounts[-1]
                if out > best_out:
                    best_out    = out
                    best_stable = try_stable
            except Exception:
                continue

        if best_out == 0:
            return {"dex": "aerodrome", "amount_out": 0, "error": "no_route"}

        decimals_out = await self._get_decimals(token_out)
        return {
            "dex":           "aerodrome",
            "amount_out":    best_out,
            "amount_out_ui": best_out / (10 ** decimals_out),
            "stable":        best_stable,
            "token_in":      token_in,
            "token_out":     token_out,
            "amount_in":     amount_in_wei,
        }

    # ── Best quote (auto-selects DEX) ──────────────────────────────────────

    async def best_quote(
        self,
        token_in:       str,
        token_out:      str,
        amount_in_ui:   float,
        input_decimals: int = USDC_DECIMALS,
        slippage_bps:   int = DEFAULT_SLIPPAGE_BPS,
    ) -> dict:
        """
        Get quotes from both Uniswap V3 and Aerodrome, return the better one.

        Args:
            token_in:       Input token address
            token_out:      Output token address
            amount_in_ui:   Amount in human units (e.g. 100.0 for 100 USDC)
            input_decimals: Decimals of input token
            slippage_bps:   Slippage tolerance in basis points

        Returns:
            Best quote dict, with slippage-adjusted min_amount_out added
        """
        amount_in_wei = int(amount_in_ui * (10 ** input_decimals))

        uni_q, aero_q = await asyncio.gather(
            self.get_quote_uniswap(token_in, token_out, amount_in_wei),
            self.get_quote_aerodrome(token_in, token_out, amount_in_wei),
            return_exceptions=True
        )

        uni_out  = uni_q.get("amount_out", 0)  if isinstance(uni_q,  dict) else 0
        aero_out = aero_q.get("amount_out", 0) if isinstance(aero_q, dict) else 0

        best = uni_q if uni_out >= aero_out else aero_q
        if isinstance(best, Exception) or best.get("amount_out", 0) == 0:
            raise RuntimeError(f"No route found for {token_in[:8]}→{token_out[:8]}")

        slippage_factor    = 1 - (slippage_bps / 10000)
        best["min_out"]    = int(best["amount_out"] * slippage_factor)
        best["slippage_bps"] = slippage_bps

        logger.info(
            f"Best quote: {best['dex']} | in={amount_in_ui} "
            f"out≈{best.get('amount_out_ui', '?'):.4f} "
            f"(uni={uni_out} vs aero={aero_out})"
        )
        return best


    # ── Swap execution ─────────────────────────────────────────────────────

    async def execute_swap(self, quote: dict) -> str:
        """
        Execute a swap using the best quote returned by best_quote().

        Args:
            quote: Quote dict from best_quote(), get_quote_uniswap(), or get_quote_aerodrome()

        Returns:
            Transaction hash string
        """
        dex = quote.get("dex")
        if dex == "uniswap_v3":
            return await self._swap_uniswap(quote)
        elif dex == "aerodrome":
            return await self._swap_aerodrome(quote)
        else:
            raise ValueError(f"Unknown DEX in quote: {dex}")

    async def _swap_uniswap(self, quote: dict) -> str:
        """Execute swap via Uniswap V3 exactInputSingle."""
        token_in  = quote["token_in"]
        token_out = quote["token_out"]
        amount_in = quote["amount_in"]
        min_out   = quote["min_out"]
        fee       = quote["fee"]

        await self._ensure_approval(token_in, UNISWAP_V3_ROUTER, amount_in)

        router  = self._w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(UNISWAP_V3_ROUTER),
            abi=UNISWAP_V3_ROUTER_ABI
        )
        deadline  = (await self._w3.eth.get_block("latest"))["timestamp"] + 300
        nonce     = await self._w3.eth.get_transaction_count(self._address)
        gas_price = await self._w3.eth.gas_price

        params = {
            "tokenIn":            AsyncWeb3.to_checksum_address(token_in),
            "tokenOut":           AsyncWeb3.to_checksum_address(token_out),
            "fee":                fee,
            "recipient":          self._address,
            "amountIn":           amount_in,
            "amountOutMinimum":   min_out,
            "sqrtPriceLimitX96":  0,
        }

        tx = await router.functions.exactInputSingle(params).build_transaction({
            "from":      self._address,
            "nonce":     nonce,
            "gasPrice":  int(gas_price * 1.1),
            "value":     0,
        })
        tx["gas"] = int(await self._w3.eth.estimate_gas(tx) * 1.2)

        signed  = self._account.sign_transaction(tx)
        tx_hash = await self._w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = await self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt.status != 1:
            raise RuntimeError(f"Uniswap swap failed: {tx_hash.hex()}")

        logger.info(f"Uniswap swap confirmed | tx={tx_hash.hex()}")
        logger.info(f"Track: https://basescan.org/tx/{tx_hash.hex()}")
        return tx_hash.hex()

    async def _swap_aerodrome(self, quote: dict) -> str:
        """Execute swap via Aerodrome swapExactTokensForTokens."""
        token_in  = quote["token_in"]
        token_out = quote["token_out"]
        amount_in = quote["amount_in"]
        min_out   = quote["min_out"]
        stable    = quote.get("stable", False)

        await self._ensure_approval(token_in, AERODROME_ROUTER, amount_in)

        router    = self._w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(AERODROME_ROUTER),
            abi=AERODROME_ROUTER_ABI
        )
        deadline  = (await self._w3.eth.get_block("latest"))["timestamp"] + 300
        nonce     = await self._w3.eth.get_transaction_count(self._address)
        gas_price = await self._w3.eth.gas_price

        route = [{
            "from":    AsyncWeb3.to_checksum_address(token_in),
            "to":      AsyncWeb3.to_checksum_address(token_out),
            "stable":  stable,
            "factory": AsyncWeb3.to_checksum_address(AERODROME_FACTORY),
        }]

        tx = await router.functions.swapExactTokensForTokens(
            amount_in, min_out, route, self._address, deadline
        ).build_transaction({
            "from":     self._address,
            "nonce":    nonce,
            "gasPrice": int(gas_price * 1.1),
            "value":    0,
        })
        tx["gas"] = int(await self._w3.eth.estimate_gas(tx) * 1.2)

        signed  = self._account.sign_transaction(tx)
        tx_hash = await self._w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = await self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt.status != 1:
            raise RuntimeError(f"Aerodrome swap failed: {tx_hash.hex()}")

        logger.info(f"Aerodrome swap confirmed | tx={tx_hash.hex()}")
        logger.info(f"Track: https://basescan.org/tx/{tx_hash.hex()}")
        return tx_hash.hex()


    # ── Convenience methods ────────────────────────────────────────────────

    async def buy_token(
        self,
        token_mint:   str,
        usdc_amount:  float,
        slippage_bps: int = MEME_SLIPPAGE_BPS,
    ) -> str:
        """Buy a Base token using USDC from the trading wallet."""
        quote = await self.best_quote(
            token_in=USDC_ADDRESS,
            token_out=token_mint,
            amount_in_ui=usdc_amount,
            input_decimals=USDC_DECIMALS,
            slippage_bps=slippage_bps,
        )
        return await self.execute_swap(quote)

    async def sell_token(
        self,
        token_address:  str,
        token_amount:   float,
        token_decimals: int,
        slippage_bps:   int = MEME_SLIPPAGE_BPS,
    ) -> str:
        """Sell a Base token back to USDC."""
        quote = await self.best_quote(
            token_in=token_address,
            token_out=USDC_ADDRESS,
            amount_in_ui=token_amount,
            input_decimals=token_decimals,
            slippage_bps=slippage_bps,
        )
        return await self.execute_swap(quote)

    # ── Balance queries ────────────────────────────────────────────────────

    async def get_eth_balance(self) -> float:
        """Returns ETH balance of the trading wallet in human units."""
        wei = await self._w3.eth.get_balance(self._address)
        return float(AsyncWeb3.from_wei(wei, "ether"))

    async def get_token_balance(self, token_address: str) -> float:
        """Returns ERC-20 token balance in human-readable units."""
        token    = self._w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(token_address),
            abi=ERC20_ABI
        )
        raw      = await token.functions.balanceOf(self._address).call()
        decimals = await self._get_decimals(token_address)
        return raw / (10 ** decimals)

    async def get_usdc_balance(self) -> float:
        """Returns USDC balance on Base."""
        return await self.get_token_balance(USDC_ADDRESS)

    async def _get_decimals(self, token_address: str) -> int:
        """Returns the decimal places of an ERC-20 token."""
        known = {
            USDC_ADDRESS.lower(): 6,
            WETH_ADDRESS.lower(): 18,
        }
        addr = token_address.lower()
        if addr in known:
            return known[addr]
        try:
            token = self._w3.eth.contract(
                address=AsyncWeb3.to_checksum_address(token_address),
                abi=ERC20_ABI
            )
            return await token.functions.decimals().call()
        except Exception:
            return 18  # Safe default for unknown tokens

    # ── Health check ───────────────────────────────────────────────────────

    async def verify_connectivity(self) -> dict:
        """
        Verifies RPC connection, chain ID, and wallet balance.
        Call at startup to catch misconfiguration early.
        """
        result = {
            "rpc":          False,
            "chain_id":     None,
            "wallet":       self._address,
            "eth_balance":  0.0,
            "usdc_balance": 0.0,
            "errors":       [],
        }
        try:
            result["chain_id"]    = await self._w3.eth.chain_id
            result["eth_balance"] = await self.get_eth_balance()
            result["rpc"]         = True
            logger.info(
                f"Base RPC — connected | chain={result['chain_id']} "
                f"| ETH={result['eth_balance']:.4f}"
            )
        except Exception as e:
            result["errors"].append(f"Base RPC error: {e}")
            logger.error(f"Base connectivity check failed: {e}")

        try:
            result["usdc_balance"] = await self.get_usdc_balance()
            logger.info(f"Base USDC balance: {result['usdc_balance']:.2f}")
        except Exception as e:
            result["errors"].append(f"USDC balance error: {e}")

        return result
