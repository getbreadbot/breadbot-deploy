"""
seed_test_data.py — Populates the database with realistic test data.
Run: python3 seed_test_data.py
Safe to run multiple times. Does NOT touch any bot config or credentials.
Creates only: cryptobot/data/cryptobot.db
"""
import sqlite3, json, random
from pathlib import Path
from datetime import datetime, timezone, timedelta

DB_PATH = Path(__file__).parent.parent / "data" / "cryptobot.db"
DB_PATH.parent.mkdir(exist_ok=True)

TOKENS = [
    ("PEPE2","Pepe 2.0","solana"),("WOJAK","Wojak Coin","solana"),
    ("BONK2","Bonk 2.0","solana"),("FLOKI2","Floki 2.0","base"),
    ("DOGE2","Doge 2.0","base"),("MOCHI","Mochi Token","solana"),
    ("GIGA","Gigachad","solana"),("POPCAT","Popcat","solana"),
    ("BRETT","Brett","base"),("WIF","dogwifhat","solana"),
    ("BOME","Book of Meme","solana"),("MEW","cat in a dogs world","solana"),
    ("SLERF","Slerf","solana"),("MYRO","Myro","solana"),
    ("PONKE","Ponke","solana"),("TOSHI","Toshi","base"),
    ("DEGEN","Degen","base"),("HIGHER","Higher","base"),
    ("DOGINME","Dog in me","base"),("KEYCAT","Keyboard Cat","base"),
]
PLATFORMS = [
    ("Coinbase (app)","USDC",4.10,"Auto-earned for holding USDC on Coinbase"),
    ("Coinbase Wallet (Base)","USDC",4.70,"Hold USDC in Coinbase Wallet on Base"),
    ("Coinbase Morpho (Base)","USDC",8.20,"Variable lending via Morpho Protocol"),
    ("Aave V3 (Base)","USDC",5.40,"Variable rate DeFi lending on Base"),
    ("Compound V3 (Base)","USDC",4.90,"Battle-tested DeFi lending protocol"),
    ("Kraken Earn","USDC",1.75,"CeFi earn, simple no DeFi risk"),
]
FLAGS_POOL = [
    "🔓 Liquidity not locked","👤 Owner not renounced","⚠️ Top 10 hold 38%",
    "⚠️ Sell tax: 3.5%","🖨️ Mintable supply","⚠️ RugCheck: Mutable metadata",
    "⚠️ Low holder count (<200)","⚠️ Concentration: top 10 hold 41%",
]

def ts(days_ago=0, hours_ago=0, jitter_h=0):
    t = datetime.now(timezone.utc) - timedelta(days=days_ago, hours=hours_ago)
    t += timedelta(hours=random.uniform(-jitter_h, jitter_h))
    return t.strftime("%Y-%m-%d %H:%M:%S")

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# Schema
c.executescript("""
CREATE TABLE IF NOT EXISTS meme_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chain TEXT NOT NULL,
    token_addr TEXT NOT NULL, token_name TEXT, symbol TEXT,
    price_usd REAL, liquidity REAL, volume_24h REAL, mcap REAL,
    rug_score INTEGER, rug_flags TEXT, alert_sent INTEGER DEFAULT 0,
    decision TEXT DEFAULT 'pending', created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chain TEXT NOT NULL,
    token_addr TEXT NOT NULL, token_name TEXT, symbol TEXT,
    entry_price REAL NOT NULL, quantity REAL NOT NULL,
    cost_basis_usd REAL NOT NULL, stop_loss_usd REAL,
    take_profit_25 REAL, take_profit_50 REAL,
    status TEXT DEFAULT 'open', exchange TEXT,
    opened_at TEXT DEFAULT (datetime('now')), closed_at TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, position_id INTEGER,
    action TEXT NOT NULL, price_usd REAL, quantity REAL,
    usd_value REAL, fee_usd REAL DEFAULT 0, pnl_usd REAL,
    tx_hash TEXT, exchange TEXT,
    executed_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS yield_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL,
    asset TEXT NOT NULL, apy REAL NOT NULL, tvl_usd REAL,
    notes TEXT, recorded_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS daily_summary (
    date TEXT PRIMARY KEY, realized_pnl REAL DEFAULT 0,
    unrealized_pnl REAL DEFAULT 0, yield_earned REAL DEFAULT 0,
    fees_paid REAL DEFAULT 0, trades_count INTEGER DEFAULT 0
);
""")

# Clear existing test data
for t in ["meme_alerts","positions","trades","yield_snapshots","daily_summary"]:
    c.execute(f"DELETE FROM {t}")

# 20 meme alerts
decisions = ["buy"]*5 + ["skip"]*12 + ["pending"]*3
random.shuffle(decisions)
for i,(sym,name,chain) in enumerate(TOKENS):
    score = random.randint(45,95)
    nflags = 0 if score>=80 else 1 if score>=60 else random.randint(2,4)
    flags = random.sample(FLAGS_POOL, min(nflags, len(FLAGS_POOL)))
    price = random.uniform(0.0000045, 0.087)
    liq   = random.uniform(15000, 280000)
    vol   = random.uniform(40000, 950000)
    mcap  = liq * random.uniform(2, 8)
    c.execute("""INSERT INTO meme_alerts
        (chain,token_addr,token_name,symbol,price_usd,liquidity,volume_24h,mcap,rug_score,rug_flags,alert_sent,decision,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)""",
        (chain, f"0x{''.join(random.choices('abcdef0123456789',k=40))}", name, sym,
         price, liq, vol, mcap, score, json.dumps(flags), decisions[i], ts(days_ago=random.randint(0,6), hours_ago=random.randint(0,20))))

# 3 open positions
open_tokens = [("PEPE2","Pepe 2.0","solana"),("BONK2","Bonk 2.0","solana"),("BRETT","Brett","base")]
pos_ids = []
for sym, name, chain in open_tokens:
    ep = random.uniform(0.00001, 0.005)
    qty = round(random.uniform(5000, 50000), 2)
    cost = round(ep * qty, 2)
    c.execute("""INSERT INTO positions
        (chain,token_addr,token_name,symbol,entry_price,quantity,cost_basis_usd,
         stop_loss_usd,take_profit_25,take_profit_50,status,exchange,opened_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (chain, f"0x{''.join(random.choices('abcdef0123456789',k=40))}", name, sym,
         ep, qty, cost, ep*0.75, ep*2.0, ep*5.0,
         "open", "coinbase" if chain=="base" else "solana_dex", ts(days_ago=random.randint(1,5))))
    pos_ids.append(c.lastrowid)

# 5 closed positions
closed_tokens = [("WIF","dogwifhat","solana"),("DOGE2","Doge 2.0","base"),
                 ("MOCHI","Mochi Token","solana"),("GIGA","Gigachad","solana"),("TOSHI","Toshi","base")]
for sym, name, chain in closed_tokens:
    ep = random.uniform(0.00001, 0.003)
    qty = round(random.uniform(3000, 30000), 2)
    cost = round(ep * qty, 2)
    status = random.choice(["closed","stopped"])
    c.execute("""INSERT INTO positions
        (chain,token_addr,token_name,symbol,entry_price,quantity,cost_basis_usd,
         stop_loss_usd,take_profit_25,take_profit_50,status,exchange,opened_at,closed_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (chain, f"0x{''.join(random.choices('abcdef0123456789',k=40))}", name, sym,
         ep, qty, cost, ep*0.75, ep*2.0, ep*5.0,
         status, "coinbase" if chain=="base" else "solana_dex",
         ts(days_ago=random.randint(8,14)), ts(days_ago=random.randint(1,7))))
    pos_ids.append(c.lastrowid)

# 15 trades
all_pos = c.execute("SELECT id,symbol,chain,entry_price,cost_basis_usd FROM positions").fetchall()
for i in range(15):
    pos = random.choice(all_pos)
    action = random.choice(["buy","sell","sell","stop_loss"])
    ep = pos[3]
    multiplier = random.uniform(0.6, 3.5) if action != "buy" else 1.0
    price = ep * multiplier
    qty = random.uniform(500, 8000)
    val = round(price * qty, 2)
    pnl = round((price - ep) * qty, 2) if action != "buy" else None
    c.execute("""INSERT INTO trades
        (position_id,action,price_usd,quantity,usd_value,fee_usd,pnl_usd,exchange,executed_at)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (pos[0], action, price, qty, val, round(val*0.006, 2), pnl,
         "solana_dex" if pos[2]=="solana" else "coinbase", ts(days_ago=random.randint(0,13), jitter_h=8)))

# Yield snapshots — 30 days per platform
for plat, asset, base_apy, notes in PLATFORMS:
    for day in range(30, -1, -1):
        drift = random.uniform(-0.3, 0.3)
        apy = round(max(0.5, base_apy + drift), 2)
        c.execute("INSERT INTO yield_snapshots (platform,asset,apy,notes,recorded_at) VALUES(?,?,?,?,?)",
                  (plat, asset, apy, notes, ts(days_ago=day)))

# Daily summary — last 14 days
for day in range(13, -1, -1):
    pnl = round(random.uniform(-120, 280), 2)
    c.execute("""INSERT OR REPLACE INTO daily_summary
        (date,realized_pnl,unrealized_pnl,yield_earned,fees_paid,trades_count)
        VALUES(?,?,?,?,?,?)""",
        ((datetime.now(timezone.utc)-timedelta(days=day)).strftime("%Y-%m-%d"),
         pnl, round(pnl*0.4,2), round(random.uniform(0.5,3.5),2),
         round(abs(pnl)*0.006,2), random.randint(0,5)))

conn.commit()
conn.close()
print(f"\n  Seed data written to: {DB_PATH}")
print("  Tables populated: meme_alerts(20), positions(8), trades(15), yield_snapshots, daily_summary")
print("  Reload http://localhost:8000 to see data\n")
