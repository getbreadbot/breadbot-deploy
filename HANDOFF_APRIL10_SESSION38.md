# Breadbot — Master Session Handoff
*April 10, 2026 — Session 38*

## FIRST ACTION EVERY SESSION
```bash
ssh vps "python3 /opt/projects/breadbot/session_state.py update"
```

## INFRASTRUCTURE
| Service | URL | Status |
|---|---|---|
| Landing | breadbot.app | Live |
| Demo dashboard | demo.breadbot.app | Live — panel React UI, all 12 pages verified |
| License server | keys.breadbot.app:8002 | Live |
| MCP server | mcp.breadbot.app | Live |
| Web panel | panel.breadbot.app | Live |
| GitHub | github.com/getbreadbot/breadbot-deploy | Latest: **059d2c6** |

**VPS:** 76.13.100.34 — `ssh vps` — `/opt/projects/breadbot/`
**Bot PID:** verify with `pgrep -fa main.py`
**Deploy repo:** `/Users/adrez/Desktop/cryptobot/deploy_repo/`
**AUTO_EXECUTE:** ON — conservative strategy (score ≥ 83)

---

## WHAT WAS COMPLETED SESSION 38

### demo.breadbot.app → panel React UI (full sync)
- 24 panel-compatible API endpoints added to demo server with auth stubs
- Panel dist copied to `dashboard/panel_dist/`
- All 12 pages browser-audited and verified working

### Data shape fixes (demo + panel mcp_proxy)
- Alerts: `{alerts:[...]}` wrapping, DB→React field mapping, `rug_flags` JSON parsing
- Yields: `{platforms:[...]}` wrapping with APY/type fields
- Positions: `{positions:[...]}` wrapping
- Funding rates: venue metadata, threshold config, rate objects
- Strategy/performance: `yield_rebalancer`, `closed_pnl_usd`, `volume_usd` fields
- PnL history: `realized_pnl`, `cumulative` fields

### React component fixes
- Alerts.jsx: default filter "All", loading spinner, non-mutating `[...].reverse()`, error logging
- Performance.jsx: null-safe guards (`?.` and `?? 0`) on all `.toFixed()`/`.toLocaleString()` calls
- Layout.jsx: footer moved inside `<main>` — was creating invisible 1050px overlay blocking ALL clicks
- styles.css: `.main` now `display:flex; flex-direction:column` for footer positioning
- App.jsx: added missing `<Route path="backtest">`

### Backtest fixes
- `trigger_backtest()` in mcp_server.py: all output goes to log file, JSON validated before write
- Previously rate-limit logs corrupted `backtest_last.json` — now impossible
- Killed 4 rogue concurrent backtest processes
- Regenerated `backtest_last.json` from bt_90 sweep (751 trades, 15% win rate)

### Infrastructure
- Nginx no-cache headers for HTML, immutable cache for hashed assets
- Alt data signals composite index added to DB init for Railway deploys

---

## ALL 12 PAGES VERIFIED ✅
Dashboard, Alerts (200 cards), Positions, Yields (17 platforms), Controls,
Signal Channels, Grid Trading, Funding Arb, Performance, Backtest (751 trades),
Settings — all rendering, all buttons functional.

---

## NEXT SESSION PRIORITY ORDER
1. Verify auto_executor writes to `positions` table (0 open despite 15+ auto_buy decisions)
2. Re-render 3 stale videos (scanner_alerts, yields_page, strategy_setup)
3. Dashboard: Auto-execute shows OFF but bot is in auto mode — fix field mapping

## BLOCKED ON MORGAN
| Item | Action |
|---|---|
| Whop Publish | Admin toggle → Publish |
| Instagram Reels | Mobile upload — 18 videos |
| Robinhood funding | Load funds |

## BLOCKED EXTERNAL
| Item | Status |
|---|---|
| Arkham API key | Approval pending |
| Coinbase CFM derivatives | Access pending |

---

## GITHUB LOG (Sessions 36–38)
```
059d2c6  fix: backtest trigger corruption + alerts loading state
48d67b5  fix: footer overlay blocking all clicks + overlapping content
6798760  fix: Performance null safety, strategy perf fields, pnl history fields
0aec60c  fix: all panel data shape mismatches — yields, positions, funding, alerts default
72d271d  fix: add composite index to alt_data_signals DB init for Railway deploys
002cff7  feat: demo serves panel React UI + alerts field mapping fix
0a7fee1  fix: alerts table quote injection + signals O(n2) query
fe40c3b  fix: dedup gate in process_pair + axiom queue condition bug
```
