#!/usr/bin/env python3
"""Breadbot self-serve status report (read-only).

Run anytime to see how the bot is doing without waiting on a session:
    ssh vps '/opt/projects/breadbot/venv/bin/python3 /opt/projects/breadbot/weekly_report.py'

Read-only connection, no writes. Safe to run as often as you like.
"""
import sqlite3
import math
from pathlib import Path

DB = "/opt/projects/breadbot/data/cryptobot.db"


def ro():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def rule(title):
    print("\n" + "=" * 62)
    print("  " + title)
    print("=" * 62)


def weekly_performance(c):
    rule("PERFORMANCE BY WEEK (closed trades)")
    rows = c.execute(
        """SELECT strftime('%Y-W%W', opened_at) wk,
                  COUNT(*) n,
                  SUM(CASE WHEN realized_pnl_usd > 0 THEN 1 ELSE 0 END) w,
                  ROUND(SUM(realized_pnl_usd), 2) pnl,
                  ROUND(AVG(CASE WHEN realized_pnl_usd > 0
                        THEN 100.0*realized_pnl_usd/NULLIF(cost_basis_usd,0) END), 0) aw,
                  ROUND(AVG(CASE WHEN realized_pnl_usd <= 0
                        THEN 100.0*realized_pnl_usd/NULLIF(cost_basis_usd,0) END), 0) al
           FROM positions
           WHERE status='closed' AND realized_pnl_usd IS NOT NULL
           GROUP BY wk ORDER BY wk DESC LIMIT 10"""
    ).fetchall()
    print(f"  {'week':9} {'trades':>6} {'win%':>5} {'PnL $':>9} {'avgWin%':>8} {'avgLoss%':>9}")
    for wk, n, w, pnl, aw, al in rows:
        wr = 100 * w / n if n else 0
        print(f"  {wk:9} {n:>6} {wr:>4.0f}% {pnl:>9.2f} {aw or 0:>8.0f} {al or 0:>9.0f}")


def recent_trades(c):
    rule("LAST 10 CLOSED TRADES")
    rows = c.execute(
        """SELECT symbol, ROUND(realized_pnl_usd,2) pnl,
                  ROUND(100.0*realized_pnl_usd/NULLIF(cost_basis_usd,0),0) pct,
                  date(opened_at) d
           FROM positions WHERE status='closed' AND realized_pnl_usd IS NOT NULL
           ORDER BY id DESC LIMIT 10"""
    ).fetchall()
    for sym, pnl, pct, d in rows:
        tag = "WIN " if (pnl or 0) > 0 else "loss"
        print(f"  {d}  {tag}  {sym[:16]:16} ${pnl:>7.2f}  ({pct or 0:+.0f}%)")


def open_positions(c):
    rule("OPEN POSITIONS")
    rows = c.execute(
        "SELECT symbol, date(opened_at) d, COALESCE(parked_reason,'') pr "
        "FROM positions WHERE status='open' ORDER BY id DESC"
    ).fetchall()
    if not rows:
        print("  (none)")
    for sym, d, pr in rows:
        print(f"  {sym[:16]:16} opened {d}  {('['+pr+']') if pr else ''}")


def gate_behaviour(c):
    rule("ENTRY GATE (last 30 days)")
    rows = c.execute(
        """SELECT decision, COUNT(*) n FROM meme_alerts
           WHERE created_at >= date('now','-30 day') GROUP BY decision"""
    ).fetchall()
    total = sum(n for _, n in rows) or 1
    for dec, n in rows:
        print(f"  {dec:12} {n:>4}  ({100*n/total:.0f}%)")
    r = c.execute(
        """SELECT mcap*1.0/liquidity FROM meme_alerts
           WHERE liquidity>0 AND mcap>0 ORDER BY id DESC LIMIT 120"""
    ).fetchall()
    ratios = sorted(x[0] for x in r)
    if len(ratios) >= 20:
        k = max(0, min(len(ratios)-1, int(math.ceil(0.65*len(ratios)))-1))
        thr = max(4.0, ratios[k])
        print(f"\n  adaptive mcap/liq threshold now ~{thr:.1f}x "
              f"(demotes worst ~35% by ratio; auto-tunes to market)")


def yields(c):
    rule("TOP STABLECOIN / LST YIELDS (latest)")
    rows = c.execute(
        """SELECT platform, ROUND(apy,2) apy FROM yield_snapshots
           WHERE recorded_at = (SELECT MAX(recorded_at) FROM yield_snapshots)
           ORDER BY apy DESC LIMIT 8"""
    ).fetchall()
    for plat, apy in rows:
        print(f"  {plat:14} {apy:>6.2f}%")


def main():
    if not Path(DB).exists():
        print("DB not found:", DB)
        return
    c = ro()
    print("BREADBOT STATUS REPORT")
    weekly_performance(c)
    recent_trades(c)
    open_positions(c)
    gate_behaviour(c)
    yields(c)
    print("\n(read-only snapshot -- nothing was modified)")
    c.close()


if __name__ == "__main__":
    main()
