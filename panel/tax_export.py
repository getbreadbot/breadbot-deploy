"""
Tax Export endpoint + CSV writers (S70 Phase A).

Exposes GET /api/tax/export?year=YYYY&format=koinly|cointracker|irs8949
which streams a CSV download from the tax_export_view.

Three writers, each tuned to a target consumer:
  • Koinly Universal — broadest 3rd-party tax tool importer
  • CoinTracker      — 2nd-most-popular crypto tax tool
  • IRS Form 8949    — direct column shape for self-filers / accountants

Funding income (Schedule 1, ordinary income) is emitted as separate
rows from funding_income_events with tx_type='funding_income'. Form
8949 writer skips those rows (8949 is capital gains only) and
produces a footer note pointing the filer at Schedule 1.

Auth: existing panel session cookie (`verify_session` from auth.py).
"""

from __future__ import annotations

import csv
import io
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from auth import verify_session

log = logging.getLogger(__name__)

router = APIRouter()

# Resolve DB path the same way the rest of the bot does.
# /opt/projects/breadbot/data/cryptobot.db on VPS;
# Railway buyers get an equivalent path inside the deploy container.
DB_PATH = (
    Path(os.environ.get("BREADBOT_DB_PATH"))
    if os.environ.get("BREADBOT_DB_PATH")
    else Path(__file__).parent.parent / "data" / "cryptobot.db"
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _connect_ro() -> sqlite3.Connection:
    """Read-only connection so the panel can never accidentally write."""
    if not DB_PATH.exists():
        raise HTTPException(status_code=503, detail="bot database not found")
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)


def _fetch_capital_gains(year: int) -> list[dict]:
    """Pull capital-gains rows from tax_export_view for the requested year.

    Year filter applies to disposed_at (date of taxable event), which is
    the IRS realization rule for crypto-to-fiat trades.
    """
    conn = _connect_ro()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT row_id, tx_type, asset, chain, venue,
                   acquired_at, disposed_at,
                   entry_price, exit_price, quantity,
                   cost_basis_usd, proceeds_usd, realized_pnl_usd,
                   holding_period, wash_sale_disallowed
            FROM tax_export_view
            WHERE strftime('%Y', disposed_at) = ?
            ORDER BY disposed_at
            """,
            (str(year),),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _fetch_funding_income(year: int) -> list[dict]:
    """Pull funding income (Schedule 1) rows for the requested year."""
    conn = _connect_ro()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id AS row_id, pair, payment_usd, funding_rate, occurred_at
            FROM funding_income_events
            WHERE strftime('%Y', occurred_at) = ?
            ORDER BY occurred_at
            """,
            (str(year),),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _csv_response(buf: io.StringIO, filename: str) -> StreamingResponse:
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Writer 1: Koinly Universal ──────────────────────────────────────────────
# Reference: https://koinly.io/integrations/csv/
# Universal columns are stable across years.
def write_koinly_universal(
    capital_rows: list[dict], income_rows: list[dict], year: int
) -> io.StringIO:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "Koinly Date", "Pair", "Side", "Amount", "Total",
        "Fee Amount", "Fee Currency", "Order ID", "Trade ID",
        "Description", "Sent Amount", "Sent Currency",
        "Received Amount", "Received Currency",
        "Fee Amount Field", "Fee Currency Field",
        "Net Worth Amount", "Net Worth Currency",
        "Label", "TxHash",
    ])

    for r in capital_rows:
        # One row covers buy and sell of a closed position. Koinly's
        # universal format prefers two events (acquire + dispose) but
        # also accepts a single 'trade' row with sent/received fields.
        # We emit a single trade row per closed position.
        if r["acquired_at"]:
            buf_row_acq = [
                r["acquired_at"], "", "buy",
                r["quantity"] or "",
                r["cost_basis_usd"] or "",
                "", "USD",
                f"BB-ACQ-{r['tx_type']}-{r['row_id']}",
                f"BB-ACQ-{r['tx_type']}-{r['row_id']}",
                f"Breadbot {r['tx_type']} acquisition: {r['asset']}",
                r["cost_basis_usd"] or "", "USD",
                r["quantity"] or "", r["asset"] or "",
                "", "USD",
                "", "USD",
                "trade", "",
            ]
            w.writerow(buf_row_acq)
        if r["disposed_at"]:
            buf_row_disp = [
                r["disposed_at"], "", "sell",
                r["quantity"] or "",
                r["proceeds_usd"] or "",
                "", "USD",
                f"BB-DISP-{r['tx_type']}-{r['row_id']}",
                f"BB-DISP-{r['tx_type']}-{r['row_id']}",
                f"Breadbot {r['tx_type']} disposal: {r['asset']}",
                r["quantity"] or "", r["asset"] or "",
                r["proceeds_usd"] or "", "USD",
                "", "USD",
                "", "USD",
                "trade", "",
            ]
            w.writerow(buf_row_disp)

    for r in income_rows:
        w.writerow([
            r["occurred_at"], "", "income",
            r["payment_usd"] or "", r["payment_usd"] or "",
            "", "USD",
            f"BB-FUND-{r['row_id']}",
            f"BB-FUND-{r['row_id']}",
            f"Breadbot funding income: {r['pair']}",
            "", "USD",
            r["payment_usd"] or "", "USD",
            "", "USD",
            "", "USD",
            "income", "",
        ])
    return buf


# ── Writer 2: CoinTracker ───────────────────────────────────────────────────
# CoinTracker accepts a custom CSV with these column names.
# Reference: https://help.cointracker.io/en/articles/4953418
def write_cointracker(
    capital_rows: list[dict], income_rows: list[dict], year: int
) -> io.StringIO:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "Date", "Received Quantity", "Received Currency",
        "Sent Quantity", "Sent Currency",
        "Fee Amount", "Fee Currency", "Tag",
    ])
    for r in capital_rows:
        if r["acquired_at"]:
            w.writerow([
                r["acquired_at"],
                r["quantity"] or "", r["asset"] or "",
                r["cost_basis_usd"] or "", "USD",
                "", "", "",
            ])
        if r["disposed_at"]:
            w.writerow([
                r["disposed_at"],
                r["proceeds_usd"] or "", "USD",
                r["quantity"] or "", r["asset"] or "",
                "", "", "",
            ])
    for r in income_rows:
        w.writerow([
            r["occurred_at"],
            r["payment_usd"] or "", "USD",
            "", "",
            "", "", "staked",
        ])
    return buf


# ── Writer 3: IRS Form 8949 ─────────────────────────────────────────────────
# Form 8949 columns: (a) Description, (b) Date acquired, (c) Date sold,
# (d) Proceeds, (e) Cost basis, (f) Code, (g) Adjustment, (h) Gain/loss
# Short-term and long-term go on separate parts of the form. We emit a
# 'Term' column up front so the filer/accountant can split the file.
# Reference: https://www.irs.gov/forms-pubs/about-form-8949
def write_irs8949(
    capital_rows: list[dict], income_rows: list[dict], year: int
) -> io.StringIO:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "Term", "Description", "Date Acquired", "Date Sold",
        "Proceeds", "Cost Basis", "Code", "Adjustment", "Gain or Loss",
    ])
    for r in capital_rows:
        # 8949 needs both a buy and sell timestamp. Skip rows that are
        # still open (no disposed_at). cost_basis can be NULL if the row
        # genuinely has no acquisition record (legacy grid_fills before
        # buy_filled_at was wired); we leave the cell empty so the filer
        # sees the gap.
        if not r["disposed_at"]:
            continue

        term = (r["holding_period"] or "").upper() or "UNKNOWN"
        if term not in ("SHORT", "LONG"):
            term = "UNKNOWN"

        description = (
            f"{r['quantity'] or ''} {r['asset'] or ''} "
            f"({r['tx_type']})"
        ).strip()

        w.writerow([
            term,
            description,
            r["acquired_at"] or "",
            r["disposed_at"] or "",
            f"{r['proceeds_usd']:.2f}" if r["proceeds_usd"] is not None else "",
            f"{r['cost_basis_usd']:.2f}" if r["cost_basis_usd"] is not None else "",
            "",                # code (e.g. 'W' for wash sale) — not yet
            "",                # adjustment
            f"{r['realized_pnl_usd']:.2f}" if r["realized_pnl_usd"] is not None else "",
        ])

    if income_rows:
        # Trailing notice — funding income is Schedule 1, not 8949.
        w.writerow([])
        w.writerow([
            "NOTE",
            "Funding-rate / staking income is reported on Schedule 1 "
            "(Form 1040), not Form 8949.",
        ])
        w.writerow([
            "NOTE",
            f"Total {year} funding income (USD): "
            f"{sum((r['payment_usd'] or 0) for r in income_rows):.2f}",
        ])
    return buf


# ── route ────────────────────────────────────────────────────────────────────

WRITERS = {
    "koinly":      write_koinly_universal,
    "cointracker": write_cointracker,
    "irs8949":     write_irs8949,
}


@router.get("/export")
def tax_export(
    year: int = Query(..., ge=2020, le=2099),
    format: str = Query("koinly", pattern="^(koinly|cointracker|irs8949)$"),
    _: bool = Depends(verify_session),
):
    """
    Stream a CSV containing all taxable events for the requested year.

    Year filter applies to the disposal/payment date (taxable event).
    Trades opened in year N-1 and closed in year N appear under year N,
    matching the IRS realization rule.
    """
    capital_rows = _fetch_capital_gains(year)
    income_rows  = _fetch_funding_income(year)

    if not capital_rows and not income_rows:
        raise HTTPException(
            status_code=404,
            detail=f"No taxable events found for {year}",
        )

    writer = WRITERS[format]
    buf = writer(capital_rows, income_rows, year)
    filename = f"breadbot_tax_{year}_{format}.csv"
    log.info(
        "tax_export: year=%s format=%s capital_rows=%d income_rows=%d",
        year, format, len(capital_rows), len(income_rows),
    )
    return _csv_response(buf, filename)
