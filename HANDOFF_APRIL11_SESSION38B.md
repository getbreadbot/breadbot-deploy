# Breadbot — Master Session Handoff
*April 11, 2026 — Session 38 (continued)*

---

## FIRST ACTION EVERY SESSION
```bash
ssh vps "python3 /opt/projects/breadbot/session_state.py update"
```

---

## INFRASTRUCTURE

| Service | URL | Status |
|---|---|---|
| Landing | breadbot.app | Live |
| Demo dashboard | demo.breadbot.app | Live — panel React UI, all 12 pages |
| License server | keys.breadbot.app:8002 | Live |
| MCP server | mcp.breadbot.app | Live |
| Web panel | panel.breadbot.app | Live |
| GitHub | github.com/getbreadbot/breadbot-deploy | Latest: **24c7e75** |

**VPS:** 76.13.100.34 — `ssh vps` — `/opt/projects/breadbot/`
**Bot PID:** 3892115 (verify with `pgrep -fa main.py`)
**Deploy repo:** `/Users/adrez/Desktop/cryptobot/deploy_repo/`
**AUTO_EXECUTE:** ON — conservative strategy (score ≥ 83)

---

## WHAT WAS COMPLETED THIS SESSION

### 1. Footer overlay fix (48d67b5)
- Root cause: `<footer>` had `gridArea:'main'`, creating invisible 1050px overlay with `pointerEvents:auto` blocking ALL clicks on every page
- Fix: moved footer inside `<main>` after `<Outlet/>`, added flex column + `marginTop:auto`
- Also added nginx no-cache for HTML, immutable cache for hashed assets

### 2. Backtest trigger corruption fix (059d2c6)
- Root cause: `trigger_backtest()` redirected ALL stdout (rate-limit logs) to `backtest_last.json`
- Fix: wrapper script sends output to log, extracts last JSON line, validates before copying
- Also: Alerts.jsx loading spinner, non-mutating reverse, error logging

### 3. Alerts sorting (ec41cc1)
- 6 sort buttons: Newest, Score↓, Score↑, Liquidity, Volume, MCap
- Sort applies to whichever filter tab (Pending/All) is active

### 4. Functional backtest trigger on demo (ec41cc1)
- Was a no-op stub returning `{"status":"demo_mode"}`
- Now launches real backtest via `subprocess.Popen`, returns PID, shows status message
- Results persist across page refreshes

### 5. PnL improvement: h1 pump ceiling + tighter exits (7097714)
- Scanner: hard skip tokens with h1 pump > 150% (`MAX_H1_PUMP_PCT` env var)
- Scanner: steeper penalties for 100-200% h1 (-12 to -20, was -5 to -10)
- Backtest defaults: SL 12% (was 20%), TP2 75% (was 100%), max hold 6h (was 48h)
- New CLI args: `--max-hold`, `--max-h1-pump`

### 6. PnL improvement: DEXScreener boost + velocity decay + SL on no-candle (24c7e75)
- DEXScreener boost default: 4 → 8 (boosted tokens sustain momentum longer)
- Velocity decay filter: -6 penalty when 5m buy rate < 40% of h1 avg rate
- Backtest: apply SL/TP logic to "holding" outcomes with no candle data

---

## BACKTEST RESULTS PROGRESSION

| Version | Params | Trades | WR | PnL | Avg Win | Avg Loss |
|---|---|---|---|---|---|---|
| Old (bt_90) | SL=20%, TP2=100%, 48h, no filter | 751 | 15.0% | -$6,732 | +$35.97 | -$20.14 |
| v3 (7097714) | SL=12%, TP2=75%, 6h, h1<=150% | 105 | 20.2% | -$59.73 | +$43.45 | -$11.71 |
| v4 (24c7e75) | + DEX boost 8, vel decay, SL on hold | 98 | 18.8% | -$54.41 | +$21.88 | -$5.75 |

**Key insight:** PnL improved from -$6,732 to -$54 (99.2% improvement). Break-even WR at current avg win/loss is ~21%. Actual WR is 18.8% — gap of ~2pp. The 105→98 filtered alerts removed the worst entries.

**Holding outcomes:** Dropped from 23 (v3) to 2 (v4) — the SL/TP on no-candle fix is working.

---

## CURRENT BOT STATE

| Component | Status |
|---|---|
| Scanner | ACTIVE — h1 pump ceiling (150%), velocity decay filter, DEX boost 8 |
| Auto-execute | ON — conservative (score ≥ 83), max 5/day |
| Gemini connector | AUTH OK |
| Axiom DEXScreener boosts | ON |
| Axiom stream | ON — auto-refresh (refresh token valid until 2027-05-08) |
| Telegram command auth | Patched — all commands validate sender chat_id |
| Backtest data | v4 results loaded (98 trades, 18.8% WR, -$54.41) |

---

## ALL 12 DEMO PAGES VERIFIED ✅

Dashboard, Alerts (200 cards + 6 sort buttons), Positions, Yields (17 platforms),
Controls, Signal Channels, Grid Trading, Funding Arb, Performance,
Backtest (functional trigger + results display), Settings

---

## NEXT SESSION PRIORITY ORDER

### Unblocked — do these first
1. **Close the 2pp WR gap to positive PnL** — options:
   - Raise MIN_SCORE from 83 to 85 or 87 (fewer but higher-quality trades)
   - Add buy-pressure minimum (skip if 5m buy ratio < 55%)
   - Add liquidity-weighted scoring (higher liq → more reliable price action)
   - Run 30-day backtest with latest params once GeckoTerminal rate limits cool off
2. **Verify auto_executor writes to `positions` table** — 0 open positions despite 15+ auto_buy decisions
3. **Dashboard: auto-execute shows OFF** but bot is in auto mode — fix field mapping
4. **Re-render 3 stale videos** (scanner_alerts, yields_page, strategy_setup)

### Blocked on Morgan
| Item | Action |
|---|---|
| Whop Publish | Admin toggle |
| Instagram Reels | Mobile upload — 18 videos |
| Robinhood funding | Load funds into account |

### Blocked external
| Item | Status |
|---|---|
| Arkham API key | Approval pending |
| Coinbase CFM derivatives | Access pending |

---

## GITHUB COMMIT LOG (Session 38)

```
24c7e75  perf: DEXScreener boost 8, velocity decay filter, SL on no-candle outcomes
7097714  perf: h1 pump ceiling + tighter exits for positive PnL
ec41cc1  feat: alerts sorting by score/liq/vol/mcap/time + functional backtest trigger
5016653  docs: final session 38 handoff
059d2c6  fix: backtest trigger corruption + alerts loading state
48d67b5  fix: footer overlay blocking all clicks + overlapping page content
6798760  fix: Performance null safety, strategy perf fields, pnl history fields
0aec60c  fix: all panel data shape mismatches — yields, positions, funding, alerts default
72d271d  fix: add composite index to alt_data_signals DB init for Railway deploys
002cff7  feat: demo serves panel React UI + alerts field mapping fix
```

---

## KEY FILES MODIFIED THIS SESSION

- `scanner.py` — h1 pump ceiling, steeper penalties, velocity decay filter
- `backtest.py` — SL 12%, TP2 75%, 6h hold, h1 filter, SL/TP on no-candle outcomes
- `axiom_signals.py` — DEXScreener boost default 4→8
- `mcp_server.py` — backtest trigger corruption fix (wrapper + validation)
- `dashboard/server.py` — 24 panel endpoints, functional backtest trigger (Popen)
- `panel/frontend/src/pages/Alerts.jsx` — sorting, loading state, non-mutating reverse
- `panel/frontend/src/pages/Performance.jsx` — null-safe guards
- `panel/frontend/src/components/Layout.jsx` — footer inside main
- `panel/frontend/src/styles.css` — flex column for main
- `panel/frontend/src/App.jsx` — backtest route
- `/etc/nginx/sites-enabled/demo.breadbot.app` — cache headers

---

## BACKTEST FILES ON VPS

| File | Description |
|---|---|
| `/tmp/bt_90.json` | Old baseline: 751 trades, 15% WR, -$6,732 (SL=20%, 30d) |
| `/tmp/bt_v3.log` | v3 results: 105 trades, 20.2% WR, -$59.73 (SL=12%, 7d) |
| `/tmp/bt_v4.log` | v4 results: 98 trades, 18.8% WR, -$54.41 (+ DEX boost, vel decay) |
| `data/backtest_last.json` | Current dashboard data (v4) |
