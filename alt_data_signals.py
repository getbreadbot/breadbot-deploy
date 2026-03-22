#!/usr/bin/env python3
"""
alt_data_signals.py — Alternative data signal layer, Phase 1.

Sources (all unauthenticated, read-only):
  - Fear & Greed Index (alternative.me)
  - CoinGecko community sentiment (replaces SentiCrypt — TLS errors, service unreliable)
  - DefiLlama TVL (Solana + Base)
  - Kalshi prediction markets (Phase 2, off by default)

Public API:
  alt_data_loop()            — async loop, registered in main.py
  get_cached_composite()     — sync, returns -100..+100 or None
  get_cached_fear_greed()    — sync, returns 0..100 or None
  get_cached_recession_prob()— sync, returns 0.0..1.0 or None
  ensure_alt_data_table()    — DDL, called from main.py _init_db
  handle_signals_command()   — Telegram /signals
  handle_feargreed_command() — Telegram /feargreed
"""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from config import DB_PATH, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

log = logging.getLogger("alt_data")

# ── In-memory cache ──────────────────────────────────────────────────────────

_cache: dict = {
    "fear_greed": None, "fg_label": None,
    "coingecko_sentiment": None,       # CoinGecko sentiment_votes_up_percentage, normalised -1..+1
    "solana_tvl_now": None, "solana_tvl_7d_ago": None,
    "base_tvl_now": None, "base_tvl_7d_ago": None,
    "kalshi_btc_prob": None, "kalshi_eth_prob": None, "kalshi_sol_prob": None,
    "recession_prob": None, "fed_cut_prob": None,
    "coinalyze_btc_funding":  None,   # float, current 8h rate
    "coinalyze_eth_funding":  None,
    "coinalyze_sol_funding":  None,
    "coinalyze_btc_oi":       None,   # float USD
    "coinalyze_eth_oi":       None,
    "helius_sol_inflation":   None,   # float, e.g. 0.047
    "helius_sol_epoch":       None,   # int
    "composite": None, "composite_components": {},
    "last_updated": None, "last_error": None,
}

# ── Env helpers ──────────────────────────────────────────────────────────────

def _env_bool(key: str, default: str = "false") -> bool:
    return os.getenv(key, default).strip().lower() in ("true", "1", "yes")

def _env_float(key: str, default: str) -> float:
    try:
        return float(os.getenv(key, default))
    except (ValueError, TypeError):
        return float(default)

def _env_int(key: str, default: str) -> int:
    try:
        return int(os.getenv(key, default))
    except (ValueError, TypeError):
        return int(default)

# ── Phase 2 env vars ─────────────────────────────────────────────────────────

COINALYZE_ENABLED  = _env_bool("COINALYZE_ENABLED", "false")
COINALYZE_API_KEY  = os.getenv("COINALYZE_API_KEY", "")
COINALYZE_BASE_URL = "https://api.coinalyze.net/v1"
COINALYZE_PAIRS    = "BTCUSDT_PERP.A,ETHUSDT_PERP.A,SOLUSDT_PERP.A"

HELIUS_ENABLED  = _env_bool("HELIUS_ENABLED", "false")
HELIUS_API_KEY  = os.getenv("HELIUS_API_KEY", "")
# Extract Helius key from SOLANA_RPC_URL if HELIUS_API_KEY not set directly
if not HELIUS_API_KEY:
    _rpc = os.getenv("SOLANA_RPC_URL", "")
    if "api-key=" in _rpc:
        HELIUS_API_KEY = _rpc.split("api-key=")[-1].split("&")[0]
HELIUS_BASE_URL = "https://api.helius.xyz/v0"
SOLANA_RPC_URL_FOR_RPC = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

# ── Public cache accessors ───────────────────────────────────────────────────

def get_cached_composite() -> Optional[float]:
    """Return composite score -100..+100, or None if not yet computed."""
    return _cache["composite"]

def get_cached_fear_greed() -> Optional[int]:
    """Return Fear & Greed index 0..100, or None if not yet fetched."""
    return _cache["fear_greed"]

def get_cached_recession_prob() -> Optional[float]:
    """Return Kalshi recession probability 0.0..1.0, or None if disabled."""
    return _cache["recession_prob"]

def get_cached_signals() -> dict:
    """Return a snapshot of the current alt-data cache for the dashboard API."""
    return {
        "composite":             _cache.get("composite"),
        "composite_components":  _cache.get("composite_components", {}),
        "fear_greed":            _cache.get("fear_greed"),
        "fg_label":              _cache.get("fg_label"),
        "coingecko_sentiment":   _cache.get("coingecko_sentiment"),
        "kalshi_btc_prob":       _cache.get("kalshi_btc_prob"),
        "kalshi_eth_prob":       _cache.get("kalshi_eth_prob"),
        "kalshi_sol_prob":       _cache.get("kalshi_sol_prob"),
        "recession_prob":        _cache.get("recession_prob"),
        "coinalyze_btc_funding": _cache.get("coinalyze_btc_funding"),
        "coinalyze_eth_funding": _cache.get("coinalyze_eth_funding"),
        "coinalyze_sol_funding": _cache.get("coinalyze_sol_funding"),
        "coinalyze_btc_oi":      _cache.get("coinalyze_btc_oi"),
        "coinalyze_eth_oi":      _cache.get("coinalyze_eth_oi"),
        "solana_tvl_now":        _cache.get("solana_tvl_now"),
        "solana_tvl_7d_ago":     _cache.get("solana_tvl_7d_ago"),
        "base_tvl_now":          _cache.get("base_tvl_now"),
        "helius_sol_inflation":  _cache.get("helius_sol_inflation"),
        "helius_sol_epoch":      _cache.get("helius_sol_epoch"),
        "last_updated":          _cache.get("last_updated"),
        "last_error":            _cache.get("last_error"),
    }

# ── DB ───────────────────────────────────────────────────────────────────────

def ensure_alt_data_table() -> None:
    """Create the alt_data_signals table if it doesn't exist."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alt_data_signals (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp        TEXT    DEFAULT (datetime('now')),
                source           TEXT    NOT NULL,
                signal_type      TEXT    NOT NULL,
                market_id        TEXT,
                description      TEXT,
                value            REAL,
                value_shift      REAL,
                composite_score  REAL,
                scanner_triggered INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        log.info("alt_data_signals table ready")
    finally:
        conn.close()

def _db_write(source: str, signal_type: str, value: float,
              market_id: str = None, description: str = None,
              value_shift: float = None, composite_score: float = None) -> None:
    """Insert a row into alt_data_signals."""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.execute(
            """INSERT INTO alt_data_signals
               (source, signal_type, market_id, description, value, value_shift, composite_score)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (source, signal_type, market_id, description, value, value_shift, composite_score),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        log.error("DB write error: %s", exc)

# ── Fetchers ─────────────────────────────────────────────────────────────────

async def _fetch_fear_greed(client: httpx.AsyncClient) -> None:
    """Fetch Fear & Greed Index from alternative.me."""
    if not _env_bool("FEAR_GREED_ENABLED", "true"):
        return
    try:
        r = await client.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        data = r.json()["data"][0]
        val = int(data["value"])
        label = data["value_classification"]
        _cache["fear_greed"] = val
        _cache["fg_label"] = label
        _db_write("fear_greed", "index", float(val), description=label)
        log.info("Fear & Greed: %d (%s)", val, label)
    except Exception as exc:
        log.error("Fear & Greed fetch error: %s", exc)
        _cache["last_error"] = f"fear_greed: {exc}"

async def _fetch_coingecko_sentiment(client: httpx.AsyncClient) -> None:
    """Fetch BTC community sentiment from CoinGecko (free, no API key).

    Returns sentiment_votes_up_percentage normalised to -1.0..+1.0.
    Formula: (up_pct / 100) * 2 - 1.0
    At 79% bullish → +0.58; at 50% neutral → 0.0; at 30% → -0.40.
    """
    try:
        r = await client.get(
            "https://api.coingecko.com/api/v3/coins/bitcoin",
            params={
                "localization": "false",
                "tickers": "false",
                "market_data": "false",
                "community_data": "false",
                "developer_data": "false",
            },
            timeout=15,
        )
        data = r.json()
        up_pct = data.get("sentiment_votes_up_percentage")
        if up_pct is None:
            return
        normalised = (float(up_pct) / 100.0) * 2.0 - 1.0
        _cache["coingecko_sentiment"] = normalised
        _db_write("coingecko", "sentiment", normalised,
                  description=f"BTC community sentiment (up={up_pct:.1f}%)")
        log.info("CoinGecko BTC sentiment: %.3f (up=%.1f%%)", normalised, up_pct)
    except Exception as exc:
        log.warning("CoinGecko sentiment fetch error: %s", exc)

async def _fetch_defillama(client: httpx.AsyncClient) -> None:
    """Fetch TVL data from DefiLlama for Solana and Base."""
    if not _env_bool("DEFILLAMA_ENABLED", "true"):
        return
    for chain in ("solana", "base"):
        try:
            r = await client.get(
                f"https://api.llama.fi/v2/historicalChainTvl/{chain}", timeout=15
            )
            arr = r.json()
            tvl_now = float(arr[-1]["tvl"])
            tvl_7d = float(arr[-8]["tvl"]) if len(arr) >= 8 else tvl_now
            _cache[f"{chain}_tvl_now"] = tvl_now
            _cache[f"{chain}_tvl_7d_ago"] = tvl_7d
            pct = (tvl_now - tvl_7d) / tvl_7d if tvl_7d else 0
            _db_write("defillama", "tvl", tvl_now, market_id=chain,
                      description=f"{chain} TVL", value_shift=pct)
            log.info("DefiLlama %s: $%.2fB (7d: %+.1f%%)", chain, tvl_now / 1e9, pct * 100)
        except Exception as exc:
            log.error("DefiLlama %s fetch error: %s", chain, exc)
            _cache["last_error"] = f"defillama_{chain}: {exc}"

async def _fetch_kalshi(client: httpx.AsyncClient) -> None:
    """Fetch prediction market data from Kalshi (read-only, no auth)."""
    if not _env_bool("KALSHI_ENABLED", "false"):
        return
    base_url = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
    series_map = {
        "KXBTC": "kalshi_btc_prob",
        "KXETH": "kalshi_eth_prob",
        "KXSOL": "kalshi_sol_prob",
        "FED": "fed_cut_prob",
        "RECESSION": "recession_prob",
    }
    for series, cache_key in series_map.items():
        try:
            r = await client.get(
                f"{base_url}/markets",
                params={"series_ticker": series, "status": "open"},
                timeout=10,
            )
            markets = r.json().get("markets", [])
            if not markets:
                continue
            # Pick highest-volume market
            best = max(markets, key=lambda m: float(m.get("volume", 0) or 0))
            yes_bid = float(best.get("yes_bid", 0) or 0)
            yes_ask = float(best.get("yes_ask", 0) or 0)
            if yes_bid and yes_ask:
                prob = (yes_bid + yes_ask) / 200.0
            else:
                prob = float(best.get("last_price", 50)) / 100.0
            _cache[cache_key] = prob
            _db_write("kalshi", "prediction", prob, market_id=series,
                      description=f"{series} probability")
            log.info("Kalshi %s: %.1f%%", series, prob * 100)
        except Exception as exc:
            log.error("Kalshi %s fetch error: %s", series, exc)
            _cache["last_error"] = f"kalshi_{series}: {exc}"
        await asyncio.sleep(0.5)  # rate limit between Kalshi calls

async def _fetch_coinalyze(client: httpx.AsyncClient) -> list:
    """
    Fetch funding rates and open interest from Coinalyze.
    Returns list of DB row dicts.
    No-op if COINALYZE_ENABLED=false or COINALYZE_API_KEY is empty.
    """
    if not COINALYZE_ENABLED or not COINALYZE_API_KEY:
        return []
    rows = []
    try:
        # Funding rates
        r = await client.get(
            f"{COINALYZE_BASE_URL}/funding-rate",
            params={"symbols": COINALYZE_PAIRS, "api_key": COINALYZE_API_KEY},
            timeout=10,
        )
        if r.status_code == 200:
            for item in r.json():
                sym = item.get("symbol", "")
                rate = float(item.get("value", 0))
                if rate == 0 and "value" not in item:
                    continue
                key = f"coinalyze_{sym.split('USDT')[0].lower()}_funding"
                prev = _cache.get(key)
                _cache[key] = rate
                rows.append({
                    "source": "coinalyze", "signal_type": "funding",
                    "market_id": sym, "description": f"{sym} funding rate",
                    "value": rate,
                    "value_shift": round(rate - prev, 6) if prev is not None else None,
                })
                log.info("Coinalyze %s funding: %.5f", sym, rate)
        # Open interest
        r2 = await client.get(
            f"{COINALYZE_BASE_URL}/open-interest",
            params={"symbols": "BTCUSDT_PERP.A,ETHUSDT_PERP.A", "api_key": COINALYZE_API_KEY},
            timeout=10,
        )
        if r2.status_code == 200:
            for item in r2.json():
                sym = item.get("symbol", "")
                oi = float(item.get("value", 0))
                key = f"coinalyze_{sym.split('USDT')[0].lower()}_oi"
                _cache[key] = oi
                rows.append({
                    "source": "coinalyze", "signal_type": "open_interest",
                    "market_id": sym, "description": f"{sym} open interest",
                    "value": oi, "value_shift": None,
                })
    except Exception as exc:
        log.warning("Coinalyze fetch failed: %s", exc)
    return rows


async def _fetch_helius(client: httpx.AsyncClient) -> list:
    """
    Fetch Solana network inflation rate via Helius RPC.
    Falls back to public RPC if Helius key unavailable.
    """
    if not HELIUS_ENABLED:
        return []
    rows = []
    try:
        rpc_url = SOLANA_RPC_URL_FOR_RPC if HELIUS_API_KEY else "https://api.mainnet-beta.solana.com"
        r = await client.post(
            rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": "getInflationRate", "params": []},
            timeout=10,
        )
        if r.status_code == 200:
            result = r.json().get("result", {})
            total  = float(result.get("total", 0))
            epoch  = result.get("epoch")
            prev   = _cache.get("helius_sol_inflation")
            _cache["helius_sol_inflation"] = total
            _cache["helius_sol_epoch"]     = epoch
            rows.append({
                "source": "helius", "signal_type": "macro",
                "market_id": "sol_inflation", "description": "SOL network inflation rate",
                "value": total,
                "value_shift": round(total - prev, 5) if prev is not None else None,
            })
            log.info("Helius SOL inflation: %.3f%% (epoch %s)", total * 100, epoch)
        # Also fetch recent Jupiter swap activity as ecosystem proxy
        if HELIUS_API_KEY:
            r2 = await client.get(
                f"{HELIUS_BASE_URL}/transactions",
                params={"api-key": HELIUS_API_KEY, "type": "SWAP", "source": "JUPITER"},
                timeout=10,
            )
            if r2.status_code == 200:
                events = r2.json()
                if isinstance(events, list):
                    rows.append({
                        "source": "helius", "signal_type": "activity",
                        "market_id": "sol_swap_volume", "description": "Recent Jupiter swap count",
                        "value": float(len(events)), "value_shift": None,
                    })
                    log.info("Helius Jupiter swaps (recent): %d", len(events))
    except Exception as exc:
        log.warning("Helius fetch failed: %s", exc)
    return rows


# ── Composite score ──────────────────────────────────────────────────────────

def _compute_composite() -> None:
    """Compute weighted composite score from all available sources."""
    if not (_env_bool("ALT_DATA_ENABLED") or _env_bool("COMPOSITE_SIGNAL_ENABLED")):
        return

    components = {}
    weights = {}
    tvl_threshold = _env_float("DEFILLAMA_TVL_DROP_THRESHOLD", "0.10")

    # Fear & Greed: (score - 50) * 2 → -100..+100
    fg = _cache["fear_greed"]
    if fg is not None:
        val = (fg - 50) * 2
        components["Fear &amp; Greed"] = val
        weights["Fear &amp; Greed"] = 0.25

    # SentiCrypt: value * 100 → -100..+100
    sc = _cache["coingecko_sentiment"]
    if sc is not None:
        val = sc * 100
        components["BTC Sentiment"] = val
        weights["BTC Sentiment"] = 0.15

    # Solana TVL trend
    sol_now = _cache["solana_tvl_now"]
    sol_7d = _cache["solana_tvl_7d_ago"]
    if sol_now is not None and sol_7d is not None and sol_7d > 0:
        pct_change = (sol_now - sol_7d) / sol_7d
        clamped = max(-1.0, min(1.0, pct_change / tvl_threshold))
        val = clamped * 100
        components["Solana TVL trend"] = val
        weights["Solana TVL trend"] = 0.15

    # Kalshi crypto avg (only if enabled)
    if _env_bool("KALSHI_ENABLED"):
        kalshi_probs = [
            _cache[k] for k in ("kalshi_btc_prob", "kalshi_eth_prob", "kalshi_sol_prob")
            if _cache[k] is not None
        ]
        if kalshi_probs:
            avg_prob = sum(kalshi_probs) / len(kalshi_probs)
            val = (avg_prob - 0.5) * 200
            components["Kalshi crypto avg"] = val
            weights["Kalshi crypto avg"] = 0.30

        # Kalshi recession
        rec = _cache["recession_prob"]
        if rec is not None:
            val = (0.5 - rec) * 200
            components["Kalshi recession"] = val
            weights["Kalshi recession"] = 0.15

    if COINALYZE_ENABLED:
        # Funding rate signal: high positive = overleveraged longs = bearish
        btc_fr = _cache.get("coinalyze_btc_funding")
        eth_fr = _cache.get("coinalyze_eth_funding")
        valid_fr = [f for f in [btc_fr, eth_fr] if f is not None]
        if valid_fr:
            avg_fr = sum(valid_fr) / len(valid_fr)
            # Normalise: 0.03% per 8h (~40% annual) is extreme overleveraged = -100
            # -0.01% per 8h is bearish shorts = +50
            fr_signal = max(-100.0, min(100.0, -(avg_fr / 0.0003) * 100.0))
            components["coinalyze_funding"] = fr_signal
            weights["coinalyze_funding"] = 0.20

    if not weights:
        return

    # Normalise weights to 1.0
    total_weight = sum(weights.values())
    composite = sum(
        components[k] * (weights[k] / total_weight) for k in components
    )
    composite = max(-100.0, min(100.0, composite))

    _cache["composite"] = round(composite, 1)
    _cache["composite_components"] = {k: round(v, 1) for k, v in components.items()}

    # Write composite to DB
    _db_write("composite", "score", composite, composite_score=composite)

# ── Telegram helpers ─────────────────────────────────────────────────────────

async def _tg_send(client: httpx.AsyncClient, text: str) -> None:
    """Send an HTML message to the configured Telegram chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        await client.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }, timeout=10)
    except Exception as exc:
        log.error("Telegram send error: %s", exc)

# ── Telegram command handlers ────────────────────────────────────────────────

async def handle_signals_command(client: httpx.AsyncClient) -> None:
    """Handle /signals — show alt data summary."""
    if not _env_bool("ALT_DATA_ENABLED"):
        await _tg_send(client, "Alt data disabled. Set ALT_DATA_ENABLED=true in .env and restart.")
        return

    composite = _cache["composite"]
    updated = _cache["last_updated"]
    components = _cache.get("composite_components", {})

    lines = ["<b>Alt Data Signal Summary</b>\n"]
    if composite is not None:
        lines.append(f"Composite: <b>{composite:+.0f}</b> / 100")
    else:
        lines.append("Composite: <i>not yet computed</i>")
    if updated:
        lines.append(f"Updated: {updated}")
    lines.append("")

    if components:
        lines.append("Components:")
        for k, v in components.items():
            lines.append(f"  {k}: {v:+.0f}")
        lines.append("")

    lines.append("Raw readings:")
    fg = _cache["fear_greed"]
    fg_label = _cache["fg_label"]
    if fg is not None:
        lines.append(f"  Fear &amp; Greed: {fg}/100 ({fg_label})")

    cg = _cache["coingecko_sentiment"]
    if cg is not None:
        lines.append(f"  BTC Sentiment (CoinGecko): {cg:+.3f}")

    sol = _cache["solana_tvl_now"]
    if sol is not None:
        lines.append(f"  Solana TVL: ${sol / 1e9:.2f}B")

    base = _cache["base_tvl_now"]
    if base is not None:
        lines.append(f"  Base TVL: ${base / 1e9:.2f}B")

    if _env_bool("KALSHI_ENABLED"):
        btc = _cache["kalshi_btc_prob"]
        if btc is not None:
            lines.append(f"  Kalshi BTC: {btc * 100:.0f}%")
        rec = _cache["recession_prob"]
        if rec is not None:
            lines.append(f"  Recession prob: {rec * 100:.0f}%")

    btc_fr = _cache.get("coinalyze_btc_funding")
    if btc_fr is not None:
        lines.append(f"  BTC Funding Rate: {btc_fr*100:.4f}%/8h")

    sol_inf = _cache.get("helius_sol_inflation")
    if sol_inf is not None:
        lines.append(f"  SOL inflation: {sol_inf*100:.2f}%/yr")

    await _tg_send(client, "\n".join(lines))

async def handle_feargreed_command(client: httpx.AsyncClient) -> None:
    """Handle /feargreed — show Fear & Greed gauge."""
    fg = _cache["fear_greed"]
    if fg is None:
        await _tg_send(client, "Fear &amp; Greed data not yet available. Wait for next poll cycle.")
        return

    fg_label = _cache["fg_label"] or "Unknown"
    filled = int(fg / 5)
    bar = "\u2588" * filled + "\u2591" * (20 - filled)

    text = (
        f"<b>Fear &amp; Greed Index</b>\n\n"
        f"<code>{bar}</code>\n"
        f"Score: <b>{fg}</b> / 100 — {fg_label}\n\n"
        f"0-24 Extreme Fear | 25-49 Fear | 50 Neutral | 51-74 Greed | 75-100 Extreme Greed\n\n"
        f"Position sizing: score below 20 reduces auto-execution size by 40%."
    )
    await _tg_send(client, text)

# ── Main loop ────────────────────────────────────────────────────────────────

async def alt_data_loop() -> None:
    """Main polling loop — registered as an asyncio task in main.py."""
    if not _env_bool("ALT_DATA_ENABLED"):
        log.info("Alt data disabled — loop idle")
        return

    interval = _env_int("ALT_DATA_POLL_INTERVAL_MINUTES", "5") * 60
    log.info("Alt data loop started (interval=%dm)", interval // 60)

    async with httpx.AsyncClient() as client:
        while True:
            try:
                await _fetch_fear_greed(client)
                await _fetch_coingecko_sentiment(client)
                await _fetch_defillama(client)
                await _fetch_kalshi(client)
                coinalyze_rows = await _fetch_coinalyze(client)
                for row in coinalyze_rows:
                    _db_write(row["source"], row["signal_type"], row["value"],
                              market_id=row.get("market_id"), description=row.get("description"),
                              value_shift=row.get("value_shift"))
                helius_rows = await _fetch_helius(client)
                for row in helius_rows:
                    _db_write(row["source"], row["signal_type"], row["value"],
                              market_id=row.get("market_id"), description=row.get("description"),
                              value_shift=row.get("value_shift"))
                _compute_composite()

                _cache["last_updated"] = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )

                # Composite pause threshold warning
                composite = _cache["composite"]
                pause_threshold = _env_float("COMPOSITE_PAUSE_THRESHOLD", "-50")
                if composite is not None and composite < pause_threshold:
                    await _tg_send(
                        client,
                        f"\u26a0\ufe0f <b>Alt Data Warning</b>\n\n"
                        f"Composite signal <b>{composite:+.0f}</b> is below "
                        f"pause threshold ({pause_threshold:.0f}).\n"
                        f"Auto-execution is suspended.",
                    )

                fg = _cache["fear_greed"]
                cg = _cache["coingecko_sentiment"]
                log.info(
                    "Alt data poll complete — composite=%s  fg=%s  coingecko=%s",
                    f"{composite:+.1f}" if composite is not None else "N/A",
                    fg if fg is not None else "N/A",
                    f"{cg:.3f}" if cg is not None else "N/A",
                )
            except Exception as exc:
                log.error("Alt data loop error: %s", exc, exc_info=True)
                _cache["last_error"] = str(exc)

            await asyncio.sleep(interval)
