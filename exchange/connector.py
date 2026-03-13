"""
exchange/connector.py — Live trade execution on Coinbase Advanced Trade and Kraken.

Called only after you tap BUY in Telegram. Never called automatically.
Wraps ccxt so both exchanges share the same method signatures.

Methods:
    place_market_buy(symbol, usd_amount)  -> order dict
    place_market_sell(symbol, quantity)   -> order dict
    get_balance()                         -> dict of asset: free balance
    get_open_orders(symbol)               -> list of open orders
    get_price(symbol)                     -> current mid price as float
    verify_connectivity()                 -> health check dict
"""

import asyncio
from typing import Optional
import ccxt.async_support as ccxt
from loguru import logger
import config


class ExchangeConnector:
    """
    Unified connector for Coinbase Advanced Trade and Kraken.

    Usage (context manager — recommended):
        async with ExchangeConnector() as conn:
            order = await conn.place_market_buy("SOL/USD", 100.00)

    Usage (manual):
        conn = ExchangeConnector()
        await conn.connect()
        order = await conn.place_market_buy("SOL/USD", 100.00)
        await conn.close()
    """

    def __init__(self):
        self._coinbase: Optional[ccxt.coinbaseadvanced] = None
        self._kraken:   Optional[ccxt.kraken]           = None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def connect(self):
        """Initialize both exchange connections. Call once at startup."""
        self._coinbase = ccxt.coinbaseadvanced({
            "apiKey":  config.COINBASE_API_KEY,
            "secret":  config.COINBASE_API_SECRET,
            "options": {"defaultType": "spot"},
            "enableRateLimit": True,
        })
        self._kraken = ccxt.kraken({
            "apiKey":  config.KRAKEN_API_KEY,
            "secret":  config.KRAKEN_API_SECRET,
            "enableRateLimit": True,
        })
        logger.info("Exchange connections initialized (Coinbase + Kraken)")

    async def close(self):
        """Close all exchange connections cleanly."""
        if self._coinbase:
            await self._coinbase.close()
        if self._kraken:
            await self._kraken.close()
        logger.info("Exchange connections closed")

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()

    # ── Exchange routing ─────────────────────────────────────────────────────

    def _exchange_for(self, symbol: str) -> ccxt.Exchange:
        """
        Route to the correct exchange.
        Default: Coinbase for all spot pairs.
        Fallback to Kraken when a symbol is not listed on Coinbase.
        """
        return self._coinbase

    # ── Core trade methods ───────────────────────────────────────────────────

    async def place_market_buy(self, symbol: str, usd_amount: float) -> dict:
        """
        Place a market buy order.

        Args:
            symbol:     ccxt pair format, e.g. "SOL/USD", "ETH/USD"
            usd_amount: Dollar amount to spend, e.g. 100.00

        Returns:
            Order dict with keys: id, symbol, side, amount, price, status

        Raises:
            ccxt.InsufficientFunds  — not enough balance
            ccxt.NetworkError       — connection issue, safe to retry
            ccxt.ExchangeError      — exchange rejected the order
        """
        exchange = self._exchange_for(symbol)
        price    = await self.get_price(symbol)
        amount   = round(usd_amount / price, 8)

        logger.info(f"BUY {symbol} | ${usd_amount:.2f} -> {amount} units @ ~${price:.4f}")

        try:
            order = await exchange.create_market_buy_order(symbol, amount)
            logger.info(
                f"BUY FILLED | {symbol} | id={order['id']} | "
                f"filled={order.get('filled','?')} @ ${order.get('average', price):.4f}"
            )
            return order
        except ccxt.InsufficientFunds as e:
            logger.error(f"BUY FAILED — Insufficient funds: {e}")
            raise
        except ccxt.NetworkError as e:
            logger.warning(f"BUY FAILED — Network error (safe to retry): {e}")
            raise
        except ccxt.ExchangeError as e:
            logger.error(f"BUY FAILED — Exchange error: {e}")
            raise

    async def place_market_sell(self, symbol: str, quantity: float) -> dict:
        """
        Place a market sell order.

        Args:
            symbol:   ccxt pair, e.g. "SOL/USD"
            quantity: Amount of base asset to sell, e.g. 1.5 (SOL)

        Returns:
            Order dict from ccxt
        """
        exchange = self._exchange_for(symbol)
        logger.info(f"SELL {symbol} | {quantity} units")

        try:
            order = await exchange.create_market_sell_order(symbol, quantity)
            logger.info(
                f"SELL FILLED | {symbol} | id={order['id']} | "
                f"filled={order.get('filled','?')} @ ${order.get('average', 0):.4f}"
            )
            return order
        except ccxt.InsufficientFunds as e:
            logger.error(f"SELL FAILED — Insufficient funds: {e}")
            raise
        except ccxt.NetworkError as e:
            logger.warning(f"SELL FAILED — Network error (safe to retry): {e}")
            raise
        except ccxt.ExchangeError as e:
            logger.error(f"SELL FAILED — Exchange error: {e}")
            raise

    # ── Account info ─────────────────────────────────────────────────────────

    async def get_balance(self) -> dict:
        """
        Returns free (available) balances across both exchanges.
        Keys are prefixed: CB:USD, KR:ZUSD, etc.
        Only non-zero balances are returned.
        """
        balances = {}

        try:
            cb = await self._coinbase.fetch_balance()
            for asset, amt in cb.get("free", {}).items():
                if amt and float(amt) > 0:
                    balances[f"CB:{asset}"] = round(float(amt), 8)
        except Exception as e:
            logger.warning(f"Could not fetch Coinbase balance: {e}")

        try:
            kr = await self._kraken.fetch_balance()
            for asset, amt in kr.get("free", {}).items():
                if amt and float(amt) > 0:
                    balances[f"KR:{asset}"] = round(float(amt), 8)
        except Exception as e:
            logger.warning(f"Could not fetch Kraken balance: {e}")

        logger.debug(f"Balances: {balances}")
        return balances

    async def get_open_orders(self, symbol: Optional[str] = None) -> list:
        """
        Returns all open orders across both exchanges.

        Args:
            symbol: Optional filter, e.g. "SOL/USD"
        """
        orders = []
        try:
            orders.extend(await self._coinbase.fetch_open_orders(symbol))
        except Exception as e:
            logger.warning(f"Could not fetch Coinbase orders: {e}")
        try:
            orders.extend(await self._kraken.fetch_open_orders(symbol))
        except Exception as e:
            logger.warning(f"Could not fetch Kraken orders: {e}")
        return orders

    async def get_price(self, symbol: str) -> float:
        """
        Returns the current mid price for a symbol.
        Falls back to Kraken if Coinbase does not list the pair.
        """
        exchange = self._exchange_for(symbol)
        try:
            ticker = await exchange.fetch_ticker(symbol)
            price  = ticker.get("last") or (
                (ticker.get("bid", 0) + ticker.get("ask", 0)) / 2
            )
            if not price:
                raise ValueError(f"No price data for {symbol}")
            return float(price)
        except ccxt.BadSymbol:
            if exchange is self._coinbase:
                ticker = await self._kraken.fetch_ticker(symbol)
                price  = ticker.get("last") or (
                    (ticker.get("bid", 0) + ticker.get("ask", 0)) / 2
                )
                return float(price)
            raise

    async def get_usd_balance(self) -> float:
        """Returns total available USD/USDC across both exchanges."""
        bal = await self.get_balance()
        return round(sum(v for k, v in bal.items() if "USD" in k), 2)

    async def verify_connectivity(self) -> dict:
        """
        Tests that both exchanges respond correctly.
        Call at startup to catch misconfigured API keys early.

        Returns:
            {"coinbase": bool, "kraken": bool, "errors": [str]}
        """
        result = {"coinbase": False, "kraken": False, "errors": []}

        try:
            await self._coinbase.fetch_balance()
            result["coinbase"] = True
            logger.info("Coinbase Advanced Trade — connected")
        except ccxt.AuthenticationError:
            msg = "Coinbase API key or secret is invalid. Check your .env file."
            result["errors"].append(msg)
            logger.error(msg)
        except Exception as e:
            msg = f"Coinbase error: {e}"
            result["errors"].append(msg)
            logger.error(msg)

        try:
            await self._kraken.fetch_balance()
            result["kraken"] = True
            logger.info("Kraken — connected")
        except ccxt.AuthenticationError:
            msg = "Kraken API key or secret is invalid. Check your .env file."
            result["errors"].append(msg)
            logger.error(msg)
        except Exception as e:
            msg = f"Kraken error: {e}"
            result["errors"].append(msg)
            logger.error(msg)

        return result
