# Breadbot — Master Session Handoff
*April 5, 2026 — Session 33*

## FIRST ACTION EVERY SESSION
```bash
ssh vps "python3 /opt/projects/breadbot/session_state.py update"
```

## INFRASTRUCTURE
GitHub latest: **b4ce4d4** | VPS: 76.13.100.34 | Service: breadbot-scanner.service (PID active)

## WHAT WAS COMPLETED THIS SESSION

### X posts — all 9 re-render videos fired ✅
- yields_page / scanner_alerts / strategy_setup × EN/ES/BR
- Tweet IDs: 2040860702 through 2040860787
- poster.py YOUTUBE_VIDEO_MAP updated with new re-render IDs, backed up

### Railway smoke test — PASSED ✅
After 11 commits resolving cascading dep conflicts, the template now deploys successfully.

**Root causes found and fixed:**
| Fix | Commit |
|---|---|
| Remove unused python-telegram-bot, httpx 0.27→0.28.1 | 81fc1e4 |
| python-dotenv 1.0.1→1.1.1, aiohttp/rich aligned | bd57613 |
| Full requirements.txt rebuilt from VPS venv freeze | 5aee64e |
| Dockerfile Python 3.11→3.12 (match VPS) | 4537fe9 |
| Remove driftpy (DRIFT_ENABLED=false, lazy import) | eebc3d7 |
| fastmcp 3.1.0→2.3.5, websockets 14.1, exceptiongroup 1.2.2 | 26b76e9 |
| websockets 15.0.1→14.1 (solana requires <15.0 strict) | 99e3121 |
| Remove anyio==4.4.0 pin (mcp>=1.9 needs >=4.5) | a45ad56 |
| wait on panel PID only, not bot+panel | d2a8d22 |
| panel/requirements.txt httpx/pydantic aligned | 16a059f |
| Remove set -e, wrap DB init in try/except | b4ce4d4 |

**Test project:** `5536ed8c-3741-4c7f-8b95-268e4592202f` — service "terrific-nourishment" ACTIVE
- Has ~12 ghost failed services from iterative testing — clean up next session

## CURRENT BOT STATE (VPS)
- Scanner: ACTIVE
- Gemini: AUTH OK
- Axiom: ON (auto-refresh on 401)
- Funding signals: BTC/ETH/SOL all negative (bearish)
- Yield platforms: 16, Coinbase Morpho top at 8.24%

## SOCIAL CONTENT STATUS
| Video | EN X | EN YT | ES X | ES YT | BR X | BR YT |
|---|---|---|---|---|---|---|
| yields_page (re-render) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| scanner_alerts (re-render) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| strategy_setup (re-render) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| All 6 originals | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
Instagram Reels: all ⏳ (Morgan mobile action)

## NEXT SESSION PRIORITY ORDER

### Unblocked
1. **Clean up Railway test project** — delete the ~12 ghost services in project `5536ed8c` (keep terrific-nourishment or just delete the whole project after confirming template deploys cleanly from `railway.com/deploy/breadbot`)
2. **Vaultwarden YouTube tokens** — unlock Vaultwarden, save 3 entries in Breadbot folder:
   - `YouTube Token — EN` = YOUTUBE_REFRESH_TOKEN (VPS .env line 193)
   - `YouTube Token — ES` = YOUTUBE_REFRESH_TOKEN_ES (VPS .env line 240)
   - `YouTube Token — PT` = YOUTUBE_REFRESH_TOKEN_PT (VPS .env line 241)
3. **Whop hub Publish** — navigate to `whop.com/joined/breadbot/updates-and-guides-usZJ4P5X9K6mML/app/`, switch Admin view, click Publish on the draft post inside the iframe (must be done manually — cross-origin JS blocked)
4. **Full buyer flow test** — deploy fresh from `railway.com/deploy/breadbot`, fill in real env vars (TELEGRAM_BOT_TOKEN, COINBASE_API_KEY, etc. from Vaultwarden test set), verify panel loads and Gemini startup message appears in logs
5. **Commit workers=4 note to deploy_repo README**

### Blocked on Morgan
- Instagram Reels upload (18 videos mobile)
- Robinhood funding
- Coinbase Commerce (commerce.coinbase.com + webhook)

### Blocked external
- Arkham API key (approval pending → inject as ARKHAM_API_KEY)
- Coinbase CFM derivatives access

## KEY NOTES
- `fastmcp==2.3.5` is now the pinned version in deploy_repo — do NOT bump to 3.x without auditing websockets/anyio/exceptiongroup conflicts against the full stack
- `driftpy` removed from requirements.txt — if DRIFT_ENABLED is ever turned on, needs separate install step or Dockerfile layer
- `panel/requirements.txt` must stay aligned with main `requirements.txt` on httpx and pydantic versions or pip will downgrade after install
- Railway `githubRepoDeploy` mutation creates a NEW service each call — use `serviceInstanceRedeploy` for redeploying existing services (but that uses cached commit). For new commit builds on existing services, push to GitHub and let webhook trigger, or use githubRepoDeploy and clean up the new service after.

## AXIOM COOKIE NOTE
auth-access-token expires every ~16 min. Bot auto-refreshes via refresh-token (valid until 2027-05-08).
If 401s persist: extract fresh cookie from Chrome DevTools → axiom.trade → update AXIOM_SESSION_COOKIE in VPS .env (hot-reloaded, no restart).
