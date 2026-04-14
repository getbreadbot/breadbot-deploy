"""
pullback_monitor.py — Delayed entry module for auto-executed trades.

When enabled, instead of buying immediately at the alert price, the monitor
watches the token price for up to PULLBACK_TIMEOUT_MIN minutes.  If the price
dips by PULLBACK_PCT or more, it executes the trade at the lower price.

On timeout the behaviour depends on PULLBACK_FALLBACK:
  "skip"   → do nothing (most conservative)
  "market" → execute at whatever the current price is

All config is read from bot_config DB first, then .env, matching the
AutoExecutor pattern.

Usage (from scanner.py):
    from pullback_monitor import maybe_pullback
    # Instead of calling execute_trade() directly:
    asyncio.create_task(maybe_pullback(client, pair, result, alert_id, score, flags))
"""

import asyncio
import logging
import os
import time
from pathlib import Path

import httpx

log = logging.getLogger("pullback_monitor")

# ── Config helpers (mirror auto_executor pattern) ────────────────────────────

def _db_get(key: str) -> str:
    db_path = Path(__file__).parent / "data" / "cryptobot.db"
    if not db_path.exists():
        return ""
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT value FROM bot_config WHERE key=?", (key,)
        ).fetchone()
        conn.close()
        return row[0].strip() if row and row[0] else ""
    except Exception:
        return ""


def _cfg(db_key: str, env_key: str, default: str) -> str:
    val = _db_get(db_key)
    return val if val else os.getenv(env_key, default).strip() or default


def _cfg_float(db_key: str, env_key: str, default: str) -> float:
    try:
        return float(_cfg(db_key, env_key, default))
    except ValueError:
        return float(default)


def _cfg_bool(db_key: str, env_key: str, default: str = "false") -> bool:
    return _cfg(db_key, env_key, default).lower() in ("true", "1", "yes")


# ── Config accessors ─────────────────────────────────────────────────────────

def is_enabled() -> bool:
    return _cfg_bool("pullback_enabled", "PULLBACK_ENABLED", "false")

def pullback_pct() -> float:
    return _cfg_float("pullback_pct", "PULLBACK_PCT", "5.0")

def timeout_minutes() -> float:
    return _cfg_float("pullback_timeout_min", "PULLBACK_TIMEOUT_MIN", "15")

def fallback_mode() -> str:
    return _cfg("pullback_fallback", "PULLBACK_FALLBACK", "skip").lower()

def poll_interval_sec() -> float:
    return _cfg_float("pullback_poll_sec", "PULLBACK_POLL_SEC", "30")


# ── Active pullback tracking ────────────────────────────────────────────────

_active: dict[str, dict] = {}   # token_addr → metadata


async def _poll_price(token_addr: str, chain: str) -> float | None:
    """Fetch current price from DEXScreener."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}"
            )
            if r.status_code != 200:
                return None
            data = r.json()
            pairs = data.get("pairs") or []
            if not pairs:
                return None
            # Use first pair on matching chain
            for p in pairs:
                if p.get("chainId", "").lower() == chain.lower():
                    return float(p["priceUsd"])
            return float(pairs[0]["priceUsd"])
    except Exception as exc:
        log.warning("pullback price poll failed for %s: %s", token_addr[:12], exc)
        return None


async def maybe_pullback(
    tg_client: httpx.AsyncClient,
    pair: dict,
    result,          # ExecutionResult from auto_executor
    alert_id: int,
    score: int,
    flags: list,
) -> None:
    """
    Entry point called from scanner when auto-execute fires and pullback is enabled.

    If pullback is disabled or the token is already being monitored, falls through
    to immediate execution.
    """
    from scanner import (
        send_message, update_alert_decision, record_position,
        build_auto_buy_message, build_approval_message, build_buy_keyboard,
    )

    token_addr = pair["token_addr"]
    symbol     = pair.get("symbol", "UNKNOWN")
    chain      = pair.get("chain", "solana")
    alert_price = float(pair.get("price_usd", 0))

    if not is_enabled() or alert_price <= 0:
        # Pullback disabled — execute immediately (same as current behaviour)
        await _immediate_execute(
            tg_client, pair, result, alert_id, score, flags
        )
        return

    if token_addr in _active:
        log.info("pullback: %s already being monitored — skip duplicate", symbol)
        return

    pct       = pullback_pct()
    timeout_m = timeout_minutes()
    target    = alert_price * (1 - pct / 100)
    interval  = poll_interval_sec()

    log.info(
        "pullback: monitoring %s | alert=$%.8f target=$%.8f (-%s%%) timeout=%smin",
        symbol, alert_price, target, pct, timeout_m,
    )

    _active[token_addr] = {
        "symbol": symbol, "alert_price": alert_price,
        "target": target, "started": time.time(),
    }

    # Notify operator
    await send_message(
        tg_client,
        f"⏳ Pullback monitor started: {symbol} ({chain.upper()})\n"
        f"Alert price: ${alert_price:.8f}\n"
        f"Target entry: ${target:.8f} (-{pct}%)\n"
        f"Timeout: {timeout_m:.0f} min | Fallback: {fallback_mode()}",
    )

    deadline = time.time() + timeout_m * 60
    entry_price = None

    try:
        while time.time() < deadline:
            await asyncio.sleep(interval)
            current = await _poll_price(token_addr, chain)
            if current is None:
                continue

            if current <= target:
                entry_price = current
                log.info(
                    "pullback: %s hit target $%.8f (current $%.8f)",
                    symbol, target, current,
                )
                break
    finally:
        _active.pop(token_addr, None)

    # ── Decision ─────────────────────────────────────────────────────────
    if entry_price is not None:
        # Dip hit — execute at better price
        pair_copy = dict(pair)
        pair_copy["price_usd"] = entry_price
        improvement = ((alert_price - entry_price) / alert_price) * 100
        log.info("pullback: executing %s at $%.8f (%.1f%% better)", symbol, entry_price, improvement)

        await _immediate_execute(
            tg_client, pair_copy, result, alert_id, score, flags,
            prefix=f"[PULLBACK ENTRY -{improvement:.1f}%] ",
        )
    elif fallback_mode() == "market":
        # Timeout + market fallback — execute at current price
        current = await _poll_price(token_addr, chain)
        if current and current > 0:
            pair_copy = dict(pair)
            pair_copy["price_usd"] = current
            log.info("pullback: timeout, fallback=market, executing %s at $%.8f", symbol, current)
            await _immediate_execute(
                tg_client, pair_copy, result, alert_id, score, flags,
                prefix="[PULLBACK TIMEOUT — market entry] ",
            )
        else:
            log.warning("pullback: timeout, fallback=market but price unavailable for %s", symbol)
            update_alert_decision(alert_id, "pending")
            await send_message(
                tg_client,
                f"Pullback timeout for {symbol} — price unavailable, skipped.",
            )
    else:
        # Timeout + skip fallback
        log.info("pullback: timeout, fallback=skip for %s", symbol)
        update_alert_decision(alert_id, "pending")
        text, position = build_approval_message(pair, score, flags, result)
        text = f"[PULLBACK TIMEOUT — no dip, manual approval]\n\n{text}"
        keyboard = build_buy_keyboard(alert_id, position)
        await send_message(tg_client, text, reply_markup=keyboard)


async def _immediate_execute(
    tg_client, pair, result, alert_id, score, flags, prefix=""
):
    """Execute trade immediately (shared by pullback hit and direct mode)."""
    from scanner import (
        send_message, update_alert_decision, record_position,
        build_auto_buy_message, build_approval_message, build_buy_keyboard,
    )
    trade_ok = False
    try:
        from exchange_executor import execute_trade
        trade_ok = execute_trade(
            chain=pair["chain"],
            token_addr=pair["token_addr"],
            symbol=pair.get("symbol", "UNKNOWN"),
            position_usd=result.position_usd,
            price_usd=pair.get("price_usd", 0.0),
        )
    except Exception as exc:
        log.error("exchange_executor failed: %s", exc)

    if not trade_ok:
        update_alert_decision(alert_id, "pending")
        text, position = build_approval_message(pair, score, flags, result)
        text = f"{prefix}[Auto-exec attempted but FAILED — manual approval needed]\n\n{text}"
        keyboard = build_buy_keyboard(alert_id, position)
        await send_message(tg_client, text, reply_markup=keyboard)
    else:
        record_position(pair, result, alert_id)
        msg = build_auto_buy_message(pair, score, flags, result)
        if prefix:
            msg = prefix + msg
        await send_message(tg_client, msg)


def get_active_pullbacks() -> list[dict]:
    """Return list of currently monitored pullbacks (for /status command)."""
    now = time.time()
    return [
        {
            "symbol": v["symbol"],
            "alert_price": v["alert_price"],
            "target": v["target"],
            "elapsed_sec": int(now - v["started"]),
        }
        for v in _active.values()
    ]
