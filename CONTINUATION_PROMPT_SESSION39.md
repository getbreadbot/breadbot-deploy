Continue Breadbot development. This is Session 39.

**Start by:**
1. Read the handoff: `ssh vps "cat /opt/projects/breadbot/HANDOFF_APRIL11_SESSION38B.md"`
2. Run session state: `ssh vps "python3 /opt/projects/breadbot/session_state.py update"`
3. Verify bot is running: `ssh vps "pgrep -fa main.py"`

**Context:** Breadbot is an automated crypto trading/scanning bot. I'm Morgan, the operator. You're the full-stack lead dev. VPS at 76.13.100.34 (`ssh vps`), deploy repo at `/Users/adrez/Desktop/cryptobot/deploy_repo/`, GitHub at `github.com/getbreadbot/breadbot-deploy`. Latest commit: `ae0888c`.

**Where we left off (Session 38):**

The demo dashboard (demo.breadbot.app) is fully synced to the panel React UI — all 12 pages working, footer overlay fixed, alerts sorting added (6 sort buttons), backtest trigger functional.

PnL improvement was the main focus. We implemented:
- Scanner: h1 pump hard ceiling (>150% → skip), steeper penalties, velocity decay filter (-6 when 5m buys < 40% of h1 avg)
- DEXScreener boost: 4→8
- Backtest: SL 12% (was 20%), TP2 75% (was 100%), max hold 6h (was 48h), SL/TP applied to no-candle outcomes
- Result: PnL improved from -$6,732 to -$54 (99.2% better) but still marginally negative (-2pp below break-even WR of 21%)

**Priority for this session:**
1. Close the 2pp WR gap to flip PnL positive — raise MIN_SCORE threshold, add buy-pressure minimum, or liquidity-weighted scoring. Run a 30-day backtest with latest params.
2. Verify auto_executor actually writes to `positions` table (showing 0 despite 15+ auto_buy decisions)
3. Fix dashboard auto-execute field mapping (shows OFF but bot is in auto mode)
4. Re-render 3 stale videos (scanner_alerts, yields_page, strategy_setup)
