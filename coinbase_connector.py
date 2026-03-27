#!/usr/bin/env python3
"""
coinbase_connector.py — Coinbase Advanced Trade (spot) + CFM Perpetuals

Spot trading uses the Coinbase Advanced Trade REST API v3.
Perpetuals use the Coinbase Financial Markets (CFM) derivatives endpoints
on the same base URL — same API key, separate margin account.

Auth: HMAC-SHA256 (legacy Advanced Trade keys).
      COINBASE_API_KEY = key name
      COINBASE_API_SECRET = secret (base64-encoded per CB format)

CFM instruments:
  BTC-PERP-INTX  — 1/100th BTC per contract (Nano)
  ETH-PERP-INTX  — 1/100th ETH per contract (Nano)

Funding accrues hourly, settles twice daily.
Leverage up to 10x. Fees: 0.02% maker / 0.05% taker.

New .env var:
  COINBASE_PERP_ENABLED=false   (opt-in)
"""

import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY     = os.getenv("COINBASE_API_KEY", "").strip()
API_SECRET  = os.getenv("COINBASE_API_SECRET", "").strip()
BASE_URL    = "https://api.coinbase.com"
PERP_ENABLED = os.getenv("COINBASE_PERP_ENABLED", "false").lower() == "true"

# Map short pair name → CFM product ID (Nano contracts)
PERP_PRODUCT = {
    "BTC": "BTC-PERP-INTX",
    "ETH": "ETH-PERP-INTX",
    "SOL": "SOL-PERP-INTX",
}


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _sign(method: str, path: str, body: str = "") -> dict:
    """
    Build HMAC-SHA256 auth headers for Coinbase Advanced Trade API.
    https://docs.cdp.coinbase.com/advanced-trade/docs/rest-api-auth
    """
    ts = str(int(time.time()))
    msg = ts + method.upper() + path + body
    sig = hmac.new(
        API_SECRET.encode("utf-8"),
        msg.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return {
        "CB-ACCESS-KEY":       API_KEY,
        "CB-ACCESS-SIGN":      sig,
        "CB-ACCESS-TIMESTAMP": ts,
        "Content-Type":        "application/json",
    }


def _get(path: str, params: Optional[dict] = None) -> dict:
    url = BASE_URL + path
    headers = _sign("GET", path)
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _post(path: str, payload: dict) -> dict:
    body = json.dumps(payload)
    url = BASE_URL + path
    headers = _sign("POST", path, body)
    resp = requests.post(url, headers=headers, data=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ── Spot / account ────────────────────────────────────────────────────────────

def get_account() -> dict:
    """
    Return spot wallet summary: USD buying power, portfolio value.
    """
    data = _get("/api/v3/brokerage/accounts")
    accounts = data.get("accounts", [])
    usdc = next(
        (a for a in accounts if a.get("currency") == "USDC"),
        {}
    )
    return {
        "buying_power_usdc": float(usdc.get("available_balance", {}).get("value", 0)),
        "accounts":          accounts,
    }


def get_spot_positions() -> list[dict]:
    """Return all non-zero spot balances."""
    data     = _get("/api/v3/brokerage/accounts")
    accounts = data.get("accounts", [])
    out = []
    for a in accounts:
        val = float(a.get("available_balance", {}).get("value", 0))
        hld = float(a.get("hold", {}).get("value", 0))
        if val + hld > 0.0001:
            out.append({
                "currency":  a["currency"],
                "available": val,
                "hold":      hld,
                "total":     val + hld,
            })
    return out


def get_price(product_id: str) -> float:
    """Return last trade price for a product (e.g. 'BTC-USDC')."""
    data = _get(f"/api/v3/brokerage/best_bid_ask", params={"product_ids": product_id})
    pricebooks = data.get("pricebooks", [{}])
    if pricebooks:
        bids = pricebooks[0].get("bids", [])
        asks = pricebooks[0].get("asks", [])
        if bids and asks:
            return (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
    return 0.0


def place_spot_order(
    product_id: str,
    side: str,          # "BUY" or "SELL"
    base_size: str,     # quantity in base asset as string
    order_type: str = "market_market_ioc",
) -> dict:
    """
    Place a spot market order.
    product_id: e.g. "BTC-USDC"
    base_size:  quantity in base asset (string to avoid float precision issues)
    """
    payload = {
        "client_order_id": f"bb_{int(time.time())}",
        "product_id":      product_id,
        "side":            side.upper(),
        "order_configuration": {
            "market_market_ioc": {
                "base_size": base_size,
            }
        },
    }
    result = _post("/api/v3/brokerage/orders", payload)
    return result.get("success_response", result)


def cancel_spot_order(order_id: str) -> dict:
    payload = {"order_ids": [order_id]}
    return _post("/api/v3/brokerage/orders/batch_cancel", payload)


# ── CFM Perpetuals ────────────────────────────────────────────────────────────

def get_perp_account() -> dict:
    """
    Return the CFM margin account summary.
    Includes: total equity, available margin, unrealized PnL, initial margin used.
    Returns empty dict with error key if COINBASE_PERP_ENABLED=false.
    """
    if not PERP_ENABLED:
        return {"error": "COINBASE_PERP_ENABLED=false"}
    try:
        data = _get("/api/v3/brokerage/cfm/user_summary")
        portfolio = data.get("summary", {})
        return {
            "unrealized_pnl":       float(portfolio.get("unrealized_pnl", {}).get("value", 0)),
            "available_margin":     float(portfolio.get("available_margin", {}).get("value", 0)),
            "portfolio_im_notional": float(portfolio.get("portfolio_im_notional", {}).get("value", 0)),
            "portfolio_initial_margin": float(portfolio.get("portfolio_initial_margin", {}).get("value", 0)),
            "raw": portfolio,
        }
    except Exception as exc:
        log.error("get_perp_account: %s", exc)
        return {"error": str(exc)}


def get_perp_positions() -> list[dict]:
    """
    Return all open CFM perpetual positions with unrealized PnL.
    Each dict includes: product_id, side, size, entry_price, current_price,
    unrealized_pnl, funding_collected.
    """
    if not PERP_ENABLED:
        return []
    try:
        data = _get("/api/v3/brokerage/cfm/positions")
        positions = data.get("positions", [])
        out = []
        for p in positions:
            out.append({
                "product_id":      p.get("product_id"),
                "side":            p.get("side"),           # LONG or SHORT
                "size":            float(p.get("net_size", 0)),
                "entry_price":     float(p.get("entry_vwap", {}).get("value", 0)),
                "unrealized_pnl":  float(p.get("unrealized_pnl", {}).get("value", 0)),
                "position_id":     p.get("product_id"),    # use product_id as handle
                "raw":             p,
            })
        return out
    except Exception as exc:
        log.error("get_perp_positions: %s", exc)
        return []


def get_perp_funding_rate(pair: str) -> dict:
    """
    Return current hourly funding rate for a CFM perpetual.
    pair: "BTC", "ETH", or "SOL"
    Returns: {rate_hourly, rate_annualized, product_id}

    CFM uses hourly funding (not 8h like Bybit). Annualized = rate * 24 * 365 * 100.
    """
    if not PERP_ENABLED:
        return {"error": "COINBASE_PERP_ENABLED=false"}

    product_id = PERP_PRODUCT.get(pair.upper())
    if not product_id:
        return {"error": f"Unknown pair: {pair}"}

    try:
        data = _get(f"/api/v3/brokerage/products/{product_id}")
        # Funding rate is in the product's perpetual details
        product = data.get("product", data)
        perp_details = product.get("future_product_details", {})
        rate_str = perp_details.get("funding_rate", "0")
        try:
            rate = float(rate_str)
        except (TypeError, ValueError):
            rate = 0.0

        ann = rate * 24 * 365 * 100   # hourly → annualized %
        return {
            "product_id":      product_id,
            "rate_hourly":     rate,
            "rate_annualized": round(ann, 4),
            "direction":       "long_pays_short" if rate > 0 else "short_pays_long",
        }
    except Exception as exc:
        log.error("get_perp_funding_rate %s: %s", pair, exc)
        return {"error": str(exc), "rate_hourly": 0.0, "rate_annualized": 0.0}


def place_perp_order(
    pair: str,
    side: str,          # "BUY" (long) or "SELL" (short)
    size: float,        # number of contracts (1 contract = 1/100th coin for Nano)
    leverage: int = 1,
    reduce_only: bool = False,
) -> dict:
    """
    Place a CFM perpetual futures order.
    pair:   "BTC", "ETH", or "SOL"
    side:   "BUY" to open long / close short; "SELL" to open short / close long
    size:   number of contracts (Nano: 1 contract = 0.01 BTC / 0.01 ETH)
    leverage: 1–10 (enforced on the margin account side by CFM)
    reduce_only: True to close an existing position only

    Returns the order response dict or raises on failure.
    """
    if not PERP_ENABLED:
        raise RuntimeError("COINBASE_PERP_ENABLED=false — perpetuals are disabled")

    product_id = PERP_PRODUCT.get(pair.upper())
    if not product_id:
        raise ValueError(f"Unknown pair for CFM: {pair}")

    payload = {
        "client_order_id": f"bb_perp_{int(time.time())}",
        "product_id":      product_id,
        "side":            side.upper(),
        "order_configuration": {
            "market_market_ioc": {
                "base_size": str(round(size, 8)),
            }
        },
        "leverage":        str(leverage),
    }
    if reduce_only:
        payload["order_configuration"]["market_market_ioc"]["post_only"] = False
        # CFM: pass margin_type CROSS for reduce-only close
        payload["margin_type"] = "CROSS"

    result = _post("/api/v3/brokerage/orders", payload)
    order = result.get("success_response", result)
    log.info(
        "CFM perp order placed: %s %s %s x%d contracts | order_id=%s",
        side, product_id, size, leverage, order.get("order_id", "?"),
    )
    return order


def close_perp_position(pair: str) -> dict:
    """
    Close the entire open CFM position for a pair by placing a reduce-only
    market order on the opposite side.
    pair: "BTC", "ETH", or "SOL"
    Returns the order response.
    """
    if not PERP_ENABLED:
        raise RuntimeError("COINBASE_PERP_ENABLED=false")

    positions = get_perp_positions()
    product_id = PERP_PRODUCT.get(pair.upper())
    pos = next((p for p in positions if p.get("product_id") == product_id), None)

    if not pos:
        log.warning("close_perp_position: no open position found for %s", pair)
        return {"error": "no open position", "pair": pair}

    size         = abs(pos["size"])
    current_side = pos["side"]
    close_side   = "BUY" if current_side == "SHORT" else "SELL"

    log.info(
        "Closing CFM perp: %s | side=%s size=%.6f → placing %s order",
        pair, current_side, size, close_side,
    )
    return place_perp_order(pair, close_side, size, reduce_only=True)


# ── Convenience: CFM funding rate in Bybit-compatible format ─────────────────

def get_funding_rate(pair: str) -> dict:
    """
    Alias used by funding_arb_engine when FUNDING_ARB_EXCHANGE=coinbase_cfm.
    Returns rate in the same shape as bybit_connector.get_funding_rate().
    Converts hourly CFM rate to 8h equivalent for arb engine compatibility.
    """
    result = get_perp_funding_rate(pair)
    hourly = result.get("rate_hourly", 0.0)
    # Convert hourly → 8h to match the Bybit-based arb engine's rate scale
    rate_8h = hourly * 8
    return {
        "fundingRate":      rate_8h,
        "fundingRateHourly": hourly,
        "annualized":       result.get("rate_annualized", 0.0),
        "product_id":       result.get("product_id", ""),
        "source":           "coinbase_cfm",
    }


# ── Self-test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    print("Coinbase connector self-test")
    print(f"  API key configured:  {'yes' if API_KEY else 'NO — set COINBASE_API_KEY'}")
    print(f"  Secret configured:   {'yes' if API_SECRET else 'NO — set COINBASE_API_SECRET'}")
    print(f"  Perp enabled:        {PERP_ENABLED}")
    print()

    if not API_KEY or not API_SECRET:
        print("API key/secret not set. Skipping live tests.")
        sys.exit(0)

    # Spot account test
    print("Fetching spot account...")
    try:
        acct = get_account()
        print(f"  USDC buying power: ${acct.get('buying_power_usdc', 0):,.2f}")
    except Exception as e:
        print(f"  FAILED: {e}")

    # Price test
    print("Fetching BTC-USDC price...")
    try:
        p = get_price("BTC-USDC")
        print(f"  BTC mid price: ${p:,.2f}")
    except Exception as e:
        print(f"  FAILED: {e}")

    # Perp tests (only if enabled)
    if PERP_ENABLED:
        print("Fetching CFM perp account...")
        try:
            pa = get_perp_account()
            if "error" not in pa:
                print(f"  Available margin: ${pa.get('available_margin', 0):,.2f}")
                print(f"  Unrealized PnL:   ${pa.get('unrealized_pnl', 0):,.4f}")
            else:
                print(f"  {pa['error']}")
        except Exception as e:
            print(f"  FAILED: {e}")

        print("Fetching BTC-PERP funding rate...")
        try:
            fr = get_perp_funding_rate("BTC")
            if "error" not in fr:
                print(f"  Rate (hourly):     {fr['rate_hourly']*100:.6f}%")
                print(f"  Rate (annualized): {fr['rate_annualized']:.2f}%")
                print(f"  Direction:         {fr['direction']}")
            else:
                print(f"  {fr['error']}")
        except Exception as e:
            print(f"  FAILED: {e}")

        print("Fetching open perp positions...")
        try:
            positions = get_perp_positions()
            print(f"  Open positions: {len(positions)}")
            for p in positions:
                print(f"    {p['product_id']} {p['side']} {p['size']} | uPnL ${p['unrealized_pnl']:.4f}")
        except Exception as e:
            print(f"  FAILED: {e}")
    else:
        print("COINBASE_PERP_ENABLED=false — skipping perp tests")
        print("  Set COINBASE_PERP_ENABLED=true in .env to enable")

    print("\nSelf-test complete.")
