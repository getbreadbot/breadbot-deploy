"""
alpaca_connector.py — Phase 2A
Commission-free stocks + crypto execution via Alpaca Broker API v2.
Paper trading mode is the default — set ALPACA_BASE_URL to the live endpoint
only after testing the full flow on paper.

New .env vars required:
  ALPACA_API_KEY    — API key ID (paper or live)
  ALPACA_SECRET_KEY — API secret key (paper or live)
  ALPACA_BASE_URL   — https://paper-api.alpaca.markets (paper, default)
                      https://api.alpaca.markets (live)
"""

import os
import logging
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY    = os.getenv("ALPACA_API_KEY",    "").strip()
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "").strip()
BASE_URL   = os.getenv("ALPACA_BASE_URL",   "https://paper-api.alpaca.markets").strip().rstrip("/")

_HEADERS = {
    "APCA-API-KEY-ID":     API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
    "Content-Type":        "application/json",
}

_DATA_URL = "https://data.alpaca.markets"


def _check_config() -> None:
    if not API_KEY or not SECRET_KEY:
        raise RuntimeError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env. "
            "Add credentials from Vaultwarden → Breadbot → Alpaca."
        )


# ── Account ───────────────────────────────────────────────────────────────────

def get_account() -> dict:
    """
    Return account details: buying power, portfolio value, cash, pattern-day-trader flag.

    Returns:
        Alpaca account object as a dict.
    """
    _check_config()
    resp = requests.get(f"{BASE_URL}/v2/account", headers=_HEADERS, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    logger.info(
        "Account: portfolio=$%.2f cash=$%.2f buying_power=$%.2f",
        float(data.get("portfolio_value", 0)),
        float(data.get("cash", 0)),
        float(data.get("buying_power", 0)),
    )
    return data


def get_positions() -> list[dict]:
    """
    Return all open positions.

    Returns:
        List of position dicts with symbol, qty, avg_entry_price, unrealized_pl.
    """
    _check_config()
    resp = requests.get(f"{BASE_URL}/v2/positions", headers=_HEADERS, timeout=10)
    resp.raise_for_status()
    positions = resp.json()
    logger.info("Open positions: %d", len(positions))
    return positions


# ── Orders ────────────────────────────────────────────────────────────────────

def place_order(
    symbol: str,
    qty: float | None = None,
    notional: float | None = None,
    side: str = "buy",
    order_type: str = "market",
    limit_price: float | None = None,
    stop_price: float | None = None,
    trail_percent: float | None = None,
    time_in_force: str = "day",
) -> dict:
    """
    Place a stock or crypto order.

    Args:
        symbol:        Ticker symbol, e.g. "AAPL" or "BTC/USD".
        qty:           Number of shares/coins. Provide either qty OR notional, not both.
        notional:      Dollar amount for fractional/notional orders.
        side:          "buy" or "sell".
        order_type:    "market", "limit", "stop", "stop_limit", or "trailing_stop".
        limit_price:   Required for limit and stop_limit orders.
        stop_price:    Required for stop and stop_limit orders.
        trail_percent: Required for trailing_stop orders (percentage, not basis points).
        time_in_force: "day", "gtc", "ioc", "fok". Crypto uses "gtc" or "ioc".

    Returns:
        Alpaca order object as a dict.

    Raises:
        ValueError  if required parameters are missing for the chosen order_type.
        RuntimeError if Alpaca rejects the order.
    """
    _check_config()

    if qty is None and notional is None:
        raise ValueError("Provide either qty or notional — not both, not neither.")
    if order_type in ("limit", "stop_limit") and limit_price is None:
        raise ValueError(f"limit_price required for order_type={order_type}")
    if order_type in ("stop", "stop_limit") and stop_price is None:
        raise ValueError(f"stop_price required for order_type={order_type}")
    if order_type == "trailing_stop" and trail_percent is None:
        raise ValueError("trail_percent required for order_type=trailing_stop")

    payload: dict = {
        "symbol":        symbol,
        "side":          side,
        "type":          order_type,
        "time_in_force": time_in_force,
    }
    if qty is not None:
        payload["qty"] = str(qty)
    else:
        payload["notional"] = str(notional)

    if limit_price is not None:
        payload["limit_price"] = str(limit_price)
    if stop_price is not None:
        payload["stop_price"] = str(stop_price)
    if trail_percent is not None:
        payload["trail_percent"] = str(trail_percent)

    resp = requests.post(f"{BASE_URL}/v2/orders", headers=_HEADERS, json=payload, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Alpaca rejected order: {resp.status_code} {resp.text}")

    order = resp.json()
    logger.info(
        "Order placed: %s %s %s type=%s id=%s",
        side.upper(), symbol, qty or f"${notional}", order_type, order.get("id"),
    )
    return order


def cancel_order(order_id: str) -> bool:
    """
    Cancel an open order by its Alpaca order ID.

    Returns:
        True if cancelled, False if already filled or not found.
    """
    _check_config()
    resp = requests.delete(f"{BASE_URL}/v2/orders/{order_id}", headers=_HEADERS, timeout=10)
    if resp.status_code == 204:
        logger.info("Order cancelled: %s", order_id)
        return True
    if resp.status_code == 422:
        logger.warning("Order %s could not be cancelled (already filled?)", order_id)
        return False
    resp.raise_for_status()
    return False


def cancel_all_orders() -> int:
    """
    Cancel all open orders. Returns the count of cancelled orders.
    """
    _check_config()
    resp = requests.delete(f"{BASE_URL}/v2/orders", headers=_HEADERS, timeout=15)
    if resp.status_code == 207:
        cancelled = [o for o in resp.json() if o.get("status") == 200]
        logger.info("Cancelled %d orders", len(cancelled))
        return len(cancelled)
    resp.raise_for_status()
    return 0


# ── Market data ───────────────────────────────────────────────────────────────

def get_bars(
    symbol: str,
    timeframe: str = "1Day",
    limit: int = 50,
    feed: str = "iex",
) -> list[dict]:
    """
    Return OHLCV bars for a stock or crypto symbol.

    Args:
        symbol:    Ticker, e.g. "AAPL" or "BTC/USD".
        timeframe: Bar size — "1Min", "5Min", "15Min", "1Hour", "1Day".
        limit:     Number of bars to return.
        feed:      Data feed — "iex" (free, stocks) or "sip" (requires subscription).
                   Use "crypto" endpoint for crypto symbols automatically.

    Returns:
        List of bar dicts with t (timestamp), o, h, l, c, v.
    """
    _check_config()

    is_crypto = "/" in symbol
    if is_crypto:
        endpoint = f"{_DATA_URL}/v1beta3/crypto/us/bars"
        params = {
            "symbols":   symbol,
            "timeframe": timeframe,
            "limit":     limit,
        }
        resp = requests.get(endpoint, headers=_HEADERS, params=params, timeout=10)
        resp.raise_for_status()
        bars = resp.json().get("bars", {}).get(symbol, [])
    else:
        endpoint = f"{_DATA_URL}/v2/stocks/{symbol}/bars"
        params = {
            "timeframe": timeframe,
            "limit":     limit,
            "feed":      feed,
        }
        resp = requests.get(endpoint, headers=_HEADERS, params=params, timeout=10)
        resp.raise_for_status()
        bars = resp.json().get("bars", [])

    logger.info("Bars for %s: %d candles returned", symbol, len(bars))
    return bars


def get_latest_quote(symbol: str) -> dict:
    """
    Return the latest bid/ask quote for a stock or crypto symbol.

    Returns:
        Dict with ap (ask price), bp (bid price), and as/bs (ask/bid sizes).
    """
    _check_config()

    is_crypto = "/" in symbol
    if is_crypto:
        endpoint = f"{_DATA_URL}/v1beta3/crypto/us/latest/quotes"
        params   = {"symbols": symbol}
        resp = requests.get(endpoint, headers=_HEADERS, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json().get("quotes", {}).get(symbol, {})
    else:
        endpoint = f"{_DATA_URL}/v2/stocks/{symbol}/quotes/latest"
        params   = {"feed": "iex"}
        resp = requests.get(endpoint, headers=_HEADERS, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json().get("quote", {})


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(f"alpaca_connector self-test | BASE_URL={BASE_URL}")

    if not API_KEY or not SECRET_KEY:
        print("ALPACA_API_KEY / ALPACA_SECRET_KEY not set — set them in .env to test")
    else:
        try:
            acct = get_account()
            print(f"Account OK — portfolio=${acct.get("portfolio_value")} cash=${acct.get("cash")}")
        except Exception as e:
            print(f"get_account failed: {e}")

        try:
            bars = get_bars("AAPL", limit=3)
            print(f"AAPL bars OK — {len(bars)} candles returned")
        except Exception as e:
            print(f"get_bars failed: {e}")
