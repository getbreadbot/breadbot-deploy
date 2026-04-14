# Breadbot — Master Session Handoff
*April 8, 2026 — Session 35*

## FIRST ACTION EVERY SESSION
```bash
ssh vps "python3 /opt/projects/breadbot/session_state.py update"
```

## INFRASTRUCTURE
| Service | URL | Status |
|---|---|---|
| Landing | breadbot.app | Live |
| Demo dashboard | demo.breadbot.app | Live |
| License server | keys.breadbot.app:8002 | Live |
| MCP server | mcp.breadbot.app | Live |
| Web panel | panel.breadbot.app | Live |
| GitHub | github.com/getbreadbot/breadbot-deploy | Latest: **e1f4d8c** |
| Railway template | https://railway.com/deploy/breadbot | Live |
| Railway test project | 5536ed8c | 1 service: terrific-nourishment (passing) |

**VPS:** 76.13.100.34 — `ssh vps` — `/opt/projects/breadbot/`
**Bot PID:** 2477550 (verify with `pgrep -fa main.py`)
**Deploy repo:** `/Users/adrez/Desktop/cryptobot/deploy_repo/`

---

## WHAT WAS COMPLETED SESSIONS 33–35

### Session 33 — X re-render posts + Railway smoke test ✅
All 9 re-render X posts fired (EN/ES/BR × yields_page/scanner_alerts/strategy_setup).
Railway smoke test passed after 11 dep-conflict fix commits.

### Session 34 — Two critical deploy bugs fixed ✅
**commit c763a09:** `scanner/` stub package (empty `__init__.py`) was shadowing `scanner.py` — Python picked the package, silently breaking `scan_loop` import on every Railway deploy. Bot started but scanner never ran.
**commit e1f4d8c:** `broadcaster.py` was on VPS but never committed to deploy_repo. Broke scanner import chain.
**Buyer flow test: PASSED** on service `a6010632` (now deleted). Logs confirmed: `Scanner loop started`, 4 alerts on first cycle, `GET /api/health 200 OK`, all 9 tasks started.

### Session 35 — Vaultwarden + backtest analysis ✅
3 YouTube token entries saved to Breadbot folder in Vaultwarden:
- YouTube Token — EN → itemId f43a241a (YOUTUBE_REFRESH_TOKEN)
- YouTube Token — ES → itemId a896026c (YOUTUBE_REFRESH_TOKEN_ES)
- YouTube Token — PT → itemId a3058b4e (YOUTUBE_REFRESH_TOKEN_PT)

Railway test project 5536ed8c cleaned: only `terrific-nourishment` remains (healthy on e1f4d8c).

Backtest analysis of pre-April 3 data (1140 trades, Mar 4–30):
- All thresholds 70–90: win rate 21–24%, below break-even of ~28.6%
- score≥95: 50% win rate but only 14 trades — not reliable
- **This data predates Session 27 scoring improvements** (momentum scoring, Axiom signals, h1 pump penalty) — not actionable for AUTO_EXECUTE decision

Fresh backtest triggered on new scorer: PID 3050077, mode=all, min_score=75, days=7
Output: `/opt/projects/breadbot/data/backtest_last.json`

---

## CURRENT BOT STATE (VPS)
| Component | Status |
|---|---|
| Scanner | ACTIVE — running every 300s |
| Gemini | AUTH OK on startup |
| Axiom | ON — DEXScreener boosts loaded, auto-refresh on 401 |
| Telegram auth | Patched — sender chat_id validation on all commands |
| Alt data | composite=-38.4, fear/greed=17 (Extreme Fear) |
| Yield platforms | 16 — Coinbase Morpho top at 8.24% |
| Backtest | Running PID 3050077 (started ~Apr 8 13:30 UTC) |

---

## NEXT SESSION PRIORITY ORDER

### 1. Read backtest results (first action after session_state update)
```bash
ssh vps "python3 /tmp/read_bt.py"
# or
ssh vps "python3 -c \"import json; d=json.load(open('/opt/projects/breadbot/data/backtest_last.json')); [print(k,v) for k,v in d.items() if not isinstance(v,list)]\""
```
If win_rate > 30% → consider setting `SET_MIN_SCORE_THRESHOLD` and enabling `AUTO_EXECUTE`.

### 2. Whop Publish — MANUAL CLICK REQUIRED
Cross-origin iframe blocks all automation. Morgan must:
1. Go to `whop.com/joined/breadbot/updates-and-guides-usZJ4P5X9K6mML/app/`
2. Click **Admin** toggle (top right of app)
3. Click **Publish** on the "Unnamed document" draft

### 3. Railway test project — assign domain to terrific-nourishment
Railway dashboard → project 5536ed8c → terrific-nourishment → Settings → Domains → Generate Domain
Navigate to domain, confirm panel loads at `/` and `/api/health` returns 200.

### 4. Commit workers=4 note to README
```bash
cd /Users/adrez/Desktop/cryptobot/deploy_repo
echo "" >> README.md
echo "## Panel Workers" >> README.md
echo "The panel runs with workers=4 by default (set in panel/railway.toml). Reduce to 1 if RAM constrained." >> README.md
git add README.md && git commit -m "docs: note panel workers=4 default" && git push origin main
```

---

## BLOCKED ON MORGAN
| Item | Action |
|---|---|
| Instagram Reels | Mobile upload — 6 EN + 6 ES + 6 BR = 18 videos |
| Robinhood funding | Load funds into account |
| Coinbase Commerce | Create at commerce.coinbase.com + add webhook to license server |

## BLOCKED EXTERNAL
| Item | Status |
|---|---|
| Arkham API key | Approval pending → inject as `ARKHAM_API_KEY` when received |
| Coinbase CFM derivatives | Access pending — needed for funding arb live trading |
| Gemini API key (Whop buyers) | Railway template requires buyer to supply their own |

---

## KEY TECHNICAL NOTES

**scanner/ stub pattern:** `git ls-files scanner/` is the fast check. If files appear, the stub is re-introduced and needs `git rm -r scanner/`. The VPS has `scanner.py` (correct), no `scanner/` directory.

**broadcaster.py:** Now committed to deploy_repo (e1f4d8c). If scanner import errors recur on Railway, check if new VPS-only files exist: `ssh vps "ls *.py" | diff - <(ls /Users/adrez/Desktop/cryptobot/deploy_repo/*.py | xargs -I{} basename {})`.

**Railway redeploy pattern:** `serviceInstanceRedeploy` replays the cached commit — won't pick up new pushes. For new commit deploys on an existing project without GitHub webhook: use `githubRepoDeploy` (creates a new service), verify it passes, then delete the temp service. Or push a trigger commit and let webhook fire if GitHub integration is wired.

**Vaultwarden automation:** Works via `execCommand('insertText')` to fill fields + `form.requestSubmit()` to save. The Save button is blocked by Bitwarden extension — osascript and direct button clicks both fail. The form submit approach bypasses it.

**Backtest data location:**
- `/tmp/bt_all_trades.json` — 1140 raw pre-April trades (March 4–30)
- `/opt/projects/breadbot/data/backtest_last.json` — latest run output (fresh, post-April scorer)
- `/tmp/bt_90.json` — completed score≥90 run: 15% win rate, −$6,732 (pre-April scorer)

**Whop cross-origin:** All auth is HttpOnly cookies — no JS extraction. Whop API at `api.whop.com` requires Bearer token not accessible from parent frame. Publish is a hard manual requirement.

---

## SOCIAL CONTENT STATUS
All 6 videos × EN/ES/BR posted on X and YouTube ✅
Instagram Reels: all ⏳ (Morgan mobile)
TikTok: all ⏳

## GITHUB LOG (Sessions 33–35)
```
e1f4d8c  fix: add missing broadcaster.py — was on VPS but never committed, broke scanner import
b546c76  chore: trigger Railway rebuild on c763a09 (scanner stub fix)
c763a09  fix: remove scanner/ stub package — shadows scanner.py, breaking scan_loop import on Railway
b4ce4d4  fix: remove set -e, wrap DB init in try/except
[...11 Railway dep-fix commits from Session 33...]
```
