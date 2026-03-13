"""
notifications/telegram_bot.py — Your control panel.
Sends all alerts. Handles /status, /yields, /positions, /pause, /resume commands.
"""

import asyncio
import json
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
)
from loguru import logger
import config

# ── Alert formatting ────────────────────────────────────────────────────────

def format_meme_alert(token: dict, score: int, flags: list, position_size_usd: float) -> str:
    """Builds the Telegram message for a new meme coin opportunity."""
    chain_emoji = "🟣" if token["chain"] == "solana" else "🔵"
    score_emoji = "✅" if score >= 80 else "⚠️" if score >= 60 else "🔴"

    flag_lines = ""
    if flags:
        flag_lines = "\n".join(f"  · {f.replace('_', ' ')}" for f in flags)
        flag_lines = f"\n\n*Flags:*\n{flag_lines}"

    return (
        f"{chain_emoji} *{token['symbol']}* · {token['chain'].upper()}\n\n"
        f"💰 Price:      ${token['price_usd']:.8f}\n"
        f"💧 Liquidity:  ${token['liquidity']:,.0f}\n"
        f"📊 Vol 24h:    ${token['volume_24h']:,.0f}\n"
        f"📈 Market Cap: ${token['market_cap']:,.0f}\n"
        f"⏱ Age:        {token['age_hours']:.1f}h\n\n"
        f"{score_emoji} *Security Score: {score}/100*"
        f"{flag_lines}"
    )


def meme_alert_keyboard(alert_id: int, position_size_usd: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ BUY ${position_size_usd:.0f}", callback_data=f"buy:{alert_id}"),
        InlineKeyboardButton("❌ Skip", callback_data=f"skip:{alert_id}"),
    ]])


# ── TelegramController ──────────────────────────────────────────────────────

class TelegramController:
    def __init__(self, risk_manager, db_getter, execute_trade=None):
        self.risk          = risk_manager
        self.get_db        = db_getter
        self.execute_trade = execute_trade   # wired in main.py — None in Phase 1
        self.app           = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()
        self._register_handlers()

    def _register_handlers(self):
        self.app.add_handler(CommandHandler("status",    self._cmd_status))
        self.app.add_handler(CommandHandler("yields",    self._cmd_yields))
        self.app.add_handler(CommandHandler("positions", self._cmd_positions))
        self.app.add_handler(CommandHandler("pause",     self._cmd_pause))
        self.app.add_handler(CommandHandler("resume",    self._cmd_resume))
        self.app.add_handler(CallbackQueryHandler(self._handle_button))

    # ── Commands ────────────────────────────────────────────

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        s = self.risk.status_dict()
        status = "🟢 Active" if s["trading_active"] else "🔴 Paused"
        msg = (
            f"*Breadbot Status*\n\n"
            f"Trading:    {status}\n"
            f"Daily P&L:  ${s['daily_pnl']:+.2f}\n"
            f"Loss used:  {s['daily_loss_pct_used']:.1f}%\n"
            f"Positions:  {s['open_positions']} open\n"
            f"Portfolio:  ${s['portfolio_usd']:,.0f}\n"
        )
        if s["pause_reason"]:
            msg += f"\n_{s['pause_reason']}_"
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def _cmd_yields(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        db = await self.get_db()
        try:
            rows = await db.execute_fetchall("""
                SELECT platform, asset, apy, notes
                FROM yield_snapshots y1
                WHERE recorded_at=(
                    SELECT MAX(recorded_at) FROM yield_snapshots y2
                    WHERE y2.platform=y1.platform AND y2.asset=y1.asset
                )
                ORDER BY apy DESC
            """)
            if not rows:
                await update.message.reply_text("No yield data yet. Runs on the hour.")
                return
            lines = ["*Current Stablecoin Yields*\n"]
            for r in rows:
                lines.append(f"`{r['apy']:5.2f}%` — {r['platform']} {r['asset']}")
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        finally:
            await db.close()

    async def _cmd_positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        db = await self.get_db()
        try:
            rows = await db.execute_fetchall(
                "SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC"
            )
            if not rows:
                await update.message.reply_text("No open positions.")
                return
            lines = ["*Open Positions*\n"]
            for r in rows:
                lines.append(
                    f"*{r['symbol']}* ({r['chain']})\n"
                    f"  Entry: ${r['entry_price']:.6f} | Stop: ${r['stop_loss']:.6f}\n"
                    f"  Size:  ${r['cost_basis']:.2f}\n"
                )
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        finally:
            await db.close()

    async def _cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self.risk.pause()
        await update.message.reply_text("⏸ Trading paused. Send /resume when ready.")

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self.risk.resume()
        await update.message.reply_text("▶️ Trading resumed.")

    # ── Button handler ──────────────────────────────────────

    async def _handle_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        action, alert_id_str = query.data.split(":", 1)
        alert_id = int(alert_id_str)

        db = await self.get_db()
        try:
            await db.execute(
                "UPDATE meme_alerts SET decision=? WHERE id=?",
                (action, alert_id)
            )
            await db.commit()
        finally:
            await db.close()

        if action == "buy":
            await query.edit_message_reply_markup(None)
            if self.execute_trade:
                # Phase 2 — fetch alert details and fire the trade
                db2 = await self.get_db()
                try:
                    rows = await db2.execute_fetchall(
                        "SELECT symbol, chain, position_size_usd FROM meme_alerts WHERE id=?",
                        (alert_id,)
                    )
                finally:
                    await db2.close()
                if rows:
                    r = rows[0]
                    await self.execute_trade(alert_id, r["symbol"], r["position_size_usd"], r["chain"])
                else:
                    await query.message.reply_text(f"Alert #{alert_id} not found in database.")
            else:
                await query.message.reply_text(f"✅ Buy logged for alert #{alert_id}. Phase 2 will execute this automatically.")
        else:
            await query.edit_message_reply_markup(None)
            await query.message.reply_text(f"❌ Skipped alert #{alert_id}.")

    # ── Public send helpers ─────────────────────────────────

    async def send_alert(self, token: dict, score: int, flags: list,
                         position_size_usd: float, alert_id: int):
        text = format_meme_alert(token, score, flags, position_size_usd)
        kb   = meme_alert_keyboard(alert_id, position_size_usd)
        await self.app.bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="Markdown",
            reply_markup=kb,
        )
        logger.info(f"Alert sent: {token['symbol']} score={score}")

    async def send_message(self, text: str):
        await self.app.bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="Markdown",
        )

    async def run(self):
        """Start polling. Call this in main.py."""
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        logger.info("Telegram bot polling started")
