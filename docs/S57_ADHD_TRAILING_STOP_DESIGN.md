# S57 Design Doc — ADHD-style Trailing Stop (design only, no implementation)

**Status:** Design proposal. Not shipped. Do not implement without operator sign-off.

**Problem.** The current exit ladder is static: fixed SL at entry × (1 - SL_PCT), TP25 at +30%, TP50 at +75% (typical). Once TP50 fires, the position is fully out. Trades that keep running beyond TP50 are capped at the TP50 realized return.

## The ADHD case study (position #29)

Concrete numbers pulled from logs on 2026-04-19:

| Event | Price | Change from entry | Action |
|---|---|---|---|
| Entry | $0.00005973 | — | Buy $5.00 (83,710 tokens) |
| TP25 fire | $0.00007789 | +30.4% | Sell 50% → expected $3.02 |
| TP50 fire | $0.00014780 | +147.4% | Sell remaining 50% → expected $5.22 |
| Peak (handoff note) | ~$0.00023892 | ~+4×, or +300% | — (already flat, missed) |
| Post-peak rescan | (h1 pump +161%) | — | Scanner dropped the re-entry (h1 ceiling = 150%) |

Blended realized return on the $5 position:
- Half at +30.4% = $3.02
- Half at +147.4% = $5.22
- Total out: $8.24 → **+64.8% realized on the position** (2.47× on a 4× underlying move)

Captured fraction of the move: 8.24 / (5.00 × 4.0) = **41.2%**. That's the gap a trailing stop would chase.

## Proposal: Tighten-on-TP25 trailing SL

Change the exit behavior so that once TP25 hits, the hard SL converts into a trailing SL that follows price up and never steps down. TP50 continues to be a hard take-profit (trim the tail, not chase infinity).

### State machine

```
OPEN (hard SL @ entry-N%, TP25 @ entry+30%, TP50 @ entry+75%)
  │
  │  TP25 fires → sell 25% (or 50% — match current behavior)
  ▼
TRAILING (trail SL @ peak × (1 - TRAIL_PCT), TP50 still armed)
  │
  ├── TP50 fires → sell remainder at hard TP50, done
  ├── trail SL hits → sell remainder at trailing price
  └── peak updates continuously: peak = max(peak, current)
```

### Parameters (all in .env, defaults conservative)

| Key | Default | Description |
|---|---|---|
| `TRAIL_ENABLED` | `false` | Opt-in. Default static ladder. |
| `TRAIL_ACTIVATE_ON` | `TP25` | When to switch from hard SL to trailing. `TP25` \| `TP50` \| `custom_pct` |
| `TRAIL_ACTIVATE_CUSTOM_PCT` | `25` | Only if `TRAIL_ACTIVATE_ON=custom_pct` — activate at +N% from entry |
| `TRAIL_PCT` | `20` | Trail width. Once trailing, SL sits at peak × (1 - TRAIL_PCT/100) |
| `TRAIL_MIN_LOCK_PCT` | `10` | Floor: trailing SL may never drop below entry × (1 + TRAIL_MIN_LOCK_PCT/100). Guarantees any trailing exit is a winner. |
| `TRAIL_TP50_STILL_ARMED` | `true` | Whether TP50 continues as a hard take-profit alongside the trail. If `false`, only the trail closes the position. |

### Apply to the ADHD case

With defaults (`TRAIL_ENABLED=true`, `TRAIL_ACTIVATE_ON=TP25`, `TRAIL_PCT=20`, `TRAIL_MIN_LOCK_PCT=10`, `TRAIL_TP50_STILL_ARMED=true`):

- Entry $0.00005973. TP25 fires at +30.4%, sell 50%. Position enters TRAILING state.
- Peak tracked. At the +147% level, peak = $0.00014780. Trailing SL = $0.00014780 × 0.80 = $0.00011824. TP50 simultaneously fires and takes the remaining 50% — same outcome as today. **No change yet.**

To capture the post-TP50 leg, one of two things must change:
1. Disarm TP50 when trailing (`TRAIL_TP50_STILL_ARMED=false`) — price continues up to ~$0.00023892, trail peak tracks it, then the pullback to (say) peak × 0.80 = $0.00019114 closes the rest. Realized on the trailing half: ~+220% vs. the current +147%. Total blended: ~+125% vs. +65% today.
2. Keep TP50 armed but add a second, wider tranche (TP75, TP100) — this is a ladder extension, not a trailing stop per se.

### Recommendation

Build option 1 as an opt-in flag (`TRAIL_TP50_STILL_ARMED=false`). Keep it **off by default** until 20–30 completed trades produce enough statistical signal. Gate it behind `TRAIL_ENABLED` so operators can A/B test without code changes.

### Risk and counter-cases

- **Chop kills trails.** A position that crosses TP25 then oscillates ±20% around the peak will trigger the trail exit at a lower realized level than a static TP50 would've given. Current data shows ADHD kept running cleanly, but a representative sample should include at least one flat-after-TP25 case before sign-off.
- **Slippage on the exit.** Trailing triggers on polled DEXScreener price; execution hits a Jupiter quote ~3–10 seconds later. On a falling wick, the realized exit can be well below the trigger. Not a new risk (current SL has the same property), but the trail fires on *healthy* positions, so any slippage feels worse to operators.
- **Gas cost amplifies on low-cap positions.** A $5 position paying $0.04 each for two exits (TP25 + trail) is ~1.6% of capital in fees. Current two-exit ladder (TP25 + TP50) has the same cost, so no regression.
- **Floor interaction.** `TRAIL_MIN_LOCK_PCT` must be strictly below the trailing level at activation time. Activating on TP25 (+30%) with floor +10% is fine. Activating on custom_pct +12% with floor +10% leaves a 2%-wide band where tiny moves exit the position immediately — the code should enforce `TRAIL_MIN_LOCK_PCT < TRAIL_ACTIVATE_*_PCT - TRAIL_PCT%` at startup.

### What this doc deliberately does not cover

- Backtest numbers. Need 20–30 real closes first; 2 closes today is not a sample.
- Implementation sketch in `position_manager.py`. That comes after operator sign-off.
- Interaction with S55 P3 slippage escalation on the exit path — already works for SL, trivially extends to trail.
- Dashboard display of the trail peak and current trail level. Separate UI task.

### Decision points the operator should answer before code

1. Default activation trigger: TP25 (aggressive, early switch) or TP50 (conservative, only trail the tail)?
2. Trail width: 20% (ADHD would've exited ~$0.00019k on the pullback, +220% on the trailing half) or 15% (tighter, exits at ~$0.00020k on earlier dips, but more susceptible to chop)?
3. Keep TP50 armed alongside the trail (safer, locks in known +75%) or disarm (chases the peak, current ADHD case would've captured the whole move)?

All three are reasonable. The data doesn't support one choice over another yet.

---

*Design written S57 2026-04-20. Carries forward to S58+ as a live decision item.*
