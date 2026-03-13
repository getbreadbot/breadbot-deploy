"""
bybit_connector.py — Phase 2B
Crypto perpetual contracts via Bybit V5 Unified API.
Enables short selling and leveraged positions on BTC, ETH, SOL, and altcoins.

New .env vars required:
  BYBIT_API_KEY      — API key (Derivatives Trading enabled — no withdrawal)
  BYBIT_SECRET_KEY   — API secret key
  BYBIT_MAX_LEVERAGE — Maximum leverage bot will set per position (default 3)
  BYBIT_BASE_URL     — https://api.bybit.com (testnet: https://api-testnet.bybit.com)

Permissions: Derivatives Trading only. Withdraw stays OFF.
Store keys in Vaultwarden → Breadbot → "Bybit API Key" before adding to .env.

Risk rules enforced here:
  - Max leverage capped at BYBIT_MAX_LEVERAGE before every order
  - Liquidation price must be >20% from mark price
  Both checks raise ValueError — calling code must catch.
"""

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
API_KEY      = os.getenv("BYBIT_API_KEY",      "").strip()
SECRET_KEY   = os.getenv("BYBIT_SECRET_KEY",   "").strip()
MAX_LEVERAGE = int(os.getenv("BYBIT_MAX_LEVERAGE", "3"))
BASE_URL     = os.getenv("BYBIT_BASE_URL", "https://api.bybit.com").strip().rstrip("/")
_RECV_WINDOW = "5000"
_REQUEST_TIMEOUT = 10


def _check_config() -> None:
    if not API_KEY or not SECRET_KEY:
        raise RuntimeError(
            "BYBIT_API_KEY and BYBIT_SECRET_KEY must be set in .env. "
            "Retrieve from Vaultwarden → Breadbot → Bybit API Key."
        )


def _sign(params_str: str, timestamp: str) -> str:
    pre_hash = f"{timestamp}{API_KEY}{_RECV_WINDOW}{params_str}"
    return hmac.new(SECRET_KEY.encode("utf-8"), pre_hash.encode("utf-8"), hashlib.sha256).hexdigest()


def _headers(timestamp: str, signature: str) -> dict:
    return {"X-BAPI-API-KEY": API_KEY, "X-BAPI-SIGN": signature,
            "X-BAPI-TIMESTAMP": timestamp, "X-BAPI-RECV-WINDOW": _RECV_WINDOW,
            "Content-Type": "application/json"}


def _get(endpoint: str, params: dict | None = None, signed: bool = False) -> dict:
    if signed:
        _check_config()
    qs = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    ts = str(int(time.time() * 1000))
    sig = _sign(qs, ts) if signed else ""
    resp = requests.get(f"{BASE_URL}{endpoint}",
                        headers=_headers(ts, sig) if signed else {},
                        params=params, timeout=_REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("retCode", 0) != 0:
        raise RuntimeError(f"Bybit API error: {data.get('retCode')} — {data.get('retMsg')}")
    return data


def _post(endpoint: str, body: dict) -> dict:
    _check_config()
    payload = json.dumps(body)
    ts = str(int(time.time() * 1000))
    sig = _sign(payload, ts)
    resp = requests.post(f"{BASE_URL}{endpoint}", headers=_headers(ts, sig),
                         data=payload, timeout=_REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("retCode", 0) != 0:
        raise RuntimeError(f"Bybit API error: {data.get('retCode')} — {data.get('retMsg')}")
    return data


# ── Market data (no auth) ──────────────────────────────────────────────────────

def get_ticker(symbol: str) -> dict:
    """Return real-time ticker for a linear perpetual. Fields: lastPrice, markPrice, fundingRate."""
    data = _get("/v5/market/tickers", params={"category": "linear", "symbol": symbol.upper()})
    result = data["result"]["list"][0] if data["result"]["list"] else {}
    logger.info("Ticker %s: last=%s mark=%s funding=%s",
                symbol, result.get("lastPrice"), result.get("markPrice"), result.get("fundingRate"))
    return result


def get_funding_rate(symbol: str, limit: int = 5) -> list:
    """Return recent funding rate history for a symbol."""
    data = _get("/v5/market/funding/history",
                params={"category": "linear", "symbol": symbol.upper(), "limit": limit})
    return data["result"]["list"]


# ── Account (signed) ───────────────────────────────────────────────────────────

def get_wallet_balance(coin: str = "USDT") -> dict:
    """Return unified account balance for a coin. Fields: equity, availableToWithdraw, unrealisedPnl."""
    data = _get("/v5/account/wallet-balance",
                params={"accountType": "UNIFIED", "coin": coin.upper()}, signed=True)
    coins = data["result"]["list"][0]["coin"] if data["result"]["list"] else []
    coin_data = next((c for c in coins if c["coin"] == coin.upper()), {})
    logger.info("Wallet %s: equity=%s available=%s", coin, coin_data.get("equity"), coin_data.get("availableToWithdraw"))
    return coin_data


def get_positions(symbol: str | None = None) -> list:
    """Return open perpetual positions. Fields per position: symbol, side, size, entryPrice, liqPrice, unrealisedPnl."""
    params: dict = {"category": "linear", "settleCoin": "USDT"}
    if symbol:
        params["symbol"] = symbol.upper()
    data = _get("/v5/position/list", params=params, signed=True)
    positions = [p for p in data["result"]["list"] if float(p.get("size", 0)) > 0]
    logger.info("Open positions: %d", len(positions))
    return positions


# ── Risk helpers ───────────────────────────────────────────────────────────────

def _check_leverage(requested: int) -> None:
    if requested > MAX_LEVERAGE:
        raise ValueError(
            f"Requested leverage {requested}x exceeds BYBIT_MAX_LEVERAGE={MAX_LEVERAGE}x. "
            "Reduce leverage or raise BYBIT_MAX_LEVERAGE in .env."
        )


def _check_liquidation_distance(mark_price: float, liq_price: float, side: str) -> None:
    """Raise if liquidation is within 20% of mark price."""
    if liq_price <= 0:
        return
    pct_away = ((mark_price - liq_price) / mark_price if side.upper() == "BUY"
                else (liq_price - mark_price) / mark_price)
    if pct_away < 0.20:
        raise ValueError(
            f"Liquidation {liq_price} is {pct_away:.1%} from mark {mark_price}. "
            "Reduce position size or leverage to maintain >20% buffer."
        )


# ── Trading (signed) ───────────────────────────────────────────────────────────

def set_leverage(symbol: str, leverage: int) -> dict:
    """Set leverage for a symbol. Capped by BYBIT_MAX_LEVERAGE."""
    _check_leverage(leverage)
    result = _post("/v5/position/set-leverage", {
        "category": "linear", "symbol": symbol.upper(),
        "buyLeverage": str(leverage), "sellLeverage": str(leverage),
    })
    logger.info("Leverage set: %s → %dx", symbol, leverage)
    return result


def place_perp_order(symbol: str, side: str, qty: float,
                     order_type: str = "Market", price: float | None = None,
                     reduce_only: bool = False) -> dict:
    """
    Place a linear perpetual order. side: 'Buy'/'Sell'. order_type: 'Market'/'Limit'.
    Risk checks: validates leverage and liquidation distance before placing.
    """
    existing = get_positions(symbol)
    for pos in existing:
        _check_leverage(int(float(pos.get("leverage", 1))))
        liq  = float(pos.get("liqPrice") or 0)
        mark = float(pos.get("markPrice") or 0)
        if mark > 0:
            _check_liquidation_distance(mark, liq, pos.get("side", side))

    body: dict = {"category": "linear", "symbol": symbol.upper(),
                  "side": side.capitalize(), "orderType": order_type.capitalize(),
                  "qty": str(qty), "reduceOnly": reduce_only}
    if order_type.lower() == "limit":
        if price is None:
            raise ValueError("price required for Limit orders")
        body["price"] = str(price)

    result = _post("/v5/order/create", body)
    logger.info("Perp order: %s %s %s orderId=%s", side, qty, symbol, result["result"].get("orderId"))
    return result["result"]


def cancel_order(symbol: str, order_id: str) -> dict:
    """Cancel an open perpetual order by symbol and orderId."""
    result = _post("/v5/order/cancel", {"category": "linear", "symbol": symbol.upper(), "orderId": order_id})
    logger.info("Order cancelled: orderId=%s symbol=%s", order_id, symbol)
    return result["result"]


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(f"bybit_connector self-test | BASE_URL={BASE_URL} | MAX_LEVERAGE={MAX_LEVERAGE}x")

    try:
        ticker = get_ticker("BTCUSDT")
        print(f"BTC ticker OK — last={ticker.get('lastPrice')} mark={ticker.get('markPrice')} funding={ticker.get('fundingRate')}")
    except Exception as e:
        print(f"get_ticker failed: {e}")

    try:
        rates = get_funding_rate("SOLUSDT", limit=3)
        print(f"Funding history OK — {len(rates)} records, latest={rates[0].get('fundingRate') if rates else 'n/a'}")
    except Exception as e:
        print(f"get_funding_rate failed: {e}")

    if not API_KEY or not SECRET_KEY:
        print("BYBIT_API_KEY / BYBIT_SECRET_KEY not set — add to .env to test authenticated calls")
    else:
        try:
            bal = get_wallet_balance("USDT")
            print(f"Wallet OK — USDT equity={bal.get('equity')} available={bal.get('availableToWithdraw')}")
        except Exception as e:
            print(f"get_wallet_balance failed: {e}")
        try:
            positions = get_positions()
            print(f"Positions OK — {len(positions)} open")
            for p in positions[:3]:
                print(f"  {p['symbol']} {p['side']} size={p['size']} entry={p['entryPrice']} "
                      f"liq={p.get('liqPrice')} pnl={p.get('unrealisedPnl')}")
        except Exception as e:
            print(f"get_positions failed: {e}")

    try:
        _check_leverage(MAX_LEVERAGE + 1)
        print("WARN: leverage guard did not fire")
    except ValueError as e:
        print(f"Leverage guard OK — caught: {e}")
