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
| Demo dashboard | demo.breadbot.app | **Live — panel React UI, all 12 pages verified** |
| License server | keys.breadbot.app:8002 | Live |
| MCP server | mcp.breadbot.app | Live |
| Web panel | panel.breadbot.app | Live (rebuilt dist deployed) |
| GitHub | github.com/getbreadbot/breadbot-deploy | Latest: **6798760** |

**VPS:** 76.13.100.34 — `ssh vps` — `/opt/projects/breadbot/`
**Bot PID:** 3412125 (verify with `pgrep -fa main.py`)
**Deploy repo:** `/Users/adrez/Desktop/cryptobot/deploy_repo/`
**AUTO_EXECUTE:** ON — conservative strategy (score ≥ 83)

---

## WHAT WAS COMPLETED SESSION 38

### Demo → Panel React UI (full sync)
- Added 24 panel-compatible API endpoints to demo server with auth stubs
- Copied panel dist to `dashboard/panel_dist/`
- Demo now serves identical React UI as panel.breadbot.app (no login required)

### Alerts fix (demo + panel)
- Root cause: React expected `{"alerts": [...]}`, endpoints returned raw `[...]`
- Field mapping: `rug_score`→`security_score`, `token_name`→`token`, `token_addr`→`contract`, etc.
- `rug_flags` JSON array parsed into `[{label, type}]` with risk/warn/ok classification
- Default filter changed from "Pending" to "All" (all alerts are expired, Pending showed empty)

### Yields fix (demo + panel)
- Wrapped in `{platforms: [...]}` with `type` mapped from `asset`
- Added `rebalance_threshold` and `last_updated` fields

### Positions fix (demo + panel)
- Wrapped in `{positions: [...]}`

### Funding rates fix (demo + panel)
- Added venue metadata: `venue_label`, `venue_color`, `venue_legal_us`
- Added threshold config: `entry_threshold_pct`, `exit_threshold_pct`
- Rate objects: `{pair, rate_8h_pct, annualized_pct, above_entry}`

### Performance page fix
- **Root cause:** React called `.toFixed()` / `.toLocaleString()` on undefined fields with no null guards
- Added `?.` and `?? 0` guards on every unsafe call in Performance.jsx
- Added missing server fields: `yield_rebalancer`, `closed_pnl_usd`, `volume_usd`, `realized_pnl`, `cumulative`
- Same field normalization in panel mcp_proxy.py

### Backtest page fix
- Added missing `<Route path="backtest">` to App.jsx
- Populated `backtest_last.json` with bt_90 sweep data (751 trades, 15% win rate)

### Other
- Alt data signals composite index added to `ensure_alt_data_table()` for Railway deploys
- Panel dist rebuilt 3x during session, deployed to both services each time

---

## FULL PAGE AUDIT — ALL 12 PAGES VERIFIED ✅

| Page | Status | Notes |
|---|---|---|
| Dashboard | ✅ | Active, P&L, positions, config card |
| Alerts | ✅ | 200 cards, scores, flags, "All" default |
| Positions | ✅ | Empty (correct — 0 open) |
| Yields | ✅ | 17 platforms, APY bars, "Best" badge |
| Controls | ✅ | Trading active, auto-execute toggle |
| Signal Channels | ✅ | Active/inactive, hits, add button |
| Grid Trading | ✅ | STANDBY, RSI gauge, config |
| Funding Arb | ✅ | Bybit venue, thresholds, CFM recommendation |
| Performance | ✅ | Stat cards, strategy table, chart placeholder |
| Backtest | ✅ | 751 trades, outcome chart, run controls |
| Settings | ✅ | Risk params, advanced keys |

---

## AUTO_EXECUTE TRADES
| Symbol | Score | Time (UTC) |
|---|---|---|
| ETF | 85 | Apr 10 02:50 |
| Daisy | 85 | Apr 10 02:45 |
| Billy | 99 | Apr 10 02:14 |
| CHITOSHI | 88 | Apr 10 00:19 |
| FART | 90 | Apr 10 00:19 |
| PETE | 92 | Apr 9 03:37 |
| CHUD | 95 | Apr 9 01:47 |
| Cupsey | 93 | Apr 9 01:32 |
| TRENCHOOR | 98 | Apr 9 01:12 |
| BIGREVEAL | 86 | Apr 9 00:27 |

All within conservative threshold (score ≥ 83).

---

## NEXT SESSION PRIORITY ORDER

### 1. Verify auto_executor writes to positions table
`positions` table has 0 open rows despite 10+ auto_buy decisions. Check if auto_executor creates position entries or only records decisions in meme_alerts.

### 2. Re-render 3 stale videos
scanner_alerts, yields_page, strategy_setup — screenshot current panel UI.

### 3. Dashboard minor improvements
- Auto-execute shows OFF on dashboard but bot is in auto mode — check field mapping
- Daily loss limit shows "$— remaining" — needs computation

### 4. Whop Publish — Morgan manual action

---

## BLOCKED ON MORGAN
| Item | Action |
|---|---|
| Whop Publish | Admin toggle → Publish |
| Instagram Reels | Mobile upload — 18 videos |
| Robinhood funding | Load funds |
| Coinbase Commerce | Create + webhook |

## BLOCKED EXTERNAL
| Item | Status |
|---|---|
| Arkham API key | Approval pending |
| Coinbase CFM derivatives | Access pending |

---

## KEY TECHNICAL NOTES

**demo server backups:** server.py.bak, .bak2, .bak3
**panel mcp_proxy backup:** mcp_proxy.py.bak
**Performance.jsx backup:** Performance.jsx.bak

**DB schema (key tables):**
- `meme_alerts`: id, chain, token_addr, token_name, symbol, price_usd, liquidity, volume_24h, mcap, rug_score, rug_flags, alert_sent, decision, created_at
- `positions`: id, chain, token_addr, token_name, symbol, entry_price, quantity, cost_basis_usd, stop_loss_usd, take_profit_25, take_profit_50, status, exchange, opened_at, closed_at
- `trades`: id, position_id, action, price_usd, quantity, usd_value, fee_usd, pnl_usd, tx_hash, exchange, executed_at

**Panel React dist location:** `/opt/projects/breadbot/panel/frontend/dist/`
**Demo panel dist copy:** `/opt/projects/breadbot/dashboard/panel_dist/`
**Both must be updated when React source changes** (rebuild + copy + restart both services)

---

## GITHUB LOG (Sessions 36–38)
```
6798760  fix: Performance null safety, strategy perf fields, pnl history fields
0aec60c  fix: all panel data shape mismatches — yields, positions, funding, alerts default
72d271d  fix: add composite index to alt_data_signals DB init for Railway deploys
002cff7  feat: demo serves panel React UI + alerts field mapping fix
0a7fee1  fix: alerts table quote injection + signals O(n2) query
fe40c3b  fix: dedup gate in process_pair + axiom queue condition bug
```
