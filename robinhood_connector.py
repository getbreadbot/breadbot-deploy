#!/usr/bin/env python3
"""
robinhood_connector.py — Robinhood Crypto connector for Breadbot.

Uses the community robin_stocks library for session-based auth.
Credentials stored in Vaultwarden → Breadbot → Robinhood.

Mirrors the interface of existing connectors:
    get_account()           → dict with portfolio value and buying power
    get_crypto_positions()  → list of open crypto positions
    place_crypto_order()    → place market or limit buy/sell
    cancel_order()          → cancel an open order by id
    get_crypto_price()      → current bid/ask/mark price for a symbol

New .env vars:
    ROBINHOOD_USERNAME=     from Vaultwarden
    ROBINHOOD_PASSWORD=     from Vaultwarden
    ROBINHOOD_ENABLED=false opt-in, off by default

Note on 2FA: Robinhood requires 2FA on first login. robin_stocks handles
this interactively the first time. After that, the session is cached in
~/.tokens/ and subsequent calls are automatic. On Railway, a manual
first-auth step is required before the bot can trade — document this
clearly in the buyer onboarding flow.
"""

import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

ROBINHOOD_ENABLED  = os.getenv("ROBINHOOD_ENABLED",  "false").lower() == "true"
ROBINHOOD_USERNAME = os.getenv("ROBINHOOD_USERNAME", "").strip()
ROBINHOOD_PASSWORD = os.getenv("ROBINHOOD_PASSWORD", "").strip()

# Module-level login state — only authenticate once per process lifetime
_logged_in = False


def _ensure_login() -> bool:
    """Authenticate with Robinhood if not already logged in. Returns True on success."""
    global _logged_in
    if _logged_in:
        return True
    if not ROBINHOOD_USERNAME or not ROBINHOOD_PASSWORD:
        log.error("robinhood: ROBINHOOD_USERNAME / ROBINHOOD_PASSWORD not set")
        return False
    try:
        import robin_stocks.robinhood as r
        r.login(
            username=ROBINHOOD_USERNAME,
            password=ROBINHOOD_PASSWORD,
            expiresIn=86400,       # 24-hour session
            store_session=True,    # cache to ~/.tokens/ so restarts don't re-auth
            mfa_code=None,         # uses cached session after first login
        )
        _logged_in = True
        log.info("robinhood: logged in as %s", ROBINHOOD_USERNAME)
        return True
    except ImportError:
        log.error("robinhood: robin_stocks not installed — pip install robin_stocks")
        return False
    except Exception as exc:
        log.error("robinhood: login failed: %s", exc)
        return False

def get_account() -> dict:
    """Return portfolio value and buying power. Returns empty dict if disabled or error."""
    if not ROBINHOOD_ENABLED:
        return {"status": "disabled"}
    if not _ensure_login():
        return {"status": "auth_failed"}
    try:
        import robin_stocks.robinhood as r
        profile   = r.load_portfolio_profile()
        buying    = r.load_account_profile()
        return {
            "status":             "ok",
            "portfolio_value":    float(profile.get("equity") or 0),
            "extended_hours_equity": float(profile.get("extended_hours_equity") or 0),
            "buying_power":       float(buying.get("buying_power") or 0),
            "crypto_buying_power": float(buying.get("crypto_buying_power") or 0),
            "currency":           "USD",
        }
    except Exception as exc:
        log.error("robinhood get_account error: %s", exc)
        return {"status": "error", "message": str(exc)}


def get_crypto_positions() -> list[dict]:
    """Return list of current crypto positions with quantity and average cost."""
    if not ROBINHOOD_ENABLED:
        return []
    if not _ensure_login():
        return []
    try:
        import robin_stocks.robinhood as r
        raw = r.get_crypto_positions()
        positions = []
        for pos in (raw or []):
            qty = float(pos.get("quantity") or 0)
            if qty <= 0:
                continue
            positions.append({
                "symbol":        pos.get("currency", {}).get("code", "UNKNOWN"),
                "quantity":      qty,
                "avg_cost":      float(pos.get("average_buy_price") or 0),
                "cost_basis":    float(pos.get("cost_bases", [{}])[0].get("direct_cost_basis") or 0),
                "id":            pos.get("id"),
            })
        return positions
    except Exception as exc:
        log.error("robinhood get_crypto_positions error: %s", exc)
        return []


def get_crypto_price(symbol: str) -> dict:
    """
    Return current bid, ask, and mark price for a crypto symbol (e.g. 'BTC', 'ETH').
    Returns empty dict on error.
    """
    if not ROBINHOOD_ENABLED:
        return {}
    if not _ensure_login():
        return {}
    try:
        import robin_stocks.robinhood as r
        quote = r.get_crypto_quote(symbol)
        return {
            "symbol":     symbol.upper(),
            "bid_price":  float(quote.get("bid_price") or 0),
            "ask_price":  float(quote.get("ask_price") or 0),
            "mark_price": float(quote.get("mark_price") or 0),
        }
    except Exception as exc:
        log.error("robinhood get_crypto_price(%s) error: %s", symbol, exc)
        return {}

def place_crypto_order(
    symbol: str,
    side: str,           # "buy" or "sell"
    amount_usd: float,   # dollar amount (Robinhood crypto uses dollar amounts)
    order_type: str = "market",
    limit_price: Optional[float] = None,
) -> dict:
    """
    Place a crypto buy or sell order on Robinhood.

    Args:
        symbol:      Token symbol, e.g. "BTC", "ETH", "DOGE"
        side:        "buy" or "sell"
        amount_usd:  Dollar amount to buy/sell
        order_type:  "market" (default) or "limit"
        limit_price: Required if order_type == "limit"

    Returns:
        dict with order details or {"status": "error", "message": ...}
    """
    if not ROBINHOOD_ENABLED:
        log.info("robinhood: order skipped — connector disabled")
        return {"status": "disabled"}
    if not _ensure_login():
        return {"status": "auth_failed"}
    if side not in ("buy", "sell"):
        return {"status": "error", "message": f"Invalid side: {side}"}
    if amount_usd <= 0:
        return {"status": "error", "message": "amount_usd must be positive"}

    try:
        import robin_stocks.robinhood as r

        if order_type == "market":
            if side == "buy":
                result = r.order_buy_crypto_by_price(
                    symbol=symbol.upper(),
                    amountInDollars=amount_usd,
                    timeInForce="gtc",
                )
            else:
                result = r.order_sell_crypto_by_price(
                    symbol=symbol.upper(),
                    amountInDollars=amount_usd,
                    timeInForce="gtc",
                )
        elif order_type == "limit":
            if not limit_price:
                return {"status": "error", "message": "limit_price required for limit orders"}
            if side == "buy":
                result = r.order_buy_crypto_limit_by_price(
                    symbol=symbol.upper(),
                    amountInDollars=amount_usd,
                    limitPrice=limit_price,
                    timeInForce="gtc",
                )
            else:
                result = r.order_sell_crypto_limit_by_price(
                    symbol=symbol.upper(),
                    amountInDollars=amount_usd,
                    limitPrice=limit_price,
                    timeInForce="gtc",
                )
        else:
            return {"status": "error", "message": f"Unknown order_type: {order_type}"}

        if not result or result.get("non_field_errors"):
            err = result.get("non_field_errors", ["Unknown error"]) if result else ["No response"]
            return {"status": "error", "message": str(err)}

        order_id = result.get("id", "")
        log.info(
            "robinhood: %s %s $%.2f order placed — id=%s state=%s",
            side, symbol, amount_usd, order_id, result.get("state", "?")
        )
        return {
            "status":     "ok",
            "order_id":   order_id,
            "side":       side,
            "symbol":     symbol.upper(),
            "amount_usd": amount_usd,
            "state":      result.get("state", "unknown"),
            "type":       order_type,
        }
    except Exception as exc:
        log.error("robinhood place_crypto_order error: %s", exc)
        return {"status": "error", "message": str(exc)}


def cancel_order(order_id: str) -> dict:
    """Cancel an open Robinhood crypto order by ID."""
    if not ROBINHOOD_ENABLED:
        return {"status": "disabled"}
    if not _ensure_login():
        return {"status": "auth_failed"}
    try:
        import robin_stocks.robinhood as r
        result = r.cancel_crypto_order(order_id)
        log.info("robinhood: cancelled order %s", order_id)
        return {"status": "ok", "order_id": order_id, "result": result}
    except Exception as exc:
        log.error("robinhood cancel_order(%s) error: %s", order_id, exc)
        return {"status": "error", "message": str(exc)}


def get_open_orders() -> list[dict]:
    """Return list of all open crypto orders."""
    if not ROBINHOOD_ENABLED:
        return []
    if not _ensure_login():
        return []
    try:
        import robin_stocks.robinhood as r
        orders = r.get_all_open_crypto_orders() or []
        return [
            {
                "id":         o.get("id"),
                "symbol":     o.get("currency_pair_id", "").split("-")[0],
                "side":       o.get("side"),
                "type":       o.get("type"),
                "amount_usd": float(o.get("rounded_executed_notional") or o.get("quantity") or 0),
                "state":      o.get("state"),
                "created_at": o.get("created_at"),
            }
            for o in orders
        ]
    except Exception as exc:
        log.error("robinhood get_open_orders error: %s", exc)
        return []
