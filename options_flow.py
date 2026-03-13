"""
options_flow.py — Phase 2B
Institutional options flow and dark pool data via Unusual Whales API.
Surfaces large block trades and unusual call/put activity before price moves.

New .env vars required:
  UNUSUAL_WHALES_API_KEY — $25/mo tier sufficient at unusualwhales.com
                           Store in Vaultwarden → Breadbot → "Unusual Whales API Key"

Telegram command to add: /flow [TICKER]
"""

import logging
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
API_KEY  = os.getenv("UNUSUAL_WHALES_API_KEY", "").strip()
BASE_URL = "https://api.unusualwhales.com"
_REQUEST_TIMEOUT = 10


def _check_config() -> None:
    if not API_KEY:
        raise RuntimeError(
            "UNUSUAL_WHALES_API_KEY is not set in .env. "
            "Retrieve from Vaultwarden → Breadbot → Unusual Whales API Key."
        )


def _headers() -> dict:
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def _get(endpoint: str, params: dict | None = None) -> dict | list:
    _check_config()
    resp = requests.get(f"{BASE_URL}{endpoint}", headers=_headers(),
                        params=params, timeout=_REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"Unusual Whales API error: {data['error']}")
    return data.get("data", data) if isinstance(data, dict) else data


# ── Options flow ───────────────────────────────────────────────────────────────

def get_flow(ticker: str, limit: int = 25) -> list:
    """
    Return recent options flow for a ticker — large premium trades, newest first.

    Returns list of flow dicts. Key fields:
      ticker, strike, expiry, call_or_put, sentiment (bullish/bearish/neutral),
      premium (USD), open_interest, volume, underlying_price, date_expiry, executed_at
    """
    data = _get(f"/api/stock/{ticker.upper()}/flow", params={"limit": limit})
    records = data if isinstance(data, list) else data.get("flow", [])
    bullish = sum(1 for r in records if r.get("sentiment") == "bullish")
    bearish = sum(1 for r in records if r.get("sentiment") == "bearish")
    logger.info("Flow %s: %d records (%d bullish / %d bearish)", ticker, len(records), bullish, bearish)
    return records


def get_dark_pool_data(ticker: str) -> dict:
    """
    Return dark pool (off-exchange block trade) data for a ticker.
    Fields: date, ticker, dp_volume, notional_premium, avg_fill, high_fill, low_fill.
    """
    data = _get(f"/api/darkpool/{ticker.upper()}")
    logger.info("Dark pool %s: %s records", ticker, len(data) if isinstance(data, list) else 1)
    return data


def get_market_flow_summary() -> dict:
    """
    Return today's total options market flow summary.
    Fields: call_premium, put_premium, call_put_ratio.
    """
    data = _get("/api/market/flow/summary")
    logger.info("Market flow: call_premium=%s put_premium=%s cp_ratio=%s",
                data.get("call_premium"), data.get("put_premium"), data.get("call_put_ratio"))
    return data


def format_flow_for_telegram(ticker: str, records: list, max_records: int = 5) -> str:
    """Format options flow records into a Telegram-ready message string."""
    if not records:
        return f"No recent options flow found for {ticker.upper()}."
    lines = [f"Options Flow — {ticker.upper()} (last {min(max_records, len(records))} trades)\n"]
    for r in records[:max_records]:
        icon = "🟢" if r.get("sentiment") == "bullish" else ("🔴" if r.get("sentiment") == "bearish" else "⚪")
        lines.append(
            f"{icon} {r.get('call_or_put', '?').upper()} "
            f"${r.get('strike', '?')} exp {r.get('date_expiry', '?')} "
            f"premium=${int(r.get('premium') or 0):,} vol={r.get('volume', '?')}"
        )
    return "\n".join(lines)


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(f"options_flow self-test | BASE_URL={BASE_URL}")

    if not API_KEY:
        print("UNUSUAL_WHALES_API_KEY not set — add to .env to test")
        print("  Get key at unusualwhales.com → API → $25/mo tier")
    else:
        try:
            flow = get_flow("SPY", limit=10)
            print(f"get_flow SPY OK — {len(flow)} records")
            if flow:
                r = flow[0]
                print(f"  Latest: {r.get('call_or_put','?').upper()} ${r.get('strike','?')} "
                      f"exp={r.get('date_expiry','?')} premium=${int(r.get('premium') or 0):,} "
                      f"sentiment={r.get('sentiment','?')}")
            print("\nTelegram format preview:")
            print(format_flow_for_telegram("SPY", flow))
        except Exception as e:
            print(f"get_flow failed: {e}")

        try:
            dp = get_dark_pool_data("SPY")
            print(f"\nget_dark_pool_data SPY OK — type={type(dp).__name__}")
        except Exception as e:
            print(f"get_dark_pool_data failed: {e}")

        try:
            summary = get_market_flow_summary()
            print(f"\nMarket flow summary OK — "
                  f"call_premium=${int(summary.get('call_premium') or 0):,} "
                  f"put_premium=${int(summary.get('put_premium') or 0):,}")
        except Exception as e:
            print(f"get_market_flow_summary failed: {e}")
