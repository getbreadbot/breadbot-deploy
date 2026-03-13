# Breadbot — Deploy Repository

This is the private deployment repository for Breadbot license holders.

## What This Contains

The complete Breadbot application: scanner, risk manager, yield monitor,
dashboard, and all exchange connectors. Your license key (received after
purchase at breadbot.app) unlocks live trading features. The dashboard,
scanner alerts, and yield monitor all function without exchange API keys.

## Deploy to Railway

Click the link in your purchase confirmation email to deploy a private
instance to Railway. The deploy flow will prompt you for your environment
variables before the container starts.

**Required variables:**
- `TELEGRAM_BOT_TOKEN` — from @BotFather in Telegram
- `TELEGRAM_CHAT_ID` — from @userinfobot in Telegram
- `LICENSE_KEY` — from your purchase confirmation email

**Optional (needed for live trade execution):**
- `COINBASE_API_KEY` / `COINBASE_API_SECRET`
- `KRAKEN_API_KEY` / `KRAKEN_API_SECRET`

All other risk and scanner settings have safe defaults and can be adjusted
in the dashboard after deploy.

## Persistent Storage

Your scan history, positions, and yield data live in a SQLite database at
`/app/data/cryptobot.db`. To keep this data across redeploys:

1. In your Railway project, go to your service → Settings → Volumes
2. Add a volume mounted at `/app/data`
3. Redeploy

Without a volume, the database resets on every deploy. The dashboard will
still work — it reseeds with demo data automatically on first run.

## First Deploy Experience

On first start, the dashboard seeds realistic demo data so you see a fully
populated UI immediately. Once your real Telegram and exchange keys are set,
live data starts flowing and the demo data fades out naturally as real
activity replaces it.

## Support

Purchase support: hello@breadbot.app
Setup guide: included in your purchase confirmation email
