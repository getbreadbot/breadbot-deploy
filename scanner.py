#!/usr/bin/env python3
"""
scanner.py — Core token scanner for Breadbot.

Runs two concurrent async tasks:
  1. scan_loop()       — polls DEXScreener every 5 min for new Solana/Base pairs,
                         runs GoPlus security checks, calls auto_executor.evaluate(),
                         and dispatches Telegram alerts.
  2. telegram_poller() — long-polls Telegram for Buy/Skip callback queries so the
                         inline keyboard on manual-approval alerts works without a
                         separate webhook server.

Decision flow per token:
  executor.evaluate() -> result.blocked  -> log silently; notify if bot is paused
                      -> result.executed -> log decision='auto_buy', send notice
                      -> not executed   -> log decision='pending', send approval msg
"""

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from broadcaster import broadcast_alert

load_dotenv(Path(__file__).parent / ".env")

from config import (
    DB_PATH,
    GOPLUS_API_KEY,
    MIN_LIQUIDITY_USD,
    MIN_VOLUME_24H_USD,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from auto_executor import AutoExecutor
from yield_rebalancer import handle_rebalance_command, handle_rebalance_callback
from pendle_connector import handle_pendle_command
from robinhood_connector import get_account as rh_get_account, get_crypto_positions as rh_get_positions
from grid_engine import GridEngine, handle_grid_command
from funding_arb_engine import FundingArbEngine, handle_funding_command
from alt_data_signals import (
    get_cached_composite,
    handle_signals_command,
    handle_feargreed_command,
)
from social_signals import get_social_score_boost, handle_alpha_command, handle_channels_command
from axiom_signals import get_axiom_score_boost, drain_new_token_queue

# Engine singletons — shared across poller and main loop
_grid_engine    = GridEngine()
_funding_engine = FundingArbEngine()


# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Globals ───────────────────────────────────────────────────────────────────
executor      = AutoExecutor()
_seen_tokens: set[str] = set()  # in-memory dedup — avoids re-alerting same token
_tg_offset    = 0               # Telegram getUpdates offset

SCAN_INTERVAL     = 300   # seconds between scans (5 minutes)
TG_POLL_INTERVAL  = 3     # seconds between Telegram callback polls
DEDUP_HOURS       = 6     # window: don't re-alert same token address

DEXSCREENER_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_TOKENS   = "https://api.dexscreener.com/latest/dex/tokens/{addr}"
GOPLUS_EVM           = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={addr}"
GOPLUS_SOL           = "https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={addr}"
TELEGRAM_BASE        = "https://api.telegram.org/bot{token}/{method}"


# ── DB helpers ────────────────────────────────────────────────────────────────

def db_rw():
    """Return a read-write sqlite3 connection with row_factory set."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def is_already_alerted(token_addr: str) -> bool:
    """True if this address appeared in meme_alerts within DEDUP_HOURS."""
    if token_addr in _seen_tokens:
        return True
    if not DB_PATH.exists():
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=DEDUP_HOURS)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        row  = conn.execute(
            "SELECT id FROM meme_alerts WHERE token_addr=? AND created_at>?",
            (token_addr, cutoff),
        ).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


def log_alert_to_db(pair: dict, score: int, flags: list[str], decision: str) -> int:
    """Insert a row into meme_alerts. Returns the new row id."""
    conn = db_rw()
    try:
        cur = conn.execute(
            """
            INSERT INTO meme_alerts
              (chain, token_addr, token_name, symbol, price_usd,
               liquidity, volume_24h, mcap, rug_score, rug_flags,
               alert_sent, decision, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
            """,
            (
                pair["chain"],
                pair["token_addr"],
                pair.get("token_name", ""),
                pair.get("symbol", ""),
                pair.get("price_usd", 0.0),
                pair.get("liquidity", 0.0),
                pair.get("volume_24h", 0.0),
                pair.get("mcap", 0.0),
                score,
                json.dumps(flags),
                1,
                decision,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_alert_decision(alert_id: int, decision: str) -> None:
    """Update the decision field on an existing meme_alerts row."""
    conn = db_rw()
    try:
        conn.execute(
            "UPDATE meme_alerts SET decision=? WHERE id=?", (decision, alert_id)
        )
        conn.commit()
    finally:
        conn.close()


def db_get_config(key: str) -> str:
    """Read a single value from the bot_config table."""
    if not DB_PATH.exists():
        return ""
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        row  = conn.execute(
            "SELECT value FROM bot_config WHERE key=?", (key,)
        ).fetchone()
        conn.close()
        return row[0].strip() if row and row[0] else ""
    except Exception:
        return ""


def db_clear_force_scan() -> None:
    """Clear the force_scan flag so the dashboard button doesn't loop."""
    conn = db_rw()
    try:
        conn.execute(
            "UPDATE bot_config SET value='0' WHERE key='force_scan'"
        )
        conn.commit()
    finally:
        conn.close()


# ── Telegram helpers ──────────────────────────────────────────────────────────

async def tg_call(client: httpx.AsyncClient, method: str, **payload) -> dict:
    """Call the Telegram Bot API and return the JSON response."""
    url = TELEGRAM_BASE.format(token=TELEGRAM_BOT_TOKEN, method=method)
    try:
        r = await client.post(url, json=payload, timeout=10)
        return r.json()
    except Exception as exc:
        log.error("Telegram API error (%s): %s", method, exc)
        return {}


async def send_message(
    client: httpx.AsyncClient, text: str, reply_markup: dict | None = None
) -> dict:
    kwargs: dict = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        kwargs["reply_markup"] = reply_markup
    return await tg_call(client, "sendMessage", **kwargs)


# ── Alert message formatters ──────────────────────────────────────────────────

def _chain_label(chain: str) -> str:
    return "Solana" if chain == "solana" else "Base"


def _flags_text(flags: list[str]) -> str:
    return "\n".join(f"  {f}" for f in flags) if flags else "  None"


def _fallback_position(score: int) -> float:
    """Estimate position size for manual mode (executor returns 0.0)."""
    portfolio = float(os.getenv("TOTAL_PORTFOLIO_USD", "5000"))
    max_pct   = float(os.getenv("MAX_POSITION_SIZE_PCT", "0.02"))
    return round(portfolio * max_pct * (0.5 + 0.5 * score / 100), 2)


def build_auto_buy_message(pair: dict, score: int, flags: list[str], result) -> str:
    return (
        f"AUTO-EXECUTED [{result.strategy.upper()}]\n\n"
        f"{_chain_label(pair['chain'])} | {pair.get('symbol','?')} "
        f"- {pair.get('token_name','')}\n"
        f"{pair['token_addr']}\n\n"
        f"Price:       ${pair.get('price_usd', 0):.8f}\n"
        f"Liquidity:   ${pair.get('liquidity', 0):,.0f}\n"
        f"Volume 24h:  ${pair.get('volume_24h', 0):,.0f}\n"
        f"Market cap:  ${pair.get('mcap', 0):,.0f}\n"
        f"Score:       {score}/100\n\n"
        f"Flags:\n{_flags_text(flags)}\n\n"
        f"Executed: ${result.position_usd:.2f}\n"
        f"{result.reason}"
    )


def build_approval_message(pair: dict, score: int, flags: list[str], result) -> tuple[str, float]:
    """Returns (message_text, recommended_position_usd)."""
    position = result.position_usd if result.position_usd > 0 else _fallback_position(score)
    text = (
        f"NEW ALERT\n\n"
        f"{_chain_label(pair['chain'])} | {pair.get('symbol','?')} "
        f"- {pair.get('token_name','')}\n"
        f"{pair['token_addr']}\n\n"
        f"Price:       ${pair.get('price_usd', 0):.8f}\n"
        f"Liquidity:   ${pair.get('liquidity', 0):,.0f}\n"
        f"Volume 24h:  ${pair.get('volume_24h', 0):,.0f}\n"
        f"Market cap:  ${pair.get('mcap', 0):,.0f}\n"
        f"Score:       {score}/100\n\n"
        f"Flags:\n{_flags_text(flags)}\n\n"
        f"{result.reason}"
    )
    return text, position


def build_buy_keyboard(alert_id: int, position_usd: float) -> dict:
    return {
        "inline_keyboard": [[
            {"text": f"BUY ${position_usd:.2f}", "callback_data": f"buy_{alert_id}"},
            {"text": "Skip",                     "callback_data": f"skip_{alert_id}"},
        ]]
    }


# ── GoPlus security check ─────────────────────────────────────────────────────

async def check_token_security(
    client: httpx.AsyncClient, chain: str, token_addr: str
) -> tuple[int, list[str]]:
    """
    Query GoPlus and return a (score, flags) tuple.
    Score starts at 100 and deductions are applied per risk factor.
    Returns (50, ['GoPlus unavailable']) on network failure so the token
    can still be forwarded for manual review rather than silently dropped.
    """
    headers = {"Authorization": GOPLUS_API_KEY} if GOPLUS_API_KEY else {}
    try:
        if chain == "solana":
            url  = GOPLUS_SOL.format(addr=token_addr)
            r    = await client.get(url, headers=headers, timeout=12)
            data = r.json().get("result", {})
            info = data.get(token_addr) or data.get(token_addr.lower()) or {}
        else:
            url  = GOPLUS_EVM.format(chain_id="8453", addr=token_addr.lower())
            r    = await client.get(url, headers=headers, timeout=12)
            data = r.json().get("result", {})
            info = data.get(token_addr.lower()) or {}
    except Exception as exc:
        log.warning("GoPlus unavailable for %s: %s", token_addr[:12], exc)
        return 50, ["GoPlus check unavailable — review manually"]

    if not info:
        return 50, ["No security data found"]

    score  = 100
    flags: list[str] = []

    def deduct(points: int, label: str) -> None:
        nonlocal score
        score -= points
        flags.append(label)

    def bonus(points: int, label: str) -> None:
        nonlocal score
        score += points
        flags.append(f"+{points} {label}")

    # Honeypot — most severe risk
    if str(info.get("is_honeypot", "0")) == "1":
        deduct(40, "Honeypot detected")

    # Sell tax — gradient scoring
    try:
        st = float(info.get("sell_tax") or 0)
        if st > 0.10:
            deduct(30, f"Sell tax {st*100:.1f}%")
        elif st > 0.05:
            deduct(15, f"Sell tax {st*100:.1f}%")
        elif st > 0.01:
            pass  # 1–5%: neutral
        elif st == 0:
            bonus(2, "Zero sell tax")
        else:
            bonus(1, f"Low sell tax {st*100:.1f}%")
    except (ValueError, TypeError):
        pass

    # Buy tax
    try:
        bt = float(info.get("buy_tax") or 0)
        if bt > 0.10:
            deduct(20, f"Buy tax {bt*100:.1f}%")
        elif bt > 0.05:
            deduct(10, f"Buy tax {bt*100:.1f}%")
    except (ValueError, TypeError):
        pass

    if str(info.get("is_mintable",            "0")) == "1": deduct(20, "Owner can mint")
    if str(info.get("owner_change_balance",    "0")) == "1": deduct(30, "Owner can change balances")
    if str(info.get("can_take_back_ownership", "0")) == "1": deduct(10, "Ownership reclaim function")
    if str(info.get("is_proxy",               "0")) == "1": deduct(10, "Proxy contract")
    if str(info.get("external_call",          "0")) == "1": deduct(10, "External call in transfer")
    if str(info.get("trading_cooldown",       "0")) == "1": deduct(10, "Trading cooldown")
    if str(info.get("is_blacklisted",         "0")) == "1": deduct(15, "Blacklist function")
    if str(info.get("transfer_pausable",      "0")) == "1": deduct(15, "Transfers can be paused")

    # Holder analysis — count + concentration + creator exposure
    try:
        holders = info.get("holders", []) or []
        num_holders = len(holders)

        # Holder count signal
        if num_holders >= 500:
            bonus(5, f"{num_holders} holders")
        elif num_holders >= 200:
            bonus(3, f"{num_holders} holders")
        elif num_holders >= 100:
            bonus(1, f"{num_holders} holders")
        elif num_holders < 50 and num_holders > 0:
            deduct(5, f"Only {num_holders} holders")
        elif num_holders < 20 and num_holders > 0:
            deduct(10, f"Only {num_holders} holders")

        if holders:
            top10_pct = sum(float(h.get("percent") or 0) for h in holders[:10])

            # Concentration — 4 bands (was 2)
            if top10_pct > 0.70:
                deduct(20, f"Top 10 hold {top10_pct*100:.0f}% of supply")
            elif top10_pct > 0.55:
                deduct(15, f"Top 10 hold {top10_pct*100:.0f}% of supply")
            elif top10_pct > 0.40:
                deduct(10, f"Top 10 hold {top10_pct*100:.0f}% of supply")
            elif top10_pct > 0.30:
                deduct(5, f"Top 10 hold {top10_pct*100:.0f}% of supply")

            # Creator/owner still holding check
            creator_pct = 0.0
            for h in holders:
                tag = str(h.get("tag") or "").lower()
                if "creator" in tag or "owner" in tag or "deployer" in tag:
                    try:
                        creator_pct += float(h.get("percent") or 0)
                    except (ValueError, TypeError):
                        pass
            if creator_pct > 0.10:
                deduct(10, f"Creator holds {creator_pct*100:.0f}%")
            elif creator_pct > 0.05:
                deduct(5, f"Creator holds {creator_pct*100:.0f}%")
    except Exception:
        pass

    # Liquidity lock
    if str(info.get("lp_locked", "0")) not in ("1", "true"):
        deduct(10, "Liquidity not locked")

    # Open source (EVM only)
    if chain != "solana" and str(info.get("is_open_source", "0")) != "1":
        deduct(5, "Contract not open source")

    return max(0, score), flags


# ── DEXScreener fetchers ──────────────────────────────────────────────────────

async def fetch_new_pairs(client: httpx.AsyncClient) -> list[dict]:
    """
    Pull the latest token profiles from DEXScreener, then fetch live pair
    data for each Solana / Base token that clears our liquidity + volume floors.
    Returns a list of normalized pair dicts ready for security scoring.
    """
    try:
        r        = await client.get(DEXSCREENER_PROFILES, timeout=15)
        profiles = r.json() if r.status_code == 200 else []
    except Exception as exc:
        log.error("DEXScreener profiles error: %s", exc)
        return []

    pairs: list[dict] = []
    for profile in profiles:
        chain = profile.get("chainId", "").lower()
        if chain not in ("solana", "base"):
            continue
        addr = profile.get("tokenAddress", "").strip()
        if not addr or is_already_alerted(addr):
            continue
        # Reserve immediately — prevents duplicate profiles in the same
        # payload from both passing is_already_alerted() before process_pair runs.
        _seen_tokens.add(addr)
        detail = await _fetch_pair_detail(client, chain, addr)
        if detail:
            pairs.append(detail)
        await asyncio.sleep(0.25)  # gentle rate-limit courtesy

    # Supplement with tokens discovered via Axiom stream (if cookie configured)
    axiom_tokens = await drain_new_token_queue()
    for tok in axiom_tokens:
        addr = tok.get("token_addr", "").strip()
        if not addr or is_already_alerted(addr):
            continue
        _seen_tokens.add(addr)
        chain = tok.get("chain", "solana")
        detail = await _fetch_pair_detail(client, chain, addr)
        if detail:
            # Inject Axiom metadata as hints so scorer can use them
            detail["_axiom_queued"] = True
            pairs.append(detail)
        await asyncio.sleep(0.25)

    return pairs


async def _fetch_pair_detail(
    client: httpx.AsyncClient, chain: str, token_addr: str
) -> dict | None:
    """
    Fetch /latest/dex/tokens/{addr} and return a normalized dict for the
    highest-liquidity pair on the requested chain, or None if thresholds fail.
    """
    try:
        r    = await client.get(DEXSCREENER_TOKENS.format(addr=token_addr), timeout=10)
        pool = (r.json().get("pairs") or []) if r.status_code == 200 else []
    except Exception as exc:
        log.warning("DEXScreener token fetch error %s: %s", token_addr[:12], exc)
        return None

    candidates = [p for p in pool if p.get("chainId", "").lower() == chain]
    if not candidates:
        return None

    # Pick the pair with the highest USD liquidity
    best      = max(candidates, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
    liquidity = float((best.get("liquidity") or {}).get("usd") or 0)
    volume_24 = float((best.get("volume") or {}).get("h24") or 0)

    if liquidity < MIN_LIQUIDITY_USD or volume_24 < MIN_VOLUME_24H_USD:
        return None

    mcap = float(best.get("marketCap") or 0) or float(best.get("fdv") or 0)

    # Pair age in hours — used for informational display only
    age_h: float = 0.0
    try:
        created_ms = best.get("pairCreatedAt")
        if created_ms:
            created = datetime.fromtimestamp(int(created_ms) / 1000, tz=timezone.utc)
            age_h   = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    except Exception:
        pass

    return {
        "chain":       chain,
        "token_addr":  token_addr,
        "token_name":  (best.get("baseToken") or {}).get("name", ""),
        "symbol":      (best.get("baseToken") or {}).get("symbol", ""),
        "price_usd":   float(best.get("priceUsd") or 0),
        "liquidity":   liquidity,
        "volume_24h":  volume_24,
        "mcap":        mcap,
        "pair_addr":       best.get("pairAddress", ""),
        "age_hours":       round(age_h, 1),
        "price_change_m5": float((best.get("priceChange") or {}).get("m5") or 0),
        "price_change_h1": float((best.get("priceChange") or {}).get("h1") or 0),
        "txns_m5_buys":    int((best.get("txns") or {}).get("m5", {}).get("buys") or 0),
        "txns_m5_sells":   int((best.get("txns") or {}).get("m5", {}).get("sells") or 0),
        "txns_h1_buys":    int((best.get("txns") or {}).get("h1", {}).get("buys") or 0),
        "txns_h1_sells":   int((best.get("txns") or {}).get("h1", {}).get("sells") or 0),
    }


# ── Process one pair ──────────────────────────────────────────────────────────

async def process_pair(client: httpx.AsyncClient, pair: dict) -> None:
    """
    Full pipeline for a single token pair:
      1. GoPlus security check  -> score + flags
      2. auto_executor.evaluate()  -> ExecutionResult
      3. Write to meme_alerts with the correct decision label
      4. Dispatch Telegram message (auto-buy notice or manual-approval keyboard)
    """
    token_addr = pair["token_addr"]
    symbol     = pair.get("symbol", "UNKNOWN")
    chain      = pair["chain"]

    # Hard dedup gate — catches any token that slipped through fetch_new_pairs
    # (e.g. same tokenAddress appearing twice in a DEXScreener profiles payload)
    if is_already_alerted(token_addr):
        log.debug("  Dedup: skipping already-alerted %s (%s)", symbol, token_addr[:12])
        return
    # Reserve immediately so any duplicate in the pairs list is blocked.
    _seen_tokens.add(token_addr)

    log.info("Processing %s (%s) %s...", symbol, chain, token_addr[:12])

    # 1. Security check
    score, flags = await check_token_security(client, chain, token_addr)

    # Alt data composite signal adjustment
    composite = get_cached_composite()
    if composite is not None:
        if composite > 30:
            score = min(100, score + 3)
        elif composite < -30:
            score = max(0, score - 3)

    # Market-structure signals (DEXScreener data already in pair dict)
    age_h    = float(pair.get("age_hours", 0) or 0)
    liq_usd  = float(pair.get("liquidity", 0) or 0)
    vol_24h  = float(pair.get("volume_24h", 0) or 0)

    # Token age — younger = higher risk
    if age_h < 1:
        score = max(0, score - 8)
        flags.append("Token < 1h old")
    elif age_h < 3:
        score = max(0, score - 5)
        flags.append(f"Token {age_h:.1f}h old")
    elif age_h < 12:
        score = max(0, score - 2)
        flags.append(f"Token {age_h:.1f}h old")
    elif age_h > 168:
        score = min(100, score + 3)
        flags.append(f"+3 Survived {int(age_h/24)}d")

    # Liquidity depth bonus
    if liq_usd >= 100_000:
        score = min(100, score + 5)
        flags.append(f"+5 Liquidity ${liq_usd/1000:.0f}k")
    elif liq_usd >= 50_000:
        score = min(100, score + 3)
        flags.append(f"+3 Liquidity ${liq_usd/1000:.0f}k")
    elif liq_usd >= 25_000:
        score = min(100, score + 1)
        flags.append(f"+1 Liquidity ${liq_usd/1000:.0f}k")

    # Volume / liquidity ratio — measures activity vs pool size
    if liq_usd > 0:
        vl_ratio = vol_24h / liq_usd
        if vl_ratio >= 10:
            score = min(100, score + 5)
            flags.append(f"+5 Vol/Liq {vl_ratio:.1f}x")
        elif vl_ratio >= 5:
            score = min(100, score + 3)
            flags.append(f"+3 Vol/Liq {vl_ratio:.1f}x")
        elif vl_ratio >= 2:
            score = min(100, score + 1)
            flags.append(f"+1 Vol/Liq {vl_ratio:.1f}x")
        elif vl_ratio < 0.5:
            score = max(0, score - 5)
            flags.append(f"Low Vol/Liq {vl_ratio:.2f}x")
        elif vl_ratio < 1:
            score = max(0, score - 3)
            flags.append(f"Low Vol/Liq {vl_ratio:.2f}x")

    # Momentum scoring — price velocity and buy pressure
    pc_m5    = pair.get('price_change_m5', 0.0)
    pc_h1    = pair.get('price_change_h1', 0.0)
    m5_buys  = pair.get('txns_m5_buys',  0)
    m5_sells = pair.get('txns_m5_sells', 0)
    h1_buys  = pair.get('txns_h1_buys',  0)

    if pc_m5 >= 20:
        score = min(100, score + 6); flags.append(f'+6 Price +{pc_m5:.0f}% (5m)')
    elif pc_m5 >= 10:
        score = min(100, score + 4); flags.append(f'+4 Price +{pc_m5:.0f}% (5m)')
    elif pc_m5 >= 5:
        score = min(100, score + 2); flags.append(f'+2 Price +{pc_m5:.0f}% (5m)')
    elif pc_m5 <= -15:
        score = max(0, score - 5); flags.append(f'Price {pc_m5:.0f}% (5m)')
    elif pc_m5 <= -5:
        score = max(0, score - 2); flags.append(f'Price {pc_m5:.0f}% (5m)')

    # h1 velocity: extreme pumps are late entries — hard skip or heavy penalty
    max_h1_pump = float(os.environ.get("MAX_H1_PUMP_PCT", "150"))
    if pc_h1 >= max_h1_pump:
        log.info("  Dropped %s: h1 pump +%.0f%% exceeds ceiling %.0f%%", symbol, pc_h1, max_h1_pump)
        _seen_tokens.add(token_addr)
        return
    elif pc_h1 >= 200:
        score = max(0, score - 20); flags.append(f'Already pumped +{pc_h1:.0f}% (1h)')
    elif pc_h1 >= 100:
        score = max(0, score - 12); flags.append(f'Already pumped +{pc_h1:.0f}% (1h)')
    elif pc_h1 >= 50:
        score = min(100, score + 3); flags.append(f'+3 Price +{pc_h1:.0f}% (1h)')
    elif pc_h1 >= 20:
        score = min(100, score + 2); flags.append(f'+2 Price +{pc_h1:.0f}% (1h)')
    elif pc_h1 >= 10:
        score = min(100, score + 1); flags.append(f'+1 Price +{pc_h1:.0f}% (1h)')
    elif pc_h1 <= -30:
        score = max(0, score - 6); flags.append(f'Price {pc_h1:.0f}% (1h)')
    elif pc_h1 <= -10:
        score = max(0, score - 3); flags.append(f'Price {pc_h1:.0f}% (1h)')

    m5_total = m5_buys + m5_sells
    if m5_total >= 5:
        m5_ratio = m5_buys / m5_total
        if m5_ratio >= 0.80:
            score = min(100, score + 5); flags.append(f'+5 Buy pressure {m5_ratio*100:.0f}% (5m)')
        elif m5_ratio >= 0.65:
            score = min(100, score + 3); flags.append(f'+3 Buy pressure {m5_ratio*100:.0f}% (5m)')
        elif m5_ratio >= 0.55:
            score = min(100, score + 1); flags.append(f'+1 Buy pressure {m5_ratio*100:.0f}% (5m)')
        elif m5_ratio <= 0.30:
            score = max(0, score - 4); flags.append(f'Sell pressure {(1-m5_ratio)*100:.0f}% (5m)')

    if h1_buys >= 200:
        score = min(100, score + 4); flags.append(f'+4 Buy velocity {h1_buys} txns (1h)')
    elif h1_buys >= 100:
        score = min(100, score + 2); flags.append(f'+2 Buy velocity {h1_buys} txns (1h)')
    elif h1_buys >= 50:
        score = min(100, score + 1); flags.append(f'+1 Buy velocity {h1_buys} txns (1h)')

    # Social signals — Arkham + alpha channel boost
    social_boost, social_flags = await get_social_score_boost(token_addr, chain, client)
    if social_boost:
        score = min(100, score + social_boost)
        flags += social_flags

    # Axiom signals — DEXScreener boost + Axiom stream
    axiom_boost, axiom_flags = await get_axiom_score_boost(token_addr, client)
    if axiom_boost:
        score = min(100, score + axiom_boost)
        flags += axiom_flags

    # Hard drop: score below minimum — don't alert at all
    if score < 50:
        log.info("  Dropped %s: score %d < 50", symbol, score)
        _seen_tokens.add(token_addr)
        return

    # 2. Executor evaluation
    alert_dict = {
        "score":      score,
        "market_cap": pair.get("mcap", 0),
        "token":      symbol,
        "chain":      chain,
        "price":      pair.get("price_usd", 0),
    }
    result = executor.evaluate(alert_dict)

    # 3. Determine decision label for DB
    if result.blocked:
        decision = "pending"   # keep for manual review when bot resumes
    elif result.executed:
        decision = "auto_buy"
    else:
        decision = "pending"

    # 4. Log to DB and mark token as seen
    alert_id = log_alert_to_db(pair, score, flags, decision)
    _seen_tokens.add(token_addr)

    log.info("  Logged alert_id=%d decision=%s score=%d", alert_id, decision, score)

    # 5. Telegram dispatch
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("  Telegram not configured — DB only")
        return

    if result.blocked:
        # Only surface to Telegram if the bot is explicitly paused; daily-cap
        # blocks are informational and would create noise.
        if "paused" in result.reason.lower():
            await send_message(
                client,
                f"Alert received while paused: {symbol} ({chain.upper()})\n"
                f"Score {score}/100  |  mcap ${pair.get('mcap',0):,.0f}\n"
                f"{result.reason}",
            )
        return

    if result.executed:
        # Attempt actual exchange execution
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
            log.error("exchange_executor import or call failed: %s", exc)

        # Telegram notice — adjust message if execution failed
        if not trade_ok:
            # Execution failed — downgrade to manual approval so operator can act
            update_alert_decision(alert_id, "pending")
            text, position = build_approval_message(pair, score, flags, result)
            text = f"[Auto-exec attempted but FAILED — manual approval needed]\n\n{text}"
            keyboard = build_buy_keyboard(alert_id, position)
            await send_message(client, text, reply_markup=keyboard)
        else:
            msg = build_auto_buy_message(pair, score, flags, result)
            await send_message(client, msg)
    else:
        text, position = build_approval_message(pair, score, flags, result)
        keyboard       = build_buy_keyboard(alert_id, position)
        await send_message(client, text, reply_markup=keyboard)

    # Alpha broadcast — fire-and-forget to all registered buyers
    # Runs only when an alert is not blocked (blocked paths return early above).
    asyncio.create_task(broadcast_alert(client, pair, score, flags))


# ── Telegram callback poller ──────────────────────────────────────────────────

async def telegram_poller(client: httpx.AsyncClient) -> None:
    """
    Polls Telegram getUpdates for callback_query events (Buy / Skip button presses).
    Updates the meme_alerts row with the user's decision and edits the original
    message so the inline keyboard disappears after it's been acted on.
    """
    global _tg_offset
    if not TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN not set — callback polling disabled")
        return

    log.info("Telegram callback poller started")
    while True:
        try:
            resp    = await tg_call(
                client, "getUpdates",
                offset=_tg_offset, timeout=2,
                allowed_updates=["callback_query", "message"],
            )
            updates = resp.get("result") or []
            for upd in updates:
                _tg_offset = upd["update_id"] + 1
                await _handle_callback(client, upd.get("callback_query"))
                await _handle_message(client, upd.get("message"))
        except Exception as exc:
            log.error("Telegram poller error: %s", exc)
        await asyncio.sleep(TG_POLL_INTERVAL)


async def _handle_callback(client: httpx.AsyncClient, cb: dict | None) -> None:
    """Process a single inline keyboard callback (buy_N or skip_N)."""
    if not cb:
        return
    # Security: only process callbacks from the authorised chat
    _cb_chat_id = str((cb.get("message") or {}).get("chat", {}).get("id", ""))
    if TELEGRAM_CHAT_ID and _cb_chat_id != str(TELEGRAM_CHAT_ID):
        return
    cb_id    = cb.get("id")
    data     = cb.get("data", "")
    msg      = cb.get("message") or {}
    msg_id   = msg.get("message_id")

    if data.startswith("buy_"):
        alert_id = int(data.split("_", 1)[1])
        update_alert_decision(alert_id, "buy")
        reply = "Trade marked as BUY. Place your order on the exchange."
        log.info("Manual BUY decision recorded for alert_id=%d", alert_id)
    elif data.startswith("skip_"):
        alert_id = int(data.split("_", 1)[1])
        update_alert_decision(alert_id, "skip")
        reply = "Alert skipped and logged."
        log.info("Manual SKIP decision recorded for alert_id=%d", alert_id)
    elif data.startswith("rebalance_"):
        await handle_rebalance_callback(client, data, cb_id)
        return
    else:
        return

    # Acknowledge the callback to dismiss the loading spinner on the button
    await tg_call(client, "answerCallbackQuery", callback_query_id=cb_id)

    # Replace the original message text (removes the keyboard)
    if msg_id and TELEGRAM_CHAT_ID:
        await tg_call(
            client, "editMessageText",
            chat_id=TELEGRAM_CHAT_ID,
            message_id=msg_id,
            text=reply,
            parse_mode="HTML",
        )




async def _handle_message(client: httpx.AsyncClient, msg: dict | None) -> None:
    """Route incoming /command text messages from the bot owner."""
    if not msg:
        return
    # Security: only process messages from the authorised chat
    _msg_chat_id = str((msg.get("chat") or {}).get("id", ""))
    if TELEGRAM_CHAT_ID and _msg_chat_id != str(TELEGRAM_CHAT_ID):
        return
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    parts = text.lstrip("/").split(None, 1)
    cmd   = parts[0].lower()
    args  = parts[1] if len(parts) > 1 else ""
    if cmd == "rebalance":
        await handle_rebalance_command(client, args)
    elif cmd == "pendle":
        await handle_pendle_command(client, args)
    elif cmd == "grid":
        await handle_grid_command(client, args, _grid_engine)
    elif cmd == "funding":
        await handle_funding_command(client, args, _funding_engine)
    elif cmd == "signals":
        await handle_signals_command(client)
    elif cmd == "feargreed":
        await handle_feargreed_command(client)
    elif cmd == "automode":
        await handle_automode_command(client, args)
    elif cmd == "alpha":
        await handle_alpha_command(client, send_message)
    elif cmd == "channels":
        parts = (cmd + (" " + args if args else "")).split()
        await handle_channels_command(parts, send_message)
    elif cmd == "robinhood":
        await handle_robinhood_command(client)
    elif cmd == "status":
        await handle_status_command(client)

async def handle_automode_command(client: httpx.AsyncClient, args: str) -> None:
    """Handle /automode on|off — toggle auto-execution mode at runtime."""
    arg = args.strip().lower()
    if arg not in ("on", "off"):
        await send_message(client, "Usage: /automode on  or  /automode off")
        return
    # Write to bot_config table (DB-first config pattern used by AutoExecutor)
    conn = db_rw()
    try:
        conn.execute(
            """INSERT INTO bot_config (key, value, updated_at)
               VALUES ('execution_mode', ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
               updated_at=excluded.updated_at""",
            ("auto" if arg == "on" else "manual",)
        )
        conn.commit()
    finally:
        conn.close()
    mode_label = "AUTO" if arg == "on" else "MANUAL"
    await send_message(
        client,
        f"Execution mode set to <b>{mode_label}</b>.\n\n"
        f"{'Bot will auto-execute alerts that pass strategy thresholds.' if arg == 'on' else 'Bot will send alerts for manual approval.'}"
    )
    log.info("Execution mode changed to %s via Telegram", mode_label)


async def handle_status_command(client: httpx.AsyncClient) -> None:
    """Handle /status — show full bot state snapshot."""
    summary = executor.get_strategy_summary()

    from alt_data_signals import get_cached_composite, get_cached_fear_greed, _cache as _alt_cache

    composite = get_cached_composite()
    fg = get_cached_fear_greed()
    fg_label = _alt_cache.get("fg_label", "")

    paused_str  = "PAUSED" if summary["is_paused"] else "ACTIVE"
    mode_str    = summary["execution_mode"].upper()
    strat_str   = summary["strategy"].upper()
    trades_str  = f"{summary['trades_today']}/{summary['max_trades_per_day']}"
    loss_str    = "LIMIT HIT" if summary["daily_loss_exceeded"] else "OK"

    comp_str = f"{composite:+.0f}" if composite is not None else "N/A"
    fg_str   = f"{fg}/100 ({fg_label})" if fg is not None else "N/A"

    text = (
        f"<b>Breadbot Status</b>\n\n"
        f"State:         {paused_str}\n"
        f"Mode:          {mode_str}\n"
        f"Strategy:      {strat_str}\n"
        f"Trades today:  {trades_str}\n"
        f"Daily loss:    {loss_str}\n\n"
        f"<b>Alt Data Signals</b>\n"
        f"Composite:     {comp_str}\n"
        f"Fear/Greed:    {fg_str}\n"
    )
    await send_message(client, text)


# ── Main scan loop ────────────────────────────────────────────────────────────

async def scan_loop(client: httpx.AsyncClient) -> None:
    """
    Runs the DEXScreener poll cycle every SCAN_INTERVAL seconds.
    Also responds immediately to a force_scan flag written by the dashboard's
    /api/action/force-scan endpoint — clears the flag before scanning so a
    second force request queued while scanning fires on the next cycle.
    """
    log.info("Scanner loop started (interval=%ds)", SCAN_INTERVAL)
    while True:
        # Check for force-scan request from dashboard
        forced = db_get_config("force_scan") == "1"
        if forced:
            log.info("Force scan triggered from dashboard")
            db_clear_force_scan()

        log.info("--- Scan cycle starting ---")
        try:
            pairs = await fetch_new_pairs(client)
            log.info("Found %d qualifying pairs this cycle", len(pairs))
            for pair in pairs:
                await process_pair(client, pair)
        except Exception as exc:
            log.error("Scan cycle error: %s", exc)

        # Sleep shorter after a forced scan so normal rhythm resumes quickly
        await asyncio.sleep(60 if forced else SCAN_INTERVAL)


# ── Startup ───────────────────────────────────────────────────────────────────

async def _startup_notify(client: httpx.AsyncClient) -> None:
    """Send a startup message to Telegram to confirm the bot is live."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning(
            "Telegram not configured. "
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env to enable alerts."
        )
        return
    summary  = executor.get_strategy_summary()
    mode_str = summary["execution_mode"].upper()
    strat    = summary["strategy"]
    await send_message(
        client,
        f"Breadbot scanner started\n\n"
        f"Mode:      {mode_str}\n"
        f"Strategy:  {strat}\n"
        f"Min liq:   ${MIN_LIQUIDITY_USD:,.0f}\n"
        f"Min vol:   ${MIN_VOLUME_24H_USD:,.0f}\n\n"
        f"Scanning Solana + Base every 5 minutes.",
    )
    log.info("Startup notification sent (mode=%s strategy=%s)", mode_str, strat)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    async with httpx.AsyncClient() as client:
        await _startup_notify(client)
        await asyncio.gather(
            scan_loop(client),
            telegram_poller(client),
        )


if __name__ == "__main__":
    asyncio.run(main())
