#!/usr/bin/env python3
"""
telethon_auth.py — One-time Telethon session string generator.

Run this ONCE on the VPS interactively:
    python3 /opt/projects/breadbot/telethon_auth.py

It will:
1. Ask for your phone number
2. Send a code to your Telegram
3. Ask you to enter the code
4. Print a session string

Copy the session string and add it to .env:
    TELEGRAM_SESSION_STRING=<the string printed below>

After that, this script is never needed again.
The session string is reused automatically on every restart.

Requirements:
    pip install telethon  (already in requirements.txt)

Credentials needed first (get from https://my.telegram.org/apps):
    TELEGRAM_API_ID=       (numeric)
    TELEGRAM_API_HASH=     (hex string)

Set those in .env before running this script.
"""

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

API_ID   = os.getenv("TELEGRAM_API_ID", "").strip()
API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()


def check_env():
    missing = []
    if not API_ID or API_ID == "0":
        missing.append("TELEGRAM_API_ID")
    if not API_HASH:
        missing.append("TELEGRAM_API_HASH")
    if missing:
        print(f"\nMissing .env vars: {', '.join(missing)}")
        print("Get them from https://my.telegram.org/apps (takes 2 minutes)")
        print("  1. Log in with your phone number")
        print("  2. Click 'API Development Tools'")
        print("  3. Create an app (any name/platform is fine)")
        print("  4. Copy 'App api_id' and 'App api_hash' into .env")
        sys.exit(1)


async def generate_session():
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        print("\ntelethon not installed. Run:")
        print("  pip install telethon")
        sys.exit(1)

    print("\n── Breadbot Telethon Auth ───────────────────────────────")
    print("This generates a session string for the alpha channel monitor.")
    print("It uses YOUR Telegram account to read public channel messages.")
    print("No messages are sent on your behalf.\n")

    client = TelegramClient(StringSession(), int(API_ID), API_HASH)

    await client.start()

    session_string = client.session.save()

    print("\n── Session string generated ─────────────────────────────")
    print("\nCopy this value into your .env file:\n")
    print(f"TELEGRAM_SESSION_STRING={session_string}")
    print("\n─────────────────────────────────────────────────────────")
    print("Done. You can now run the bot normally.")
    print("The session is cached — this script does not need to be run again.")

    await client.disconnect()


if __name__ == "__main__":
    check_env()
    asyncio.run(generate_session())
