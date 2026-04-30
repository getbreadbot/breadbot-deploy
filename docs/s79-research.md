# S79 — Research Findings

*April 30, 2026. Deferred from S77/S78. None of this shipped as code in S79; all of it is data + analysis to inform S80 decisions.*

---

## 1. Slippage investigation (ELIEN-class fills)

**Question from S77 handoff:** "Wait for n≥10 clean post-fix exits before drawing conclusions about ELIEN-class -50% slippage."

**Data:** `exit_attempts` table, n=18 successful sells with both `observed_price` and `executed_price` populated.

### Topline

| Metric | Value |
|---|---|
| Median slippage vs observed | **−7.6 %** |
| Mean slippage vs observed | −12.5 % |
| Worst single fill | −67.2 % (DOGE SL retry, 04/30) |
| Best single fill | +57.2 % (positive — observed was stale on the way up) |
| Exits worse than −20 % | **7 / 18 = 39 %** |
| Exits worse than −50 % | 4 / 18 = 22 % |

### Key finding: latency, not freshness

Splitting by per-attempt latency:

| Latency bucket | n | Median slip |
|---|---|---|
| <6000 ms | 13 | **−4.3 %** |
| ≥6000 ms | 5 | **−22.2 %** |

The slow attempts are the ones that crater. The original "DEXScreener stale prices" hypothesis is partially wrong — DEXScreener is one factor, but the bigger issue is the 5–10 second window between observed-price snapshot and Jito-confirmed swap on a fast-falling meme.

### The 1500-bps retry tier executes at any price the market reaches

DOGE position 68 SL is the canonical bad case:
1. T+0 s: poll, observed_price = $0.0000244
2. T+0 s: tier-0 swap submits at 500 bps slippage tolerance. **Fails** with `err_code=SLIPPAGE` (market moved >5 % during quote→submit).
3. T+~3 s: tier-1 retry submits at 1500 bps. **Succeeds**.
4. Executed price: $0.0000080. That's **−67 %** vs observed.

How does a 1500-bps (15 %) tolerance produce a −67 % fill? Because tolerance is enforced against the *quote at submit time*, not against the original observed price. The retry fetches a fresh quote at the now-collapsed price, then accepts up to 15 % slippage on *that* quote. The semantics are working as Jupiter designed; the operator's mental model of "1500 bps = 15 % max slippage from where I saw the price" is wrong.

### Catastrophic sub-bucket (n=4)

The four fills worse than −50 % split into two patterns:

| ID | Pattern |
|---|---|
| 8 (Rupeblican SL) | Position already at −95.7 % vs entry. Token effectively dead, liquidity gone. |
| 17 (ELIEN SL) | SL sat 9.4 hours on a thin token. Overnight drift. |
| 20 (DOGE SL) | Tier-1 retry on a fast-falling token. The semantic issue above. |
| 14 (monk TP50) | Latency 10 s on TP50, position +82 % vs entry but executed at −66 % vs observed. Fast-up tokens collapse fast. |

### Possible fixes (none shipped, all touch hot modules)

1. **Tighten tier escalation.** 500 → 1000 → 1500 (instead of 500 → 1500 → 3000). Catastrophic exits would fail-revert and the position would sit unsold and decay further. **Probably worse, not better.**
2. **Drop Jito tip on exits.** Save ~1 s of latency on the bundle path. Costs MEV protection on exits, but exits are less attractive to sandwich than entries (smaller markup). Worth measuring before shipping.
3. **Hard-cap absolute price floor.** "Don't fill if executed_price < observed_price × 0.7." Not possible mid-tx with Jupiter — fill is atomic and price is unknown until post-confirm.
4. **Earlier exit on thin/aging tokens.** time-stop activation (handoff backlog item, S74 P1a) plus a liquidity-degradation check would have caught id 8 and id 17 before they became catastrophic. **Best risk-adjusted lever.**

### Recommendation for S80

Don't ship a slippage patch reactively. Activate the time-stop (`TIME_STOP_ENABLED=true`) which the handoff has been deferring "for a clean post-fix baseline" — we now have it. That alone removes the id-17-class failure mode without touching solana_executor.

---

## 2. Soak read on Jito vs standard RPC fallback (S77 P1, maxAccounts=64)

Period: 2026-04-30 04:58 UTC → 19:30 UTC (~14.5 h).

| Metric | Count |
|---|---|
| Submitted via Jito | 16 |
| Submitted via standard RPC fallback | 1 |
| Solana entries | 6 |
| Solana exits | 4 |
| Slippage escalation retries | 3 |
| Solana execution failures | 1 (Yippee — `base64 encoded too large` at maxAccounts=64) |
| **Yippee-class miss rate** | **1 / 6 = 17 %** |

The S78 reading was 1/5 = 20 %. The number is essentially flat. Sample size is still 4 entries shy of the n≥10 threshold the handoff set for shipping the dynamic retry patch (re-quote at maxAccounts=24 if the maxAccounts=64 quote returns a tx too large for Jito). Defer to S80 or later.

Failure mode is graceful — RPC rejects pre-commit, no money committed, opportunity loss only. Not bleeding.

---

## 3. CallAnalyser CPW-aware tiered boost research

**Question from handoff:** "Read ~50 sample messages from the live monitor first, validate format consistency, then ship tiered scoring (CPW<200=+8, CPW<500=+5, CPW>500=+2)."

**Method:** One-shot Telethon read of last 60 messages from `@CallAnalyserSol`. Live monitor not disrupted. Sample period: 2026-04-30 06:39 UTC → 19:31 UTC (~13 h, 60 messages = 4–5 calls/hour).

### Format consistency: rock solid

| Field | Present in |
|---|---|
| `CPW: <num>/1000` | 60 / 60 = **100 %** |
| `Score: <num>/100` | 60 / 60 = 100 % |
| Solana contract address | 60 / 60 = 100 % |
| `⚡️Caller: <name>` | 60 / 60 = 100 % |
| `(First Call)` / similar tag | 37 / 60 = 62 % |

A regex parser with `CPW: (\d+)/1000`, `Score: (\d+)/100`, `⚡️Caller: (.+?) \| CPW` is reliable. No format drift across the whole window.

### CPW distribution — the handoff plan needs revision

Min 281, max 898, **median 495, mean 493**. Bucketed:

| CPW bucket | Messages | % |
|---|---|---|
| < 200 | **0** | **0 %** |
| 200–499 | 32 | 53 % |
| 500–999 | 28 | 47 % |
| 1000+ | 0 | 0 % |

**The +8 tier (`CPW < 200`) is dead — no messages qualify.** The CPW field is bounded 281–898 in practice; sub-200 is theoretical. Tiering as written would never fire the high-quality bucket.

### CPW is a caller fingerprint, not a per-call signal

Each caller's CPW is near-constant over the sample window:

| Caller | Times seen | CPW range |
|---|---|---|
| Nyales Kripto Channel's 💎 | 11 | 313–314 |
| Nik's Eth + Sol Plays | 4 | 439–439 |
| Az Calls | 3 | 396–399 |
| 💰ANIME GEMS💰 | 3 | 500–501 |
| The Degens Den | 3 | 281–281 |
| Pow's Gem Calls | 2 | 879–880 |

CPW is computed weekly from the caller's history, so any given week's value is a property of the caller, not the call. Tagging a CPW threshold is functionally equivalent to whitelisting/blacklisting callers by frequency.

### The Score field is the more discriminating signal

| Score | Messages | % |
|---|---|---|
| 31–34 | 43 | 72 % |
| 60–69 | 10 | 17 % |
| 85+ | 7 | **12 %** |

Score is per-call (not per-caller — same caller produces calls at different scores). High-score messages (≥85) are 7/60 = 12 % of the sample.

### Score × CPW crosstab

| Score bucket | CPW 281–349 | CPW 350–499 | CPW 500–699 | CPW 700–898 |
|---|---|---|---|---|
| 31–34 (low) | 16 | 14 | 13 | 0 |
| 60–69 (mid) | 0 | 1 | 6 | 3 |
| 85+ (high) | 0 | 1 | 2 | 4 |

**Score and CPW are *inversely* related**, not aligned. High-score calls cluster in *high-CPW* (busier) callers — busy callers occasionally land a great call, and CallAnalyser scores those. Low-CPW (selective) callers' calls cluster at the floor score. The handoff's assumption that low CPW = better signal is not what the data shows.

### Recommended re-anchored tiering for S80

Replace CPW-based tiering with Score-based tiering, plus CPW as a tiebreaker:

| Condition | Boost |
|---|---|
| Score ≥ 95 | **+8** |
| Score ≥ 85 | **+5** |
| Score 60–84 | **+3** |
| Score < 60 (any caller) | **+1** (matches existing trusted-channel baseline) |
| `CPW < 350` AND `Score ≥ 60` | **+2 bonus** (selective caller landing a real call) |

Distribution under this model: +8 fires on 7 % of messages, +5 on 5 %, +3 on 17 %, +1 baseline on 72 %, bonus on ~10 %. Aligned with how the actual Score field is distributed.

**Decision pending Morgan.** This rewrites the handoff plan; needs sign-off before I touch `social_signals.py` or `scanner.py`.

---

## Files referenced

- `data/cryptobot.db::exit_attempts` (n=22 rows total, 18 with valid slippage data)
- `data/cryptobot.db::positions` (last 12 closed positions inspected)
- VPS scanner `journalctl` logs since 2026-04-30 04:58 UTC
- One-shot Telethon dump: `/tmp/callanalyser_dump.json` (60 messages, kept on VPS for S80 if needed)

