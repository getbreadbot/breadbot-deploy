#!/usr/bin/env python3
"""
session_state.py — Persistent session state for Breadbot Claude sessions.

Writes and reads a machine-readable JSON file that tracks implementation
status, open tasks, and key infrastructure facts across sessions.
Claude reads this at session start instead of relying on memory alone.

Usage:
    python3 session_state.py            # print current state
    python3 session_state.py update     # update state from live VPS checks
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = Path(__file__).parent / "SESSION_STATE.json"


def load() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))
    print(f"Session state saved to {STATE_FILE}")


_IS_VPS = Path("/opt/projects/breadbot").exists()
_BOT_ROOT = Path("/opt/projects/breadbot") if _IS_VPS else None


def check_vps_file(filename: str) -> bool:
    """Check if a file exists on the VPS (local if running on VPS, SSH otherwise)."""
    if _IS_VPS and _BOT_ROOT:
        return (_BOT_ROOT / filename).exists()
    result = subprocess.run(
        ["ssh", "vps", f"test -f /opt/projects/breadbot/{filename} && echo yes || echo no"],
        capture_output=True, text=True, timeout=10
    )
    return result.stdout.strip() == "yes"


def check_vps_grep(filename: str, pattern: str) -> bool:
    """Check if a pattern exists in a VPS file."""
    if _IS_VPS and _BOT_ROOT:
        try:
            return pattern in (_BOT_ROOT / filename).read_text()
        except Exception:
            return False
    result = subprocess.run(
        ["ssh", "vps", f"grep -q '{pattern}' /opt/projects/breadbot/{filename} 2>/dev/null && echo yes || echo no"],
        capture_output=True, text=True, timeout=10
    )
    return result.stdout.strip() == "yes"


def build_state() -> dict:
    """Build state by checking VPS directly."""
    print("Checking VPS state...")

    modules = {
        "auto_executor":        check_vps_file("auto_executor.py"),
        "social_signals":       check_vps_file("social_signals.py"),
        "yield_monitor":        check_vps_file("yield_monitor.py"),
        "yield_rebalancer":     check_vps_file("yield_rebalancer.py"),
        "pendle_connector":     check_vps_file("pendle_connector.py"),
        "robinhood_connector":  check_vps_file("robinhood_connector.py"),
        "grid_engine":          check_vps_file("grid_engine.py"),
        "funding_arb_engine":   check_vps_file("funding_arb_engine.py"),
        "alt_data_signals":     check_vps_file("alt_data_signals.py"),
        "mcp_server":           check_vps_file("mcp_server.py"),
    }

    wired = {
        "auto_executor_in_scanner":      check_vps_grep("scanner.py", "auto_executor"),
        "social_signals_in_scanner":     check_vps_grep("scanner.py", "social_signals"),
        "alpha_monitor_in_main":         check_vps_grep("main.py", "alpha_monitor"),
        "lst_yields_in_monitor":         check_vps_grep("yield_monitor.py", "poll_liquid_staking"),
        "spark_kamino_in_monitor":       check_vps_grep("yield_monitor.py", "poll_spark_rates"),
        "jito_in_solana_executor":       check_vps_grep("solana_executor.py", "jito"),
        "flashbots_in_evm_executor":     check_vps_grep("evm_executor.py", "flashbots"),
    }

    state = {
        "updated_at":   datetime.now(timezone.utc).isoformat(),
        "github_repo":  "getbreadbot/breadbot-deploy",
        "vps":          "76.13.100.34",
        "services": {
            "landing":        "breadbot.app",
            "demo":           "demo.breadbot.app:8001",
            "license_server": "keys.breadbot.app:8002",
            "mcp_server":     "mcp.breadbot.app",
        },
        "railway": {
            "template_url":   "https://railway.com/deploy/breadbot",
            "template_id":    "1c24c0ab-0cb2-46e8-a4f2-d21ba4bd6ad0",
            "bot_service_id": "1fa0eb37-cfdc-4774-9d4e-9c5902606d47",
            "panel_service_id": "691b1fda-aabe-4107-85e4-6220e46b5cd4",
            "plan":           "Hobby",
        },
        "modules":  modules,
        "wired":    wired,
        "pending": {
            "gemini_connector":     "File exists, keys empty — blocked on support ticket",
            "social_signals_env":   "ARKHAM_API_KEY, ALPHA_CHANNEL_IDS, TELEGRAM_SESSION_STRING not set in .env",
            "robinhood_connector":  "File built, needs ROBINHOOD_USERNAME/PASSWORD in .env + first-login 2FA",
            "panel_smoke_test":     "Deploy fresh template, test first-login flow",
            "whop_lessons":         "Paste lessons 2-6 into Whop course editor",
            "whop_priority_support":"Enable Priority Support on Professional License in Whop Apps tab",
        },
        "sprint_status": {
            "sprint_0_mev":           "DONE — Jito + Flashbots live",
            "sprint_0_gemini":        "BLOCKED — waiting on support ticket for API keys",
            "sprint_1a_auto_execute": "DONE — wired in scanner.py",
            "sprint_1b_social":       "DONE — social_signals.py built and live",
            "sprint_1c_lst_yields":   "DONE — jitoSOL, mSOL, Sanctum INF in yield_monitor",
            "sprint_1d_spark_kamino": "DONE — Spark + Kamino in yield_monitor",
            "sprint_2a_rebalancer":   "DONE — yield_rebalancer.py exists, YIELD_REBALANCE_ENABLED=false",
            "sprint_2b_pendle":       "DONE — pendle_connector.py exists, PENDLE_ENABLED=false",
            "sprint_2c_robinhood":    "BUILT — robinhood_connector.py, needs env vars + first-login",
            "sprint_3a_grid":         "DONE — grid_engine.py exists, GRID_ENABLED=false",
            "sprint_3b_funding_arb":  "DONE — funding_arb_engine.py exists, FUNDING_ARB_ENABLED=false",
            "alt_data_signals":       "DONE — alt_data_signals.py live, ALT_DATA_ENABLED=true",
            "web_panel":              "DONE — built + in deploy repo + Railway template updated",
            "mcp_12_tools":           "DONE — all 12 panel tools live on VPS",
        }
    }
    return state


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "update":
        state = build_state()
        save(state)
    else:
        state = load()
        if not state:
            print("No state file found. Run: python3 session_state.py update")
        else:
            print(json.dumps(state, indent=2))
