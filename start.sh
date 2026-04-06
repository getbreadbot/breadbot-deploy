#!/bin/bash
# Breadbot startup script — Railway container entry point
#
# Starts three processes in one container:
#   1. Trading bot (main.py) — scanner, risk manager, Telegram alerts
#   2. MCP server (embedded in main.py) — on localhost:8051, not exposed externally
#   3. Web control panel (panel/main.py) — serves on $PORT, accessible via browser
#
# Railway exposes $PORT to the internet. Everything else is internal.
# Railway restarts the container automatically if any process exits.
# NOTE: No set -e — startup failures (missing env vars, DB errors) must not
# kill the container before the panel binds and passes the healthcheck.

echo "=== Breadbot starting ==="
echo "Python: $(python3 --version)"
echo "Node: $(node --version 2>/dev/null || echo 'not available')"
echo "Working directory: $(pwd)"

# --- Database initialisation ---
# Creates all tables on first run. Safe to run on every start — no-op if tables exist.
# || true ensures a failure here does not kill the container.
echo "Initializing database..."
python3 -c "
import asyncio, sys
sys.path.insert(0, '/app')
try:
    from data.database import init_db
    asyncio.run(init_db())
    print('Database ready')
except Exception as e:
    print(f'DB init skipped: {e}')
" || true

# --- Seed demo data (first run only) ---
SEED_SENTINEL="/app/data/.seeded"
if [ ! -f "$SEED_SENTINEL" ]; then
    echo "Seeding dashboard with demo data..."
    cd /app
    python3 -c "
import sys
sys.path.insert(0, '/app')
try:
    import dashboard.seed_test_data
    print('Seed complete')
except Exception as e:
    print(f'Seed skipped: {e}')
" && touch "$SEED_SENTINEL" || true
    cd /app
fi

# --- Start the trading bot (includes the MCP server on localhost:8051) ---
echo "Starting trading bot + MCP server..."
cd /app
python3 main.py &
BOT_PID=$!

# Give the bot and MCP server time to bind before the panel tries to connect.
echo "Waiting for MCP server to be ready..."
sleep 4

# --- Start the web control panel ---
echo "Starting web panel on port ${PORT:-8000}..."
cd /app/panel
python3 -m uvicorn main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers 1 \
    --log-level info &
PANEL_PID=$!

echo ""
echo "=== Breadbot is running ==="
echo "  Bot PID:   $BOT_PID"
echo "  Panel PID: $PANEL_PID"
echo "  Panel URL: your Railway service URL"
echo "  Telegram:  bot will message you when the scanner fires"
echo ""

# Wait on the PANEL only — bot restart is handled by Railway.
# Panel must stay up for healthcheck; bot crash should not kill the panel.
wait $PANEL_PID
EXIT_CODE=$?
echo "Panel exited with code $EXIT_CODE — container shutting down"
exit $EXIT_CODE
