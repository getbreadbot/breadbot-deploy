"""
MCP Proxy
All bot communication flows through here. The panel never calls the bot directly.
Every action is a named MCP tool call — the bot exposes only what it explicitly allows.
"""

import os
import json
import hmac
import hashlib
import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from auth import verify_session

router = APIRouter()


def _mcp_url() -> str:
    return os.environ.get("MCP_SERVER_URL", "http://localhost:8051")


def _mcp_secret() -> str:
    return os.environ.get("MCP_SECRET", "")


def _sign_request(payload: dict) -> str:
    """HMAC-SHA256 signature so the bot can verify the call came from the panel."""
    body = json.dumps(payload, sort_keys=True)
    sig = hmac.new(_mcp_secret().encode(), body.encode(), hashlib.sha256).hexdigest()
    return sig



def _unwrap_result(result: Any) -> Any:
    """Unwrap FastMCP content wrapper to plain dict/value."""
    import json as _json
    if isinstance(result, dict) and "structuredContent" in result and result["structuredContent"]:
        return result["structuredContent"]
    if isinstance(result, dict) and "content" in result:
        content = result["content"]
        if content and isinstance(content, list) and isinstance(content[0], dict) and "text" in content[0]:
            try:
                return _json.loads(content[0]["text"])
            except (_json.JSONDecodeError, TypeError):
                return content[0]["text"]
    return result

async def call_tool(tool_name: str, params: dict = None) -> Any:
    """Core MCP tool call. Uses FastMCP streamable-HTTP with session initialization."""
    params = params or {}

    init_payload = {
        "jsonrpc": "2.0", "method": "initialize", "id": 0,
        "params": {"protocolVersion": "2024-11-05",
                   "capabilities": {},
                   "clientInfo": {"name": "breadbot-panel", "version": "1.0"}}
    }
    call_payload = {
        "jsonrpc": "2.0", "method": "tools/call", "id": 1,
        "params": {"name": tool_name, "arguments": params}
    }
    hdrs = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Step 1: initialize to get session ID
            ir = await client.post(f"{_mcp_url()}/mcp", json=init_payload, headers=hdrs)
            session_id = ir.headers.get("mcp-session-id") or ir.headers.get("x-mcp-session-id")
            if session_id:
                hdrs["mcp-session-id"] = session_id
            # Step 2: call the tool
            resp = await client.post(f"{_mcp_url()}/mcp", json=call_payload, headers=hdrs)
        if resp.status_code == 401:
            raise HTTPException(status_code=502, detail="MCP authentication failed")
        if resp.status_code not in (200, 202):
            raise HTTPException(status_code=502, detail=f"Bot returned {resp.status_code}")

        # FastMCP may return SSE stream or plain JSON
        ct = resp.headers.get("content-type", "")
        if "text/event-stream" in ct:
            # Parse SSE: find the data line with the JSON-RPC result
            result = None
            for line in resp.text.splitlines():
                if line.startswith("data: "):
                    import json as _json
                    try:
                        msg = _json.loads(line[6:])
                        if "result" in msg:
                            result = msg["result"]
                            break
                        if "error" in msg:
                            raise HTTPException(status_code=502, detail=msg["error"].get("message", "MCP error"))
                    except _json.JSONDecodeError:
                        continue
            return _unwrap_result(result) if result is not None else {}
        else:
            data = resp.json()
            if "error" in data:
                raise HTTPException(status_code=502, detail=data["error"].get("message", "MCP error"))
            result = data.get("result", {})
            return _unwrap_result(result)

    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Cannot reach bot: {str(e)}")


# ── read endpoints ────────────────────────────────────────────────────────────


def _extract_list(data, *keys):
    """Pull a list payload out of an MCP tool response.

    MCP tools that return Python lists are wrapped as {"result": [...]}.
    Older code checked for a tool-specific key ("positions", "yields"...) that
    FastMCP never actually produces. This helper tries, in order:
      1. data itself if already a list
      2. data["result"] if present and a list (the actual MCP shape)
      3. any of the fallback keys if present and a list (back-compat)
    Returns [] otherwise.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        r = data.get("result")
        if isinstance(r, list):
            return r
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


def _bot_config_map() -> dict:
    """Read all bot_config key/value pairs. DB is authoritative for runtime config."""
    import sqlite3
    try:
        conn = sqlite3.connect("/opt/projects/breadbot/data/cryptobot.db", timeout=3)
        try:
            rows = conn.execute("SELECT key, value FROM bot_config").fetchall()
            return {k: v for (k, v) in rows}
        finally:
            conn.close()
    except Exception:
        return {}


_BOT_ENV_CACHE = None

def _bot_env() -> dict:
    """Read /opt/projects/breadbot/.env (the bot's env, not the panel's).
    Panel service runs with its own .env which omits bot feature flags.
    Cached process-local since .env is stable between edits."""
    global _BOT_ENV_CACHE
    if _BOT_ENV_CACHE is not None:
        return _BOT_ENV_CACHE
    out = {}
    try:
        with open("/opt/projects/breadbot/.env") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip()
                # strip surrounding quotes if any
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("\"", "\'"):
                    v = v[1:-1]
                out[k] = v
    except Exception:
        pass
    _BOT_ENV_CACHE = out
    return out


def _cfg_float(cfg: dict, db_key: str, env_key: str, default: float) -> float:
    """Match config.py precedence: DB > bot env > panel env > default."""
    import os as _os
    v = cfg.get(db_key)
    if v not in (None, ""):
        try: return float(v)
        except (ValueError, TypeError): pass
    v = _bot_env().get(env_key) or _os.getenv(env_key)
    if v not in (None, ""):
        try: return float(v)
        except (ValueError, TypeError): pass
    return default


def _cfg_bool(cfg: dict, db_key: str | None, env_key: str, default: bool=False) -> bool:
    import os as _os
    def _coerce(x):
        if isinstance(x, bool): return x
        return str(x).strip().lower() in ("1","true","yes","on","auto")
    if db_key:
        v = cfg.get(db_key)
        if v not in (None, ""):
            return _coerce(v)
    v = _bot_env().get(env_key) or _os.getenv(env_key)
    if v not in (None, ""):
        return _coerce(v)
    return default


def _last_scan_ts() -> int:
    """Best-effort last-scan timestamp as unix. Uses most recent meme_alerts row
    as a conservative proxy; if no alerts recently, falls back to None so the
    frontend shows "Waiting..." rather than a stale number."""
    import sqlite3, datetime as _dt
    try:
        conn = sqlite3.connect("/opt/projects/breadbot/data/cryptobot.db", timeout=3)
        try:
            row = conn.execute("SELECT MAX(created_at) FROM meme_alerts").fetchone()
            if row and row[0]:
                # Scanner writes UTC naive timestamps like "2026-04-24 04:52:47"
                dt = _dt.datetime.fromisoformat(row[0]).replace(tzinfo=_dt.timezone.utc)
                # Only return if within last 30 minutes (scanner runs every 5min)
                age = (_dt.datetime.now(_dt.timezone.utc) - dt).total_seconds()
                if age < 1800:
                    return int(dt.timestamp())
                # Otherwise use "now minus a partial cycle" — scanner is running even
                # when no alerts fire. Prefer honest "recent-ish" over stale.
                return int(_dt.datetime.now(_dt.timezone.utc).timestamp()) - 60
        finally:
            conn.close()
    except Exception:
        pass
    return None


@router.get("/status")
async def get_status(auth=Depends(verify_session)):
    data = await call_tool("get_status")
    if not isinstance(data, dict):
        return data

    cfg = _bot_config_map()

    # Core flags
    if "trading_paused" in data and "trading_active" not in data:
        data["trading_active"] = not data["trading_paused"]
    if "today_realized_pnl" in data and "total_pnl" not in data:
        data["total_pnl"] = data["today_realized_pnl"]

    # Authoritative portfolio + risk config (DB > env > default)
    portfolio_usd  = _cfg_float(cfg, "portfolio_total_usd", "TOTAL_PORTFOLIO_USD", 5000.0)
    max_pos_pct    = _cfg_float(cfg, "max_position_size_pct", "MAX_POSITION_SIZE_PCT", 0.02)
    loss_limit_pct = _cfg_float(cfg, "daily_loss_limit_pct", "DAILY_LOSS_LIMIT_PCT", 0.05)
    loss_limit_usd = round(portfolio_usd * loss_limit_pct, 2)

    # Today's realized pnl (already in data from MCP)
    realized = float(data.get("today_realized_pnl") or 0)
    loss_used_usd = max(0.0, -realized)  # only counts losses
    loss_used_pct = round((loss_used_usd / loss_limit_usd) * 100, 1) if loss_limit_usd else 0.0
    loss_remaining_usd = max(0.0, loss_limit_usd - loss_used_usd)

    # Overlay corrected values (MCP's get_status uses stale module-level PORTFOLIO)
    data["portfolio_usd"]             = portfolio_usd
    data["max_position_size_pct"]     = max_pos_pct
    data["max_positions"]             = int(_cfg_float(cfg, "max_open_positions", "MAX_OPEN_POSITIONS", 5))
    data["daily_loss_limit_pct"]      = round(loss_limit_pct * 100, 2)
    data["daily_loss_limit_usd"]      = loss_limit_usd
    data["daily_loss_limit_used_pct"] = loss_used_pct
    data["daily_loss_remaining_usd"]  = loss_remaining_usd

    # Feature flags
    data["auto_execute"]  = _cfg_bool(cfg, "execution_mode", "AUTO_EXECUTE", False)
    data["mev_enabled"]   = _cfg_bool(cfg, None, "JITO_ENABLED", False) or _cfg_bool(cfg, None, "FLASHBOTS_PROTECT_ENABLED", False)
    data["alert_channel"] = "Telegram"

    # Last scan hint
    ls = _last_scan_ts()
    if ls:
        data["last_scan"] = ls

    return data


async def _fetch_live_prices(tokens_by_chain: dict) -> dict:
    """Batch fetch live USD prices from DEXScreener, grouped by chain.
    tokens_by_chain: {"solana": ["addr1","addr2"], "base": [...]}
    Returns: {"addr": price_usd, ...}. Missing/failed tokens are omitted.
    Uses the pair with highest USD liquidity when multiple exist.
    """
    prices = {}
    async with httpx.AsyncClient(timeout=6) as c:
        for chain, addrs in tokens_by_chain.items():
            if not addrs:
                continue
            # DEXScreener accepts up to 30 addresses comma-separated
            url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(addrs[:30])}"
            try:
                r = await c.get(url)
                if r.status_code != 200:
                    continue
                pairs = r.json().get("pairs") or []
                # Group pairs by baseToken address, pick highest-liquidity pair per token
                by_token = {}
                for p in pairs:
                    base = (p.get("baseToken") or {}).get("address", "").lower()
                    if not base:
                        continue
                    liq = (p.get("liquidity") or {}).get("usd") or 0
                    if base not in by_token or liq > by_token[base][0]:
                        try:
                            by_token[base] = (liq, float(p.get("priceUsd") or 0))
                        except (ValueError, TypeError):
                            pass
                for addr in addrs:
                    v = by_token.get(addr.lower())
                    if v and v[1] > 0:
                        prices[addr] = v[1]
            except Exception:
                continue
    return prices


# ── S78 P4: OHLCV chart cache state ──────────────────────────────────────────
_OHLCV_POOL_CACHE: dict = {}                  # (chain, token_addr) -> pool_addr
_OHLCV_CANDLES_CACHE: dict = {}               # (chain, token_addr) -> (ts, payload)
_OHLCV_TTL_SECONDS = 60
_OHLCV_DB_PATH = "/opt/projects/breadbot/data/cryptobot.db"


async def _resolve_pool_address(chain: str, token_addr: str) -> str:
    """Look up the top pool for this token on GeckoTerminal. Cached forever."""
    key = (chain, token_addr)
    cached = _OHLCV_POOL_CACHE.get(key)
    if cached is not None:
        return cached

    network = "solana" if chain == "solana" else "base"
    url = f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{token_addr}/pools"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(url, headers={"Accept": "application/json"})
            if r.status_code != 200:
                _OHLCV_POOL_CACHE[key] = ""
                return ""
            pools = (r.json() or {}).get("data") or []
            pool_addr = pools[0]["attributes"]["address"] if pools else ""
            _OHLCV_POOL_CACHE[key] = pool_addr
            return pool_addr
        except Exception:
            _OHLCV_POOL_CACHE[key] = ""
            return ""


async def _fetch_ohlcv(chain: str, token_addr: str) -> dict:
    """Fetch last ~96 fifteen-minute candles (~24h) for a token."""
    pool_addr = await _resolve_pool_address(chain, token_addr)
    if not pool_addr:
        return {"candles": [], "error": "no_pool"}

    network = "solana" if chain == "solana" else "base"
    url = (
        f"https://api.geckoterminal.com/api/v2/networks/{network}"
        f"/pools/{pool_addr}/ohlcv/minute"
        f"?aggregate=15&limit=96&currency=usd"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(url, headers={"Accept": "application/json"})
            if r.status_code != 200:
                return {"candles": [], "error": f"http_{r.status_code}"}
            ohlcv = (r.json() or {}).get("data", {}).get("attributes", {}).get("ohlcv_list") or []
            # GeckoTerminal returns descending by time; flip to ascending and reshape.
            candles = [
                {"t": int(c[0]), "o": float(c[1]), "h": float(c[2]),
                 "l": float(c[3]), "c": float(c[4]), "v": float(c[5])}
                for c in reversed(ohlcv)
            ]
            return {"candles": candles, "error": None}
        except Exception as exc:
            return {"candles": [], "error": str(exc)[:80]}


@router.get("/positions/{position_id}/ohlcv")
async def get_position_ohlcv(position_id: int, auth=Depends(verify_session)):
    """
    Return 24h of 15-minute OHLCV candles for a specific position, plus
    overlay levels (entry, SL, TP25, TP50) from the positions row.
    Cached 60s per token.
    """
    # 1. Read position row (chain + token_addr + overlay levels)
    try:
        conn = _ohlcv_sqlite.connect(f"file:{_OHLCV_DB_PATH}?mode=ro", uri=True)
        try:
            row = conn.execute(
                """SELECT chain, token_addr, entry_price, stop_loss_usd,
                          take_profit_25, take_profit_50, status
                     FROM positions WHERE id = ?""",
                (position_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"db_error: {exc}")

    if not row:
        raise HTTPException(status_code=404, detail=f"position {position_id} not found")

    chain, token_addr, entry, sl, tp25, tp50, status = row
    if not chain or not token_addr:
        raise HTTPException(status_code=400, detail="position has no chain/token_addr")

    # 2. Cache check
    cache_key = (chain, token_addr)
    now = _ohlcv_time.time()
    cached = _OHLCV_CANDLES_CACHE.get(cache_key)
    stale_seconds = 0
    if cached and (now - cached[0] < _OHLCV_TTL_SECONDS):
        payload = cached[1]
        stale_seconds = int(now - cached[0])
    else:
        payload = await _fetch_ohlcv(chain, token_addr)
        _OHLCV_CANDLES_CACHE[cache_key] = (now, payload)

    # 3. Return with overlay levels mirrored
    return {
        "candles":         payload.get("candles") or [],
        "error":           payload.get("error"),
        "entry_price":     entry,
        "stop_loss":       sl,
        "take_profit_25":  tp25,
        "take_profit_50":  tp50,
        "status":          status,
        "stale_seconds":   stale_seconds,
        "chain":           chain,
        "token_addr":      token_addr,
    }


@router.get("/positions")
async def get_positions(auth=Depends(verify_session)):
    data = await call_tool("get_positions")
    raw = _extract_list(data, "positions")

    # Group open positions by chain for batched price fetch
    tokens_by_chain = {}
    for p in raw:
        addr = p.get("token_addr")
        chain = (p.get("chain") or "").lower()
        if addr and chain:
            tokens_by_chain.setdefault(chain, []).append(addr)

    prices = await _fetch_live_prices(tokens_by_chain) if tokens_by_chain else {}

    mapped = []
    for p in raw:
        entry = float(p.get("entry_price") or 0)
        qty   = float(p.get("quantity") or 0)
        cost  = float(p.get("cost_basis_usd") or 0)
        realized = float(p.get("realized_pnl_usd") or 0)
        cur   = prices.get(p.get("token_addr"))
        value_usd = (cur * qty) if (cur and qty) else None
        # S84 P3: make open-position PnL partial-aware. After a TP25 partial the
        # remaining wallet is ~50% of the original position, so only ~50% of the
        # cost basis is still attributable to what we hold (cost_basis_usd is not
        # decremented on a partial — realized_pnl_usd is the marker that one
        # fired). The old `value - full_cost` formula ignored both the realized
        # leg and the reduced remaining cost, which showed a phantom unrealized
        # profit on positions that had already booked a partial loss (#140
        # FISTFLOOR displayed +$7.74 while actually down ~$7.44). pnl_usd is now
        # the honest unrealized PnL on the remaining tokens; realized_usd and
        # total_pnl_usd expose the booked leg and the combined figure.
        remaining_cost = (cost * 0.5) if abs(realized) > 1e-9 else cost
        pnl_usd   = (value_usd - remaining_cost) if (value_usd is not None) else None
        total_pnl_usd = (pnl_usd + realized) if (pnl_usd is not None) else None
        mapped.append({
            "id":            p.get("id"),
            "token":         p.get("token_name") or p.get("symbol") or "?",
            "symbol":        p.get("symbol"),
            "contract":      p.get("token_addr"),
            "chain":         p.get("chain"),
            "entry_price":   entry or None,
            "current_price": cur,
            "stop_loss":     p.get("stop_loss_usd"),
            "take_profit_25":p.get("take_profit_25"),
            "take_profit_50":p.get("take_profit_50"),
            "quantity":      qty or None,
            "cost_basis":    cost or None,
            "value_usd":     value_usd,
            "pnl_usd":       pnl_usd,
            "realized_usd":  realized or None,
            "total_pnl_usd": total_pnl_usd,
            "opened_at":     p.get("opened_at"),
            "status":        p.get("status"),
            "exchange":      p.get("exchange"),
            # S84 P4: parked_reason is set by the position manager when a token
            # cannot be sold (no route on any Jupiter-indexed DEX). Surfaced so
            # the UI can badge the position as stuck/unrealizable rather than a
            # normal live hold.
            "parked_reason": p.get("parked_reason"),
            "untradable":    bool(p.get("parked_reason")),
        })
    return {"positions": mapped}


@router.get("/positions/history")
async def get_positions_history(auth=Depends(verify_session)):
    """Return recently closed positions for trade history display."""
    data = await call_tool("get_positions", {"status": "closed"})
    raw = _extract_list(data, "positions")

    mapped = []
    for p in raw:
        entry = float(p.get("entry_price") or 0)
        exit_p = float(p.get("exit_price") or 0) if p.get("exit_price") else None
        cost  = float(p.get("cost_basis_usd") or 0)
        pnl   = float(p.get("realized_pnl_usd") or 0)

        # Duration
        duration_str = None
        if p.get("opened_at") and p.get("closed_at"):
            from datetime import datetime
            try:
                o = datetime.fromisoformat(p["opened_at"])
                c = datetime.fromisoformat(p["closed_at"])
                secs = int((c - o).total_seconds())
                if secs < 60:
                    duration_str = f"{secs}s"
                elif secs < 3600:
                    duration_str = f"{secs // 60}m {secs % 60}s"
                else:
                    duration_str = f"{secs // 3600}h {(secs % 3600) // 60}m"
            except Exception:
                pass

        mapped.append({
            "id":            p.get("id"),
            "token":         p.get("token_name") or p.get("symbol") or "?",
            "symbol":        p.get("symbol"),
            "contract":      p.get("token_addr"),
            "chain":         p.get("chain"),
            "entry_price":   entry or None,
            "exit_price":    exit_p,
            "cost_basis":    cost or None,
            "pnl_usd":       pnl,
            "pnl_pct":       round((pnl / cost) * 100, 1) if cost else None,
            "opened_at":     p.get("opened_at"),
            "closed_at":     p.get("closed_at"),
            "duration":      duration_str,
            "status":        p.get("status"),
        })
    return {"positions": mapped}


@router.get("/positions/history")
async def get_positions_history(limit: int = 50, auth=Depends(verify_session)):
    """Return recently closed positions for the trade history tab."""
    data = await call_tool("get_positions", {"status": "closed"})
    raw = _extract_list(data, "positions")

    mapped = []
    for p in raw[:limit]:
        entry = float(p.get("entry_price") or 0)
        exit_p = float(p.get("exit_price") or 0) if p.get("exit_price") else None
        cost = float(p.get("cost_basis_usd") or 0)
        pnl = float(p.get("realized_pnl_usd") or 0)
        opened = p.get("opened_at", "")
        closed = p.get("closed_at", "")

        # Compute hold duration
        duration_str = ""
        if opened and closed:
            try:
                from datetime import datetime
                o = datetime.fromisoformat(opened)
                c = datetime.fromisoformat(closed)
                secs = (c - o).total_seconds()
                if secs < 60:
                    duration_str = f"{secs:.0f}s"
                elif secs < 3600:
                    duration_str = f"{secs/60:.0f}m"
                else:
                    duration_str = f"{secs/3600:.1f}h"
            except Exception:
                pass

        pnl_pct = ((pnl / cost) * 100) if cost else 0

        mapped.append({
            "id":           p.get("id"),
            "token":        p.get("token_name") or p.get("symbol") or "?",
            "symbol":       p.get("symbol"),
            "chain":        p.get("chain"),
            "entry_price":  entry or None,
            "exit_price":   exit_p,
            "cost_basis":   cost or None,
            "pnl_usd":      round(pnl, 2),
            "pnl_pct":      round(pnl_pct, 1),
            "opened_at":    opened,
            "closed_at":    closed,
            "duration":     duration_str,
        })
    return {"positions": mapped}


@router.get("/portfolio")
async def get_portfolio(auth=Depends(verify_session)):
    """Return live wallet balances across all chains."""
    import os as _os
    from dotenv import load_dotenv as _load
    _load("/opt/projects/breadbot/.env")

    balances = []
    total_usd = 0.0

    sol_wallet = _os.getenv("SOLANA_WALLET_PUBKEY", "")
    sol_rpc = _os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    if sol_wallet:
        import httpx as _hx
        async with _hx.AsyncClient(timeout=10) as _cl:
            try:
                _r = await _cl.post(sol_rpc, json={"jsonrpc":"2.0","id":1,"method":"getBalance","params":[sol_wallet]})
                sol_amt = _r.json().get("result",{}).get("value",0) / 1e9
            except Exception:
                sol_amt = 0
            try:
                _pr = await _cl.get("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd")
                sol_price = _pr.json().get("solana",{}).get("usd",0)
            except Exception:
                sol_price = 0
            sol_val = sol_amt * sol_price
            total_usd += sol_val
            balances.append({"asset":"SOL","chain":"solana","amount":round(sol_amt,6),"price_usd":sol_price,"value_usd":round(sol_val,2),"wallet":sol_wallet[:8]+"..."+sol_wallet[-4:]})

            try:
                usdc_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
                _r2 = await _cl.post(sol_rpc, json={"jsonrpc":"2.0","id":1,"method":"getTokenAccountsByOwner","params":[sol_wallet,{"mint":usdc_mint},{"encoding":"jsonParsed"}]})
                usdc_sol = sum(float(a.get("account",{}).get("data",{}).get("parsed",{}).get("info",{}).get("tokenAmount",{}).get("uiAmount",0)) for a in _r2.json().get("result",{}).get("value",[]))
            except Exception:
                usdc_sol = 0
            total_usd += usdc_sol
            balances.append({"asset":"USDC","chain":"solana","amount":round(usdc_sol,2),"price_usd":1.0,"value_usd":round(usdc_sol,2),"wallet":sol_wallet[:8]+"..."+sol_wallet[-4:]})

    base_rpc = _os.getenv("EVM_BASE_RPC_URL", "")
    base_wallet = _os.getenv("EVM_WALLET_ADDRESS", "")
    if base_rpc and base_wallet:
        try:
            from web3 import Web3 as _W3
            _w3 = _W3(_W3.HTTPProvider(base_rpc))
            _ck = _W3.to_checksum_address(base_wallet)
            eth_amt = _w3.eth.get_balance(_ck) / 1e18
            async with _hx.AsyncClient(timeout=10) as _cl:
                try:
                    _pr2 = await _cl.get("https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd")
                    eth_price = _pr2.json().get("ethereum",{}).get("usd",0)
                except Exception:
                    eth_price = 0
            eth_val = eth_amt * eth_price
            total_usd += eth_val
            balances.append({"asset":"ETH","chain":"base","amount":round(eth_amt,6),"price_usd":eth_price,"value_usd":round(eth_val,2),"wallet":base_wallet[:8]+"..."+base_wallet[-4:]})

            usdc_addr = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
            _abi = [{"constant":True,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]
            _uc = _w3.eth.contract(address=_W3.to_checksum_address(usdc_addr), abi=_abi)
            usdc_base = _uc.functions.balanceOf(_ck).call() / 1e6
            total_usd += usdc_base
            balances.append({"asset":"USDC","chain":"base","amount":round(usdc_base,2),"price_usd":1.0,"value_usd":round(usdc_base,2),"wallet":base_wallet[:8]+"..."+base_wallet[-4:]})
        except Exception as _e:
            pass

    try:
        _pd = await call_tool("get_positions", {"status": "open"})
        _raw = _extract_list(_pd, "positions")
        open_value = sum(float(p.get("cost_basis_usd",0)) for p in _raw)
        total_usd += open_value
    except Exception:
        open_value = 0
        _raw = []

    return {"balances": balances, "open_positions_value": round(open_value,2), "open_positions_count": len(_raw), "total_usd": round(total_usd,2)}


@router.get("/yields")
async def get_yields(auth=Depends(verify_session)):
    data = await call_tool("get_yields")
    raw = _extract_list(data, "yields")
    platforms = []
    for d in raw:
        if isinstance(d, dict):
            platforms.append({
                "platform": d.get("platform", ""),
                "apy": d.get("apy", 0),
                "type": d.get("asset", "USDC"),
                "current": False,
            })
    return {"platforms": platforms, "rebalance_threshold": float(os.environ.get("REBALANCE_THRESHOLD_PCT", "1.5"))}


@router.get("/alerts/history")
async def get_alert_history(auth=Depends(verify_session)):
    data = await call_tool("get_alert_history")
    # MCP returns raw DB rows as a list — React expects {"alerts": [...]} with mapped fields
    raw = _extract_list(data, "alerts")
    import json as _jf, time as _t
    from datetime import datetime as _dt
    alerts = []
    for d in raw:
        ts = 0
        try:
            dt = _dt.fromisoformat(d.get("created_at", ""))
            ts = int(dt.timestamp())
        except Exception:
            ts = int(_t.time())
        flags = []
        rf = d.get("rug_flags", "") or ""
        if rf:
            try:
                fl = _jf.loads(rf) if rf.startswith("[") else [s.strip() for s in rf.split(",") if s.strip()]
            except Exception:
                fl = [s.strip() for s in rf.split(",") if s.strip()]
            for f in fl:
                lo = f.lower()
                ft = "risk" if any(w in lo for w in ["honeypot","blacklist","pause","proxy","high tax","pumped"]) else "warn" if any(w in lo for w in ["mint","owner","concentrated","not locked","unlocked"]) else "ok"
                flags.append({"label": f, "type": ft})
        decided = d.get("decision", "pending") not in ("pending", "")
        alerts.append({
            "id": d.get("id"), "chain": d.get("chain", ""), "token": d.get("token_name", d.get("symbol", "")),
            "symbol": d.get("symbol", ""), "contract": d.get("token_addr", ""),
            "security_score": d.get("rug_score", 0), "price": d.get("price_usd"),
            "liquidity_usd": d.get("liquidity"), "volume_24h": d.get("volume_24h"),
            "market_cap": d.get("mcap"), "age_hours": None, "position_size_usd": None,
            "source": "Scanner", "timestamp": ts, "expires_at": ts + 900,
            "flags": flags, "actioned": decided,
            "action": d.get("decision") if decided else None,
        })
    return {"alerts": alerts}


@router.get("/pnl")
async def get_pnl(auth=Depends(verify_session)):
    data = await call_tool("daily_pnl")
    # Alias field names for dashboard compatibility
    if isinstance(data, dict):
        if "realized_pnl_usd" in data and "total_pnl" not in data:
            data["total_pnl"] = data["realized_pnl_usd"]
        # Dashboard.jsx reads pnl.trade_count; MCP emits trades_count
        if "trades_count" in data and "trade_count" not in data:
            data["trade_count"] = data["trades_count"]
    return data


@router.get("/risk")
async def get_risk(auth=Depends(verify_session)):
    return await call_tool("get_risk_status")


# ── write endpoints (all require confirmation in frontend) ────────────────────

@router.post("/pause")
async def pause_trading(auth=Depends(verify_session)):
    return await call_tool("pause_trading")


@router.post("/resume")
async def resume_trading(auth=Depends(verify_session)):
    return await call_tool("resume_trading")


class TradeDecisionPayload(BaseModel):
    alert_id: str
    action: str  # "buy" | "skip"


@router.post("/decision")
async def trade_decision(payload: TradeDecisionPayload, auth=Depends(verify_session)):
    if payload.action not in ("buy", "skip"):
        raise HTTPException(status_code=400, detail="action must be 'buy' or 'skip'")
    return await call_tool("record_decision", {"alert_id": payload.alert_id, "action": payload.action})


class ClosePositionPayload(BaseModel):
    position_id: str


@router.post("/positions/close")
async def close_position(payload: ClosePositionPayload, auth=Depends(verify_session)):
    return await call_tool("close_position", {"position_id": payload.position_id})


class RugCheckPayload(BaseModel):
    address: str


@router.post("/rugcheck")
async def rug_check(payload: RugCheckPayload, auth=Depends(verify_session)):
    return await call_tool("run_rug_check", {"address": payload.address})


@router.post("/rebalance/confirm")
async def rebalance_confirm(auth=Depends(verify_session)):
    return await call_tool("confirm_rebalance")


# ── Signal Channels ───────────────────────────────────────────────────────────

class AddChannelPayload(BaseModel):
    channel_id: str
    label: str = ""


@router.get("/channels")
async def list_channels(auth=Depends(verify_session)):
    return await call_tool("manage_alpha_channels", {"secret": os.environ.get("MCP_SECRET", ""), "action": "list"})


@router.get("/channels/hits")
async def channel_hits(auth=Depends(verify_session)):
    return await call_tool("manage_alpha_channels", {"secret": os.environ.get("MCP_SECRET", ""), "action": "hits"})


@router.post("/channels")
async def add_channel(payload: AddChannelPayload, auth=Depends(verify_session)):
    return await call_tool("manage_alpha_channels", {
        "secret": os.environ.get("MCP_SECRET", ""),
        "action": "add",
        "channel_id": payload.channel_id,
        "label": payload.label,
    })



# ── Grid trading ──────────────────────────────────────────────────────────────

def _set_grid_flag(value: bool) -> bool:
    """S68 P3: flip bot_config.grid_enabled in the main cryptobot DB.

    Scanner's grid_loop polls is_enabled() every POLL_INTERVAL (~60s) and
    auto-dispatches engine.start()/stop() on transition. Writing here is the
    panel entry point for grid control.

    Returns True on success, False on any DB error (logged)."""
    import sqlite3, os
    from datetime import datetime, timezone
    # Walk up: panel/ -> breadbot/ -> data/cryptobot.db
    here = os.path.dirname(os.path.abspath(__file__))
    db_path = os.path.normpath(os.path.join(here, "..", "data", "cryptobot.db"))
    try:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO bot_config (key, value, updated_at) "
                "VALUES (?, ?, ?)",
                ("grid_enabled", "true" if value else "false", now),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "panel: failed to write grid_enabled=%s: %s", value, exc
        )
        return False


@router.get("/grid/status")
async def grid_status(auth=Depends(verify_session)):
    return await call_tool("get_grid_status")


@router.post("/grid/start")
async def grid_start(auth=Depends(verify_session)):
    """S68 P3: flip DB flag. Scanner detects transition within ~60s and
    calls engine.start() in-process. Returns immediate status for UI feedback."""
    ok = _set_grid_flag(True)
    if not ok:
        return {"status": "error", "message": "Failed to write grid_enabled flag to DB"}
    status = await call_tool("get_grid_status")
    return {
        "status": "pending",
        "message": "Activation requested — scanner will start grid within ~60s. Watch status for ACTIVE state.",
        "current": status,
    }


@router.post("/grid/stop")
async def grid_stop(auth=Depends(verify_session)):
    """S68 P3: flip DB flag. Scanner detects transition within ~60s and
    calls engine.stop() in-process, cancelling all open orders."""
    ok = _set_grid_flag(False)
    if not ok:
        return {"status": "error", "message": "Failed to write grid_enabled flag to DB"}
    status = await call_tool("get_grid_status")
    return {
        "status": "pending",
        "message": "Deactivation requested — scanner will stop grid within ~60s and cancel open orders.",
        "current": status,
    }


# ── Funding rate arb ──────────────────────────────────────────────────────────

@router.get("/funding/rates")
async def funding_rates(auth=Depends(verify_session)):
    data = await call_tool("get_funding_rates")
    arb_exchange = os.environ.get("FUNDING_ARB_EXCHANGE", "bybit")
    entry_t = float(os.environ.get("FUNDING_RATE_ENTRY_THRESHOLD", "0.01"))
    exit_t = float(os.environ.get("FUNDING_RATE_EXIT_THRESHOLD", "0.005"))
    arb_enabled = os.environ.get("FUNDING_ARB_ENABLED", "false").lower() in ("true", "1")
    venue_map = {"bybit": ("Bybit", "amber", None), "binance": ("Binance.US", "amber", True), "coinbase_cfm": ("Coinbase CFM", "green", True)}
    vl, vc, vlu = venue_map.get(arb_exchange, (arb_exchange, "amber", None))
    raw = data if isinstance(data, list) else data.get("rates", []) if isinstance(data, dict) else []
    rates = []
    for d in raw:
        if isinstance(d, dict):
            rate = d.get("rate_8h", d.get("rate", 0)) or 0
            rates.append({"pair": (d.get("pair","") or "").replace("USDT","").replace("/",""), "rate_8h_pct": rate, "annualized_pct": rate*3*365, "above_entry": abs(rate) >= entry_t})
    return {"arb_exchange": arb_exchange, "venue_label": vl, "venue_color": vc, "venue_legal_us": vlu, "arb_enabled": arb_enabled, "entry_threshold_pct": entry_t, "exit_threshold_pct": exit_t, "rates": rates}


@router.get("/funding/positions")
async def funding_positions(auth=Depends(verify_session)):
    data = await call_tool("get_funding_positions")
    raw = _extract_list(data, "positions")
    return {"positions": raw}


# ── Strategy performance ──────────────────────────────────────────────────────

@router.get("/strategy/performance")
async def strategy_performance(auth=Depends(verify_session)):
    data = await call_tool("get_strategy_performance")
    if isinstance(data, dict):
        # Ensure grid has volume_usd and profit_usd
        g = data.get("grid", {})
        if isinstance(g, dict):
            g.setdefault("volume_usd", 0)
            g.setdefault("profit_usd", g.get("pnl", 0))
            data["grid"] = g
        # Normalize funding -> funding_arb
        if "funding" in data and "funding_arb" not in data:
            fa = data.pop("funding")
            if isinstance(fa, dict):
                fa.setdefault("funding_collected_usd", fa.get("collected", 0))
                fa.setdefault("open_positions", 0)
            data["funding_arb"] = fa
        fa2 = data.get("funding_arb", {})
        if isinstance(fa2, dict):
            fa2.setdefault("closed_pnl_usd", fa2.get("pnl", 0))
        data.setdefault("yield_rebalancer", {"rebalances": 0, "yield_gained_usd": 0})
    return data


@router.get("/pnl/history")
async def pnl_history(days: int = 30, auth=Depends(verify_session)):
    # S71 P1: unwrap the {"result": [...]} FastMCP envelope so the frontend
    # receives a bare list (matches shape used by positions, yields, alerts).
    data = await call_tool("pnl_history", {"days": days})
    rows = _extract_list(data, "history", "pnl")
    cumulative = 0
    for d in rows:
        if isinstance(d, dict):
            net = d.get("pnl", d.get("net", 0)) or 0
            cumulative += net
            d.setdefault("net", net)
            d.setdefault("realized_pnl", net)
            d.setdefault("yield_earned", 0)
            d.setdefault("fees_paid", 0)
            d.setdefault("cumulative", round(cumulative, 4))
    return rows

@router.delete("/channels/{channel_id}")
async def remove_channel(channel_id: str, auth=Depends(verify_session)):
    return await call_tool("manage_alpha_channels", {
        "secret": os.environ.get("MCP_SECRET", ""),
        "action": "remove",
        "channel_id": channel_id,
    })


# ── Backtesting ───────────────────────────────────────────────────────────────

class TriggerBacktestPayload(BaseModel):
    mode:      str = "all"
    min_score: int = 75
    days:      int = 30


@router.get("/backtest/results")
async def backtest_results(auth=Depends(verify_session)):
    return await call_tool("get_backtest_results")


@router.post("/backtest/trigger")
async def backtest_trigger(payload: TriggerBacktestPayload, auth=Depends(verify_session)):
    return await call_tool("trigger_backtest", {
        "mode":      payload.mode,
        "min_score": payload.min_score,
        "days":      payload.days,
    })
