#!/usr/bin/env python3
"""
pendle_connector.py — Sprint 2B
Pendle Finance fixed-yield Principal Token (PT) connector.

Lets users lock in a fixed APY for a defined term by purchasing the PT of a
yield-bearing asset. On maturity the PT redeems 1:1 for the underlying, capturing
the fixed spread as profit regardless of variable rate movements.

Only stablecoin-denominated PT markets are surfaced (yoUSD / USDC base).
No YT speculation — strictly the fixed-rate PT path.

Chains supported:   Base (chainId 8453) — primary
                    Arbitrum (chainId 42161) — secondary
API:                https://api-v2.pendle.finance/core/v1/ — no key required
On-chain execution: Pendle Router V4 contract on Base via web3.py

New .env vars:
  PENDLE_ENABLED          true|false   (default false — opt-in)
  PENDLE_CHAIN            base|arbitrum (default base)
  PENDLE_MIN_RATE         float        (default 4.0 — only show markets above this %)
  PENDLE_MAX_TERM_DAYS    int          (default 180 — only show markets within this window)
"""

import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
PENDLE_ENABLED      = os.getenv("PENDLE_ENABLED",       "false").lower() == "true"
PENDLE_CHAIN        = os.getenv("PENDLE_CHAIN",          "base").lower()
PENDLE_MIN_RATE     = float(os.getenv("PENDLE_MIN_RATE",         "4.0"))
PENDLE_MAX_TERM     = int(os.getenv("PENDLE_MAX_TERM_DAYS",      "180"))

_CHAIN_IDS = {"base": 8453, "arbitrum": 42161}
_PENDLE_API = "https://api-v2.pendle.finance/core/v1"
_REQUEST_TIMEOUT = 12

# Pendle Router V4 — same address on Base and Arbitrum
_PENDLE_ROUTER = "0x888888888889758F76e7103c6CbF23ABbF58F946"

# Stablecoin keywords — used to filter markets to USD/stablecoin only
_STABLE_KEYWORDS = {"usd", "usdc", "usdt", "dai", "frax", "susd", "yousd", "yousd"}


# ── Data types ────────────────────────────────────────────────────────────────

from dataclasses import dataclass

@dataclass
class PendleMarket:
    market_address: str     # LP / market contract address
    pt_address:     str     # Principal Token contract address
    symbol:         str     # Human-readable name e.g. "PT yoUSD (USDC)"
    fixed_apy:      float   # Implied fixed APY as a percentage
    expiry:         datetime
    days_to_expiry: int
    liquidity_usd:  float
    chain:          str

    def summary(self) -> str:
        return (
            f"{self.symbol}\n"
            f"  Fixed APY:      {self.fixed_apy:.2f}%\n"
            f"  Maturity:       {self.expiry.strftime('%Y-%m-%d')} "
            f"({self.days_to_expiry}d)\n"
            f"  Liquidity:      ${self.liquidity_usd:,.0f}\n"
            f"  Market:         {self.market_address}\n"
            f"  PT:             {self.pt_address}"
        )


# ── Market fetcher ────────────────────────────────────────────────────────────

def get_available_markets(
    chain: str | None = None,
    min_rate: float | None = None,
    max_term_days: int | None = None,
) -> list[PendleMarket]:
    """
    Fetch active Pendle markets and filter to stablecoin PT opportunities.

    Args:
        chain:         "base" or "arbitrum". Defaults to PENDLE_CHAIN env var.
        min_rate:      Minimum implied APY %. Defaults to PENDLE_MIN_RATE.
        max_term_days: Maximum days to expiry. Defaults to PENDLE_MAX_TERM.

    Returns:
        List of PendleMarket, sorted by fixed_apy descending.

    Raises:
        RuntimeError on API failure.
    """
    chain         = (chain or PENDLE_CHAIN).lower()
    min_rate      = min_rate      if min_rate      is not None else PENDLE_MIN_RATE
    max_term_days = max_term_days if max_term_days is not None else PENDLE_MAX_TERM
    chain_id      = _CHAIN_IDS.get(chain)

    if not chain_id:
        raise ValueError(f"Unsupported chain: {chain}. Use 'base' or 'arbitrum'.")

    url = f"{_PENDLE_API}/{chain_id}/markets"
    try:
        resp = requests.get(url, params={"limit": 50, "is_expired": "false"},
                            timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        raw_markets = resp.json().get("results", [])
    except Exception as exc:
        raise RuntimeError(f"Pendle API error: {exc}") from exc

    now     = datetime.now(timezone.utc)
    markets = []

    for m in raw_markets:
        try:
            implied_apy = float(m.get("impliedApy") or 0) * 100
            if implied_apy < min_rate:
                continue

            expiry_str = m.get("expiry", "")
            if not expiry_str:
                continue
            expiry = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
            days_left = (expiry - now).days
            if days_left <= 0 or days_left > max_term_days:
                continue

            # Filter to stablecoin markets only
            symbol = (
                m.get("pt", {}).get("simpleSymbol", "")
                or m.get("simpleSymbol", "")
                or m.get("symbol", "")
            ).lower()
            if not any(kw in symbol for kw in _STABLE_KEYWORDS):
                continue

            liquidity = float(
                (m.get("liquidity") or {}).get("usd", 0) or 0
            )
            pt_address = m.get("pt", {}).get("address", "")
            display_symbol = (
                m.get("pt", {}).get("simpleSymbol", "")
                or m.get("simpleSymbol", "")
                or m.get("symbol", "")
            )

            markets.append(PendleMarket(
                market_address = m["address"],
                pt_address     = pt_address,
                symbol         = display_symbol,
                fixed_apy      = round(implied_apy, 4),
                expiry         = expiry,
                days_to_expiry = days_left,
                liquidity_usd  = liquidity,
                chain          = chain,
            ))

        except Exception as exc:
            log.warning("Skipping malformed market entry: %s", exc)
            continue

    markets.sort(key=lambda x: x.fixed_apy, reverse=True)
    log.info(
        "Pendle markets on %s: %d qualifying (min_rate=%.1f%% max_term=%dd)",
        chain, len(markets), min_rate, max_term_days,
    )
    return markets


# ── PT price / implied rate ───────────────────────────────────────────────────

def get_pt_price(market_address: str, chain: str | None = None) -> dict:
    """
    Return current PT price in USD and implied fixed rate for a specific market.

    Args:
        market_address: Market contract address (from get_available_markets).
        chain:          "base" or "arbitrum". Defaults to PENDLE_CHAIN.

    Returns:
        Dict with keys: pt_price_usd, implied_apy_pct, symbol, expiry, days_to_expiry.
    """
    chain    = (chain or PENDLE_CHAIN).lower()
    chain_id = _CHAIN_IDS.get(chain)
    if not chain_id:
        raise ValueError(f"Unsupported chain: {chain}")

    # Pull all markets and find the matching one
    try:
        resp = requests.get(
            f"{_PENDLE_API}/{chain_id}/markets",
            params={"limit": 50, "is_expired": "false"},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as exc:
        raise RuntimeError(f"Pendle API error: {exc}") from exc

    target = market_address.lower()
    for m in results:
        if m.get("address", "").lower() != target:
            continue

        pt          = m.get("pt", {})
        pt_price    = float((pt.get("price") or {}).get("usd", 0) or 0)
        implied_apy = float(m.get("impliedApy") or 0) * 100
        expiry_str  = m.get("expiry", "")
        expiry      = datetime.fromisoformat(expiry_str.replace("Z", "+00:00")) if expiry_str else None
        days_left   = (expiry - datetime.now(timezone.utc)).days if expiry else 0

        log.info(
            "PT price for %s: $%.6f | implied APY: %.2f%% | %dd to expiry",
            market_address[:10], pt_price, implied_apy, days_left,
        )
        return {
            "pt_price_usd":   pt_price,
            "implied_apy_pct": round(implied_apy, 4),
            "symbol":         pt.get("simpleSymbol", ""),
            "expiry":         expiry.isoformat() if expiry else "",
            "days_to_expiry": days_left,
        }

    raise RuntimeError(f"Market {market_address} not found on {chain}")


# ── Quote for PT purchase ─────────────────────────────────────────────────────

def get_swap_quote(market_address: str, token_in: str,
                   amount_in: int, chain: str | None = None,
                   slippage: float = 0.005) -> dict:
    """
    Get a swap quote from the Pendle API for buying PT with a given token.

    Args:
        market_address: Market contract address.
        token_in:       Input token address (e.g. USDC on Base).
        amount_in:      Amount of token_in in its smallest unit.
        chain:          "base" or "arbitrum". Defaults to PENDLE_CHAIN.
        slippage:       Max slippage as a decimal (default 0.005 = 0.5%).

    Returns:
        Dict with keys: amount_out (raw PT), price_impact, calldata (hex),
                        to (router address), value (ETH to send, usually 0).

    Raises:
        RuntimeError on API or quote failure.
    """
    chain    = (chain or PENDLE_CHAIN).lower()
    chain_id = _CHAIN_IDS.get(chain)
    if not chain_id:
        raise ValueError(f"Unsupported chain: {chain}")

    # Pendle SDK quote endpoint
    url = f"{_PENDLE_API}/{chain_id}/markets/{market_address}/swap"
    payload = {
        "receiver":   os.getenv("EVM_WALLET_ADDRESS", ""),
        "slippage":   slippage,
        "tokenIn":    token_in,
        "amountIn":   str(amount_in),
        "enableAggregator": True,
    }

    if not payload["receiver"]:
        raise RuntimeError("EVM_WALLET_ADDRESS not set in .env")

    try:
        resp = requests.post(url, json=payload, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise RuntimeError(f"Pendle swap quote failed: {exc}") from exc

    tx = data.get("transaction", {})
    out = data.get("data", {})

    log.info(
        "Pendle swap quote: in=%d %s → PT amount_out=%s price_impact=%s",
        amount_in, token_in[:10],
        out.get("amountOut", "?"),
        out.get("priceImpact", "?"),
    )

    return {
        "amount_out":   out.get("amountOut", "0"),
        "price_impact": out.get("priceImpact", None),
        "calldata":     tx.get("data", ""),
        "to":           tx.get("to", _PENDLE_ROUTER),
        "value":        tx.get("value", "0"),
    }


# ── Positions tracker ─────────────────────────────────────────────────────────

import sqlite3
from config import DB_PATH


def ensure_pendle_table() -> None:
    """Create pendle_positions table if it doesn't exist. Safe to call repeatedly."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pendle_positions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                chain          TEXT    NOT NULL,
                market_address TEXT    NOT NULL,
                pt_address     TEXT    NOT NULL,
                symbol         TEXT,
                amount_usd     REAL    NOT NULL,
                pt_amount      REAL    NOT NULL,
                entry_pt_price REAL    NOT NULL,
                fixed_apy      REAL    NOT NULL,
                expiry         TEXT    NOT NULL,
                days_at_entry  INTEGER,
                tx_hash        TEXT,
                status         TEXT    DEFAULT 'open',
                opened_at      TEXT    DEFAULT (datetime('now')),
                closed_at      TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()


def log_pendle_position(chain: str, market_address: str, pt_address: str,
                         symbol: str, amount_usd: float, pt_amount: float,
                         entry_pt_price: float, fixed_apy: float,
                         expiry: str, days_at_entry: int,
                         tx_hash: str = "") -> int:
    """Record a new Pendle PT position. Returns new row id."""
    ensure_pendle_table()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.execute("""
            INSERT INTO pendle_positions
              (chain, market_address, pt_address, symbol, amount_usd,
               pt_amount, entry_pt_price, fixed_apy, expiry,
               days_at_entry, tx_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (chain, market_address, pt_address, symbol, amount_usd,
              pt_amount, entry_pt_price, fixed_apy, expiry,
              days_at_entry, tx_hash))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_open_positions() -> list[dict]:
    """Return all open Pendle PT positions with maturity countdown."""
    ensure_pendle_table()
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        rows = conn.execute("""
            SELECT id, chain, symbol, amount_usd, pt_amount, entry_pt_price,
                   fixed_apy, expiry, days_at_entry, tx_hash, opened_at
            FROM pendle_positions WHERE status='open'
            ORDER BY expiry ASC
        """).fetchall()
        conn.close()
    except Exception as exc:
        log.error("get_open_positions error: %s", exc)
        return []

    now = datetime.now(timezone.utc)
    positions = []
    for r in rows:
        expiry = datetime.fromisoformat(r[7].replace("Z", "+00:00")) \
            if "+" not in r[7] else datetime.fromisoformat(r[7])
        days_left = max(0, (expiry - now).days)
        guaranteed_yield = round(r[3] * (r[6] / 100) * (days_left / 365), 4)
        positions.append({
            "id":               r[0],
            "chain":            r[1],
            "symbol":           r[2],
            "amount_usd":       r[3],
            "pt_amount":        r[4],
            "entry_pt_price":   r[5],
            "fixed_apy":        r[6],
            "expiry":           r[7],
            "days_remaining":   days_left,
            "guaranteed_yield": guaranteed_yield,
            "tx_hash":          r[9],
            "opened_at":        r[10],
        })
    return positions


# ── Telegram command handlers ─────────────────────────────────────────────────

import asyncio
import httpx

_TELEGRAM_BASE = "https://api.telegram.org/bot{token}/{method}"


async def _tg_send(client: httpx.AsyncClient, text: str,
                   reply_markup: dict | None = None) -> None:
    from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = _TELEGRAM_BASE.format(token=TELEGRAM_BOT_TOKEN, method="sendMessage")
    payload: dict = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        await client.post(url, json=payload, timeout=10)
    except Exception as exc:
        log.warning("Telegram send failed: %s", exc)


async def handle_pendle_command(client: httpx.AsyncClient,
                                 subcommand: str = "") -> None:
    """
    Handle /pendle Telegram commands.
      /pendle               → list available markets
      /pendle positions     → open PT positions with countdown
      /pendle buy [addr] [amount]  → initiate purchase (returns quote for confirmation)

    Wire into scanner.py's _handle_message alongside /rebalance.
    """
    parts = subcommand.strip().split()
    sub   = parts[0].lower() if parts else ""

    if sub == "positions":
        positions = get_open_positions()
        if not positions:
            await _tg_send(client, "No open Pendle positions.")
            return
        lines = [f"Pendle Positions ({len(positions)})\n"]
        total_locked  = sum(p["amount_usd"] for p in positions)
        total_yield   = sum(p["guaranteed_yield"] for p in positions)
        for p in positions:
            lines.append(
                f"{p['symbol']}\n"
                f"  Locked: ${p['amount_usd']:,.0f} | APY: {p['fixed_apy']:.2f}%\n"
                f"  Matures: {p['expiry'][:10]} ({p['days_remaining']}d)\n"
                f"  Guaranteed yield remaining: ${p['guaranteed_yield']:.2f}"
            )
        lines.append(f"\nTotal locked: ${total_locked:,.0f}")
        lines.append(f"Total guaranteed yield: ${total_yield:.2f}")
        await _tg_send(client, "\n".join(lines))
        return

    if sub == "buy" and len(parts) >= 3:
        market_addr = parts[1]
        try:
            amount_usd = float(parts[2])
        except ValueError:
            await _tg_send(client, "Usage: /pendle buy [market_address] [amount_usd]")
            return

        try:
            info = get_pt_price(market_addr)
        except Exception as exc:
            await _tg_send(client, f"Could not fetch market data: {exc}")
            return

        # USDC on Base — 6 decimals
        USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        amount_in = int(amount_usd * 1_000_000)

        try:
            quote = get_swap_quote(market_addr, USDC_BASE, amount_in)
        except Exception as exc:
            await _tg_send(client, f"Quote failed: {exc}")
            return

        pt_out  = int(quote.get("amount_out", "0")) / 1_000_000
        impact  = quote.get("price_impact")
        impact_str = f"{float(impact)*100:.3f}%" if impact else "unknown"

        await _tg_send(
            client,
            f"Pendle PT Purchase Quote\n\n"
            f"Market:        {info['symbol']}\n"
            f"Fixed APY:     {info['implied_apy_pct']:.2f}%\n"
            f"Maturity:      {info['expiry'][:10]} ({info['days_to_expiry']}d)\n"
            f"You send:      ${amount_usd:,.2f} USDC\n"
            f"You receive:   {pt_out:,.2f} PT\n"
            f"Price impact:  {impact_str}\n\n"
            f"To execute, send the signed transaction using your EVM wallet.\n"
            f"Calldata and router address logged to VPS console."
        )
        log.info(
            "Pendle buy quote: market=%s amount_usd=%.2f pt_out=%s calldata_len=%d",
            market_addr[:10], amount_usd, quote.get("amount_out"), len(quote.get("calldata",""))
        )
        return

    # Default: list available markets
    try:
        markets = get_available_markets()
    except Exception as exc:
        await _tg_send(client, f"Pendle API error: {exc}")
        return

    if not markets:
        await _tg_send(
            client,
            f"No Pendle markets found above {PENDLE_MIN_RATE:.1f}% "
            f"within {PENDLE_MAX_TERM}d on {PENDLE_CHAIN}."
        )
        return

    lines = [f"Pendle Fixed-Rate Markets ({PENDLE_CHAIN.capitalize()})\n"]
    for i, m in enumerate(markets, 1):
        lines.append(
            f"{i}. {m.symbol}\n"
            f"   {m.fixed_apy:.2f}% fixed | "
            f"matures {m.expiry.strftime('%Y-%m-%d')} ({m.days_to_expiry}d) | "
            f"liq ${m.liquidity_usd:,.0f}\n"
            f"   {m.market_address}"
        )
    lines.append(f"\n/pendle buy [market_address] [amount] to get a quote.")
    await _tg_send(client, "\n".join(lines))


# ── Self-test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ensure_pendle_table()
    print(f"Pendle connector self-test | chain={PENDLE_CHAIN} "
          f"min_rate={PENDLE_MIN_RATE}% max_term={PENDLE_MAX_TERM}d\n")

    markets = get_available_markets()
    if not markets:
        print("No qualifying markets found.")
    else:
        for m in markets:
            print(m.summary())
            print()
