#!/usr/bin/env python3
"""
drift_connector.py — Sprint 4D
Drift Protocol perpetuals connector (Solana DEX).

Drift is a decentralised perpetuals exchange on Solana.
No KYC, no US geo-block. Unified cross-margin: single USDC deposit
covers all positions simultaneously.

Architecture:
  - Uses driftpy SDK (installed: 0.8.89)
  - Shares the existing Solana wallet keypair (SOLANA_PRIVATE_KEY / SOLANA_WALLET_PUBKEY)
  - All async — every public function is a coroutine, run via asyncio.run()
    for synchronous callers
  - Logs to standard Python logger ("drift_connector")

Market indices (mainnet):
  SOL-PERP  → 0
  BTC-PERP  → 1
  ETH-PERP  → 2

New .env vars:
  DRIFT_ENABLED=false          — opt-in toggle
  DRIFT_MARKET_PAIRS=BTC,ETH,SOL — pairs to monitor / trade
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
DRIFT_ENABLED  = os.getenv("DRIFT_ENABLED", "false").lower() == "true"
MARKET_PAIRS   = [p.strip().upper() for p in os.getenv("DRIFT_MARKET_PAIRS", "BTC,ETH,SOL").split(",")]
RPC_URL        = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
PRIVATE_KEY_B58 = os.getenv("SOLANA_PRIVATE_KEY", "").strip()

# Market index mapping (Drift mainnet perpetuals)
MARKET_INDEX = {
    "SOL": 0,
    "BTC": 1,
    "ETH": 2,
}

# Drift precision constants
QUOTE_PRECISION = 1_000_000          # 1 USDC = 1_000_000
BASE_PRECISION  = 1_000_000_000      # 1 BTC  = 1_000_000_000


# ── Keypair loader ────────────────────────────────────────────────────────────

def _load_keypair():
    """Load the Solana wallet keypair from SOLANA_PRIVATE_KEY env var."""
    if not PRIVATE_KEY_B58:
        raise RuntimeError(
            "SOLANA_PRIVATE_KEY not set. "
            "Add it from Vaultwarden → Breadbot → Solana Wallet Private Key."
        )
    try:
        import base58
        from solders.keypair import Keypair
        secret_bytes = base58.b58decode(PRIVATE_KEY_B58)
        return Keypair.from_bytes(secret_bytes)
    except ImportError as e:
        raise ImportError("pip install solders base58") from e


# ── DriftClient factory ───────────────────────────────────────────────────────

async def _make_client():
    """
    Build an initialised DriftClient connected to mainnet.
    Caller is responsible for calling client.unsubscribe() when done.
    """
    from driftpy.drift_client import DriftClient, Wallet
    from driftpy.account_subscription_config import AccountSubscriptionConfig
    from solana.rpc.async_api import AsyncClient

    keypair    = _load_keypair()
    wallet     = Wallet(keypair)
    connection = AsyncClient(RPC_URL)

    client = DriftClient(
        connection,
        wallet,
        env="mainnet",
        account_subscription=AccountSubscriptionConfig("cached"),
    )
    await client.subscribe()
    return client


# ── Public API ────────────────────────────────────────────────────────────────

async def get_market_info(pair: str) -> dict:
    """
    Return current price and funding rate for a Drift perpetual market.
    pair: "BTC", "ETH", or "SOL"

    Returns:
        {
          pair, market_index, oracle_price, funding_rate_hourly,
          funding_rate_annualized, open_interest
        }
    """
    market_index = MARKET_INDEX.get(pair.upper())
    if market_index is None:
        raise ValueError(f"Unknown pair: {pair}. Known: {list(MARKET_INDEX)}")

    client = await _make_client()
    try:
        market = client.get_perp_market_account(market_index)
        oracle = client.get_oracle_price_data_and_slot(market.amm.oracle)

        oracle_price     = oracle.data.price / PRICE_PRECISION if oracle else 0
        # Funding rate is stored as per-hour rate in PRICE_PRECISION
        funding_hourly   = market.amm.last_funding_rate / PRICE_PRECISION if market else 0
        funding_ann      = funding_hourly * 24 * 365 * 100   # annualised %

        return {
            "pair":                    pair.upper(),
            "market_index":            market_index,
            "oracle_price":            oracle_price,
            "funding_rate_hourly":     funding_hourly,
            "funding_rate_annualized": round(funding_ann, 4),
            "base_spread":             market.amm.base_spread if market else 0,
        }
    finally:
        await client.unsubscribe()


async def get_positions(verbose: bool = False) -> list[dict]:
    """
    Return all open Drift perpetual positions for the configured wallet.

    Each dict contains:
        pair, market_index, base_asset_amount (in base coin), entry_price,
        unrealized_pnl (USD), side ("LONG" or "SHORT")
    """
    client = await _make_client()
    try:
        user       = client.get_user()
        positions  = user.get_active_perp_positions()
        out        = []
        index_to_pair = {v: k for k, v in MARKET_INDEX.items()}

        for pos in positions:
            idx     = pos.market_index
            pair    = index_to_pair.get(idx, f"UNKNOWN_{idx}")
            base    = pos.base_asset_amount / BASE_PRECISION   # in base coin
            side    = "LONG" if base > 0 else "SHORT"
            # entry cost: quote_asset_amount / BASE_PRECISION gives entry price
            entry_price = 0.0
            if pos.base_asset_amount != 0:
                entry_price = abs(
                    pos.quote_asset_amount / pos.base_asset_amount
                ) if pos.base_asset_amount else 0

            upnl = user.get_unrealized_pnl(pos.market_index) / QUOTE_PRECISION

            row = {
                "pair":           pair,
                "market_index":   idx,
                "side":           side,
                "size":           abs(base),
                "entry_price":    round(entry_price, 4),
                "unrealized_pnl": round(upnl, 4),
            }
            if verbose:
                row["raw"] = pos
            out.append(row)
        return out
    finally:
        await client.unsubscribe()


async def get_perp_funding_rate(pair: str) -> dict:
    """
    Compatibility alias used by funding_arb_engine when FUNDING_ARB_EXCHANGE=drift.
    Returns rate in the same shape as coinbase_connector.get_funding_rate() and
    bybit_connector.get_funding_rate().
    Hourly rate converted to 8h equivalent to match arb engine rate scale.
    """
    info      = await get_market_info(pair)
    hourly    = info.get("funding_rate_hourly", 0.0)
    rate_8h   = hourly * 8
    return {
        "fundingRate":       rate_8h,
        "fundingRateHourly": hourly,
        "annualized":        info.get("funding_rate_annualized", 0.0),
        "oracle_price":      info.get("oracle_price", 0.0),
        "source":            "drift",
    }


async def deposit_collateral(amount_usdc: float) -> dict:
    """
    Deposit USDC into the Drift unified margin account.
    Requires the wallet to hold USDC on Solana mainnet.

    amount_usdc: USD amount to deposit (e.g. 500.0)
    Returns: {success, amount, tx_sig}
    """
    if not DRIFT_ENABLED:
        return {"error": "DRIFT_ENABLED=false"}

    client = await _make_client()
    try:
        amount_raw = int(amount_usdc * QUOTE_PRECISION)
        sig = await client.deposit(
            amount_raw,
            market_index=0,   # USDC spot market index = 0
            user_token_account=None,  # driftpy resolves ATA automatically
        )
        log.info("Drift deposit: $%.2f USDC | sig=%s", amount_usdc, sig)
        return {"success": True, "amount_usdc": amount_usdc, "tx_sig": str(sig)}
    except Exception as exc:
        log.error("deposit_collateral failed: %s", exc)
        return {"success": False, "error": str(exc)}
    finally:
        await client.unsubscribe()


async def withdraw_collateral(amount_usdc: float) -> dict:
    """
    Withdraw USDC from the Drift unified margin account back to the wallet.

    amount_usdc: USD amount to withdraw
    Returns: {success, amount, tx_sig}
    """
    if not DRIFT_ENABLED:
        return {"error": "DRIFT_ENABLED=false"}

    client = await _make_client()
    try:
        amount_raw = int(amount_usdc * QUOTE_PRECISION)
        sig = await client.withdraw(
            amount_raw,
            market_index=0,
            user_token_account=None,
            reduce_only=False,
        )
        log.info("Drift withdraw: $%.2f USDC | sig=%s", amount_usdc, sig)
        return {"success": True, "amount_usdc": amount_usdc, "tx_sig": str(sig)}
    except Exception as exc:
        log.error("withdraw_collateral failed: %s", exc)
        return {"success": False, "error": str(exc)}
    finally:
        await client.unsubscribe()


async def open_short_perp(pair: str, size_usd: float) -> dict:
    """
    Open a short perpetual position on Drift.
    Used as the short leg of the funding arb strategy.

    pair:     "BTC", "ETH", or "SOL"
    size_usd: USD notional value of the short (e.g. 500.0)

    Returns: {success, pair, side, size_base, entry_price, tx_sig}
    """
    if not DRIFT_ENABLED:
        return {"error": "DRIFT_ENABLED=false"}

    market_index = MARKET_INDEX.get(pair.upper())
    if market_index is None:
        return {"error": f"Unknown pair: {pair}"}

    from driftpy.drift_client import OrderParams, OrderType, PositionDirection, MarketType

    client = await _make_client()
    try:
        # Get oracle price to convert USD → base asset size
        market     = client.get_perp_market_account(market_index)
        oracle     = client.get_oracle_price_data_and_slot(market.amm.oracle)
        price      = oracle.data.price / PRICE_PRECISION if oracle else 0
        if price <= 0:
            return {"error": f"Could not fetch oracle price for {pair}"}

        # Convert USD notional to base asset units (with BASE_PRECISION)
        base_size  = int((size_usd / price) * BASE_PRECISION)

        order_params = OrderParams(
            order_type=OrderType.Market(),
            market_type=MarketType.Perp(),
            direction=PositionDirection.Short(),
            market_index=market_index,
            base_asset_amount=base_size,
            reduce_only=False,
        )

        sig = await client.place_perp_order(order_params)
        log.info("Drift short opened: %s $%.2f (base=%d) | sig=%s",
                 pair, size_usd, base_size, sig)
        return {
            "success":    True,
            "pair":       pair.upper(),
            "side":       "SHORT",
            "size_base":  base_size / BASE_PRECISION,
            "entry_price": round(price, 4),
            "tx_sig":     str(sig),
        }
    except Exception as exc:
        log.error("open_short_perp %s failed: %s", pair, exc)
        return {"success": False, "error": str(exc)}
    finally:
        await client.unsubscribe()


async def close_perp_position(market_index: int) -> dict:
    """
    Close an open Drift perpetual position by market index.
    Places a market order in the opposite direction for the full position size.

    market_index: Drift market index (0=SOL, 1=BTC, 2=ETH)
    Returns: {success, market_index, tx_sig}
    """
    if not DRIFT_ENABLED:
        return {"error": "DRIFT_ENABLED=false"}

    from driftpy.drift_client import OrderParams, OrderType, PositionDirection, MarketType

    client = await _make_client()
    try:
        user = client.get_user()
        pos  = next(
            (p for p in user.get_active_perp_positions() if p.market_index == market_index),
            None,
        )
        if pos is None:
            return {"error": f"No open position at market_index={market_index}"}

        # Opposite direction to close
        is_long   = pos.base_asset_amount > 0
        direction = PositionDirection.Short() if is_long else PositionDirection.Long()

        order_params = OrderParams(
            order_type=OrderType.Market(),
            market_type=MarketType.Perp(),
            direction=direction,
            market_index=market_index,
            base_asset_amount=abs(pos.base_asset_amount),
            reduce_only=True,
        )

        sig = await client.place_perp_order(order_params)
        log.info("Drift position closed: market_index=%d | sig=%s", market_index, sig)
        return {"success": True, "market_index": market_index, "tx_sig": str(sig)}
    except Exception as exc:
        log.error("close_perp_position market_index=%d failed: %s", market_index, exc)
        return {"success": False, "error": str(exc)}
    finally:
        await client.unsubscribe()


# ── Synchronous wrappers (for use in non-async callers like arb engine) ───────

def get_market_info_sync(pair: str) -> dict:
    """Synchronous wrapper around get_market_info()."""
    return asyncio.run(get_market_info(pair))


def get_positions_sync() -> list[dict]:
    """Synchronous wrapper around get_positions()."""
    return asyncio.run(get_positions())


def get_funding_rate_sync(pair: str) -> dict:
    """Synchronous wrapper around get_perp_funding_rate()."""
    return asyncio.run(get_perp_funding_rate(pair))


def open_short_perp_sync(pair: str, size_usd: float) -> dict:
    """Synchronous wrapper around open_short_perp()."""
    return asyncio.run(open_short_perp(pair, size_usd))


def close_perp_position_sync(market_index: int) -> dict:
    """Synchronous wrapper around close_perp_position()."""
    return asyncio.run(close_perp_position(market_index))


# ── Funding arb engine compatibility shim ─────────────────────────────────────

def get_funding_rate(pair: str) -> dict:
    """
    Drop-in alias for funding_arb_engine.get_funding_rates() routing.
    Called when FUNDING_ARB_EXCHANGE=drift.
    """
    return get_funding_rate_sync(pair)


# ── Self-test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    print("Drift connector self-test")
    print(f"  DRIFT_ENABLED:    {DRIFT_ENABLED}")
    print(f"  DRIFT_PAIRS:      {MARKET_PAIRS}")
    print(f"  RPC_URL:          {RPC_URL[:40]}...")
    print(f"  Wallet key set:   {'yes' if PRIVATE_KEY_B58 else 'NO — set SOLANA_PRIVATE_KEY'}")
    print(f"  Market indices:   SOL=0, BTC=1, ETH=2")
    print()

    if not PRIVATE_KEY_B58:
        print("SOLANA_PRIVATE_KEY not set. Skipping live tests.")
        sys.exit(0)

    print("Fetching market info (live RPC call)...")
    for pair in ["BTC", "ETH", "SOL"]:
        if pair not in MARKET_PAIRS:
            continue
        try:
            info = get_market_info_sync(pair)
            print(f"  {pair}: oracle=${info.get('oracle_price', 0):,.2f} | "
                  f"funding {info.get('funding_rate_hourly', 0)*100:.6f}%/hr "
                  f"({info.get('funding_rate_annualized', 0):.2f}%/yr)")
        except Exception as e:
            print(f"  {pair}: FAILED — {e}")

    print("\nFetching open positions...")
    try:
        positions = get_positions_sync()
        if positions:
            for p in positions:
                print(f"  {p['pair']} {p['side']} {p['size']:.6f} | uPnL ${p['unrealized_pnl']:.4f}")
        else:
            print("  No open positions.")
    except Exception as e:
        print(f"  FAILED — {e}")

    print("\nSelf-test complete.")
