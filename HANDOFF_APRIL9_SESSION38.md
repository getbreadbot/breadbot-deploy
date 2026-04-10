# Breadbot — Master Session Handoff
*April 9, 2026 — Session 38*

## FIRST ACTION EVERY SESSION
```bash
ssh vps "python3 /opt/projects/breadbot/session_state.py update"
```

## INFRASTRUCTURE
| Service | URL | Status |
|---|---|---|
| Landing | breadbot.app | Live |
| Demo dashboard | demo.breadbot.app | **Live — now serves panel React UI** |
| License server | keys.breadbot.app:8002 | Live |
| MCP server | mcp.breadbot.app | Live |
| Web panel | panel.breadbot.app | Live |
| GitHub | github.com/getbreadbot/breadbot-deploy | Latest: **72d271d** |
| Railway template | https://railway.com/deploy/breadbot | Live |
| Railway test domain | terrific-nourishment-production-e116.up.railway.app | Online |

**VPS:** 76.13.100.34 — `ssh vps` — `/opt/projects/breadbot/`
**Bot PID:** 2477550 (verify with `pgrep -fa main.py`)
**Deploy repo:** `/Users/adrez/Desktop/cryptobot/deploy_repo/`
**AUTO_EXECUTE:** ON — conservative strategy (score ≥ 83)

---

## WHAT WAS COMPLETED SESSION 38

### demo.breadbot.app → panel React UI (commit 002cff7)

Synced demo to serve the same React panel frontend as panel.breadbot.app.

**Auth stubs added:** `/api/auth/status` → `{"configured": true}`, `/api/auth/me` → `{"authenticated": true}`, plus login/logout/setup — all auto-pass so demo requires no login.

**24 panel-compatible endpoints added:**
- `/api/bot/status` — queries positions + trades + bot_config tables
- `/api/bot/positions` — open positions from `positions` table
- `/api/bot/yields` — latest yield snapshots with GROUP BY join
- `/api/bot/alerts/history` — 200 alerts with full field mapping (see below)
- `/api/bot/pnl` — today's realized PnL from `trades` table
- `/api/bot/strategy/performance` — 30-day scanner/grid/funding summary
- `/api/bot/grid/status` — grid state from bot_config + grid_fills
- `/api/bot/funding/rates` — latest funding rates per pair
- `/api/bot/funding/positions` — open funding arb positions
- `/api/bot/channels` — alpha channels list
- `/api/bot/channels/hits` — recent channel hits
- `/api/bot/backtest/results` — reads backtest_last.json
- `/api/bot/pnl/history` — daily PnL aggregation
- `/api/settings/basic` — returns current env var values
- `/api/settings/advanced` — returns masked key status
- All POST write endpoints — return demo no-ops
- WebSocket `/api/ws/alerts` — keepalive stub

**Panel dist:** Copied `panel/frontend/dist/` → `dashboard/panel_dist/`. Static mount serves `/assets` from panel_dist, catch-all serves panel `index.html`.

**Backup:** `dashboard/server.py.bak3`

### Alerts field mapping fix (both demo + panel, commit 002cff7)

**Root cause:** React `Alerts.jsx` calls `get('/bot/alerts/history')` and checks `data?.alerts` — expects `{"alerts": [...]}`. Both demo and panel were returning raw arrays, so alerts never rendered.

**Second issue:** `AlertCard` component uses field names `security_score`, `token`, `contract`, `timestamp`, `liquidity_usd`, `market_cap` — but DB stores `rug_score`, `token_name`, `token_addr`, `created_at`, `liquidity`, `mcap`.

**Fix applied to both:**
- Wrapped response in `{"alerts": [...]}`
- Mapped all DB field names → React field names
- Parsed `rug_flags` JSON array into structured `[{label, type}]` with risk/warn/ok classification
- Converted `created_at` ISO string → unix timestamp
- Set `actioned` flag based on decision field

**Files changed:** `dashboard/server.py`, `panel/mcp_proxy.py` (backup at `.bak`)

### alt_data_signals composite index in DB init (commit 72d271d)

Added `CREATE INDEX IF NOT EXISTS idx_alt_data_signals_lookup` to `ensure_alt_data_table()` in `alt_data_signals.py`. Previously only existed in VPS DB (created manually Session 37). Now Railway deploys also get it.

---

## CURRENT BOT STATE (VPS)
| Component | Status |
|---|---|
| Scanner | ACTIVE |
| Execution mode | AUTO — conservative (score ≥ 83) |
| Auto-buy trades | Cupsey (93), TRENCHOOR (98), BIGREVEAL (86), BECK (90), SATOCOIN (89), LIONESS (90) |
| Demo dashboard | Panel React UI — all endpoints verified |
| Panel dashboard | Alerts fix deployed |
| Dashboard service | 2 workers, healthy |

---

## NEXT SESSION PRIORITY ORDER

### 1. Verify demo + panel alerts rendering in browser
Open demo.breadbot.app and panel.breadbot.app — confirm alerts page shows cards.

### 2. Dashboard page data gaps
The Dashboard page renders but some fields may show "—" because:
- `daily_loss_limit_used_pct` not returned by bot_status (needs computation)
- `last_scan` timestamp not tracked
Consider adding these fields to bot_status endpoint.

### 3. Positions page — may show empty
`positions` table has 0 open rows (all closed). Auto-buy trades are in `meme_alerts` with `decision='auto_buy'` but actual positions depend on whether auto_executor created entries in `positions` table. Verify auto_executor is writing to `positions`.

### 4. Re-render 3 stale videos
scanner_alerts, yields_page, strategy_setup — screenshot current live dashboard and regenerate.

### 5. Whop Publish — Morgan manual action

---

## BLOCKED ON MORGAN
| Item | Action |
|---|---|
| Whop Publish | Admin toggle → Publish |
| Instagram Reels | Mobile upload — 18 videos |
| Robinhood funding | Load funds |
| Coinbase Commerce | Create + webhook to license server |

## BLOCKED EXTERNAL
| Item | Status |
|---|---|
| Arkham API key | Approval pending → inject as ARKHAM_API_KEY |
| Coinbase CFM derivatives | Access pending |

---

## KEY TECHNICAL NOTES

**demo.breadbot.app server:** `/opt/projects/breadbot/dashboard/server.py`
  Runs as breadbot-dashboard.service, uvicorn --workers 2, port 8001, nginx proxies.
  Backups: server.py.bak, server.py.bak2, server.py.bak3
  Panel dist at: `dashboard/panel_dist/` (copied from panel/frontend/dist/)

**panel mcp_proxy:** `/opt/projects/breadbot/panel/mcp_proxy.py`
  Backup: mcp_proxy.py.bak
  Alerts endpoint now wraps + maps fields before returning to React

**DB schema reminder:**
  - `meme_alerts`: id, chain, token_addr, token_name, symbol, price_usd, liquidity, volume_24h, mcap, rug_score, rug_flags, alert_sent, decision, created_at
  - `positions`: id, chain, token_addr, token_name, symbol, entry_price, quantity, cost_basis_usd, stop_loss_usd, take_profit_25, take_profit_50, status, exchange, opened_at, closed_at
  - `trades`: id, position_id, action, price_usd, quantity, usd_value, fee_usd, pnl_usd, tx_hash, exchange, executed_at

**AUTO_EXECUTE:** execution_mode=auto, auto_strategy=conservative in bot_config DB.
  Conservative = score ≥ 83, mcap < $1M, 5 trades/day max.

---

## GITHUB LOG (Sessions 36–38)
```
72d271d  fix: add composite index to alt_data_signals DB init for Railway deploys
002cff7  feat: demo serves panel React UI + alerts field mapping fix
0a7fee1  fix: alerts table quote injection + signals O(n2) query
fe40c3b  fix: dedup gate in process_pair + axiom queue condition bug
```
