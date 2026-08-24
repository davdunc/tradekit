# Morning Game Plan — portable workflow

A harness-agnostic specification of the premarket game-plan process. Any
operator — a human at a terminal, or any LLM agent in any harness — should be
able to execute this workflow using only the `tradekit` CLI and produce the
same output document. No assumptions about the driving model, its tools, or
its memory system.

**Inputs (operator config, not hardcoded):**

| Parameter | Meaning | Example |
|-----------|---------|---------|
| `R` | Risk unit = 1% of active account equity | $280 |
| `price_band` | Tradable price range and A+ band | $3–50 sweet spot, $50–400 A+ only, >$400 excluded |
| `max_live_tickers` | Concurrent live tickers | 2 |
| `banned_tickers` | Tickers restricted to sim | per operator list |
| `session_window` | Trading window (local) | 8:30–12:00 CT |

**Outputs:** one game-plan document (template below). Publish wherever your
stack shares documents (Notion, wiki, markdown file, S3) — the format is
plain markdown + tables so it survives any target.

---

## Phase 1 — Market context

1. `tradekit regime` — indexes, VIX, sector breadth. Record regime label.
2. Economic calendar (any source): flag scheduled releases and speakers.

## Phase 2 — Sector & overnight comps  *(the 2026-08-24 MU lesson)*

For every ticker you intend to trade or shortlist:

3. `tradekit comps TICKER --direction long|short`

   The command builds the five-signal table: overnight Asia comps (memory →
   Samsung/SK Hynix; semis → TSMC), sector ETF premarket, inverse-ETF tell
   (sign-flipped), QQQ-vs-SPY skew — and prints the verdict.

   **Rule (non-negotiable):** 3+ signals against the intended direction ⇒
   the thesis is counter-trend. No breakout-bias entries; an explicit
   reclaim trigger is required or the trade is dropped.

   Manual addition: check the morning premarket-movers press column — is
   your ticker named, and on which side?

## Phase 3 — Scan & watchlist

4. `tradekit scan --preset premarket_gap` — gappers with volume.
5. Filter by `price_band`; drop `banned_tickers` from live consideration
   (sim allowed).
6. `tradekit second-day` — prior-session movers for continuation candidates.
7. Per candidate: `tradekit analyze TICKER` and `tradekit levels TICKER`
   (Camarilla pivots + ATR from the daily chart).

## Phase 4 — Grade and separate

8. Grade each candidate A/B/C (catalyst + chart + volume + covariance
   alignment). C ⇒ sim only.
9. Separate **Fresh News** (new catalyst today) from **Second Day /
   Technical** (continuations, range breaks).

## Phase 5 — Thesis trade

10. Pick ONE highest-conviction ticker: strongest catalyst + cleanest chart
    + sector verdict ALIGNED. It gets live capital and conviction sizing.
11. Prepare BOTH directions: long scenario and short scenario with entry
    trigger, stop (ATR or level), and targets in R multiples.

## Phase 6 — Output

Fill this template. All performance numbers in **R units** when the document
is shared beyond the operator (account dollars and balances never leave the
private copy).

```
═══ MORNING GAME PLAN — [Date] ═══

Market Regime: [label]
Behavioral Focus: [one line]
Thesis Trade: [TICKER] — [setup] — [direction]

SECTOR & OVERNIGHT COMPS:
[per intended ticker: tradekit comps table + verdict]
Rule: 3+ signals against intended direction → counter-trend, reclaim trigger required.

FRESH NEWS:
[Ticker | Support | Resistance | Bias | Grade | Plan]

SECOND DAY PLAYS:
[Ticker | Support | Resistance | Bias | Grade | Plan]

RULES REMINDER:
[top 3 operator rules for today]
```

---

## Adapting to your stack

- **Different LLM/harness:** hand this file to the agent as its task spec.
  Every data step is a `tradekit` CLI call; the agent needs shell access and
  nothing else. Output-schema conventions: see `docs/output-schema.md`.
- **Different publish target:** Phase 6 is plain markdown — post it to
  Notion, a wiki, S3, or a file. Keep the private/shared split: R units in
  shared copies, dollars only in the operator's private copy.
- **Different market focus:** extend the comp map in
  `src/tradekit/analysis/comps.py` (`COMP_MAP`, `TICKER_SECTOR`) — it is
  data, not logic.
