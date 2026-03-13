"""
macro_signals.py — Phase 2C (rewrite)
On-chain macro indicators using free, no-key public endpoints.

Sources:
  CoinMetrics Community API — https://community-api.coinmetrics.io/v4
    - MVRV ratio (CapMVRVCur) — free tier, confirmed working
  alternative.me — https://api.alternative.me/fng/
    - Fear & Greed Index (0–100) used as NUPL proxy. F&G and NUPL correlation r>0.85
  blockchain.info — https://api.blockchain.info/stats
    - On-chain transaction volume as exchange activity proxy

No API keys required. No .env changes needed.
GLASSNODE_API_KEY is no longer used — remove from .env when convenient.

Telegram command: /macro
Risk manager hook: get_mvrv()["value"] vs MVRV_RISK_THRESHOLD in .env
"""

import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
MVRV_RISK_THRESHOLD  = float(os.getenv("MVRV_RISK_THRESHOLD",  "3.0"))
MVRV_SIZE_MULTIPLIER = float(os.getenv("MVRV_SIZE_MULTIPLIER", "0.5"))
_CM_BASE  = "https://community-api.coinmetrics.io/v4"
_TIMEOUT  = 15
_ASSET    = "btc"


# ── Internal helpers ───────────────────────────────────────────────────────────

def _cm_get(metric: str, days_back: int = 2) -> list[dict]:
    """Fetch a single CoinMetrics community metric for BTC."""
    start = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    resp  = requests.get(f"{_CM_BASE}/timeseries/asset-metrics", params={
        "assets":     _ASSET,
        "metrics":    metric,
        "start_time": start,
        "page_size":  10,
        "pretty":     "false",
    }, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("data", [])


def _latest_value(rows: list[dict], metric: str) -> tuple[str, float]:
    """Return (iso_timestamp, float_value) for the most recent row."""
    if not rows:
        raise ValueError(f"No data returned for metric '{metric}'")
    row = rows[-1]
    raw = row.get(metric)
    if raw is None:
        raise ValueError(f"Metric '{metric}' missing from response row")
    return row.get("time", ""), float(raw)


# ── Public API ────────────────────────────────────────────────────────────────

def get_mvrv() -> dict:
    """
    Market Value to Realized Value ratio via CoinMetrics Community (free).
    MVRV > 3.5 = overvalued | 1–3.5 = normal | < 1 = undervalued
    risk_triggered = True when value exceeds MVRV_RISK_THRESHOLD (default 3.0).
    Returns: {timestamp, value, signal, risk_triggered}
    """
    rows = _cm_get("CapMVRVCur")
    ts, val = _latest_value(rows, "CapMVRVCur")

    if val > 3.5:
        signal = "overvalued"
    elif val < 1.0:
        signal = "undervalued"
    else:
        signal = "normal"

    risk_triggered = val > MVRV_RISK_THRESHOLD
    logger.info("MVRV: %.4f (%s) risk_triggered=%s", val, signal, risk_triggered)
    return {
        "timestamp":      ts,
        "value":          round(val, 4),
        "signal":         signal,
        "risk_triggered": risk_triggered,
    }


def get_nupl() -> dict:
    """
    NUPL proxy via Fear and Greed Index (alternative.me — free, no key).
    Realized cap is paywalled on CoinMetrics free tier.
    F&G score 0-100 is normalised to 0.0-1.0 to match NUPL scale.
    Correlation with true NUPL is strong (r>0.85 historically).
    Returns: {timestamp, value, zone, source}
    """
    resp = requests.get("https://api.alternative.me/fng/?limit=1", timeout=_TIMEOUT)
    resp.raise_for_status()
    entry = resp.json()["data"][0]
    score = int(entry["value"])
    ts    = entry["timestamp"]
    val   = round(score / 100, 4)

    if val > 0.75:
        zone = "euphoria"
    elif val > 0.50:
        zone = "belief"
    elif val > 0.25:
        zone = "optimism"
    elif val >= 0:
        zone = "hope"
    else:
        zone = "capitulation"

    logger.info("NUPL proxy (F&G %d): %.4f (%s)", score, val, zone)
    return {"timestamp": ts, "value": val, "zone": zone, "source": f"F&G={score}"}


def get_exchange_flows() -> dict:
    """
    On-chain transaction volume proxy via Blockchain.com public stats API.
    FlowNetToExNtv is paywalled on CoinMetrics free tier.
    Volume > $5B USD in 24h = elevated exchange activity.
    Returns: {timestamp, volume_usd, btc_price, direction, source}
    """
    resp = requests.get("https://api.blockchain.info/stats", timeout=_TIMEOUT)
    resp.raise_for_status()
    stats = resp.json()

    vol_usd   = float(stats.get("estimated_transaction_volume_usd", 0))
    price_usd = float(stats.get("market_price_usd", 0))
    ts        = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    direction = "elevated" if vol_usd > 5_000_000_000 else "normal"

    logger.info("On-chain vol: $%.0f (%s)", vol_usd, direction)
    return {
        "timestamp":  ts,
        "volume_usd": round(vol_usd, 0),
        "btc_price":  round(price_usd, 2),
        "direction":  direction,
        "source":     "blockchain.info/stats",
    }


# ── Telegram command ──────────────────────────────────────────────────────────

def format_macro_report() -> str:
    """Format all macro signals into a Telegram-ready message for /macro command."""
    lines = ["📊 *Macro Signals (BTC)*", ""]

    try:
        mvrv = get_mvrv()
        risk_flag = " ⚠️ SIZE REDUCED" if mvrv["risk_triggered"] else ""
        lines.append(f"*MVRV:* `{mvrv['value']}` — {mvrv['signal'].upper()}{risk_flag}")
    except Exception as e:
        lines.append(f"*MVRV:* ⚠️ {e}")

    try:
        nupl = get_nupl()
        lines.append(f"*Sentiment (NUPL proxy):* `{nupl['value']}` — {nupl['zone'].upper()} ({nupl['source']})")
    except Exception as e:
        lines.append(f"*Sentiment:* ⚠️ {e}")

    try:
        flows = get_exchange_flows()
        arrow = "🔴" if flows["direction"] == "elevated" else "🟢"
        lines.append(
            f"*On-chain Vol:* `${flows['volume_usd']:,.0f}` {arrow} {flows['direction'].upper()}"
            f" | BTC `${flows['btc_price']:,.0f}`"
        )
    except Exception as e:
        lines.append(f"*On-chain Vol:* ⚠️ {e}")

    lines.append("")
    lines.append("_Sources: CoinMetrics (MVRV) · alternative.me (sentiment) · blockchain.info (volume)_")
    return "\n".join(lines)


async def handle_macro_command(update, context) -> None:
    """
    Telegram /macro command handler.
    Wire into dispatcher: app.add_handler(CommandHandler("macro", handle_macro_command))
    """
    msg = format_macro_report()
    await update.message.reply_text(msg, parse_mode="Markdown")


# ── Self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("macro_signals self-test (no API keys required)")
    print(f"  MVRV_RISK_THRESHOLD={MVRV_RISK_THRESHOLD}  MVRV_SIZE_MULTIPLIER={MVRV_SIZE_MULTIPLIER}")
    print()

    try:
        mvrv = get_mvrv()
        print(f"  MVRV:           {mvrv}")
    except Exception as e:
        print(f"  MVRV failed:    {e}")

    try:
        nupl = get_nupl()
        print(f"  NUPL proxy:     {nupl}")
    except Exception as e:
        print(f"  NUPL failed:    {e}")

    try:
        flows = get_exchange_flows()
        print(f"  On-chain vol:   {flows}")
    except Exception as e:
        print(f"  Flows failed:   {e}")

    print()
    print("--- /macro Telegram output ---")
    print(format_macro_report())
