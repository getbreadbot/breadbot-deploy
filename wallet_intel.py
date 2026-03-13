"""
wallet_intel.py — Phase 2B
On-chain wallet intelligence for rug detection improvement.
Starts with free Etherscan (EVM) and Solscan (Solana) lookups.
Nansen and Arkham are stubbed — activate when budget allows.

New .env vars required:
  ETHERSCAN_API_KEY  — Free at etherscan.io/apis
                       Store in Vaultwarden → Breadbot → "Etherscan API Key"
  SOLSCAN_API_KEY    — Free tier at pro.solscan.io
                       Store in Vaultwarden → Breadbot → "Solscan API Key"

Optional (paid — activate when profitable):
  NANSEN_API_KEY     — ~$150/mo. Vaultwarden → Breadbot → "Nansen API Key"
  ARKHAM_API_KEY     — Free waitlist. Vaultwarden → Breadbot → "Arkham API Key"

Score impact (used by scanner):
  Known exchange wallet holding >5% of supply  → +5 security score
  Unknown wallet age <7 days holding >10%      → -15 security score

Integration: call label_top_holders(holders, chain) for top 5 holders before scoring.
"""

import logging
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
ETHERSCAN_KEY = os.getenv("ETHERSCAN_API_KEY", "").strip()
SOLSCAN_KEY   = os.getenv("SOLSCAN_API_KEY",   "").strip()
NANSEN_KEY    = os.getenv("NANSEN_API_KEY",    "").strip()
ARKHAM_KEY    = os.getenv("ARKHAM_API_KEY",    "").strip()
_REQUEST_TIMEOUT = 10

# Known exchange and custodian labels — extend as more are confirmed
KNOWN_EXCHANGE_LABELS: dict[str, str] = {
    # EVM
    "0x3f5ce5fbfe3e9af3971dd833d26ba9b5c936f0be": "Binance",
    "0xd551234ae421e3bcba99a0da6d736074f22192ff": "Binance",
    "0x564286362092d8e7936f0549571a803b203aaced": "Binance",
    "0x28c6c06298d514db089934071355e5743bf21d60": "Binance",
    "0xbe0eb53f46cd790cd13851d5eff43d12404d33e8": "Binance Cold",
    "0xfe9e8709d3215310075d67e3ed32a380ccf451c8": "Coinbase",
    "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43": "Coinbase",
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3": "Coinbase",
    "0x503828976d22510aad0201ac7ec88293211d23da": "Coinbase",
    "0x2b5634c42055806a59e9107ed44d43c426e58258": "Kraken",
    "0x0a869d79a7052c7f1b55a8ebabbea3420f0d1e13": "Kraken",
    "0xe853c56864a2ebe4576a807d26fdc4a0ada51919": "Kraken",
    "0x267be1c1d684f78cb4f6a176c4911b741e4ffdc0": "Kraken",
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b": "OKX",
    "0x98ec059dc3adfbdd63429454aeb0c990fba4a128": "Binance.US",
    # Solana
    "5tzFkiKscXHK5ZXCGbGuykEzwEBJAJaJPnFdGEXKxF5G": "Binance",
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS": "Kraken",
    "2AQdpHJ2JpcEgPiATUXjQxA8QmafFegfQwSLWSprPicm": "Coinbase",
}


# ── EVM wallet intelligence ────────────────────────────────────────────────────

def get_evm_wallet_age_days(address: str) -> float | None:
    """Return EVM wallet age in days based on first tx. Returns None if lookup fails."""
    if not ETHERSCAN_KEY:
        logger.warning("ETHERSCAN_API_KEY not set — cannot check EVM wallet age")
        return None
    try:
        resp = requests.get(
            "https://api.etherscan.io/api",
            params={"module": "account", "action": "txlist", "address": address.lower(),
                    "startblock": 0, "endblock": 99999999, "page": 1, "offset": 1,
                    "sort": "asc", "apikey": ETHERSCAN_KEY},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "1" or not data.get("result"):
            return None
        first_tx_ts = int(data["result"][0]["timeStamp"])
        age_days = (time.time() - first_tx_ts) / 86400
        logger.info("EVM wallet %s age: %.1f days", address[:10] + "...", age_days)
        return age_days
    except Exception as e:
        logger.warning("get_evm_wallet_age_days failed for %s: %s", address[:10] + "...", e)
        return None


def get_evm_wallet_label(address: str) -> str | None:
    """Return known label for an EVM address, or None if unknown."""
    norm = address.lower()
    if norm in KNOWN_EXCHANGE_LABELS:
        return KNOWN_EXCHANGE_LABELS[norm]
    if not ETHERSCAN_KEY:
        return None
    try:
        resp = requests.get(
            "https://api.etherscan.io/api",
            params={"module": "contract", "action": "getsourcecode",
                    "address": address, "apikey": ETHERSCAN_KEY},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        name = resp.json().get("result", [{}])[0].get("ContractName", "")
        return name if name else None
    except Exception as e:
        logger.debug("Etherscan label lookup failed: %s", e)
    return None


# ── Solana wallet intelligence ─────────────────────────────────────────────────

def get_solana_wallet_age_days(address: str) -> float | None:
    """Return Solana wallet age in days based on first tx. Returns None if lookup fails."""
    if not SOLSCAN_KEY:
        logger.warning("SOLSCAN_API_KEY not set")
        return None
    try:
        tx_resp = requests.get(
            "https://pro-api.solscan.io/v2.0/account/transactions",
            params={"address": address, "limit": 40},
            headers={"Authorization": f"Bearer {SOLSCAN_KEY}"},
            timeout=_REQUEST_TIMEOUT,
        )
        tx_resp.raise_for_status()
        txs = tx_resp.json().get("data", [])
        if txs:
            first_ts = txs[-1].get("blockTime")
            if first_ts:
                age_days = (time.time() - int(first_ts)) / 86400
                logger.info("Solana wallet %s age: %.1f days", address[:10] + "...", age_days)
                return age_days
        return None
    except Exception as e:
        logger.warning("get_solana_wallet_age_days failed for %s: %s", address[:10] + "...", e)
        return None


def get_solana_wallet_label(address: str) -> str | None:
    """Return known label for a Solana address, or None."""
    return KNOWN_EXCHANGE_LABELS.get(address)


# ── Unified label_wallet — scanner integration point ──────────────────────────

def label_wallet(address: str, chain: str) -> dict:
    """
    Primary function for the scanner. Returns label, age, and score delta.

    Returns:
      label       (str | None)   — Human-readable label if known
      age_days    (float | None) — Wallet age in days
      is_exchange (bool)         — True if known exchange/custodian wallet
      score_delta (int)          — Adjustment to apply (+5 or -15 or 0)
      notes       (list[str])    — Explanation of adjustments
    """
    is_solana = chain.lower() == "solana"
    result: dict = {"address": address, "chain": chain, "label": None,
                    "age_days": None, "is_exchange": False, "score_delta": 0, "notes": []}

    label = get_solana_wallet_label(address) if is_solana else get_evm_wallet_label(address)
    age   = get_solana_wallet_age_days(address) if is_solana else get_evm_wallet_age_days(address)

    result["label"]    = label
    result["age_days"] = age

    if label:
        result["is_exchange"] = True
        result["score_delta"] = 5
        result["notes"].append(f"Known exchange wallet: {label} (+5 if >5% supply held)")
    elif age is not None and age < 7:
        result["score_delta"] = -15
        result["notes"].append(f"Wallet age {age:.1f} days — new wallet (-15)")

    logger.info("label_wallet %s (%s): label=%s age=%.1f delta=%+d",
                address[:10] + "...", chain, label, age or 0, result["score_delta"])
    return result


def label_top_holders(holders: list[dict], chain: str) -> list[dict]:
    """
    Run label_wallet() on top 5 holders and apply percentage-based thresholds.

    holders: list of {'address': str, 'pct': float} dicts.
    Returns: enriched label_wallet() results with holder_pct and adjusted score_delta.
    """
    results = []
    for h in holders[:5]:
        addr = h.get("address", "")
        pct  = float(h.get("pct", 0))
        info = label_wallet(addr, chain)
        info["holder_pct"] = pct

        if info["is_exchange"] and pct <= 5.0:
            info["score_delta"] = 0
            info["notes"].append(f"Exchange wallet but only {pct:.1f}% — no bonus applied")
        elif not info["is_exchange"] and info["age_days"] is not None and info["age_days"] < 7:
            if pct <= 10.0:
                info["score_delta"] = 0
                info["notes"].append(f"New wallet but only {pct:.1f}% — no penalty applied")

        results.append(info)
    return results


# ── Nansen stub ────────────────────────────────────────────────────────────────

def _nansen_label(address: str) -> str | None:
    """Stub — activate when NANSEN_API_KEY is set (~$150/mo)."""
    if not NANSEN_KEY:
        return None
    logger.debug("Nansen lookup skipped — NANSEN_API_KEY not set")
    return None


# ── Arkham stub ────────────────────────────────────────────────────────────────

def _arkham_label(address: str) -> str | None:
    """Stub — activate when ARKHAM_API_KEY is available."""
    if not ARKHAM_KEY:
        return None
    logger.debug("Arkham lookup skipped — ARKHAM_API_KEY not set")
    return None


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("wallet_intel self-test")
    print(f"  ETHERSCAN_API_KEY : {'set' if ETHERSCAN_KEY else '(not set)'}")
    print(f"  SOLSCAN_API_KEY   : {'set' if SOLSCAN_KEY else '(not set)'}")
    print(f"  NANSEN_API_KEY    : {'set (stub active)' if NANSEN_KEY else 'not set (stub)'}")
    print(f"  ARKHAM_API_KEY    : {'set (stub active)' if ARKHAM_KEY else 'not set (stub)'}")

    # Known exchange detection — no API key needed
    known_addr = "0x3f5ce5fbfe3e9af3971dd833d26ba9b5c936f0be"  # Binance hot wallet
    result = label_wallet(known_addr, "ethereum")
    print(f"\nKnown EVM address (Binance hot wallet):")
    print(f"  label={result['label']} is_exchange={result['is_exchange']} delta={result['score_delta']:+d}")
    assert result["is_exchange"] is True, "FAIL: should be flagged as exchange wallet"
    print("  Exchange detection OK")

    # Top holders enrichment
    mock_holders = [
        {"address": known_addr, "pct": 8.0},
        {"address": "0x000000000000000000000000000000000000dead", "pct": 2.0},
    ]
    enriched = label_top_holders(mock_holders, "ethereum")
    print(f"\nlabel_top_holders: {len(enriched)} holders processed")
    for h in enriched:
        print(f"  {h['address'][:12]}... pct={h['holder_pct']}% delta={h['score_delta']:+d} notes={h['notes']}")

    # Wallet age (requires key)
    if ETHERSCAN_KEY:
        vitalik = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
        age = get_evm_wallet_age_days(vitalik)
        print(f"\nVitalik wallet age: {age:.1f} days" if age else "\nWallet age lookup returned None")
    else:
        print("\nETHERSCAN_API_KEY not set — skipping wallet age test (get free key at etherscan.io/apis)")

    # Solana label — no key needed
    sol_binance = "5tzFkiKscXHK5ZXCGbGuykEzwEBJAJaJPnFdGEXKxF5G"
    sol_label = get_solana_wallet_label(sol_binance)
    print(f"\nSolana known label: {sol_label}")
    assert sol_label == "Binance", "FAIL: Binance Solana wallet not in local dict"
    print("  Solana label detection OK")
