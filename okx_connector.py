"""
okx_connector.py — Phase 2C
Crypto perpetual contracts via OKX REST API v5.
Adds perpetuals depth alongside Bybit — better liquidity on some altcoin perps.
Mirrors bybit_connector.py structure exactly.

New .env vars required:
  OKX_API_KEY      — API key (Trade only — no withdrawal)
  OKX_SECRET_KEY   — API secret key
  OKX_PASSPHRASE   — Account passphrase set when creating the API key
  OKX_MAX_LEVERAGE — Maximum leverage bot will set per position (default 3)
  OKX_BASE_URL     — https://www.okx.com

Instrument format: BTC-USDT-SWAP (perpetual swap, USDT-margined)

Permissions: Trade only. Withdraw stays OFF.
Store keys in Vaultwarden → Breadbot → "OKX API Key" before adding to .env.

Auth scheme:
  Header: OK-ACCESS-KEY, OK-ACCESS-SIGN, OK-ACCESS-TIMESTAMP, OK-ACCESS-PASSPHRASE
  Signature: HMAC-SHA256( timestamp + method + path + body )
  Timestamp: ISO 8601 — e.g. 2024-01-01T00:00:00.123Z

Risk rules enforced here:
  - Max leverage capped at OKX_MAX_LEVERAGE before every order
  - Liquidation price must be >20% from mark price
  Both checks raise ValueError — calling code must catch.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
API_KEY      = os.getenv("OKX_API_KEY",      "").strip()
SECRET_KEY   = os.getenv("OKX_SECRET_KEY",   "").strip()
PASSPHRASE   = os.getenv("OKX_PASSPHRASE",   "").strip()
MAX_LEVERAGE = int(os.getenv("OKX_MAX_LEVERAGE", "3"))
BASE_URL     = os.getenv("OKX_BASE_URL", "https://www.okx.com").strip().rstrip("/")
_REQUEST_TIMEOUT = 10


def _check_config() -> None:
    if not API_KEY or not SECRET_KEY or not PASSPHRASE:
        raise RuntimeError(
            "OKX_API_KEY, OKX_SECRET_KEY, and OKX_PASSPHRASE must be set in .env. "
            "Retrieve from Vaultwarden → Breadbot → OKX API Key."
        )


def _iso_timestamp() -> str:
    """Return current UTC time in ISO 8601 format as required by OKX auth."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _sign(timestamp: str, method: str, path: str, body: str = "") -> str:
    """HMAC-SHA256 of timestamp + METHOD + path + body, base64-encoded."""
    pre_hash = f"{timestamp}{method.upper()}{path}{body}"
    mac = hmac.new(SECRET_KEY.encode("utf-8"), pre_hash.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def _headers(timestamp: str, signature: str, signed: bool = True) -> dict:
    h = {"Content-Type": "application/json"}
    if signed:
        h.update({
            "OK-ACCESS-KEY":        API_KEY,
            "OK-ACCESS-SIGN":       signature,
            "OK-ACCESS-TIMESTAMP":  timestamp,
            "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        })
    return h


def _get(path: str, params: dict | None = None, signed: bool = False) -> dict:
    if signed:
        _check_config()
    qs = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
    ts  = _iso_timestamp()
    sig = _sign(ts, "GET", path + qs) if signed else ""
    resp = requests.get(f"{BASE_URL}{path}", headers=_headers(ts, sig, signed),
                        params=params, timeout=_REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code", "0") != "0":
        raise RuntimeError(f"OKX API error: {data.get('code')} — {data.get('msg')}")
    return data


def _post(path: str, body: dict) -> dict:
    _check_config()
    payload = json.dumps(body)
    ts  = _iso_timestamp()
    sig = _sign(ts, "POST", path, payload)
    resp = requests.post(f"{BASE_URL}{path}", headers=_headers(ts, sig),
                         data=payload, timeout=_REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code", "0") != "0":
        raise RuntimeError(f"OKX API error: {data.get('code')} — {data.get('msg')}")
    return data


# ── Market data (no auth) ──────────────────────────────────────────────────────

def get_ticker(inst_id: str) -> dict:
    """
    Return real-time ticker for a swap instrument.
    inst_id format: BTC-USDT-SWAP
    Fields returned: instId, last, markPx.
    """
    data = _get("/api/v5/market/ticker", params={"instId": inst_id.upper()})
    result = data["data"][0] if data.get("data") else {}
    logger.info("Ticker %s: last=%s markPx=%s", inst_id, result.get("last"), result.get("markPx"))
    return result


def get_funding_rate(inst_id: str) -> dict:
    """
    Return current and next funding rate for a swap instrument.
    Fields: fundingRate, nextFundingRate, fundingTime.
    """
    data = _get("/api/v5/public/funding-rate", params={"instId": inst_id.upper()})
    result = data["data"][0] if data.get("data") else {}
    logger.info("Funding %s: current=%s next=%s", inst_id,
                result.get("fundingRate"), result.get("nextFundingRate"))
    return result


# ── Account (signed) ───────────────────────────────────────────────────────────

def get_wallet_balance(ccy: str = "USDT") -> dict:
    """
    Return account balance for a currency.
    Fields: ccy, availBal, frozenBal, eq (total equity).
    """
    data = _get("/api/v5/account/balance", params={"ccy": ccy.upper()}, signed=True)
    details = data["data"][0].get("details", []) if data.get("data") else []
    coin_data = next((d for d in details if d.get("ccy") == ccy.upper()), {})
    logger.info("Wallet %s: eq=%s availBal=%s", ccy, coin_data.get("eq"), coin_data.get("availBal"))
    return coin_data


def get_positions(inst_id: str | None = None) -> list:
    """
    Return open swap positions.
    Fields per position: instId, posSide, pos, avgPx, liqPx, upl, lever.
    """
    params: dict = {"instType": "SWAP"}
    if inst_id:
        params["instId"] = inst_id.upper()
    data = _get("/api/v5/account/positions", params=params, signed=True)
    positions = [p for p in (data.get("data") or []) if float(p.get("pos", 0)) != 0]
    logger.info("Open positions: %d", len(positions))
    return positions


# ── Risk helpers ───────────────────────────────────────────────────────────────

def _check_leverage(requested: int) -> None:
    if requested > MAX_LEVERAGE:
        raise ValueError(
            f"Requested leverage {requested}x exceeds OKX_MAX_LEVERAGE={MAX_LEVERAGE}x. "
            "Reduce leverage or raise OKX_MAX_LEVERAGE in .env."
        )


def _check_liquidation_distance(mark_price: float, liq_price: float, pos_side: str) -> None:
    """Raise if liquidation is within 20% of mark price."""
    if liq_price <= 0:
        return
    pct_away = ((mark_price - liq_price) / mark_price if pos_side.lower() == "long"
                else (liq_price - mark_price) / mark_price)
    if pct_away < 0.20:
        raise ValueError(
            f"Liquidation {liq_price} is {pct_away:.1%} from mark {mark_price}. "
            "Reduce position size or leverage to maintain >20% buffer."
        )


# ── Trading (signed) ───────────────────────────────────────────────────────────

def set_leverage(inst_id: str, leverage: int, mgn_mode: str = "cross") -> dict:
    """
    Set leverage for an instrument. Capped by OKX_MAX_LEVERAGE.
    mgn_mode: 'cross' or 'isolated'.
    """
    _check_leverage(leverage)
    result = _post("/api/v5/account/set-leverage", {
        "instId":  inst_id.upper(),
        "lever":   str(leverage),
        "mgnMode": mgn_mode,
    })
    logger.info("Leverage set: %s → %dx (%s margin)", inst_id, leverage, mgn_mode)
    return result


def place_perp_order(inst_id: str, side: str, sz: float,
                     order_type: str = "market", price: float | None = None,
                     reduce_only: bool = False, td_mode: str = "cross") -> dict:
    """
    Place a swap order.
    inst_id: BTC-USDT-SWAP  |  side: 'buy'/'sell'  |  order_type: 'market'/'limit'
    Risk checks run before placing — leverage and liquidation distance.
    """
    existing = get_positions(inst_id)
    for pos in existing:
        _check_leverage(int(float(pos.get("lever", 1))))
        liq  = float(pos.get("liqPx") or 0)
        mark = float(pos.get("markPx") or 0)
        if mark > 0:
            _check_liquidation_distance(mark, liq, pos.get("posSide", "long"))

    body: dict = {
        "instId":  inst_id.upper(),
        "tdMode":  td_mode,
        "side":    side.lower(),
        "ordType": order_type.lower(),
        "sz":      str(sz),
    }
    if reduce_only:
        body["reduceOnly"] = "true"
    if order_type.lower() == "limit":
        if price is None:
            raise ValueError("price required for limit orders")
        body["px"] = str(price)

    result = _post("/api/v5/trade/order", body)
    order_info = result["data"][0] if result.get("data") else {}
    logger.info("Perp order: %s %s %s ordId=%s", side, sz, inst_id, order_info.get("ordId"))
    return order_info


def cancel_order(inst_id: str, ord_id: str) -> dict:
    """Cancel an open swap order by instrument ID and order ID."""
    result = _post("/api/v5/trade/cancel-order", {
        "instId": inst_id.upper(),
        "ordId":  ord_id,
    })
    order_info = result["data"][0] if result.get("data") else {}
    logger.info("Order cancelled: ordId=%s instId=%s", ord_id, inst_id)
    return order_info


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(f"okx_connector self-test | BASE_URL={BASE_URL} | MAX_LEVERAGE={MAX_LEVERAGE}x")

    try:
        ticker = get_ticker("BTC-USDT-SWAP")
        print(f"BTC-USDT-SWAP ticker OK — last={ticker.get('last')} markPx={ticker.get('markPx')}")
    except Exception as e:
        print(f"get_ticker failed: {e}")

    try:
        fr = get_funding_rate("SOL-USDT-SWAP")
        print(f"SOL-USDT-SWAP funding OK — current={fr.get('fundingRate')} next={fr.get('nextFundingRate')}")
    except Exception as e:
        print(f"get_funding_rate failed: {e}")

    try:
        _check_leverage(MAX_LEVERAGE + 1)
        print("WARN: leverage guard did not fire")
    except ValueError as e:
        print(f"Leverage guard OK — caught: {e}")

    if not API_KEY or not SECRET_KEY or not PASSPHRASE:
        print("OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE not set — add to .env to test authenticated calls")
    else:
        try:
            bal = get_wallet_balance("USDT")
            print(f"Wallet OK — USDT eq={bal.get('eq')} availBal={bal.get('availBal')}")
        except Exception as e:
            print(f"get_wallet_balance failed: {e}")
        try:
            positions = get_positions()
            print(f"Positions OK — {len(positions)} open")
            for p in positions[:3]:
                print(f"  {p['instId']} {p.get('posSide')} pos={p['pos']} avgPx={p.get('avgPx')} "
                      f"liqPx={p.get('liqPx')} upl={p.get('upl')}")
        except Exception as e:
            print(f"get_positions failed: {e}")
