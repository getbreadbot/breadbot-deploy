#!/usr/bin/env python3
"""Hummingbot paper market-making pilot report (read-only).

    ssh vps '/opt/projects/breadbot/venv/bin/python3 /opt/hummingbot_pilot/hb_pilot_report.py'

Reads the Hummingbot paper instance's own sqlite and reports realized spread
capture: buys vs sells, fees, remaining inventory marked to the last price, and
net PnL. Paper trade -> no capital at risk. Read-only, no writes.
"""
import sqlite3
import json
from pathlib import Path

DB = "/opt/hummingbot_pilot/data/conf_pmm_paper.sqlite"


def main():
    if not Path(DB).exists():
        print("Pilot DB not found yet:", DB)
        return
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = c.execute(
        "SELECT trade_type, price, amount, trade_fee_in_quote, timestamp "
        "FROM TradeFill ORDER BY timestamp"
    ).fetchall()
    print("HUMMINGBOT PAPER MM PILOT  (SOL-USD, Kraken feed, paper)")
    print("=" * 56)
    if not rows:
        print("No fills recorded yet. The strategy places quotes and fills")
        print("as price crosses them — check back after a few hours/days.")
        return
    buys = [(p, a, f) for (t, p, a, f, ts) in rows if str(t).upper().find("BUY") >= 0]
    sells = [(p, a, f) for (t, p, a, f, ts) in rows if str(t).upper().find("SELL") >= 0]
    b_amt = sum(a for _, a, _ in buys)
    s_amt = sum(a for _, a, _ in sells)
    b_cost = sum(p * a for p, a, _ in buys)
    s_proc = sum(p * a for p, a, _ in sells)
    fees = sum((f or 0) for _, _, f in buys) + sum((f or 0) for _, _, f in sells)
    last_price = rows[-1][1]
    inv = b_amt - s_amt                       # net inventory (SOL)
    realized_cash = s_proc - b_cost           # cash from round-trips
    mtm = realized_cash + inv * last_price - fees
    span_h = (rows[-1][4] - rows[0][4]) / 3600.0 if len(rows) > 1 else 0
    print(f"  fills:        {len(rows)}  ({len(buys)} buys / {len(sells)} sells)")
    print(f"  over:         {span_h:.1f} hours")
    print(f"  bought:       {b_amt:.3f} SOL  (${b_cost:,.2f})")
    print(f"  sold:         {s_amt:.3f} SOL  (${s_proc:,.2f})")
    print(f"  net inventory:{inv:+.3f} SOL  (marked @ ${last_price:.2f})")
    print(f"  fees paid:    ${fees:,.2f}")
    print("  " + "-" * 40)
    print(f"  NET PnL (mark-to-market): ${mtm:+,.2f}")
    if span_h > 6:
        daily = mtm / (span_h / 24.0)
        print(f"  ~ ${daily:+,.2f}/day at this rate (early, noisy)")
    print("\n  NOTE: paper sim on live order book. Gross spread always looks")
    print("  positive; what matters is NET after fees and inventory swings.")
    print("  Judge only after a week+ and a full up/down price cycle.")
    c.close()


if __name__ == "__main__":
    main()
