"""
market_signals.py — Phase 2A
Whale Alert large transaction monitor + Alternative.me Fear & Greed Index.
Provides market sentiment signals to the scanner and risk manager.

New .env vars required:
  WHALE_ALERT_API_KEY  — from whale-alert.io (free tier: 1000 req/day, 10/min)
  FEAR_GREED_WEIGHT    — multiplier applied to position sizing in Extreme Fear (default 0.8)

Fear & Greed API requires no key (alternative.me public endpoint).
"""

import os
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
WHALE_ALERT_API_KEY = os.getenv("WHALE_ALERT_API_KEY", "").strip()
FEAR_GREED_WEIGHT   = float(os.getenv("FEAR_GREED_WEIGHT", "0.8"))

WHALE_ALERT_URL     = "https://api.whale-alert.io/v1/transactions"
FEAR_GREED_URL      = "https://api.alternative.me/fng/"

# Minimum USD value of a transaction to be considered a "whale" alert
WHALE_MIN_USD       = 500_000


# ── Fear & Greed Index ────────────────────────────────────────────────────────

def get_fear_greed_index() -> dict:
    """
    Fetch the current Crypto Fear & Greed Index from alternative.me.

    Returns:
        Dict with keys:
            value      (int)  — score 0–100
            label      (str)  — e.g. "Extreme Fear", "Greed", "Neutral"
            timestamp  (str)  — UTC ISO timestamp of the reading
            weight     (float)— FEAR_GREED_WEIGHT if Extreme Fear (<25), else 1.0

    No API key required. Endpoint is public and free.
    """
    resp = requests.get(FEAR_GREED_URL, params={"limit": 1}, timeout=10)
    resp.raise_for_status()
    raw = resp.json().get("data", [{}])[0]

    value = int(raw.get("value", 50))
    label = raw.get("value_classification", "Unknown")
    ts    = datetime.fromtimestamp(
        int(raw.get("timestamp", 0)), tz=timezone.utc
    ).isoformat()

    # Position-size multiplier: reduce sizing in Extreme Fear
    weight = FEAR_GREED_WEIGHT if value < 25 else 1.0

    result = {
        "value":     value,
        "label":     label,
        "timestamp": ts,
        "weight":    weight,
    }
    logger.info("Fear & Greed: %d (%s) — size multiplier %.2f", value, label, weight)
    return result


# ── Whale Alert ───────────────────────────────────────────────────────────────

def get_whale_alerts(
    min_usd: int = WHALE_MIN_USD,
    max_results: int = 20,
    lookback_hours: int = 6,
) -> list[dict]:
    # Whale Alert API (whale-alert.io) now requires a paid subscription as of
    # 2026-03. The free tier has been discontinued. Stubbed to return an empty
    # list so callers degrade gracefully until a paid plan is evaluated.
    return []


def get_token_whale_activity(
    token_symbol: str,
    lookback_hours: int = 6,
) -> dict:
    """
    Check whether a specific token has had significant whale activity recently.
    Used by the scanner to adjust the security score for a token before alerting.

    Args:
        token_symbol:   Token ticker to check, e.g. "SOL", "ETH", "PEPE".
        lookback_hours: How far back to look.

    Returns:
        Dict with:
            found       (bool)  — True if any whale alert found for this token
            count       (int)   — Number of matching transactions
            max_usd     (float) — Largest single transaction in USD
            net_flow    (str)   — "to_exchange", "from_exchange", "wallet_to_wallet", or "mixed"
            score_delta (int)   — Suggested adjustment to security score:
                                  +3  large inflow from wallet to exchange (selling pressure)
                                  -5  large dump to exchange from unknown wallet
                                  +5  large inflow from exchange to wallet (accumulation)
                                  0   mixed or no signal
    """
    try:
        alerts = get_whale_alerts(lookback_hours=lookback_hours)
    except RuntimeError:
        # API key not configured — return neutral
        return {"found": False, "count": 0, "max_usd": 0, "net_flow": "none", "score_delta": 0}

    token_alerts = [a for a in alerts if a["symbol"] == token_symbol.upper()]

    if not token_alerts:
        return {"found": False, "count": 0, "max_usd": 0, "net_flow": "none", "score_delta": 0}

    max_usd  = max(a["amount_usd"] for a in token_alerts)
    to_exch  = sum(1 for a in token_alerts if a["to_owner"] in ("exchange", "Binance", "Coinbase", "Kraken"))
    frm_exch = sum(1 for a in token_alerts if a["from_owner"] in ("exchange", "Binance", "Coinbase", "Kraken"))

    if to_exch > frm_exch:
        net_flow    = "to_exchange"
        score_delta = -5   # whale moving to exchange = likely selling
    elif frm_exch > to_exch:
        net_flow    = "from_exchange"
        score_delta = +5   # exchange to wallet = accumulation signal
    else:
        net_flow    = "mixed"
        score_delta = 0

    return {
        "found":       True,
        "count":       len(token_alerts),
        "max_usd":     max_usd,
        "net_flow":    net_flow,
        "score_delta": score_delta,
    }


# ── Combined sentiment snapshot ───────────────────────────────────────────────

def get_market_sentiment() -> dict:
    """
    Return a combined snapshot for use by the risk manager.

    Returns:
        Dict with fear_greed and a combined size_multiplier.
        Whale data is omitted here (too token-specific for a global snapshot).
    """
    fg = get_fear_greed_index()
    return {
        "fear_greed_value":  fg["value"],
        "fear_greed_label":  fg["label"],
        "size_multiplier":   fg["weight"],
        "timestamp":         fg["timestamp"],
    }


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("market_signals self-test")

    # Fear & Greed — no key needed
    try:
        fg = get_fear_greed_index()
        print(f"Fear & Greed: {fg["value"]} ({fg["label"]}) — multiplier {fg["weight"]:.2f}")
    except Exception as e:
        print(f"Fear & Greed failed: {e}")

    # Whale alerts — requires API key
    if WHALE_ALERT_API_KEY:
        try:
            alerts = get_whale_alerts(max_results=5)
            print(f"Whale alerts: {len(alerts)} recent large transactions")
            for a in alerts[:3]:
                print(f"  {a[symbol]} ${a[amount_usd]:,.0f} | {a[from_owner]} → {a[to_owner]}")
        except Exception as e:
            print(f"Whale alerts failed: {e}")
    else:
        print("WHALE_ALERT_API_KEY not set — skipping whale alert test")
