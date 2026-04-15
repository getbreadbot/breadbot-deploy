#!/usr/bin/env python3
"""
robinhood_connector.py — Robinhood Crypto connector for Breadbot.

Uses the official Robinhood Crypto Trading API with Ed25519 signature auth.
No session-based login, no 2FA — pure API key + signature per request.

Credentials stored in Vaultwarden → Breadbot → Robinhood Crypto API Key.

Interface (same as all Breadbot connectors):
    get_account()           → dict with buying power and account info
    get_crypto_positions()  → list of open crypto positions
    place_crypto_order()    → place market or limit buy/sell
    cancel_order()          → cancel an open order by id
    get_crypto_price()      → current bid/ask for a symbol
    get_open_orders()       → list of open orders

Env vars:
    ROBINHOOD_API_KEY=          from Robinhood API credentials portal
    ROBINHOOD_PRIVATE_SEED=     Ed25519 private key seed (base64)
    ROBINHOOD_ENABLED=false     opt-in, off by default

Auth: Every request is signed with Ed25519. The message is:
    api_key + timestamp + path + method + body
Headers: x-api-key, x-signature (base64), x-timestamp
"""

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import httpx
import nacl.signing
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

ROBINHOOD_ENABLED     = os.getenv("ROBINHOOD_ENABLED", "false").lower() == "true"
ROBINHOOD_API_KEY     = os.getenv("ROBINHOOD_API_KEY", "").strip()
ROBINHOOD_PRIVATE_SEED = os.getenv("ROBINHOOD_PRIVATE_SEED", "").strip()

BASE_URL = "https://trading.robinhood.com"

# Use v2 for fee-tier orders (lower fees at volume), v1 as fallback
ORDER_API_VERSION = "v1"  # switch to "v2" when ready for fee tiers


# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_signing_key() -> Optional[nacl.signing.SigningKey]:
    """Load Ed25519 signing key from base64 seed."""
    if not ROBINHOOD_PRIVATE_SEED:
        return None
    try:
        seed_bytes = base64.b64decode(ROBINHOOD_PRIVATE_SEED)
        return nacl.signing.SigningKey(seed_bytes)
    except Exception as exc:
        log.error("robinhood: failed to load signing key: %s", exc)
        return None


def _sign_request(method: str, path: str, body: str = "") -> dict:
    """Build auth headers for a Robinhood API request."""
    signing_key = _get_signing_key()
    if not signing_key:
        raise RuntimeError("ROBINHOOD_PRIVATE_SEED not configured or invalid")
    timestamp = str(int(time.time()))
    message = f"{ROBINHOOD_API_KEY}{timestamp}{path}{method.upper()}{body}"
    signed = signing_key.sign(message.encode("utf-8"))
    signature_b64 = base64.b64encode(signed.signature).decode("utf-8")
    return {
        "x-api-key": ROBINHOOD_API_KEY,
        "x-timestamp": timestamp,
        "x-signature": signature_b64,
        "Content-Type": "application/json; charset=utf-8",
    }


def _request(method: str, path: str, body: dict = None, timeout: float = 15.0) -> dict:
    """Make a signed request to the Robinhood API. Returns parsed JSON or error dict."""
    if not ROBINHOOD_API_KEY:
        return {"error": "ROBINHOOD_API_KEY not set"}
    body_str = json.dumps(body) if body else ""
    headers = _sign_request(method, path, body_str)
    url = BASE_URL + path
    try:
        with httpx.Client(timeout=timeout) as client:
            if method.upper() == "GET":
                resp = client.get(url, headers=headers)
            elif method.upper() == "POST":
                resp = client.post(url, headers=headers, content=body_str)
            elif method.upper() == "DELETE":
                resp = client.delete(url, headers=headers)
            else:
                return {"error": f"Unsupported method: {method}"}
            if resp.status_code in (200, 201):
                return resp.json() if resp.content else {}
            else:
                log.warning("robinhood API %s %s → %d: %s",
                            method, path, resp.status_code, resp.text[:300])
                return {"error": f"HTTP {resp.status_code}", "detail": resp.text[:500]}
    except Exception as exc:
        log.error("robinhood API error: %s %s → %s", method, path, exc)
        return {"error": str(exc)}


# ── Public interface ──────────────────────────────────────────────────────────

def get_account() -> dict:
    """Return account info including buying power."""
    if not ROBINHOOD_ENABLED:
        return {"status": "disabled"}
    try:
        data = _request("GET", "/api/v1/crypto/trading/accounts/")
        if "error" in data:
            return {"status": "error", "message": data["error"]}
        account = data
        if "results" in data and data["results"]:
            account = data["results"][0]
        return {
            "status":             "ok",
            "account_number":     account.get("account_number", ""),
            "buying_power":       float(account.get("buying_power", 0)),
            "buying_power_currency": account.get("buying_power_currency", "USD"),
            "status_raw":         account.get("status", ""),
        }
    except Exception as exc:
        log.error("robinhood get_account error: %s", exc)
        return {"status": "error", "message": str(exc)}


def get_crypto_positions() -> list[dict]:
    """Return list of current crypto holdings."""
    if not ROBINHOOD_ENABLED:
        return []
    try:
        data = _request("GET", "/api/v1/crypto/trading/holdings/")
        if "error" in data:
            log.warning("robinhood get_crypto_positions: %s", data["error"])
            return []
        results = data.get("results", [])
        positions = []
        for h in results:
            qty = float(h.get("total_quantity", 0))
            if qty <= 0:
                continue
            positions.append({
                "symbol":         h.get("asset_code", "UNKNOWN"),
                "quantity":       qty,
                "available":      float(h.get("quantity_available_for_trading", 0)),
                "account_number": h.get("account_number", ""),
            })
        return positions
    except Exception as exc:
        log.error("robinhood get_crypto_positions error: %s", exc)
        return []


def get_crypto_price(symbol: str) -> dict:
    """Return current bid/ask for a crypto symbol (e.g. 'BTC')."""
    if not ROBINHOOD_ENABLED:
        return {}
    try:
        pair = f"{symbol.upper()}-USD"
        data = _request("GET", f"/api/v1/crypto/marketdata/best_bid_ask/?symbol={pair}")
        if "error" in data:
            return {}
        results = data.get("results", [])
        if not results:
            return {}
        quote = results[0]
        bid = float(quote.get("bid_inclusive_of_sell_spread", 0) or quote.get("price", 0))
        ask = float(quote.get("ask_inclusive_of_buy_spread", 0) or bid)
        return {
            "symbol":     symbol.upper(),
            "bid_price":  bid,
            "ask_price":  ask,
            "mark_price": (bid + ask) / 2 if bid and ask else 0,
        }
    except Exception as exc:
        log.error("robinhood get_crypto_price(%s) error: %s", symbol, exc)
        return {}


def place_crypto_order(
    symbol: str,
    side: str,
    amount_usd: float,
    order_type: str = "market",
    limit_price: Optional[float] = None,
) -> dict:
    """Place a crypto order via the official Robinhood API."""
    if not ROBINHOOD_ENABLED:
        return {"status": "disabled"}
    if side not in ("buy", "sell"):
        return {"status": "error", "message": f"Invalid side: {side}"}
    if amount_usd <= 0:
        return {"status": "error", "message": "amount_usd must be positive"}
    pair = f"{symbol.upper()}-USD"
    path = f"/api/{ORDER_API_VERSION}/crypto/trading/orders/"
    order_body = {"symbol": pair, "side": side, "type": order_type}
    if order_type == "market":
        order_body["market_order_config"] = {"asset_quantity": str(amount_usd)}
    elif order_type == "limit":
        if not limit_price:
            return {"status": "error", "message": "limit_price required for limit orders"}
        qty = amount_usd / limit_price
        order_body["limit_order_config"] = {
            "asset_quantity": str(qty),
            "limit_price": str(limit_price),
        }
    else:
        return {"status": "error", "message": f"Unknown order_type: {order_type}"}
    try:
        data = _request("POST", path, body=order_body)
        if "error" in data:
            return {"status": "error", "message": data.get("detail", data["error"])}
        order_id = data.get("id", "")
        log.info("robinhood: %s %s $%.2f order placed — id=%s state=%s",
                 side, symbol, amount_usd, order_id, data.get("state", "?"))
        return {
            "status": "ok", "order_id": order_id, "side": side,
            "symbol": symbol.upper(), "amount_usd": amount_usd,
            "state": data.get("state", "unknown"), "type": order_type,
        }
    except Exception as exc:
        log.error("robinhood place_crypto_order error: %s", exc)
        return {"status": "error", "message": str(exc)}


def cancel_order(order_id: str) -> dict:
    """Cancel an open order by ID."""
    if not ROBINHOOD_ENABLED:
        return {"status": "disabled"}
    try:
        data = _request("POST", f"/api/v1/crypto/trading/orders/{order_id}/cancel/")
        if "error" in data:
            return {"status": "error", "message": data["error"]}
        log.info("robinhood: cancelled order %s", order_id)
        return {"status": "ok", "order_id": order_id}
    except Exception as exc:
        log.error("robinhood cancel_order(%s) error: %s", order_id, exc)
        return {"status": "error", "message": str(exc)}


def get_open_orders() -> list[dict]:
    """Return list of open crypto orders."""
    if not ROBINHOOD_ENABLED:
        return []
    try:
        data = _request("GET", "/api/v1/crypto/trading/orders/")
        if "error" in data:
            return []
        results = data.get("results", [])
        return [
            {
                "id": o.get("id"),
                "symbol": o.get("symbol", "").replace("-USD", ""),
                "side": o.get("side"), "type": o.get("type"),
                "state": o.get("state"), "created_at": o.get("created_at"),
            }
            for o in results
            if o.get("state") in ("queued", "confirmed", "partially_filled", "pending")
        ]
    except Exception as exc:
        log.error("robinhood get_open_orders error: %s", exc)
        return []


# ── Telegram command handler ──────────────────────────────────────────────────

async def handle_robinhood_command(client) -> None:
    """Handle /robinhood — show Robinhood account status and crypto positions."""
    from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

    async def _send(text):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        try:
            await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
            }, timeout=10)
        except Exception as exc:
            log.warning("Telegram send failed: %s", exc)

    if not ROBINHOOD_ENABLED:
        await _send("Robinhood is disabled. Set ROBINHOOD_ENABLED=true to activate.")
        return
    account = get_account()
    positions = get_crypto_positions()
    lines = ["<b>Robinhood Crypto</b>\n"]
    if account.get("status") == "ok":
        bp = float(account.get("buying_power", 0))
        lines.append(f"Buying power: ${bp:,.2f}")
        lines.append(f"Account: {account.get('account_number', 'N/A')}")
    else:
        lines.append(f"Account: {account.get('message', 'error')}")
    if positions:
        lines.append(f"\nHoldings ({len(positions)}):")
        for p in positions:
            lines.append(f"  {p.get('symbol', '?')}: {p.get('quantity', 0):.6f}")
    else:
        lines.append("\nNo crypto holdings.")
    await _send("\n".join(lines))
