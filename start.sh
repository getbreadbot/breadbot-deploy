#!/bin/bash
# Breadbot startup script — Railway container entry point
# Runs the dashboard server + trading bot simultaneously
# Railway restarts the container if either process exits

set -e

echo "=== Breadbot starting ==="
echo "Python: $(python3 --version)"
echo "Working directory: $(pwd)"

# Initialize the SQLite database (creates tables on first run, no-op otherwise)
echo "Initializing database..."
python3 -c "
import asyncio, sys
sys.path.insert(0, '/app')
from data.database import init_db
asyncio.run(init_db())
print('Database ready')
"

# Seed demo data on first run so the dashboard has content immediately
# The sentinel file lives in /app/data — if you mount a Railway volume there,
# seeding only happens once even across redeploys
SEED_SENTINEL="/app/data/.seeded"
if [ ! -f "$SEED_SENTINEL" ]; then
    echo "Seeding dashboard with demo data..."
    cd /app
    python3 dashboard/seed_test_data.py && touch "$SEED_SENTINEL" && echo "Seed complete"
    cd /app
fi

# Start the dashboard server
# Railway injects $PORT — fall back to 8000 for local testing
echo "Starting dashboard on port ${PORT:-8000}..."
cd /app/dashboard
python3 -m uvicorn server:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers 1 \
    --log-level info &
DASHBOARD_PID=$!

sleep 2

# Start the trading bot
echo "Starting trading bot..."
cd /app
python3 main.py &
BOT_PID=$!

echo "=== Both processes running ==="
echo "Dashboard PID: $DASHBOARD_PID"
echo "Bot PID: $BOT_PID"

# Exit when either process exits — Railway will restart the container
wait -n $DASHBOARD_PID $BOT_PID
EXIT_CODE=$?
echo "A process exited with code $EXIT_CODE — container shutting down"
exit $EXIT_CODE
