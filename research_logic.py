"""
research_logic.py — shared token scoring used by both the scanner and the
Research page (S72 P1).

Rationale
---------
Before S72, two parallel scoring rubrics existed:

  • scanner.process_pair()        — full ~22-rule rubric: GoPlus security,
                                    holders/concentration, age, liquidity,
                                    vol/liq ratio, m5/h1 momentum, buy
                                    pressure, buy velocity, velocity decay,
                                    social signals, Axiom signals, holder
                                    growth, time-of-day, alt-data composite.
  • research_proxy._run_research  — light 7-rule mechanical-safety rubric
                                    that started at 100 and only deducted
                                    on hard rug-pull signals.

Result: the Research page rated almost everything 100 even when the
scanner had just scored the same coin at 87.

This module re-implements the full scanner rubric exactly once. The
scanner can keep using its inline implementation (it is hot, it works,
+$46 lifetime) — the immediate fix is to point research_proxy here so
the user-facing Research page matches what the bot would actually do
with that token.

A future session may switch scanner.py to call this module too,
eliminating the drift entirely. For now, treat scanner.process_pair as
the source of truth and this module as the faithful replica.

Public surface
--------------
    score_token(client, chain, token_addr, *, fetch_pair=True) -> dict

Returns a dict shaped like:
    {
        "score":       int 0..100,
        "flags":       list[str],
        "should_drop": bool,            # scanner-style early-return signal,
                                        # research callers ignore this
        "drop_reason": str | None,
        "goplus":      {is_honeypot, sell_tax, buy_tax, owner_address},
        "rugcheck":    {score, risks},
        "dexscreener": {name, symbol, price_usd, liquidity, volume_24h,
                        market_cap, age_hours, price_change_m5,
                        price_change_h1, txns_m5_buys, txns_m5_sells,
                        txns_h1_buys, txns_h1_sells},
    }

The scanner already has a normalized `pair` dict before scoring, so
when fetch_pair=False the caller may pass `pair_override=...` to skip
the DEXScreener fetch. (Reserved — not used by research_proxy.)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

log = logging.getLogger(__name__)

# These mirror scanner.py and config.py so this module is self-contained.
GOPLUS_EVM = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={addr}"
GOPLUS_SOL = "https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={addr}"
DEXSCREENER_TOKENS = "https://api.dexscreener.com/latest/dex/tokens/{addr}"
RUGCHECK_REPORT = "https://api.rugcheck.xyz/v1/tokens/{addr}/report"


# ── GoPlus block (lifted byte-for-byte from scanner.check_token_security) ────

async def _check_token_security(
    client: httpx.AsyncClient, chain: str, token_addr: str
) -> tuple[int, list[str], dict]:
    """
    Run the GoPlus security check. Returns (score, flags, raw_info).

    Mirrors scanner.check_token_security exactly. The raw `info` dict is
    also returned so callers can surface owner/tax/honeypot fields in the
    Research UI without a second round-trip.
    """
    api_key = os.environ.get("GOPLUS_API_KEY", "")
    headers = {"Authorization": api_key} if api_key else {}
    info: dict = {}

    try:
        if chain == "solana":
            url = GOPLUS_SOL.format(addr=token_addr)
            r = await client.get(url, headers=headers, timeout=12)
            data = r.json().get("result", {})
            info = data.get(token_addr) or data.get(token_addr.lower()) or {}
        else:
            url = GOPLUS_EVM.format(chain_id="8453", addr=token_addr.lower())
            r = await client.get(url, headers=headers, timeout=12)
            data = r.json().get("result", {})
            info = data.get(token_addr.lower()) or {}
    except Exception as exc:
        log.warning("GoPlus unavailable for %s: %s", token_addr[:12], exc)
        return 50, ["GoPlus check unavailable — review manually"], {}

    if not info:
        return 50, ["No security data found"], {}

    score = 100
    flags: list[str] = []

    def deduct(points: int, label: str) -> None:
        nonlocal score
        score -= points
        flags.append(label)

    def bonus(points: int, label: str) -> None:
        nonlocal score
        score += points
        flags.append(f"+{points} {label}")

    # Honeypot
    if str(info.get("is_honeypot", "0")) == "1":
        deduct(40, "Honeypot detected")

    # Sell tax — gradient
    try:
        st = float(info.get("sell_tax") or 0)
        if st > 0.10:
            deduct(30, f"Sell tax {st*100:.1f}%")
        elif st > 0.05:
            deduct(15, f"Sell tax {st*100:.1f}%")
        elif st > 0.01:
            pass
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
    if str(info.get("owner_change_balance",   "0")) == "1": deduct(30, "Owner can change balances")
    if str(info.get("can_take_back_ownership","0")) == "1": deduct(10, "Ownership reclaim function")
    if str(info.get("is_proxy",               "0")) == "1": deduct(10, "Proxy contract")
    if str(info.get("external_call",          "0")) == "1": deduct(10, "External call in transfer")
    if str(info.get("trading_cooldown",       "0")) == "1": deduct(10, "Trading cooldown")
    if str(info.get("is_blacklisted",         "0")) == "1": deduct(15, "Blacklist function")
    if str(info.get("transfer_pausable",      "0")) == "1": deduct(15, "Transfers can be paused")

    # Holder analysis
    try:
        holders = info.get("holders", []) or []
        num_holders = len(holders)

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

            if top10_pct > 0.70:
                deduct(20, f"Top 10 hold {top10_pct*100:.0f}% of supply")
            elif top10_pct > 0.55:
                deduct(15, f"Top 10 hold {top10_pct*100:.0f}% of supply")
            elif top10_pct > 0.40:
                deduct(10, f"Top 10 hold {top10_pct*100:.0f}% of supply")
            elif top10_pct > 0.30:
                deduct(5, f"Top 10 hold {top10_pct*100:.0f}% of supply")

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

    # LP lock
    if str(info.get("lp_locked", "0")) not in ("1", "true"):
        deduct(10, "Liquidity not locked")

    # Open source (EVM only)
    if chain != "solana" and str(info.get("is_open_source", "0")) != "1":
        deduct(5, "Contract not open source")

    return max(0, score), flags, info


# ── DEXScreener pair fetcher (lifted from scanner._fetch_pair_detail) ────────

async def _fetch_pair_detail(
    client: httpx.AsyncClient, chain: str, token_addr: str
) -> dict | None:
    """
    Fetch the DEXScreener token endpoint and pick the best pair on the
    requested chain. Returns the same shape scanner.process_pair expects,
    or None if no pair exists for that chain.

    Note: this does NOT enforce MIN_LIQUIDITY_USD / MIN_VOLUME_24H_USD —
    the Research page should be able to show data for tokens that fall
    below the scanner's alert floor. The scanner enforces those gates
    itself before invoking score_token.
    """
    try:
        r = await client.get(DEXSCREENER_TOKENS.format(addr=token_addr), timeout=10)
        pool = (r.json().get("pairs") or []) if r.status_code == 200 else []
    except Exception as exc:
        log.warning("DEXScreener token fetch error %s: %s", token_addr[:12], exc)
        return None

    candidates = [p for p in pool if p.get("chainId", "").lower() == chain]
    if not candidates:
        return None

    best = max(candidates, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
    liquidity = float((best.get("liquidity") or {}).get("usd") or 0)
    volume_24 = float((best.get("volume") or {}).get("h24") or 0)
    mcap = float(best.get("marketCap") or 0) or float(best.get("fdv") or 0)

    age_h: float = 0.0
    try:
        created_ms = best.get("pairCreatedAt")
        if created_ms:
            created = datetime.fromtimestamp(int(created_ms) / 1000, tz=timezone.utc)
            age_h = (datetime.now(timezone.utc) - created).total_seconds() / 3600
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


# ── Public API ───────────────────────────────────────────────────────────────

async def score_token(
    client: httpx.AsyncClient,
    chain: str,
    token_addr: str,
) -> dict:
    """
    Compute the full ~22-rule score for a token. Mirrors
    scanner.process_pair scoring exactly.

    Parameters
    ----------
    client : httpx.AsyncClient
        Reused HTTP client (keep-alive matters here — multiple upstream
        calls per token).
    chain : "solana" | "base"
    token_addr : str

    Returns
    -------
    dict
        score, flags, should_drop, drop_reason, goplus, rugcheck,
        dexscreener (see module docstring).

    Notes
    -----
    * Returns gracefully on every upstream failure. A coin that GoPlus
      cannot resolve scores 50 with one flag, matching scanner behavior.
    * The `should_drop` flag is for scanner callers — research_proxy
      ignores it and surfaces the score regardless.
    """
    out: dict = {
        "score":       100,
        "flags":       [],
        "should_drop": False,
        "drop_reason": None,
        "goplus":      {},
        "rugcheck":    {},
        "dexscreener": {},
    }

    # ── 1. GoPlus security ──────────────────────────────────────────────
    score, flags, gp_info = await _check_token_security(client, chain, token_addr)
    out["goplus"] = {
        "is_honeypot":   str(gp_info.get("is_honeypot", "0")) == "1",
        "sell_tax":      float(gp_info.get("sell_tax", 0) or 0),
        "buy_tax":       float(gp_info.get("buy_tax", 0) or 0),
        "owner_address": gp_info.get("owner_address", ""),
    }

    # ── 2. RugCheck (Solana only — RugCheck.xyz does not cover EVM) ─────
    if chain == "solana":
        try:
            rc_resp = await client.get(RUGCHECK_REPORT.format(addr=token_addr), timeout=10)
            if rc_resp.status_code == 200:
                rc_data = rc_resp.json()
                risks = rc_data.get("risks", []) or []
                out["rugcheck"] = {
                    "score": rc_data.get("score", 0),
                    "risks": [r.get("name", "") for r in risks if r.get("name")],
                }
                if any(r.get("level") == "critical" for r in risks):
                    flags.append("RugCheck critical risk")
                    score = max(0, score - 15)
        except Exception as exc:
            log.warning("RugCheck fetch failed for %s: %s", token_addr[:12], exc)

    # ── 3. Alt-data composite signal ────────────────────────────────────
    try:
        from alt_data_signals import get_cached_composite
        composite = get_cached_composite()
        if composite is not None:
            if composite > 30:
                score = min(100, score + 3)
            elif composite < -30:
                score = max(0, score - 3)
    except Exception as exc:
        log.debug("alt_data_signals composite unavailable: %s", exc)

    # ── 4. DEXScreener pair fetch ───────────────────────────────────────
    pair = await _fetch_pair_detail(client, chain, token_addr)
    if pair is None:
        # No pair on this chain — score with what we have
        out["score"] = max(0, score)
        out["flags"] = flags
        return out

    out["dexscreener"] = {
        "name":            pair.get("token_name", ""),
        "symbol":          pair.get("symbol", ""),
        "price_usd":       pair.get("price_usd", 0.0),
        "liquidity":       pair.get("liquidity", 0.0),
        "volume_24h":      pair.get("volume_24h", 0.0),
        "market_cap":      pair.get("mcap", 0.0),
        "age_hours":       pair.get("age_hours", 0.0),
        "price_change_m5": pair.get("price_change_m5", 0.0),
        "price_change_h1": pair.get("price_change_h1", 0.0),
        "txns_m5_buys":    pair.get("txns_m5_buys", 0),
        "txns_m5_sells":   pair.get("txns_m5_sells", 0),
        "txns_h1_buys":    pair.get("txns_h1_buys", 0),
        "txns_h1_sells":   pair.get("txns_h1_sells", 0),
    }

    age_h   = float(pair.get("age_hours", 0) or 0)
    liq_usd = float(pair.get("liquidity", 0) or 0)
    vol_24h = float(pair.get("volume_24h", 0) or 0)

    # ── 5. Token age ────────────────────────────────────────────────────
    if age_h < 1:
        score = max(0, score - 8); flags.append("Token < 1h old")
    elif age_h < 3:
        score = max(0, score - 5); flags.append(f"Token {age_h:.1f}h old")
    elif age_h < 12:
        score = max(0, score - 2); flags.append(f"Token {age_h:.1f}h old")
    elif age_h > 168:
        score = min(100, score + 3); flags.append(f"+3 Survived {int(age_h/24)}d")

    # ── 6. Liquidity depth bonus ────────────────────────────────────────
    if liq_usd >= 100_000:
        score = min(100, score + 5); flags.append(f"+5 Liquidity ${liq_usd/1000:.0f}k")
    elif liq_usd >= 50_000:
        score = min(100, score + 3); flags.append(f"+3 Liquidity ${liq_usd/1000:.0f}k")
    elif liq_usd >= 25_000:
        score = min(100, score + 1); flags.append(f"+1 Liquidity ${liq_usd/1000:.0f}k")

    # ── 7. Vol/Liq ratio ────────────────────────────────────────────────
    if liq_usd > 0:
        vl_ratio = vol_24h / liq_usd
        if vl_ratio >= 10:
            score = min(100, score + 5); flags.append(f"+5 Vol/Liq {vl_ratio:.1f}x")
        elif vl_ratio >= 5:
            score = min(100, score + 3); flags.append(f"+3 Vol/Liq {vl_ratio:.1f}x")
        elif vl_ratio >= 2:
            score = min(100, score + 1); flags.append(f"+1 Vol/Liq {vl_ratio:.1f}x")
        elif vl_ratio < 0.5:
            score = max(0, score - 5); flags.append(f"Low Vol/Liq {vl_ratio:.2f}x")
        elif vl_ratio < 1:
            score = max(0, score - 3); flags.append(f"Low Vol/Liq {vl_ratio:.2f}x")

    # ── 8. Momentum: 5m and 1h price change ─────────────────────────────
    pc_m5    = pair.get("price_change_m5", 0.0)
    pc_h1    = pair.get("price_change_h1", 0.0)
    m5_buys  = pair.get("txns_m5_buys",  0)
    m5_sells = pair.get("txns_m5_sells", 0)
    h1_buys  = pair.get("txns_h1_buys",  0)

    if pc_m5 >= 20:
        score = min(100, score + 6); flags.append(f"+6 Price +{pc_m5:.0f}% (5m)")
    elif pc_m5 >= 10:
        score = min(100, score + 4); flags.append(f"+4 Price +{pc_m5:.0f}% (5m)")
    elif pc_m5 >= 5:
        score = min(100, score + 2); flags.append(f"+2 Price +{pc_m5:.0f}% (5m)")
    elif pc_m5 <= -15:
        score = max(0, score - 5); flags.append(f"Price {pc_m5:.0f}% (5m)")
    elif pc_m5 <= -5:
        score = max(0, score - 2); flags.append(f"Price {pc_m5:.0f}% (5m)")

    # ── 9. h1 pump ceiling — scanner DROPS, research keeps the score ───
    max_h1_pump = float(os.environ.get("MAX_H1_PUMP_PCT", "150"))
    if pc_h1 >= max_h1_pump:
        # Scanner-side: drop the alert. Research-side: still score it
        # heavily — the user wants to see what the bot would think.
        out["should_drop"] = True
        out["drop_reason"] = f"h1 pump +{pc_h1:.0f}% exceeds ceiling {max_h1_pump:.0f}%"
        score = max(0, score - 25)
        flags.append(f"Already pumped +{pc_h1:.0f}% (1h, above scanner ceiling)")
    elif pc_h1 >= 200:
        score = max(0, score - 20); flags.append(f"Already pumped +{pc_h1:.0f}% (1h)")
    elif pc_h1 >= 100:
        score = max(0, score - 12); flags.append(f"Already pumped +{pc_h1:.0f}% (1h)")
    elif pc_h1 >= 50:
        score = min(100, score + 3); flags.append(f"+3 Price +{pc_h1:.0f}% (1h)")
    elif pc_h1 >= 20:
        score = min(100, score + 2); flags.append(f"+2 Price +{pc_h1:.0f}% (1h)")
    elif pc_h1 >= 10:
        score = min(100, score + 1); flags.append(f"+1 Price +{pc_h1:.0f}% (1h)")
    elif pc_h1 <= -30:
        score = max(0, score - 6); flags.append(f"Price {pc_h1:.0f}% (1h)")
    elif pc_h1 <= -10:
        score = max(0, score - 3); flags.append(f"Price {pc_h1:.0f}% (1h)")

    # ── 10. Buy pressure (m5) ───────────────────────────────────────────
    m5_total = m5_buys + m5_sells
    if m5_total >= 5:
        m5_ratio = m5_buys / m5_total
        if m5_ratio >= 0.80:
            score = min(100, score + 5); flags.append(f"+5 Buy pressure {m5_ratio*100:.0f}% (5m)")
        elif m5_ratio >= 0.65:
            score = min(100, score + 3); flags.append(f"+3 Buy pressure {m5_ratio*100:.0f}% (5m)")
        elif m5_ratio >= 0.55:
            score = min(100, score + 1); flags.append(f"+1 Buy pressure {m5_ratio*100:.0f}% (5m)")
        elif m5_ratio <= 0.30:
            score = max(0, score - 4); flags.append(f"Sell pressure {(1-m5_ratio)*100:.0f}% (5m)")

    # ── 11. Buy velocity (h1) ───────────────────────────────────────────
    if h1_buys >= 200:
        score = min(100, score + 4); flags.append(f"+4 Buy velocity {h1_buys} txns (1h)")
    elif h1_buys >= 100:
        score = min(100, score + 2); flags.append(f"+2 Buy velocity {h1_buys} txns (1h)")
    elif h1_buys >= 50:
        score = min(100, score + 1); flags.append(f"+1 Buy velocity {h1_buys} txns (1h)")

    # ── 12. Velocity decay ──────────────────────────────────────────────
    if h1_buys >= 24:
        h1_rate_per_5m = h1_buys / 12
        if m5_buys < h1_rate_per_5m * 0.4:
            score = max(0, score - 6)
            flags.append(f"Velocity decay: {m5_buys} vs {h1_rate_per_5m:.0f} avg/5m")

    # ── 13. Social signals (Arkham + alpha channels) ────────────────────
    try:
        from social_signals import get_social_score_boost
        social_boost, social_flags = await get_social_score_boost(token_addr, chain, client)
        if social_boost:
            score = min(100, score + social_boost)
            flags += social_flags
    except Exception as exc:
        log.debug("social_signals unavailable for %s: %s", token_addr[:12], exc)

    # ── 14. Axiom signals ───────────────────────────────────────────────
    try:
        from axiom_signals import get_axiom_score_boost
        axiom_boost, axiom_flags = await get_axiom_score_boost(token_addr, client)
        if axiom_boost:
            score = min(100, score + axiom_boost)
            flags += axiom_flags
    except Exception as exc:
        log.debug("axiom_signals unavailable for %s: %s", token_addr[:12], exc)

    # ── 15. Holder count growth (Solana via Helius) ─────────────────────
    try:
        from holder_signal import get_holder_score
        holder_adj, holder_note = await get_holder_score(pair)
        if holder_adj:
            score = max(0, min(100, score + holder_adj))
            flags.append(holder_note)
    except Exception as exc:
        log.debug("holder_signal unavailable for %s: %s", token_addr[:12], exc)

    # ── 16. Time-of-day filter ──────────────────────────────────────────
    utc_hour = datetime.now(timezone.utc).hour
    dead_hours = {4, 14, 17, 21}
    if utc_hour in dead_hours:
        score = max(0, score - 10)
        flags.append(f"Dead hour {utc_hour:02d}:00 UTC (0% hist WR)")

    out["score"] = max(0, min(100, score))
    out["flags"] = flags
    return out
