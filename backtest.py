#!/usr/bin/env python3
"""
backtest.py — Breadbot Alert Backtester

Replays meme_alerts from the local database against historical price data
fetched from DEXScreener. Simulates a fixed exit strategy and reports
per-trade and aggregate PnL.

Exit rules (configurable via CLI):
  Stop loss:   -12%  (configurable via --stop-loss)  (exit immediately when hit)
  Take profit: +50%  (sell 50% of position at TP1, hold rest)
               +75%  (sell remainder at TP2)
  Max hold:    6 hours  (configurable via --max-hold)

Usage:
  python3 backtest.py                            # only actual buy decisions, last 30d
  python3 backtest.py --mode all --min-score 70  # simulate every alert >= score 70
  python3 backtest.py --days 7 --stop-loss 0.15  # last 7 days, tighter stop
  python3 backtest.py --mode all --json          # JSON output for panel integration
"""

import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone, timedelta
# time is still used for throttle sleep
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

DB_PATH = Path(__file__).parent / "data" / "cryptobot.db"

GECKO_BASE    = "https://api.geckoterminal.com/api/v2"
GECKO_HEADERS = {"Accept": "application/json;version=20230302"}
DEXSCREENER   = "https://api.dexscreener.com/latest/dex/tokens/{addr}"

# Network slug mapping for GeckoTerminal
_GECKO_NETWORK = {"solana": "solana", "base": "base"}

# In-process cache: token_addr -> pool_addr (avoids repeat lookups within a run)
_pool_cache: dict = {}


# ── Price fetching ─────────────────────────────────────────────────────────────

def _gecko_network(chain: str) -> str:
    return _GECKO_NETWORK.get(chain.lower(), chain.lower())


def _get_pool_address(chain: str, token_addr: str) -> str:
    """
    Look up the top pool address for a token via GeckoTerminal.
    Returns empty string on failure. Retries once on 429.
    """
    key = f"{chain}:{token_addr}"
    if key in _pool_cache:
        return _pool_cache[key]
    network = _gecko_network(chain)
    for attempt in range(4):
        try:
            with httpx.Client(timeout=12, headers=GECKO_HEADERS) as c:
                r = c.get(f"{GECKO_BASE}/networks/{network}/tokens/{token_addr}/pools?page=1")
                if r.status_code == 429:
                    wait = 15 * (attempt + 1)
                    print(f"    [rate-limit] pool 429 — sleeping {wait}s (attempt {attempt+1})")
                    time.sleep(wait)
                    continue
                if r.status_code != 200:
                    print(f"    [warn] pool HTTP {r.status_code} for {token_addr[:20]}...")
                    return ""
                pools = r.json().get("data") or []
                addr  = pools[0]["attributes"]["address"] if pools else ""
                _pool_cache[key] = addr
                return addr
        except Exception as exc:
            print(f"    [error] pool exception: {exc}")
            return ""
    print(f"    [fail] pool lookup exhausted retries for {token_addr[:20]}...")
    return ""


_max_hold_h = 48  # overridden by run_backtest()

def fetch_candles(chain: str, token_addr: str, entry_ts: float) -> list:
    """
    Fetch 15-min OHLCV candles from GeckoTerminal covering entry_ts through +max_hold_h.
    Bar format from API: [timestamp_sec, open, high, low, close, volume] — descending order.
    Returns list of {"t","o","h","l","c"} dicts sorted ascending. Returns [] on failure.
    Retries once on 429 (rate limit) with a 5s back-off.
    """
    pool_addr = _get_pool_address(chain, token_addr)
    if not pool_addr:
        return []

    # Brief pause between pool lookup and ohlcv call — GeckoTerminal's free tier
    # throttles when two calls arrive in quick succession from the same IP.
    time.sleep(1.0)

    network = _gecko_network(chain)
    url = (
        f"{GECKO_BASE}/networks/{network}/pools/{pool_addr}"
        f"/ohlcv/minute?aggregate=15&limit=200"
    )
    # Pause between pool-lookup and candle fetch — both hit GeckoTerminal
    # and back-to-back requests across many tokens trips the rate limiter.
    time.sleep(1.5)
    # Brief pause between pool lookup (above) and the ohlcv call to respect rate limits
    time.sleep(2.0)

    for attempt in range(4):
        try:
            with httpx.Client(timeout=15, headers=GECKO_HEADERS) as c:
                r = c.get(url)
                if r.status_code == 429:
                    wait = 15 * (attempt + 1)
                    print(f"    [rate-limit] ohlcv 429 — sleeping {wait}s (attempt {attempt+1})")
                    time.sleep(wait)
                    continue
                if r.status_code != 200:
                    print(f"    [warn] ohlcv HTTP {r.status_code}")
                    return []
                bars = r.json()["data"]["attributes"]["ohlcv_list"]
                cutoff = entry_ts + _max_hold_h * 3600
                result = []
                for b in bars:
                    # [ts_sec, open, high, low, close, volume]
                    t = float(b[0])
                    if entry_ts <= t <= cutoff:
                        result.append({"t": t, "o": b[1], "h": b[2], "l": b[3], "c": b[4]})
                return sorted(result, key=lambda x: x["t"])
        except Exception as exc:
            print(f"    [warn] candle fetch exception: {exc}")
            return []
    return []


def fetch_price_now(chain: str, token_addr: str) -> float:
    """Current price via DEXScreener token endpoint. Returns 0.0 on failure."""
    try:
        with httpx.Client(timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = c.get(DEXSCREENER.format(addr=token_addr))
            if r.status_code != 200:
                return 0.0
            pairs = r.json().get("pairs") or []
            return float(pairs[0].get("priceUsd", 0) or 0) if pairs else 0.0
    except Exception as exc:
        print(f"    [warn] price fetch exception: {exc}")
        return 0.0


# ── Trade simulation ──────────────────────────────────────────────────────────

def simulate_trade(
    entry_price:   float,
    candles:       list,
    position_usd:  float,
    stop_loss_pct: float = 0.20,
    tp1_pct:       float = 0.50,
    tp2_pct:       float = 1.00,
    current_price: float = 0.0,
) -> dict:
    """
    Simulate a trade using candle data.

    Conservative simulation: within each candle checks low before high so stop
    losses are triggered before take profits — avoids overstating gains.

    Returns dict with keys: outcome, pnl_usd, pnl_pct, exit_price, bars_held.
    Outcomes: stop_loss | tp1 | tp2 | tp1_partial | expired | holding | no_data
    """
    if not candles:
        if current_price and current_price > 0:
            pnl_pct = (current_price - entry_price) / entry_price
            # Apply stop loss even without candle data
            if pnl_pct <= -stop_loss_pct:
                return {
                    "outcome":    "stop_loss",
                    "pnl_usd":    round(position_usd * (-stop_loss_pct), 2),
                    "pnl_pct":    round(-stop_loss_pct * 100, 1),
                    "exit_price": round(entry_price * (1 - stop_loss_pct), 8),
                    "bars_held":  0,
                }
            # Check TP levels
            if pnl_pct >= tp2_pct:
                mult = 0.5 * tp1_pct + 0.5 * tp2_pct
                return {
                    "outcome":    "tp2",
                    "pnl_usd":    round(position_usd * mult, 2),
                    "pnl_pct":    round(mult * 100, 1),
                    "exit_price": round(entry_price * (1 + tp2_pct), 8),
                    "bars_held":  0,
                }
            if pnl_pct >= tp1_pct:
                mult = 0.5 * tp1_pct
                return {
                    "outcome":    "tp1_partial",
                    "pnl_usd":    round(position_usd * mult, 2),
                    "pnl_pct":    round(mult * 100, 1),
                    "exit_price": current_price,
                    "bars_held":  0,
                }
            return {
                "outcome":    "holding",
                "pnl_usd":    round(position_usd * pnl_pct, 2),
                "pnl_pct":    round(pnl_pct * 100, 1),
                "exit_price": current_price,
                "bars_held":  0,
            }
        return {"outcome": "no_data", "pnl_usd": 0.0, "pnl_pct": 0.0,
                "exit_price": 0.0, "bars_held": 0}

    sl_price  = entry_price * (1 - stop_loss_pct)
    tp1_price = entry_price * (1 + tp1_pct)
    tp2_price = entry_price * (1 + tp2_pct)

    remaining = position_usd
    realized  = 0.0
    tp1_hit   = False

    for i, bar in enumerate(candles):
        lo, hi = bar["l"], bar["h"]

        if not tp1_hit:
            if lo <= sl_price:
                loss = remaining * (-stop_loss_pct)
                return {
                    "outcome":    "stop_loss",
                    "pnl_usd":    round(realized + loss, 2),
                    "pnl_pct":    round(((realized + loss) / position_usd) * 100, 1),
                    "exit_price": round(sl_price, 8),
                    "bars_held":  i + 1,
                }
            if hi >= tp1_price:
                half      = remaining / 2
                realized += half * tp1_pct
                remaining -= half
                tp1_hit   = True
        else:
            if lo <= sl_price:
                loss = remaining * (-stop_loss_pct)
                return {
                    "outcome":    "tp1_partial",
                    "pnl_usd":    round(realized + loss, 2),
                    "pnl_pct":    round(((realized + loss) / position_usd) * 100, 1),
                    "exit_price": round(sl_price, 8),
                    "bars_held":  i + 1,
                }
            if hi >= tp2_price:
                gain = remaining * tp2_pct
                realized += gain
                return {
                    "outcome":    "tp2",
                    "pnl_usd":    round(realized, 2),
                    "pnl_pct":    round((realized / position_usd) * 100, 1),
                    "exit_price": round(tp2_price, 8),
                    "bars_held":  i + 1,
                }

    # 48h elapsed — exit at last close
    last_close = candles[-1]["c"] if candles else entry_price
    pnl_pct    = (last_close - entry_price) / entry_price
    outcome    = "tp1" if tp1_hit else "expired"
    return {
        "outcome":    outcome,
        "pnl_usd":    round(realized + remaining * pnl_pct, 2),
        "pnl_pct":    round(((realized + remaining * pnl_pct) / position_usd) * 100, 1),
        "exit_price": round(last_close, 8),
        "bars_held":  len(candles),
    }


# ── Helpers ────────────────────────────────────────────────────────────────────

def calc_position(score: int, portfolio_usd: float = 5000.0, max_pct: float = 0.01) -> float:
    """Mirror auto_executor score-weighted sizing capped at MAX_POSITION_SIZE_PCT."""
    score_factor = 0.5 + 0.5 * (score / 100)
    return round(min(portfolio_usd * max_pct * score_factor, portfolio_usd * max_pct), 2)


def load_alerts(mode: str, min_score: int, days: int) -> list:
    conn   = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    if mode == "actual":
        rows = conn.execute(
            "SELECT * FROM meme_alerts WHERE decision IN ('buy','auto_buy') "
            "AND created_at >= ? ORDER BY created_at",
            (cutoff,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM meme_alerts WHERE rug_score >= ? "
            "AND created_at >= ? ORDER BY created_at",
            (min_score, cutoff),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Main ───────────────────────────────────────────────────────────────────────

def run_backtest(
    mode:          str   = "actual",
    min_score:     int   = 70,
    days:          int   = 30,
    stop_loss_pct: float = 0.12,
    tp1_pct:       float = 0.50,
    tp2_pct:       float = 0.75,
    portfolio_usd: float = 5000.0,
    as_json:       bool  = False,
    throttle_ms:   int   = 1200,
    max_hold_hours: int  = 6,
    max_h1_pump:   float = 150.0,
) -> dict:

    global _max_hold_h
    _max_hold_h = max_hold_hours

    alerts = load_alerts(mode, min_score, days)
    if not alerts:
        result = {"error": "No alerts found matching filters"}
        if as_json:
            print(json.dumps(result))
        return result

    trades         = []
    total_pnl      = 0.0
    wins           = 0
    losses         = 0
    no_data        = 0
    outcome_counts = {}

    for i, alert in enumerate(alerts):
        symbol     = alert.get("symbol", "?")
        chain      = alert.get("chain", "solana")
        token_addr = alert.get("token_addr", "")
        entry_px   = float(alert.get("price_usd") or 0)
        score      = int(alert.get("rug_score") or 0)
        created_at = alert.get("created_at", "")

        if not token_addr or not entry_px:
            continue

        try:
            entry_dt = datetime.fromisoformat(created_at.replace("Z", ""))
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.replace(tzinfo=timezone.utc)
            entry_ts = entry_dt.timestamp()
        except Exception:
            continue

        # Filter out late entries by h1 pump flag
        rug_flags_str = alert.get("rug_flags", "") or ""
        import re as _re
        _pump_match = _re.search(r'Already pumped \+(\d+)%', rug_flags_str)
        if _pump_match and float(_pump_match.group(1)) > max_h1_pump:
            no_data += 1
            outcome_counts["filtered_h1_pump"] = outcome_counts.get("filtered_h1_pump", 0) + 1
            continue

        position_usd = calc_position(score, portfolio_usd)

        if i > 0:
            time.sleep(throttle_ms / 1000)

        candles       = fetch_candles(chain, token_addr, entry_ts)
        current_price = fetch_price_now(chain, token_addr) if not candles else 0.0

        result = simulate_trade(
            entry_price=entry_px,
            candles=candles,
            position_usd=position_usd,
            stop_loss_pct=stop_loss_pct,
            tp1_pct=tp1_pct,
            tp2_pct=tp2_pct,
            current_price=current_price,
        )

        outcome = result["outcome"]
        pnl     = result["pnl_usd"]

        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
        total_pnl += pnl

        if outcome == "no_data":
            no_data += 1
        elif pnl > 0:
            wins += 1
        else:
            losses += 1

        trade_record = {
            "symbol":       symbol,
            "chain":        chain,
            "token_addr":   token_addr,
            "score":        score,
            "entry_price":  entry_px,
            "position_usd": position_usd,
            "created_at":   created_at,
            "decision":     alert.get("decision", "simulated"),
            **result,
        }
        trades.append(trade_record)

        if not as_json:
            pnl_str = "+${:.2f}".format(pnl) if pnl >= 0 else "-${:.2f}".format(abs(pnl))
            print("  [{:>3}/{}] {:<10} {:<8} score={} pos=${:.0f}  {:<12} {}".format(
                i + 1, len(alerts), symbol, chain, score,
                position_usd, outcome, pnl_str))

    resolved = wins + losses
    win_rate = round((wins / resolved * 100) if resolved > 0 else 0, 1)
    avg_win  = round(sum(t["pnl_usd"] for t in trades if t["pnl_usd"] > 0) / max(wins, 1), 2)
    avg_loss = round(sum(t["pnl_usd"] for t in trades if t["pnl_usd"] < 0) / max(losses, 1), 2)

    summary = {
        "mode":           mode,
        "min_score":      min_score,
        "days":           days,
        "stop_loss_pct":  stop_loss_pct,
        "max_hold_hours": max_hold_hours,
        "max_h1_pump":    max_h1_pump,
        "tp1_pct":        tp1_pct,
        "tp2_pct":        tp2_pct,
        "portfolio_usd":  portfolio_usd,
        "total_alerts":   len(alerts),
        "total_trades":   len(trades),
        "wins":           wins,
        "losses":         losses,
        "no_data":        no_data,
        "win_rate_pct":   win_rate,
        "total_pnl_usd":  round(total_pnl, 2),
        "avg_win_usd":    avg_win,
        "avg_loss_usd":   avg_loss,
        "outcome_counts": outcome_counts,
        "trades":         trades,
    }

    if as_json:
        print(json.dumps(summary))
    else:
        print("\n" + "=" * 60)
        print("  BACKTEST SUMMARY")
        print("  Mode: {}  |  Score >= {}  |  Last {}d".format(mode, min_score, days))
        print("  Alerts: {}  |  Trades simulated: {}".format(len(alerts), len(trades)))
        print("  Win rate: {}%  ({} W / {} L / {} no-data)".format(win_rate, wins, losses, no_data))
        print("  Total PnL:  ${:+.2f}".format(total_pnl))
        print("  Avg win:   ${:+.2f}    Avg loss: ${:+.2f}".format(avg_win, avg_loss))
        print("  Outcomes:  {}".format(outcome_counts))
        print("=" * 60)

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Breadbot alert backtester")
    parser.add_argument("--mode",       default="actual",
                        choices=["actual", "all"],
                        help="actual=buy decisions only, all=simulate every alert above min-score")
    parser.add_argument("--min-score",  type=int,   default=70)
    parser.add_argument("--days",       type=int,   default=30)
    parser.add_argument("--stop-loss",  type=float, default=0.12)
    parser.add_argument("--tp1",        type=float, default=0.50)
    parser.add_argument("--tp2",        type=float, default=0.75)
    parser.add_argument("--portfolio",  type=float, default=5000.0)
    parser.add_argument("--json",       action="store_true",
                        help="Machine-readable JSON output for panel integration")
    parser.add_argument("--max-hold",   type=int,   default=6,
                        help="Max hold time in hours (default 6)")
    parser.add_argument("--max-h1-pump", type=float, default=150.0,
                        help="Skip alerts where h1 pump exceeds this %% (default 150)")
    parser.add_argument("--throttle",   type=int,   default=2000,
                        help="Milliseconds between tokens (default 2000 — GeckoTerminal free tier)")
    args = parser.parse_args()

    run_backtest(
        mode=args.mode,
        min_score=args.min_score,
        days=args.days,
        stop_loss_pct=args.stop_loss,
        max_hold_hours=args.max_hold,
        max_h1_pump=args.max_h1_pump,
        tp1_pct=args.tp1,
        tp2_pct=args.tp2,
        portfolio_usd=args.portfolio,
        as_json=args.json,
        throttle_ms=args.throttle,
    )
