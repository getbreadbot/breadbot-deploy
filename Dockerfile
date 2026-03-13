# Breadbot — Railway Deployment Dockerfile
# Runs two processes: the trading bot (main.py) and the dashboard (dashboard/server.py)
# Buyers set all credentials as Railway environment variables — no .env file needed

FROM python:3.11-slim

# Install gcc for any compiled dependencies (web3, cryptography, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first so Docker can cache the pip install layer
# (only re-runs if requirements files change, not on every code change)
COPY requirements.txt requirements_dashboard.txt ./

# Install all dependencies in a single layer — main bot + dashboard
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -r requirements_dashboard.txt

# Copy the full application
COPY . .

# Create the data directory that SQLite writes to
# On Railway, mount a persistent volume at /app/data to survive deploys
RUN mkdir -p /app/data

# Make the startup script executable
RUN chmod +x start.sh

# Railway injects $PORT — the dashboard binds to it
# The trading bot (main.py) does not expose a port
EXPOSE 8000

CMD ["./start.sh"]
