"""
gemini_connector.py — Phase 2C
Spot crypto trading via Gemini REST API v1.
Gemini is US-accessible — no geo-block issues.

New .env vars required:
  GEMINI_API_KEY    — API key (Fund Management + Trading only — no withdrawal)
  GEMINI_SECRET_KEY — API secret key

Auth: HMAC-SHA384 over base64-encoded JSON payload.
Headers: X-GEMINI-APIKEY, X-GEMINI-PAYLOAD, X-GEMINI-SIGNATURE.

Permissions: Fund Management + Trading. Withdraw and Transfer stay OFF.
Store keys in Vaultwarden → Breadbot → "Gemini API Key" before adding to .env.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
API_KEY    = os.getenv("GEMINI_API_KEY",    "").strip()
SECRET_KEY = os.getenv("GEMINI_SECRET_KEY", "").strip()
BASE_URL   = os.getenv("GEMINI_BASE_URL",   "https://api.gemini.com").strip().rstrip("/")
_REQUEST_TIMEOUT = 10


def _check_config() -> None:
    if not API_KEY or not SECRET_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY and GEMINI_SECRET_KEY must be set in .env. "
            "Retrieve from Vaultwarden → Breadbot → Gemini API Key."
        )


def _nonce() -> int:
    return int(time.time() * 1000)


def _sign_payload(payload: dict) -> tuple[str, str]:
    """Return (b64_payload, hex_signature)."""
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
    sig = hmac.new(SECRET_KEY.encode("utf-8"), encoded.encode("utf-8"), hashlib.sha384).hexdigest()
    return encoded, sig


def _auth_headers(payload: dict) -> dict:
    _check_config()
    b64, sig = _sign_payload(payload)
    return {
        "Content-Type":       "text/plain",
        "X-GEMINI-APIKEY":    API_KEY,
        "X-GEMINI-PAYLOAD":   b64,
        "X-GEMINI-SIGNATURE": sig,
        "Cache-Control":      "no-cache",
    }


def _get_public(endpoint: str, params: dict | None = None) -> dict | list:
    """Unauthenticated GET — public market data endpoints."""
    resp = requests.get(f"{BASE_URL}{endpoint}", params=params, timeout=_REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _post_private(endpoint: str, extra: dict | None = None) -> dict | list:
    """Authenticated POST — private account/order endpoints."""
    payload = {"request": endpoint, "nonce": _nonce()}
    if extra:
        payload.update(extra)
    resp = requests.post(
        f"{BASE_URL}{endpoint}",
        headers=_auth_headers(payload),
        timeout=_REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


# ── Public endpoints ───────────────────────────────────────────────────────────

def get_ticker(symbol: str) -> dict:
    """Return current best bid, ask, and last trade price."""
    sym = symbol.upper().replace("-", "").replace("/", "")
    data = _get_public(f"/v1/pubticker/{sym}")
    logger.info("Ticker %s: bid=%s ask=%s last=%s", symbol, data.get("bid"), data.get("ask"), data.get("last"))
    return data


def get_order_book(symbol: str, limit_bids: int = 10, limit_asks: int = 10) -> dict:
    """Return current order book for a symbol."""
    sym = symbol.upper().replace("-", "").replace("/", "")
    params = {"limit_bids": limit_bids, "limit_asks": limit_asks}
    data = _get_public(f"/v1/book/{sym}", params=params)
    logger.info("Order book %s: %d bids / %d asks", symbol, len(data.get("bids", [])), len(data.get("asks", [])))
    return data


def get_symbols() -> list:
    """Return list of all available trading symbols."""
    return _get_public("/v1/symbols")


# ── Private endpoints ──────────────────────────────────────────────────────────

def get_account() -> dict:
    """Return account balances across all currencies."""
    data = _post_private("/v1/balances")
    nonzero = [b for b in data if float(b.get("amount", 0)) > 0]
    logger.info("Account balances: %d non-zero currencies", len(nonzero))
    return {"all": data, "nonzero": nonzero}


def get_open_orders() -> list:
    """Return all active orders."""
    data = _post_private("/v1/orders")
    logger.info("Open orders: %d found", len(data))
    return data


def place_order(symbol: str, side: str, amount: float,
                price: float, order_type: str = "exchange limit") -> dict:
    """
    Place an order.
    side: buy/sell.
    order_type: exchange limit (default), exchange market, exchange stop limit.
    amount: quantity in base currency.
    price: required for limit orders (use 0 for market).
    """
    sym = symbol.upper().replace("-", "").replace("/", "")
    extra = {
        "symbol":   sym,
        "amount":   str(amount),
        "price":    str(price),
        "side":     side.lower(),
        "type":     order_type,
        "options":  [],
    }
    result = _post_private("/v1/order/new", extra)
    logger.info("Order placed: %s %s %s @ %s order_id=%s status=%s",
                side, amount, symbol, price, result.get("order_id"), result.get("is_live"))
    return result


def cancel_order(order_id: int) -> dict:
    """Cancel an active order by order_id."""
    result = _post_private("/v1/order/cancel", {"order_id": order_id})
    logger.info("Order cancelled: order_id=%s", order_id)
    return result


def get_past_trades(symbol: str, limit: int = 50) -> list:
    """Return trade history for a symbol."""
    sym = symbol.upper().replace("-", "").replace("/", "")
    data = _post_private("/v1/mytrades", {"symbol": sym, "limit_trades": limit})
    logger.info("Past trades %s: %d records", symbol, len(data))
    return data


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(f"gemini_connector self-test | BASE_URL={BASE_URL}")

    try:
        syms = get_symbols()
        print(f"Symbols OK — {len(syms)} pairs available")
    except Exception as e:
        print(f"get_symbols failed: {e}")

    try:
        ticker = get_ticker("BTCUSD")
        print(f'BTC/USD ticker OK — bid={ticker.get('bid')} ask={ticker.get('ask')} last={ticker.get('last')}')
    except Exception as e:
        print(f"get_ticker failed: {e}")

    try:
        book = get_order_book("BTCUSD", limit_bids=3, limit_asks=3)
        print(f'Order book OK — best bid={book['bids'][0]['price']} best ask={book['asks'][0]['price']}')
    except Exception as e:
        print(f"get_order_book failed: {e}")

    if not API_KEY or not SECRET_KEY:
        print("GEMINI_API_KEY / GEMINI_SECRET_KEY not set — add to .env to test authenticated calls")
    else:
        try:
            acct = get_account()
            print(f"Account OK — {len(acct[nonzero])} non-zero balances")
        except Exception as e:
            print(f"get_account failed: {e}")

        try:
            orders = get_open_orders()
            print(f"Open orders OK — {len(orders)} open")
        except Exception as e:
            print(f"get_open_orders failed: {e}")
