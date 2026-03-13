"""
binance_connector.py — Phase 2B
Spot crypto trading via Binance.US REST API.
Adds access to token pairs not available on Coinbase or Kraken.

New .env vars required:
  BINANCE_API_KEY    — API key (Spot & Margin Trading only — no withdrawal)
  BINANCE_SECRET_KEY — API secret key
  BINANCE_BASE_URL   — https://api.binance.us (default, do not change)

Permissions: Enable Spot & Margin Trading only. Withdraw and Transfer stay OFF.
Store keys in Vaultwarden → Breadbot → "Binance.US API Key" before adding to .env.
"""

import hashlib
import hmac
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
API_KEY    = os.getenv("BINANCE_API_KEY",    "").strip()
SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "").strip()
BASE_URL   = os.getenv("BINANCE_BASE_URL",   "https://api.binance.us").strip().rstrip("/")
_REQUEST_TIMEOUT = 10


def _check_config() -> None:
    if not API_KEY or not SECRET_KEY:
        raise RuntimeError(
            "BINANCE_API_KEY and BINANCE_SECRET_KEY must be set in .env. "
            "Retrieve from Vaultwarden → Breadbot → Binance.US API Key."
        )


def _sign(params: dict) -> dict:
    params["timestamp"] = int(time.time() * 1000)
    query = urlencode(params)
    sig = hmac.new(SECRET_KEY.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    params["signature"] = sig
    return params


def _headers() -> dict:
    return {"X-MBX-APIKEY": API_KEY}


def _get(endpoint: str, params: dict | None = None, signed: bool = False) -> dict | list:
    if signed:
        _check_config()
        params = _sign(params or {})
    resp = requests.get(f"{BASE_URL}{endpoint}", headers=_headers() if signed else {},
                        params=params, timeout=_REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _post(endpoint: str, params: dict) -> dict:
    _check_config()
    params = _sign(params)
    resp = requests.post(f"{BASE_URL}{endpoint}", headers=_headers(),
                         params=params, timeout=_REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _delete(endpoint: str, params: dict) -> dict:
    _check_config()
    params = _sign(params)
    resp = requests.delete(f"{BASE_URL}{endpoint}", headers=_headers(),
                           params=params, timeout=_REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_ticker(symbol: str) -> dict:
    """Return 24-hr ticker stats. Fields: lastPrice, priceChangePercent, volume."""
    data = _get("/api/v3/ticker/24hr", params={"symbol": symbol.upper()})
    logger.info("Ticker %s: last=%s change=%s%%", symbol, data.get("lastPrice"), data.get("priceChangePercent"))
    return data


def get_order_book(symbol: str, limit: int = 10) -> dict:
    """Return order book. limit: 5/10/20/50/100/500/1000/5000."""
    data = _get("/api/v3/depth", params={"symbol": symbol.upper(), "limit": limit})
    logger.info("Order book %s: %d bids / %d asks", symbol, len(data.get("bids", [])), len(data.get("asks", [])))
    return data


def get_exchange_info(symbol: str | None = None) -> dict:
    """Return exchange info including lot-size and min-notional filters."""
    params = {"symbol": symbol.upper()} if symbol else {}
    return _get("/api/v3/exchangeInfo", params=params)


def get_account() -> dict:
    """Return account balances and trading permissions."""
    data = _get("/api/v3/account", signed=True)
    data["balances_nonzero"] = [b for b in data.get("balances", [])
                                 if float(b["free"]) > 0 or float(b["locked"]) > 0]
    logger.info("Account: %d non-zero balances, can_trade=%s",
                len(data["balances_nonzero"]), data.get("canTrade"))
    return data


def get_open_orders(symbol: str | None = None) -> list:
    """Return all open orders, optionally filtered to one symbol."""
    params: dict = {"symbol": symbol.upper()} if symbol else {}
    data = _get("/api/v3/openOrders", params=params, signed=True)
    logger.info("Open orders: %d found", len(data))
    return data


def place_order(symbol: str, side: str, order_type: str, quantity: float,
                price: float | None = None, time_in_force: str = "GTC") -> dict:
    """
    Place a spot order.
    side: 'BUY'/'SELL'. order_type: 'MARKET'/'LIMIT'/'STOP_LOSS_LIMIT'.
    price required for LIMIT orders.
    """
    params: dict = {"symbol": symbol.upper(), "side": side.upper(),
                    "type": order_type.upper(), "quantity": quantity}
    if order_type.upper() == "LIMIT":
        if price is None:
            raise ValueError("price is required for LIMIT orders")
        params["price"]       = price
        params["timeInForce"] = time_in_force
    result = _post("/api/v3/order", params)
    logger.info("Order placed: %s %s %s status=%s orderId=%s",
                side, quantity, symbol, result.get("status"), result.get("orderId"))
    return result


def cancel_order(symbol: str, order_id: int) -> dict:
    """Cancel an open order by symbol and orderId."""
    result = _delete("/api/v3/order", params={"symbol": symbol.upper(), "orderId": order_id})
    logger.info("Order cancelled: orderId=%s symbol=%s", order_id, symbol)
    return result


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(f"binance_connector self-test | BASE_URL={BASE_URL}")

    try:
        ticker = get_ticker("BTCUSDT")
        print(f"BTC/USDT ticker OK — last={ticker.get('lastPrice')} change={ticker.get('priceChangePercent')}%")
    except Exception as e:
        print(f"get_ticker failed: {e}")

    try:
        book = get_order_book("BTCUSDT", limit=5)
        print(f"Order book OK — best bid={book['bids'][0][0]} best ask={book['asks'][0][0]}")
    except Exception as e:
        print(f"get_order_book failed: {e}")

    if not API_KEY or not SECRET_KEY:
        print("BINANCE_API_KEY / BINANCE_SECRET_KEY not set — add to .env to test authenticated calls")
    else:
        try:
            acct = get_account()
            print(f"Account OK — can_trade={acct.get('canTrade')} balances={len(acct['balances_nonzero'])}")
        except Exception as e:
            print(f"get_account failed: {e}")
        try:
            orders = get_open_orders()
            print(f"Open orders OK — {len(orders)} open")
        except Exception as e:
            print(f"get_open_orders failed: {e}")
