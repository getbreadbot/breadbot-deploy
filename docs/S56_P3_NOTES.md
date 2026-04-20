# S56 P3 Phase 1 — Implementation Notes

## Scope
Compat layer for legacy `trades` table readers. New readers in Phase 2
switch `FROM trades` → `FROM trade_view` with zero other changes.

## What changed

### Schema
- `positions` gained two nullable columns: `realized_pnl_usd`, `exit_price`
- New view: `trade_view` synthesizes `buy` + `sell` rows from `positions`,
  column-compatible with `trades`

### Code
- `position_manager._mark_closed()` signature now accepts `exit_price=None`
- `_mark_closed` writes both `realized_pnl_usd` and `exit_price` to the
  `positions` UPDATE, atomic with the status='closed' write (preserves
  S55 P0 daily_summary co-write contract)
- TP/SL call site (line 445) passes trigger `price` as exit_price
- SELL_NOW path (line 556) computes exit_price = `usdc_out / quantity`
- Dust path (line 380) leaves exit_price NULL — no real exit, write-off only

### main.py init_db
- Inline `CREATE TABLE positions` includes new columns (fresh installs)
- Defensive ALTER pattern handles legacy DBs on upgrade (Railway cold-starts
  and Morgan's existing VPS DB)
- `trade_view` recreated on every startup via DROP + CREATE (idempotent)

## Accepted limitations

1. **Close reason not persisted.** The `note` parameter of `_mark_closed`
   (e.g. 'SL', 'TP25', 'TP50', 'SELL_NOW') is only logged, never written
   to the DB. Every view row has `action='sell'` for exits. No current
   reader filters on action type, so this is fine.

2. **Historical closes show NULL pnl in the view.** The 6 Apr 19 closes
   predate this migration. Their `realized_pnl_usd` and `exit_price` are
   NULL. Aggregate P&L for that date remains correct via `daily_summary`
   (S55 P2 backfill). Phase 2 readers must pull date-level aggregates
   from `daily_summary`, not from SUM over `trade_view.pnl_usd`.

3. **Pre-S55-P0 closes.** Closes before Apr 19 14:47 UTC aren't in
   daily_summary either. Full backfill would require reconstructing
   exit prices from Telegram logs or Solana RPC — out of scope.

## Test evidence
- Migration ran clean on VPS DB (21 open, 12 closed → 33 view rows)
- Column parity with trades table: OK
- All three `_mark_closed` call sites syntax-verified
- Backups at *.bak_s56p3

## Not yet done (S57 scope)
- Phase 2: Rewrite 20 reader sites in dashboard/server.py
- Phase 3: Fix get_strategy_performance MCP tool
- Phase 4: Archive trades → trades_archive, remove from active schema
