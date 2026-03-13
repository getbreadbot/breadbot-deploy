#!/bin/bash
# Breadbot startup script — runs inside the Railway container
# Launches both the trading bot and the dashboard server simultaneously
# Container stays alive as long as either process is running

set -e

echo "=== Breadbot starting ==="
echo "Python: $(python3 --version)"
echo "Working directory: $(pwd)"

# Initialize the SQLite database (creates tables if first run, no-op if already exists)
echo "Initializing database..."
python3 -c "
import asyncio
import sys
sys.path.insert(0, '/app')
from data.database import init_db
asyncio.run(init_db())
print('Database ready')
"

# Start the dashboard server in the background
# Binds to Railway's injected $PORT (falls back to 8000 for local dev)
echo "Starting dashboard on port ${PORT:-8000}..."
cd /app/dashboard
python3 -m uvicorn server:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers 1 \
    --log-level info &
DASHBOARD_PID=$!

# Brief pause so the dashboard is up before the bot starts sending data
sleep 2

# Start the trading bot in the background
echo "Starting trading bot..."
cd /app
python3 main.py &
BOT_PID=$!

echo "=== Both processes running ==="
echo "Dashboard PID: $DASHBOARD_PID"
echo "Bot PID: $BOT_PID"

# Wait for either process to exit
# If either crashes, the container exits and Railway will restart it
wait -n $DASHBOARD_PID $BOT_PID
EXIT_CODE=$?

echo "A process exited with code $EXIT_CODE — container shutting down"
exit $EXIT_CODE
