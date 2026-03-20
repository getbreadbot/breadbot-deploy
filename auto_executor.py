#!/usr/bin/env python3
"""
auto_executor.py — Strategy-based auto-execution engine.

Sits between the scanner and Telegram. When EXECUTION_MODE is 'auto',
incoming alerts are evaluated against the active strategy's thresholds.
Alerts that pass are executed immediately; those that miss the threshold
fall back to normal Telegram approval.

The daily loss limit and trading_paused flag are ALWAYS enforced,
regardless of mode. These cannot be bypassed.

Strategy presets:
  conservative  — score >= 85 | market_cap < $1M  | 0.5x position size
  balanced      — score >= 78 | market_cap < $2M  | 1.0x position size (default)
  aggressive    — score >= 68 | market_cap < $5M  | 1.5x position size (capped at max)

New .env / bot_config keys:
  execution_mode       / EXECUTION_MODE         manual|auto       (default: manual)
  auto_strategy        / AUTO_STRATEGY          conservative|balanced|aggressive (default: balanced)
  auto_max_trades_day  / AUTO_MAX_TRADES_DAY    int               (default: 5)

Usage from scanner:
    from auto_executor import AutoExecutor
    executor = AutoExecutor()
    result = executor.evaluate(alert_dict)
    # result.executed → True/False
    # result.reason   → human-readable explanation
"""

import sqlite3
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

try:
    from alt_data_signals import get_cached_fear_greed, get_cached_recession_prob, get_cached_composite
    _ALT_DATA_AVAILABLE = True
except ImportError:
    _ALT_DATA_AVAILABLE = False
    def get_cached_fear_greed(): return None
    def get_cached_recession_prob(): return None
    def get_cached_composite(): return None

# ---------------------------------------------------------------------------
# Strategy definitions
# ---------------------------------------------------------------------------

STRATEGIES: dict[str, dict] = {
    "conservative": {
        "min_score":        85,
        "max_market_cap":   1_000_000,
        "position_multiplier": 0.5,
        "description": "Score 85+, market cap under $1M, half position size",
    },
    "balanced": {
        "min_score":        78,
        "max_market_cap":   2_000_000,
        "position_multiplier": 1.0,
        "description": "Score 78+, market cap under $2M, full position size",
    },
    "aggressive": {
        "min_score":        68,
        "max_market_cap":   5_000_000,
        "position_multiplier": 1.5,
        "description": "Score 68+, market cap under $5M, 1.5x position size (capped at max)",
    },
}

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ExecutionResult:
    executed: bool          # True = auto-executed | False = needs manual approval
    reason: str             # Human-readable explanation for logs / Telegram
    position_usd: float     # Dollar amount used (0.0 if not executed)
    strategy: str           # Strategy name that was evaluated
    alert_score: int        # Score from the incoming alert
    blocked: bool = False   # True = hard block (loss limit, paused, daily cap hit)


# ---------------------------------------------------------------------------
# AutoExecutor
# ---------------------------------------------------------------------------

class AutoExecutor:
    """Evaluates scanner alerts and decides whether to auto-execute."""

    def __init__(self):
        self.db_path = Path(__file__).parent / "data" / "cryptobot.db"

    # ── Config helpers ───────────────────────────────────────────────────────

    def _db_get(self, key: str) -> str:
        """Read a value from bot_config, return empty string if missing."""
        if not self.db_path.exists():
            return ""
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            row  = conn.execute(
                "SELECT value FROM bot_config WHERE key=?", (key,)
            ).fetchone()
            conn.close()
            return row[0].strip() if row and row[0] else ""
        except Exception:
            return ""

    def _cfg(self, db_key: str, env_key: str, default: str) -> str:
        """DB-first config read, falls back to env, then default."""
        val = self._db_get(db_key)
        if val:
            return val
        return os.getenv(env_key, default).strip() or default

    @property
    def execution_mode(self) -> str:
        return self._cfg("execution_mode", "EXECUTION_MODE", "manual").lower()

    @property
    def strategy_name(self) -> str:
        name = self._cfg("auto_strategy", "AUTO_STRATEGY", "balanced").lower()
        return name if name in STRATEGIES else "balanced"

    @property
    def max_trades_per_day(self) -> int:
        val = self._cfg("auto_max_trades_day", "AUTO_MAX_TRADES_DAY", "5")
        try:
            return int(val)
        except ValueError:
            return 5

    @property
    def max_position_pct(self) -> float:
        val = self._cfg("max_position_size_pct", "MAX_POSITION_SIZE_PCT", "0.02")
        try:
            return float(val)
        except ValueError:
            return 0.02

    @property
    def portfolio_usd(self) -> float:
        val = self._cfg("portfolio_total_usd", "TOTAL_PORTFOLIO_USD", "5000")
        try:
            return float(val)
        except ValueError:
            return 5000.0

    @property
    def daily_loss_limit_pct(self) -> float:
        val = self._cfg("daily_loss_limit_pct", "DAILY_LOSS_LIMIT_PCT", "0.05")
        try:
            return float(val)
        except ValueError:
            return 0.05

    # ── Safety checks (always enforced, cannot be bypassed) ──────────────────

    def _is_paused(self) -> bool:
        val = self._db_get("trading_paused")
        return val.lower() in ("1", "true", "yes") if val else False

    def _daily_loss_exceeded(self) -> bool:
        """True if today's realized losses have hit the configured daily limit."""
        if not self.db_path.exists():
            return False
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            row  = conn.execute(
                "SELECT realized_pnl FROM daily_summary WHERE date=?", (today,)
            ).fetchone()
            conn.close()
            if not row:
                return False
            realized = float(row[0])
            limit    = self.portfolio_usd * self.daily_loss_limit_pct
            return realized < 0 and abs(realized) >= limit
        except Exception:
            return False

    def _trades_today(self) -> int:
        """Count of auto-executed trades already placed today."""
        if not self.db_path.exists():
            return 0
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            row  = conn.execute(
                """SELECT COUNT(*) FROM meme_alerts
                   WHERE decision='auto_buy'
                   AND date(created_at)=?""",
                (today,),
            ).fetchone()
            conn.close()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    # ── Position sizing ───────────────────────────────────────────────────────

    def _calc_position(self, score: int, multiplier: float) -> float:
        """
        Base size = portfolio * max_position_pct.
        Scaled by security score (0.5x at min threshold, 1.0x at 100).
        Then multiplied by the strategy's position_multiplier.
        Hard cap: never exceeds portfolio * max_position_pct * 1.5.
        """
        base         = self.portfolio_usd * self.max_position_pct
        score_factor = 0.5 + 0.5 * (score / 100)       # 0.5 at score=0, 1.0 at score=100
        raw          = base * score_factor * multiplier
        hard_cap     = base * 1.5
        return round(min(raw, hard_cap), 2)

    # ── Main entry point ──────────────────────────────────────────────────────

    def evaluate(self, alert: dict[str, Any]) -> ExecutionResult:
        """
        Evaluate a scanner alert and decide whether to auto-execute.

        alert keys used:
          score       int   security score 0-100
          market_cap  float current market cap in USD
          token       str   token symbol or name (for logging)
          chain       str   solana | base
          price       float current price

        Returns an ExecutionResult. Caller is responsible for:
          - Executing the trade if result.executed is True
          - Sending the Telegram notification (included reason string)
          - Logging the decision to meme_alerts with decision='auto_buy' or 'pending'
        """
        score      = int(alert.get("score", 0))
        market_cap = float(alert.get("market_cap", 0))
        token      = alert.get("token", "UNKNOWN")
        strategy   = STRATEGIES[self.strategy_name]

        # ── Hard safety blocks (always checked first) ──────────────────────
        if self._is_paused():
            return ExecutionResult(
                executed=False, blocked=True, strategy=self.strategy_name,
                alert_score=score, position_usd=0.0,
                reason="Trading is paused. Alert queued for manual review.",
            )

        if self._daily_loss_exceeded():
            return ExecutionResult(
                executed=False, blocked=True, strategy=self.strategy_name,
                alert_score=score, position_usd=0.0,
                reason="Daily loss limit reached. No new trades until reset or manual resume.",
            )

        # ── Mode check ─────────────────────────────────────────────────────
        if self.execution_mode != "auto":
            return ExecutionResult(
                executed=False, blocked=False, strategy=self.strategy_name,
                alert_score=score, position_usd=0.0,
                reason="Manual mode — awaiting your approval.",
            )

        # ── Daily trade cap ────────────────────────────────────────────────
        trades_today = self._trades_today()
        if trades_today >= self.max_trades_per_day:
            return ExecutionResult(
                executed=False, blocked=True, strategy=self.strategy_name,
                alert_score=score, position_usd=0.0,
                reason=f"Daily auto-trade limit ({self.max_trades_per_day}) reached. Alert queued for manual review.",
            )

        # ── Strategy threshold checks ──────────────────────────────────────
        if score < strategy["min_score"]:
            return ExecutionResult(
                executed=False, blocked=False, strategy=self.strategy_name,
                alert_score=score, position_usd=0.0,
                reason=(
                    f"Score {score} is below {strategy['min_score']} threshold for "
                    f"{self.strategy_name} strategy. Sending for manual approval."
                ),
            )

        if market_cap > strategy["max_market_cap"]:
            cap_fmt = f"${strategy['max_market_cap']:,.0f}"
            return ExecutionResult(
                executed=False, blocked=False, strategy=self.strategy_name,
                alert_score=score, position_usd=0.0,
                reason=(
                    f"Market cap ${market_cap:,.0f} exceeds {cap_fmt} limit for "
                    f"{self.strategy_name} strategy. Sending for manual approval."
                ),
            )

        # ── All checks passed — auto-execute ──────────────────────────────
        position_usd = self._calc_position(score, strategy["position_multiplier"])

        # ── Alt data hooks ─────────────────────────────────────────────
        if _ALT_DATA_AVAILABLE:
            # Hook 1: Fear & Greed — reduce position sizing in extreme fear
            fg = get_cached_fear_greed()
            if fg is not None and fg < 20:
                multiplier = float(os.getenv("FEAR_GREED_SIZE_MULTIPLIER", "0.6"))
                position_usd = round(position_usd * multiplier, 2)

            # Hook 2: Recession probability — reduce sizing when Kalshi shows >50%
            rec = get_cached_recession_prob()
            if rec is not None and rec > 0.50:
                position_usd = round(position_usd * 0.7, 2)

            # Hook 3: Composite signal — block auto-execution if below pause threshold
            composite = get_cached_composite()
            pause_threshold = float(os.getenv("COMPOSITE_PAUSE_THRESHOLD", "-50"))
            if composite is not None and composite < pause_threshold:
                return ExecutionResult(
                    executed=False, blocked=True, strategy=self.strategy_name,
                    alert_score=score, position_usd=0.0,
                    reason=(
                        f"Alt data composite signal {composite:+.0f} is below pause "
                        f"threshold ({pause_threshold:.0f}). Auto-execution suspended."
                    ),
                )

        return ExecutionResult(
            executed=True, blocked=False, strategy=self.strategy_name,
            alert_score=score, position_usd=position_usd,
            reason=(
                f"AUTO-EXECUTED [{self.strategy_name.upper()}] | "
                f"{token} | Score {score} | ${position_usd:.2f} | "
                f"Trade {trades_today + 1}/{self.max_trades_per_day} today"
            ),
        )

    def get_strategy_summary(self) -> dict[str, Any]:
        """Return current config for display in Telegram /status or dashboard."""
        strategy = STRATEGIES[self.strategy_name]
        return {
            "execution_mode":      self.execution_mode,
            "strategy":            self.strategy_name,
            "min_score":           strategy["min_score"],
            "max_market_cap_usd":  strategy["max_market_cap"],
            "position_multiplier": strategy["position_multiplier"],
            "max_trades_per_day":  self.max_trades_per_day,
            "trades_today":        self._trades_today(),
            "is_paused":           self._is_paused(),
            "daily_loss_exceeded": self._daily_loss_exceeded(),
            "description":         strategy["description"],
        }
