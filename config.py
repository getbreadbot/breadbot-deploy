"""
config.py — Loads and validates all settings from .env + bot_config DB.
Priority order for risk params: bot_config SQLite table → .env file → default.
Every other module imports from here. Never hardcode keys anywhere else.
"""

import os
import sqlite3
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the project root
load_dotenv(Path(__file__).parent / ".env")

# ── bot_config DB lookup ─────────────────────────────────────────────────────
_DB_PATH = Path(__file__).parent / "data" / "cryptobot.db"

def _db_get(key: str) -> str:
    """Return value from bot_config table, or '' if missing/unavailable."""
    if not _DB_PATH.exists():
        return ""
    try:
        conn = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True)
        row  = conn.execute("SELECT value FROM bot_config WHERE key=?", (key,)).fetchone()
        conn.close()
        return row[0].strip() if row and row[0] else ""
    except Exception:
        return ""

def _require(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val or val.startswith("your_"):
        raise ValueError(
            f"Missing configuration: '{key}' is not set in your .env file.\n"
            f"Open .env and fill in the value for {key}."
        )
    return val

def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()

def _float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default

def _float_config(db_key: str, env_key: str, default: float) -> float:
    """Check bot_config DB first (dashboard edits), then .env, then default."""
    db_val = _db_get(db_key)
    if db_val:
        try:
            return float(db_val)
        except ValueError:
            pass
    return _float(env_key, default)

def _str_config(db_key: str, env_key: str, default: str = "") -> str:
    """Check bot_config DB first (wizard-saved values), then .env, then default.
    Ignores placeholder values so wizard entries override a demo .env file."""
    _PLACEHOLDERS = {"demo", "0", ""}
    db_val = _db_get(db_key)
    if db_val and db_val not in _PLACEHOLDERS and not db_val.startswith("your_"):
        return db_val
    env_val = os.getenv(env_key, "").strip()
    if env_val and env_val not in _PLACEHOLDERS and not env_val.startswith("your_"):
        return env_val
    return default

# ── Telegram ──────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = _str_config("telegram_bot_token", "TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID    = _str_config("telegram_chat_id",   "TELEGRAM_CHAT_ID")

# ── Exchange API keys ──────────────────────────────────────
COINBASE_API_KEY    = _str_config("coinbase_api_key",    "COINBASE_API_KEY")
COINBASE_API_SECRET = _str_config("coinbase_api_secret", "COINBASE_API_SECRET")
COINBASE_PERP_ENABLED = os.getenv("COINBASE_PERP_ENABLED", "false").lower() == "true"
DRIFT_ENABLED      = os.getenv("DRIFT_ENABLED", "false").lower() == "true"
DRIFT_MARKET_PAIRS = os.getenv("DRIFT_MARKET_PAIRS", "BTC,ETH,SOL").strip()
KRAKEN_API_KEY      = _str_config("kraken_api_key",      "KRAKEN_API_KEY")
KRAKEN_API_SECRET   = _str_config("kraken_api_secret",   "KRAKEN_API_SECRET")

# ── Base / EVM wallet ──────────────────────────────────────
BASE_PRIVATE_KEY    = _str_config("base_private_key",    "BASE_PRIVATE_KEY")
BASE_PUBLIC_KEY     = _str_config("base_public_key",     "BASE_PUBLIC_KEY",
                                  "0x9EaC5E219d6a4Be6Ab539d0BDE954dDd4c20B924")
BASE_RPC_URL        = _str_config("base_rpc_url",        "BASE_RPC_URL",
                                  "https://mainnet.base.org")
FLASH_LOAN_CONTRACT = _str_config("flash_loan_contract", "FLASH_LOAN_CONTRACT",
                                  "0x60b30eb32656dfDA6Aed6fd0c073fe872717d357")

# ── Security / rug detection ───────────────────────────────
GOPLUS_API_KEY      = _str_config("goplus_api_key",      "GOPLUS_API_KEY")
BASESCAN_API_KEY    = _str_config("basescan_api_key",     "BASESCAN_API_KEY")

# ── Risk settings ───────────────────────────────────────────────────────────
# DB (bot_config table) takes precedence → .env → hard-coded default.
# This means dashboard edits in Controls → Risk Parameters take effect on the
# next bot restart without touching the .env file.
MAX_POSITION_SIZE_PCT   = _float_config("max_position_size_pct", "MAX_POSITION_SIZE_PCT",  0.02)
DAILY_LOSS_LIMIT_PCT    = _float_config("daily_loss_limit_pct",  "DAILY_LOSS_LIMIT_PCT",   0.05)
MIN_LIQUIDITY_USD       = _float_config("min_liquidity_usd",     "MIN_LIQUIDITY_USD",      15_000)
MIN_VOLUME_24H_USD      = _float_config("min_volume_24h_usd",    "MIN_VOLUME_24H_USD",     40_000)
MAX_TOP10_HOLDER_PCT    = _float("MAX_TOP10_HOLDER_PCT", 0.35)   # not exposed in UI yet
TOTAL_PORTFOLIO_USD     = _float_config("portfolio_total_usd",   "TOTAL_PORTFOLIO_USD",    5_000)

# ── Yield monitor ──────────────────────────────────────────
YIELD_CHANGE_ALERT_PCT  = _float("YIELD_CHANGE_ALERT_PCT", 0.5)

# ── Database ────────────────────────────────────────────────
DB_PATH = Path(__file__).parent / "data" / "cryptobot.db"
