"""
risk/manager.py — Position sizing, daily loss limits, circuit breaker.
The scanner calls check_and_size() before every alert.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from loguru import logger
import config


@dataclass
class RiskManager:
    portfolio_usd: float     = config.TOTAL_PORTFOLIO_USD
    max_position_pct: float  = config.MAX_POSITION_SIZE_PCT
    daily_loss_limit_pct: float = config.DAILY_LOSS_LIMIT_PCT

    # Runtime state (reset at midnight)
    daily_pnl: float         = 0.0
    open_positions: int      = 0
    paused: bool             = False
    consecutive_losses: int  = 0

    # Hard limits
    MAX_OPEN_POSITIONS: int  = 5
    MAX_CONSECUTIVE_LOSSES: int = 3

    def daily_loss_limit_usd(self) -> float:
        return self.portfolio_usd * self.daily_loss_limit_pct

    def is_trading_allowed(self) -> tuple[bool, str]:
        """Returns (allowed, reason). Reason is empty string if allowed."""
        if self.paused:
            return False, "Trading manually paused. Send /resume to restart."

        if self.daily_pnl <= -self.daily_loss_limit_usd():
            return False, (
                f"Daily loss limit hit: ${abs(self.daily_pnl):.2f} lost today "
                f"(limit: ${self.daily_loss_limit_usd():.2f}). Send /resume when ready."
            )

        if self.open_positions >= self.MAX_OPEN_POSITIONS:
            return False, f"Max open positions reached ({self.MAX_OPEN_POSITIONS}). Close one first."

        if self.consecutive_losses >= self.MAX_CONSECUTIVE_LOSSES:
            return False, (
                f"{self.consecutive_losses} consecutive losses. Pausing to protect capital. "
                f"Review your open positions, then send /resume."
            )

        return True, ""

    def size_position(self, security_score: int) -> float:
        """
        Calculate position size in USD based on security score.
        Higher confidence = larger position, up to the configured maximum.
        Score >= 80 → full size
        Score 60-79 → 60% of full size
        Score 50-59 → 30% of full size
        Score < 50  → blocked (returns 0)
        """
        max_usd = self.portfolio_usd * self.max_position_pct

        if security_score >= 80:
            multiplier = 1.0
        elif security_score >= 60:
            multiplier = 0.6
        elif security_score >= 50:
            multiplier = 0.3
        else:
            logger.warning(f"Score {security_score} below minimum threshold — blocked")
            return 0.0

        size = round(max_usd * multiplier, 2)
        logger.debug(f"Position size: ${size} (score={security_score}, max=${max_usd})")
        return size

    def record_trade_result(self, pnl_usd: float):
        """Call this after every closed trade to update daily state."""
        self.daily_pnl += pnl_usd
        if pnl_usd < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        logger.info(f"Trade result: ${pnl_usd:+.2f} | Daily P&L: ${self.daily_pnl:+.2f}")

    def pause(self):
        self.paused = True
        logger.warning("Trading PAUSED by user command")

    def resume(self):
        self.paused = False
        logger.info("Trading RESUMED")

    def reset_daily(self):
        """Call at midnight to reset daily counters."""
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        logger.info("Daily risk counters reset")

    def status_dict(self) -> dict:
        allowed, reason = self.is_trading_allowed()
        return {
            "trading_active": allowed,
            "pause_reason": reason,
            "daily_pnl": round(self.daily_pnl, 2),
            "daily_loss_limit_usd": self.daily_loss_limit_usd(),
            "daily_loss_pct_used": round(
                abs(self.daily_pnl) / self.daily_loss_limit_usd() * 100, 1
            ) if self.daily_pnl < 0 else 0.0,
            "open_positions": self.open_positions,
            "consecutive_losses": self.consecutive_losses,
            "portfolio_usd": self.portfolio_usd,
        }
