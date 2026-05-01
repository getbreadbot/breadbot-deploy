"""
Shared manual-buy logic. Single canonical entry point for all callers
that need to "the user said BUY on alert N — actually execute the trade
and create a position row".

Three callers go through this module:
  1. scanner._handle_callback (Telegram inline keyboard, S80 P4)
  2. panel.websocket_manager (panel Buy button via WebSocket, S80 P6)
  3. panel.research_proxy./buy (panel REST endpoint, S80 P6)

This is the canonical sequence:
  load alert -> AutoExecutor.evaluate -> execute_trade(force=True) -> record_position

Returns a structured ManualBuyResult so callers can format their own UX
without re-implementing the success/failure cases.

The function is synchronous — async callers should wrap in asyncio.to_thread().
This is deliberate: all the bot's trade-execution code (AutoExecutor,
exchange_executor, record_position) is synchronous, and embedding async
inside a sync chain is more complex than wrapping the whole thing.
"""

from __future__ import annotations
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "data" / "cryptobot.db"


@dataclass
class ManualBuyResult:
    """Structured outcome of a manual buy attempt.

    success: True iff the trade executed AND the position row was recorded.
    position_id: present only on success.
    reason: short machine-friendly status (e.g. 'risk_blocked', 'execute_failed', 'recorded').
    user_message: human-readable text safe to show in Telegram or panel toasts.
    decision_value: what to write to meme_alerts.decision (or None to leave alone).
    """
    success: bool
    position_id: Optional[int] = None
    reason: str = ""
    user_message: str = ""
    decision_value: Optional[str] = None
    pair: Optional[dict] = None  # passed back for the chart link, etc.


def _load_alert_for_buy(alert_id: int) -> Optional[dict]:
    """Reconstitute the pair dict + score needed to re-run AutoExecutor.evaluate()."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        try:
            cur = conn.execute(
                """SELECT chain, token_addr, token_name, symbol,
                          price_usd, liquidity, volume_24h, mcap, rug_score
                   FROM meme_alerts WHERE id = ?""",
                (alert_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
        finally:
            conn.close()
    except Exception as exc:
        log.error("_load_alert_for_buy(%d) failed: %s", alert_id, exc)
        return None


def _update_decision(alert_id: int, decision: str) -> None:
    """Update meme_alerts.decision for the given alert. Best-effort, never raises."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        try:
            conn.execute(
                "UPDATE meme_alerts SET decision = ? WHERE id = ?",
                (decision, alert_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        log.warning("_update_decision(%d, %s) failed: %s", alert_id, decision, exc)


class _Result:
    """Minimal shim for record_position (only reads .position_usd today)."""
    def __init__(self, position_usd: float):
        self.position_usd = position_usd


def execute_manual_buy(alert_id: int) -> ManualBuyResult:
    """Synchronous canonical manual-buy path.

    Steps:
      1. Look up the alert in meme_alerts. Missing -> error result.
      2. Build pair dict from alert columns.
      3. Run AutoExecutor.evaluate to get a position size + safety verdict.
         (catches paused state, daily loss limit, daily cap, composite signal)
      4. If blocked -> mark decision='blocked' and return.
      5. If soft-blocked (decision.executed=False) -> mark decision='skip', return.
      6. execute_trade(force=True). If False -> mark decision='execute_failed', return.
      7. record_position(). If None -> trade fired but no row; flag for operator.
      8. Mark decision='buy' + return success.
    """
    alert = _load_alert_for_buy(alert_id)
    if not alert:
        return ManualBuyResult(
            success=False,
            reason="alert_not_found",
            user_message=f"Alert #{alert_id} not found in database.",
        )

    try:
        from auto_executor import AutoExecutor
        from exchange_executor import execute_trade
        # Late import to avoid circular: scanner imports manual_buy, manual_buy
        # would otherwise import scanner for record_position.
        from scanner import record_position
    except ImportError as exc:
        log.error("manual_buy: bot module import failed: %s", exc)
        return ManualBuyResult(
            success=False,
            reason="import_error",
            user_message=f"Bot modules unavailable: {exc}",
        )

    pair = {
        "chain":      alert["chain"],
        "token_addr": alert["token_addr"],
        "token_name": alert.get("token_name") or alert.get("symbol") or "UNKNOWN",
        "symbol":     alert.get("symbol") or "UNKNOWN",
        "price_usd":  float(alert.get("price_usd") or 0),
    }

    try:
        ae = AutoExecutor()
        decision = ae.evaluate({
            "score":      int(alert.get("rug_score") or 0),
            "market_cap": float(alert.get("mcap") or 0),
            "token":      pair["symbol"],
            "chain":      pair["chain"],
            "price":      pair["price_usd"],
        })
    except Exception as exc:
        log.error("manual_buy: AutoExecutor failed for alert=%d: %s", alert_id, exc, exc_info=True)
        return ManualBuyResult(
            success=False,
            reason="evaluator_error",
            user_message=f"Risk evaluator error: {exc}",
            pair=pair,
        )

    if decision.blocked:
        _update_decision(alert_id, "blocked")
        return ManualBuyResult(
            success=False,
            reason="risk_blocked",
            user_message=f"Buy blocked by risk manager: {decision.reason}",
            decision_value="blocked",
            pair=pair,
        )

    if not decision.executed:
        _update_decision(alert_id, "skip")
        return ManualBuyResult(
            success=False,
            reason="soft_blocked",
            user_message=f"Buy not executed: {decision.reason}",
            decision_value="skip",
            pair=pair,
        )

    try:
        ok = execute_trade(
            chain=pair["chain"],
            token_addr=pair["token_addr"],
            symbol=pair["symbol"],
            position_usd=decision.position_usd,
            price_usd=pair["price_usd"],
            force=True,
        )
    except Exception as exc:
        log.error("manual_buy: execute_trade exception for alert=%d: %s", alert_id, exc, exc_info=True)
        _update_decision(alert_id, "execute_failed")
        return ManualBuyResult(
            success=False,
            reason="execute_exception",
            user_message=f"Trade execution failed: {exc}",
            decision_value="execute_failed",
            pair=pair,
        )

    if not ok:
        _update_decision(alert_id, "execute_failed")
        return ManualBuyResult(
            success=False,
            reason="execute_failed",
            user_message=f"{pair['symbol']}: trade did not confirm on-chain. Check journal.",
            decision_value="execute_failed",
            pair=pair,
        )

    try:
        pos_id = record_position(pair, _Result(decision.position_usd), alert_id)
    except Exception as exc:
        log.error("manual_buy: record_position exception for alert=%d: %s", alert_id, exc, exc_info=True)
        return ManualBuyResult(
            success=False,
            reason="record_failed_post_execute",
            user_message=(
                f"⚠️ {pair['symbol']}: trade fired but position record FAILED.\n"
                f"Check wallet and manually register if needed."
            ),
            pair=pair,
        )

    if pos_id is None:
        log.error("manual_buy: execute_trade ok but record_position returned None for alert=%d", alert_id)
        return ManualBuyResult(
            success=False,
            reason="record_returned_none",
            user_message=(
                f"⚠️ {pair['symbol']}: trade fired but position record FAILED.\n"
                f"Check wallet balance and manually register if needed."
            ),
            pair=pair,
        )

    _update_decision(alert_id, "buy")
    log.info(
        "Manual BUY executed: alert=%d -> position #%d %s $%.2f",
        alert_id, pos_id, pair["symbol"], decision.position_usd,
    )
    return ManualBuyResult(
        success=True,
        position_id=pos_id,
        reason="recorded",
        user_message=(
            f"✅ Bought {pair['symbol']} at ${pair['price_usd']:.8f}\n"
            f"Position #{pos_id} opened — ${decision.position_usd:.2f}\n"
            f"SL/TP/time-stop/fast-poll all active."
        ),
        decision_value="buy",
        pair=pair,
    )
