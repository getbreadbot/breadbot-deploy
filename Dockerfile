# Breadbot — Railway Deployment Dockerfile
# Single service: runs the trading bot, MCP server, and web control panel together.
# Buyers access the web panel at the Railway-provided URL.
# Telegram handles mobile alerts. The panel handles everything else.

FROM python:3.11-slim

# System dependencies:
#   gcc, libssl-dev        — cryptography, web3, solders
#   python3-dev            — C extensions including driftpy deps
#   libc-ares-dev          — pycares (aiodns, required by driftpy)
#   libzstd-dev            — zstandard (required by driftpy)
#   curl                   — used to install Node.js
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libssl-dev \
    python3-dev \
    libc-ares-dev \
    libzstd-dev \
    curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- Python dependencies ---
# Copy requirements first so Docker can cache this layer.
# Only re-runs when requirements files change, not on every code push.
COPY requirements.txt requirements_dashboard.txt panel/requirements.txt ./panel_requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -r panel_requirements.txt

# --- Application code ---
COPY . .

# --- Panel frontend build ---
# Build the React app so the panel server can serve it as static files.
# This runs once at image build time — buyers do not need npm installed.
RUN cd panel/frontend \
    && npm install \
    && npm run build \
    && echo "Panel frontend built successfully"

# --- Data directory ---
# SQLite writes here. Mount a Railway volume at /app/data for persistence.
RUN mkdir -p /app/data

RUN chmod +x start.sh

# Railway injects $PORT. The panel web server binds to it.
EXPOSE 8000

CMD ["./start.sh"]
