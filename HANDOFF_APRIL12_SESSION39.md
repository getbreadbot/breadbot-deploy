# Breadbot — Master Session Handoff
*April 12, 2026 — Session 39*

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
| GitHub | github.com/getbreadbot/breadbot-deploy | Latest: **f9512e4** |

**VPS:** 76.13.100.34 — `ssh vps` — `/opt/projects/breadbot/`
**Bot PID:** 110195 (verify with `pgrep -fa main.py`)
**Deploy repo:** `/Users/adrez/Desktop/cryptobot/deploy_repo/`
**AUTO_EXECUTE:** ON — conservative strategy (score >= 83), SL 8%
---

## WHAT WAS COMPLETED THIS SESSION

### 1. Position recording after auto-execution (24e7db4)
- ROOT CAUSE: exchange_executor.py read EXECUTION_MODE from env only (never set) so always returned False and no trades ever executed on-chain
- FIX 1: Added _db_get_config() to exchange_executor.py — reads execution_mode from bot_config DB first (where it is set to auto)
- FIX 2: Added record_position() to scanner.py — INSERTs into positions table after successful trade with SL/TP levels
- FIX 3: main.py startup display now reads execution_mode from bot_config DB and correctly shows Auto-execute ON

### 2. Dashboard auto-execute field mapping (24e7db4)
- ROOT CAUSE: /api/settings/basic returned env var value; Controls.jsx checked === true instead of === auto
- FIX: server.py reads execution_mode + auto_strategy from bot_config DB; Controls.jsx checks for auto, toggle writes auto/manual

### 3. Stop loss tightened 12% to 8% (f9512e4)
- Analysis of 98 trades (v4 backtest):
  - Score does NOT differentiate winners (avg 93.3) vs losers (avg 93.4)
  - Losers dump in first candle (0.9 bars / 5 min) vs winners (2.9 bars / 14 min)
  - Score=100 tokens had WORST WR (15%, 2/13 wins)
  - At SL 8%: BE WR = 14.8%, current WR = 18.4% — expected positive PnL
  - At SL 12%: BE WR = 20.6%, current WR = 18.4% — confirmed negative PnL
- Updated defaults in backtest.py and scanner.py record_position

### 4. Screenshots recaptured for stale videos
- Updated capture script for React panel routes (/dashboard, /alerts, /yields, /controls)
- 16 screenshots captured across EN/ES/PT for scanner_alerts, yields_page, strategy_setup

### 5. Remotion video re-renders (in progress at wrap)
- 12/18 renders complete at wrap time, remaining 6 running unattended (PID 28604 on local Mac)
- scanner_alerts: all 6 renders done (13.1MB 16:9, 9.8MB 9:16)
- yields_page: all 6 renders done (11.0MB 16:9, 9.6MB 9:16)
- strategy_setup: in progress (6 renders remaining)
- Entry point fix: must use src/index.tsx not src/index.ts
---

## BACKTEST ANALYSIS (Session 39 deep dive)

| Metric | Value |
|---|---|
| Dataset | 98 trades, 7 days, score >= 83 |
| Win Rate | 18.4% (18 wins, 80 losses) |
| Avg Win | +$21.88 |
| Avg Loss | -$5.69 (at SL 12%) |
| Net PnL | -$54.41 (at SL 12%) |
| Est PnL at SL 8% | Positive (BE WR 14.8% below actual 18.4%) |

Score threshold sweep (raising threshold does NOT help):
- >= 83: 98 trades, 18.4% WR, -$54
- >= 85: 93 trades, 18.3% WR, -$66
- >= 90: 76 trades, 17.1% WR, -$89
- >= 92: 62 trades, 19.4% WR, -$45
- >= 95: 43 trades, 20.9% WR, -$26

Key insight: Score measures rug-pull safety, not price momentum. Raising threshold removes volume without improving quality.

Score=100 analysis: 13 trades, 2 wins (15% WR) — worst cohort.

---

## CURRENT BOT STATE

| Component | Status |
|---|---|
| Scanner | ACTIVE — h1 pump ceiling (150%), velocity decay, DEX boost 8, SL 8% |
| Auto-execute | ON — conservative (score >= 83), max 5/day |
| Position recording | FIXED — now writes to positions table |
| Gemini connector | AUTH OK |
| Axiom stream | ON — auto-refresh (refresh token valid until 2027-05-08) |
| Fear and Greed | 16 (Extreme Fear) |
| Dashboard settings | Fixed — shows auto mode correctly |
---

## GITHUB COMMIT LOG (Session 39)

```
f9512e4  perf: tighten stop loss 12% -> 8% based on backtest analysis
24e7db4  fix: position recording after auto-exec, DB-first execution_mode reads, dashboard auto-execute display
```

---

## KEY FILES MODIFIED THIS SESSION

- exchange_executor.py — DB-first execution_mode read via _db_get_config()
- scanner.py — record_position() function, SL default 12 to 8
- main.py — _get_execution_mode() reads from bot_config DB
- dashboard/server.py — settings/basic reads from bot_config DB
- panel/frontend/src/pages/Controls.jsx — check auto not true
- backtest.py — SL default 12 to 8

---

## NEXT SESSION PRIORITY ORDER

### Unblocked
1. Verify strategy_setup renders completed — check video_pipeline/output/
2. Upload re-rendered videos to YouTube and X — 18 videos total
3. Run 7-day backtest with SL 8% (GeckoTerminal rate limits should have reset)
4. Consider score=100 penalty — 15% WR is the worst cohort
5. Monitor real trading results — auto trades should now execute and record positions

### Blocked on Morgan
| Item | Action |
|---|---|
| Instagram Reels | Mobile upload — 18 videos |
| Robinhood funding | Load funds into account |

### Blocked external
| Item | Status |
|---|---|
| Arkham API key | Approval pending |
| Coinbase CFM derivatives | Access pending |

---

## IMPORTANT TECHNICAL NOTES

- Remotion entry point: Must use src/index.tsx (not src/index.ts)
- GeckoTerminal rate limits: Severe. Never run concurrent backtests. Use --throttle 3000+
- Bot_config DB is source of truth for execution_mode (env var intentionally not set)
- Backup files: exchange_executor.py.bak, scanner.py.bak, main.py.bak, server.py.bak, Controls.jsx.bak, backtest.py.bak2