"""
exchange/jupiter.py — Solana DEX execution via Jupiter Aggregator v6.

Jupiter finds the best route across all Solana DEXs (Raydium, Orca, Meteora,
Phoenix, etc.) and returns a pre-built transaction. We sign it and send it.

Methods:
    get_quote(input_mint, output_mint, amount_lamports)  -> quote dict
    execute_swap(quote)                                  -> tx signature str
    get_token_balance(mint)                              -> float (UI amount)
    get_sol_balance()                                    -> float (SOL)
    verify_connectivity()                                -> health dict

Token mint addresses (mainnet):
    SOL  native — use WSOL mint for swaps: So11111111111111111111111111111111111111112
    USDC 6 decimals:                        EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
    USDT 6 decimals:                        Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB

Usage:
    async with JupiterConnector() as jup:
        quote = await jup.get_quote(USDC_MINT, target_mint, usdc_amount_ui=100.0)
        sig   = await jup.execute_swap(quote)
        print(f"Swap confirmed: https://solscan.io/tx/{sig}")
"""

import asyncio
import base64
import os
from typing import Optional
import aiohttp
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
import base58
from loguru import logger
import config

# ── Constants ─────────────────────────────────────────────────────────────────

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL  = "https://quote-api.jup.ag/v6/swap"

SOL_MINT  = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

USDC_DECIMALS = 6
SOL_DECIMALS  = 9

# Slippage in basis points (50 = 0.5%). Meme coins need more room.
DEFAULT_SLIPPAGE_BPS      = 50    # 0.5% — standard pairs
MEME_SLIPPAGE_BPS         = 300   # 3.0% — new/low-liquidity tokens


class JupiterConnector:
    """
    Solana DEX execution via Jupiter Aggregator v6.

    Loads wallet from SOLANA_PRIVATE_KEY in .env (base58 encoded).
    Sends transactions via SOLANA_RPC_URL (Helius recommended).
    """

    def __init__(self):
        self._session:  Optional[aiohttp.ClientSession] = None
        self._keypair:  Optional[Keypair]               = None
        self._rpc_url:  str                              = ""
        self._pubkey:   str                              = ""

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def connect(self):
        raw_key  = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
        rpc_url  = os.getenv("SOLANA_RPC_URL", "").strip()

        if not raw_key:
            raise ValueError("SOLANA_PRIVATE_KEY not set in .env")
        if not rpc_url:
            raise ValueError("SOLANA_RPC_URL not set in .env — get a free key at helius.dev")

        key_bytes       = base58.b58decode(raw_key)
        self._keypair   = Keypair.from_bytes(key_bytes)
        self._pubkey    = str(self._keypair.pubkey())
        self._rpc_url   = rpc_url
        self._session   = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
        logger.info(f"Jupiter connector ready | wallet: {self._pubkey}")

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("Jupiter connector closed")

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()


    # ── Quote ──────────────────────────────────────────────────────────────

    async def get_quote(
        self,
        input_mint:      str,
        output_mint:     str,
        amount_ui:       float,
        input_decimals:  int  = USDC_DECIMALS,
        slippage_bps:    int  = DEFAULT_SLIPPAGE_BPS,
    ) -> dict:
        """
        Fetch the best swap route from Jupiter.

        Args:
            input_mint:     Mint address of the token you are spending
            output_mint:    Mint address of the token you are buying
            amount_ui:      Human-readable amount to spend (e.g. 100.0 for 100 USDC)
            input_decimals: Decimal places of the input token (USDC=6, SOL=9)
            slippage_bps:   Max slippage in basis points (50=0.5%, 300=3%)

        Returns:
            Quote dict from Jupiter — pass directly to execute_swap()

        Raises:
            RuntimeError on no route found or API error
        """
        amount_raw = int(amount_ui * (10 ** input_decimals))

        params = {
            "inputMint":   input_mint,
            "outputMint":  output_mint,
            "amount":      str(amount_raw),
            "slippageBps": str(slippage_bps),
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false",
        }

        logger.info(
            f"Jupiter quote | {amount_ui} ({input_mint[:6]}…) → {output_mint[:6]}… "
            f"| slippage {slippage_bps}bps"
        )

        async with self._session.get(JUPITER_QUOTE_URL, params=params) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Jupiter quote failed ({resp.status}): {text}")
            data = await resp.json()

        if not data or "outAmount" not in data:
            raise RuntimeError(f"No route found for {input_mint} → {output_mint}")

        out_ui = int(data["outAmount"]) / 1e6   # assumes USDC/token decimals — adjust per token
        logger.info(
            f"Jupiter quote OK | in={amount_ui} out≈{out_ui:.4f} "
            f"| impact={data.get('priceImpactPct','?')}%"
        )
        return data


    # ── Swap execution ─────────────────────────────────────────────────────

    async def execute_swap(self, quote: dict) -> str:
        """
        Build, sign, and send a swap transaction using a Jupiter quote.

        Args:
            quote: The quote dict returned by get_quote()

        Returns:
            Transaction signature string (viewable at solscan.io/tx/<sig>)

        Raises:
            RuntimeError on build failure or RPC rejection
        """
        # Step 1 — ask Jupiter to build the transaction
        swap_payload = {
            "quoteResponse":            quote,
            "userPublicKey":            self._pubkey,
            "wrapAndUnwrapSol":         True,
            "dynamicComputeUnitLimit":  True,
            "prioritizationFeeLamports": "auto",  # Jupiter sets priority fee automatically
        }

        async with self._session.post(JUPITER_SWAP_URL, json=swap_payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Jupiter swap build failed ({resp.status}): {text}")
            swap_data = await resp.json()

        tx_b64 = swap_data.get("swapTransaction")
        if not tx_b64:
            raise RuntimeError(f"No swapTransaction in Jupiter response: {swap_data}")

        # Step 2 — deserialize, sign, re-serialize
        tx_bytes = base64.b64decode(tx_b64)
        tx       = VersionedTransaction.from_bytes(tx_bytes)
        msg_bytes = to_bytes_versioned(tx.message)
        sig       = self._keypair.sign_message(msg_bytes)
        signed_tx = VersionedTransaction(tx.message, [sig])
        signed_b64 = base64.b64encode(bytes(signed_tx)).decode()

        # Step 3 — send via RPC
        rpc_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                signed_b64,
                {
                    "encoding":              "base64",
                    "skipPreflight":         False,
                    "preflightCommitment":   "confirmed",
                    "maxRetries":            3,
                },
            ],
        }

        async with self._session.post(self._rpc_url, json=rpc_payload) as resp:
            rpc_data = await resp.json()

        if "error" in rpc_data:
            raise RuntimeError(f"RPC rejected transaction: {rpc_data['error']}")

        signature = rpc_data["result"]
        logger.info(f"Swap submitted | sig={signature}")
        logger.info(f"Track: https://solscan.io/tx/{signature}")
        return signature


    # ── Confirmation polling ───────────────────────────────────────────────

    async def confirm_transaction(self, signature: str, timeout_s: int = 60) -> bool:
        """
        Poll RPC until the transaction is confirmed or timeout is reached.

        Returns True if confirmed, False if timed out or failed.
        """
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignatureStatuses",
                "params": [[signature], {"searchTransactionHistory": True}],
            }
            async with self._session.post(self._rpc_url, json=payload) as resp:
                data = await resp.json()

            result = data.get("result", {}).get("value", [None])[0]
            if result:
                err = result.get("err")
                if err:
                    logger.error(f"Transaction failed on-chain: {err}")
                    return False
                commitment = result.get("confirmationStatus", "")
                if commitment in ("confirmed", "finalized"):
                    logger.info(f"Transaction confirmed | status={commitment}")
                    return True

            await asyncio.sleep(2)

        logger.warning(f"Transaction confirmation timed out after {timeout_s}s")
        return False

    # ── Balance queries ────────────────────────────────────────────────────

    async def get_sol_balance(self) -> float:
        """Returns SOL balance of the trading wallet."""
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getBalance",
            "params": [self._pubkey, {"commitment": "confirmed"}],
        }
        async with self._session.post(self._rpc_url, json=payload) as resp:
            data = await resp.json()
        lamports = data.get("result", {}).get("value", 0)
        return lamports / 1e9

    async def get_token_balance(self, mint: str) -> float:
        """
        Returns the UI balance of a SPL token in the trading wallet.

        Args:
            mint: Token mint address, e.g. USDC_MINT

        Returns:
            Float balance in human-readable units (e.g. 100.5 USDC)
        """
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                self._pubkey,
                {"mint": mint},
                {"encoding": "jsonParsed", "commitment": "confirmed"},
            ],
        }
        async with self._session.post(self._rpc_url, json=payload) as resp:
            data = await resp.json()

        accounts = data.get("result", {}).get("value", [])
        if not accounts:
            return 0.0

        ui_amount = (
            accounts[0]
            .get("account", {})
            .get("data", {})
            .get("parsed", {})
            .get("info", {})
            .get("tokenAmount", {})
            .get("uiAmount", 0.0)
        )
        return float(ui_amount or 0.0)


    # ── Health check ───────────────────────────────────────────────────────

    async def verify_connectivity(self) -> dict:
        """
        Checks RPC connection and Jupiter API availability.
        Call at startup to catch misconfiguration early.

        Returns:
            {"rpc": bool, "jupiter": bool, "wallet": str, "sol_balance": float, "errors": [str]}
        """
        result = {
            "rpc":         False,
            "jupiter":     False,
            "wallet":      self._pubkey,
            "sol_balance": 0.0,
            "errors":      [],
        }

        # Check RPC
        try:
            result["sol_balance"] = await self.get_sol_balance()
            result["rpc"] = True
            logger.info(f"Solana RPC — connected | SOL balance: {result['sol_balance']:.4f}")
        except Exception as e:
            msg = f"Solana RPC error: {e}"
            result["errors"].append(msg)
            logger.error(msg)

        # Check Jupiter API
        try:
            async with self._session.get(
                JUPITER_QUOTE_URL,
                params={
                    "inputMint":  USDC_MINT,
                    "outputMint": SOL_MINT,
                    "amount":     "1000000",  # 1 USDC
                    "slippageBps": "50",
                }
            ) as resp:
                if resp.status == 200:
                    result["jupiter"] = True
                    logger.info("Jupiter API — reachable")
                else:
                    result["errors"].append(f"Jupiter API returned {resp.status}")
        except Exception as e:
            msg = f"Jupiter API error: {e}"
            result["errors"].append(msg)
            logger.error(msg)

        return result

    # ── Convenience: buy token with USDC ──────────────────────────────────

    async def buy_token(
        self,
        token_mint:   str,
        usdc_amount:  float,
        slippage_bps: int = MEME_SLIPPAGE_BPS,
    ) -> str:
        """
        Buy a token using USDC from the trading wallet.

        Args:
            token_mint:   Mint address of the token to buy
            usdc_amount:  USDC to spend (e.g. 100.0)
            slippage_bps: Slippage tolerance (default 300bps / 3% for meme coins)

        Returns:
            Transaction signature string
        """
        quote = await self.get_quote(
            input_mint=USDC_MINT,
            output_mint=token_mint,
            amount_ui=usdc_amount,
            input_decimals=USDC_DECIMALS,
            slippage_bps=slippage_bps,
        )
        return await self.execute_swap(quote)

    async def sell_token(
        self,
        token_mint:    str,
        token_amount:  float,
        token_decimals: int,
        slippage_bps:  int = MEME_SLIPPAGE_BPS,
    ) -> str:
        """
        Sell a token back to USDC.

        Args:
            token_mint:     Mint address of the token to sell
            token_amount:   Amount of tokens to sell (UI units)
            token_decimals: Decimal places of the token
            slippage_bps:   Slippage tolerance

        Returns:
            Transaction signature string
        """
        quote = await self.get_quote(
            input_mint=token_mint,
            output_mint=USDC_MINT,
            amount_ui=token_amount,
            input_decimals=token_decimals,
            slippage_bps=slippage_bps,
        )
        return await self.execute_swap(quote)
