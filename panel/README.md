# Breadbot Web Control Panel

Web-based control panel for Breadbot. Deploys as a second Railway service alongside the bot.
Communicates with the bot exclusively through the MCP server — no direct bot access.

## What it does

- Real-time trade alerts with Buy/Skip buttons (WebSocket)
- Browser push notifications when the panel tab is closed
- Pause/resume trading, toggle auto-execute
- Position management with close button
- Yield comparison table with one-click rebalance
- Settings editor — basic (risk/filters) and advanced (API keys)

## Architecture

```
Railway project
├── breadbot          (bot service — existing)
└── breadbot-panel    (this service)
      ├── FastAPI backend (Python)
      └── React frontend (built to static, served by FastAPI)
```

The panel talks to the bot via MCP tool calls only.
The panel writes env var changes to the bot service via the Railway API.

## Deployment

### Add to Railway template

In `railway.toml` of the bot template, add a second service block:

```toml
[[services]]
name = "breadbot-panel"
source = { repo = "getbreadbot/breadbot-deploy", branch = "main", path = "panel" }
```

### Required env vars (panel service)

See `.env.example` for all variables. The minimum set to get running:

```
MCP_SERVER_URL=http://breadbot.railway.internal:8051
MCP_SECRET=<same value as bot's MCP_SECRET>
RAILWAY_API_TOKEN=<Railway account token>
RAILWAY_PROJECT_ID=<your Railway project ID>
RAILWAY_SERVICE_ID=<ID of the bot service, not the panel>
WHOP_API_KEY=<your Whop API key>
```

### First login

1. Navigate to the panel URL (Railway will provide it)
2. Enter your Whop license key and set a panel password
3. Password is stored as a hash in `PANEL_PASSWORD_HASH` in the Railway environment

## Local development

```bash
# Backend
pip install -r requirements.txt
cp .env.example .env  # fill in values
uvicorn main:app --reload

# Frontend (separate terminal)
cd frontend
npm install
npm run dev
```

Frontend dev server proxies `/api` to `localhost:8000`.

## Push notifications (optional)

Generate VAPID keys:

```python
from pywebpush import Vapid
v = Vapid()
v.generate_keys()
print("Private:", v.private_key.decode())
print("Public: ", v.public_key.decode())
```

Add both keys and `VAPID_EMAIL` to the panel service env vars.
