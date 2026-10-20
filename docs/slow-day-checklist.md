# Slow Day Checklist

The prioritization protocol for sessions where the normal news-driven workflow has nothing
to say: muted volatility, almost no fresh news, a gap into the middle of the prior session's
range, and the big idea not playing out. The answer on those days is not "there's nothing
to do" — it is to run five checks, in order, and let the market show you where to look.

Source: Roberto's slow-day checklist, built with his trading room and inspired by
*The Checklist Manifesto*. This document is the operator reference; the executable
workflow (phases, output placement) lives in LifeOS
`skills/Trading/Workflows/SlowDayChecklist.md`.

## The five checks — always in this order

### 1. Big Picture

Where is the market relative to its most important support and resistance? Always start
here; it is the context every later check is read against.

- Locate price versus the key levels, and the gap versus the prior session's range.
- State whether the original thesis is still playing out: INTACT / NOT PLAYING OUT / UNCLEAR.

Evidence source: `tradekit regime --json` — the completed-session dimensional read
(direction, structure, volatility, participation, liquidity) plus the configured
playbook-eligibility decisions. `tradekit regime` is built from completed sessions, so it
is a pre-session context read, not a live intraday assessment; it never sets direction for
a name on its own.

### 2. Sector ETFs

Which sectors are gapping into levels that matter, and which are just drifting? Gaps into
support or resistance show where other participants will need to act faster.

Evidence source: `tradekit groups` ranks sector/industry/country rotation with relative
volume — it identifies *where* to look. It does **not** provide ETF price levels; levels
come from the operator's charts. A level that was not actually pulled is written
UNAVAILABLE with its reason, never guessed.

### 3. Catalysts

Any fresh news, upgrades or rating changes, scheduled events? Note which candidates have a
fresh catalyst and which do not — the absence of a catalyst does not disqualify a name on
a slow day; that is precisely when the other checks carry the ranking.

### 4. Elevated premarket volume

Dollar volume = premarket VWAP × premarket shares traded. Compare it to prior days and to
other names, then prioritize what is really moving.

`tradekit` does not compute premarket VWAP. Pull VWAP and shares from the trading
platform, compute the product per candidate, and write UNAVAILABLE with the reason where
premarket data is missing. Do not fabricate or extrapolate a dollar-volume figure.

### 5. Relative strength / weakness

At the open, switch from execution mindset to scanning mindset. Names holding bid while
others roll over are telling you they are In Play. Let the market show you.

Premarket relative strength is a **pending observation** until the open confirms it — the
check produces candidates to watch, not a completed read.

## Risk and participation rules

These are Roberto's stated guidance, not validated or tradekit-enforced gates:

- **C-size risk** on slow-day ideas. A C-risk trade can still become an A outcome — the
  AMZN 4:1 review example. C-size comes from the operator's approved R-CONFIG definition;
  this checklist does not define it. If absent, write C-size UNDEFINED and derive no risk
  or share count.
- **RVOL target 1.5 or higher.** Most traders lose the most money at 0.8 or less. The
  note does not define the RVOL calculation — compute it on the platform and name the
  lookback used.

## When to run it

- **Premarket**, whenever the normal news checklist came up empty or the tape is quiet.
- **Mid-session**, whenever the published thesis is not playing out — restart from check 1.
  The note's example time is 11:30 AM; it is an illustration, not a schedule.

A slow day is **not** automatically a no-trade day. The checklist is a prioritization pass;
its output can still be a full game plan. What a reset never does: resurrect expired
triggers, bypass R-viability or playbook gates, or authorize an automatic reversal.
