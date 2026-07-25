"""Bull/bear debate prototype — TradingAgents-style decision layer.

Pipeline:
  1. Build a structured context for one ticker (price, ATR, RVOL, levels, recent OHLC).
  2. In parallel, ask a bull agent and a bear agent to each make their strongest case
     given the same context. Each returns a structured JSON {thesis, evidence[], invalidation}.
  3. Pass both cases to a judge agent that returns a final verdict
     {verdict: long|short|skip, confidence: 0-1, rationale, key_levels{entry, stop, target}}.

Output is a DebateResult with full transcript, persisted as JSON for replay/backtest.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tradekit.agents.inference import call as inference_call

DEFAULT_TRANSCRIPT_DIR = Path.home() / "market_data" / "debates"

# ---------- prompts ---------------------------------------------------------

BULL_SYSTEM = """You are a senior bull-side equity analyst at an intraday trading firm.
Given structured context for a single ticker, build the strongest possible long-bias case.
You must be specific and evidence-based — no generic platitudes. Cite the numbers in the context.
If the data does not support a long case, say so honestly rather than fabricate one.

Output STRICT JSON only, no prose, no markdown fences:
{
  "thesis": "one-sentence long thesis",
  "evidence": ["specific data point 1", "specific data point 2", "..."],
  "invalidation": "what price level or signal would kill this thesis",
  "conviction": 0.0-1.0
}"""

BEAR_SYSTEM = """You are a senior bear-side equity analyst at an intraday trading firm.
Given structured context for a single ticker, build the strongest possible short-bias case.
You must be specific and evidence-based — no generic platitudes. Cite the numbers in the context.
If the data does not support a short case, say so honestly rather than fabricate one.

Output STRICT JSON only, no prose, no markdown fences:
{
  "thesis": "one-sentence short thesis",
  "evidence": ["specific data point 1", "specific data point 2", "..."],
  "invalidation": "what price level or signal would kill this thesis",
  "conviction": 0.0-1.0
}"""

JUDGE_SYSTEM = """You are the head trader. Two analysts have presented bull and bear cases for the same ticker.
Read both cases against the structured context. Decide: long, short, or skip.
Skip is a valid and often correct answer when neither side is compelling enough to justify
intraday risk capital.

Output STRICT JSON only, no prose, no markdown fences:
{
  "verdict": "long|short|skip",
  "confidence": 0.0-1.0,
  "rationale": "2-3 sentences explaining why this side won and why this size of confidence",
  "key_levels": {"entry": float|null, "stop": float|null, "target": float|null},
  "stronger_side": "bull|bear|neither"
}"""

# ---------- data classes ---------------------------------------------------


@dataclass
class AgentCase:
    role: str  # "bull" | "bear"
    raw_output: str
    parsed: dict[str, Any] | None
    latency_ms: int
    error: str | None = None


@dataclass
class JudgeVerdict:
    verdict: str
    confidence: float
    rationale: str
    key_levels: dict[str, float | None]
    stronger_side: str
    raw_output: str
    latency_ms: int


@dataclass
class DebateResult:
    ticker: str
    timestamp: str
    context: dict[str, Any]
    bull: AgentCase
    bear: AgentCase
    judge: JudgeVerdict | None
    error: str | None = None

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, indent=2, default=str)


# ---------- main entry ----------------------------------------------------


async def _run_side(role: str, system: str, user: str, level: str) -> AgentCase:
    res = await inference_call(system, user, level=level, expect_json=True, timeout_s=60)
    return AgentCase(
        role=role,
        raw_output=res.output,
        parsed=res.parsed if isinstance(res.parsed, dict) else None,
        latency_ms=res.latency_ms,
        error=res.error,
    )


def _user_prompt(ticker: str, context: dict[str, Any]) -> str:
    return f"Ticker: {ticker}\n\nStructured context:\n```json\n{json.dumps(context, indent=2, default=str)}\n```"


async def bull_bear_debate(
    ticker: str,
    context: dict[str, Any],
    level: str = "standard",
    persist: bool = True,
    transcript_dir: Path | None = None,
) -> DebateResult:
    """Run the full bull/bear/judge debate for one ticker.

    Args:
        ticker: e.g. "AAPL"
        context: structured dict with whatever signals you have — price, atr, rvol,
                 vwap, recent_ohlc, news_summary, sector_context, etc. The richer
                 the context, the better the debate.
        level: "fast" (haiku), "standard" (sonnet), "smart" (opus)
        persist: if True, write the full transcript to disk
        transcript_dir: override default ~/market_data/debates/
    """
    user = _user_prompt(ticker, context)

    # bull and bear in parallel
    bull, bear = await asyncio.gather(
        _run_side("bull", BULL_SYSTEM, user, level),
        _run_side("bear", BEAR_SYSTEM, user, level),
    )

    judge: JudgeVerdict | None = None
    err: str | None = None

    if bull.parsed and bear.parsed:
        judge_user = (
            f"Ticker: {ticker}\n\nStructured context:\n```json\n{json.dumps(context, indent=2, default=str)}\n```\n\n"
            f"Bull case:\n```json\n{json.dumps(bull.parsed, indent=2)}\n```\n\n"
            f"Bear case:\n```json\n{json.dumps(bear.parsed, indent=2)}\n```"
        )
        jres = await inference_call(JUDGE_SYSTEM, judge_user, level=level, expect_json=True, timeout_s=120)
        if jres.parsed and isinstance(jres.parsed, dict):
            judge = JudgeVerdict(
                verdict=jres.parsed.get("verdict", "skip"),
                confidence=float(jres.parsed.get("confidence", 0.0)),
                rationale=jres.parsed.get("rationale", ""),
                key_levels=jres.parsed.get("key_levels", {"entry": None, "stop": None, "target": None}),
                stronger_side=jres.parsed.get("stronger_side", "neither"),
                raw_output=jres.output,
                latency_ms=jres.latency_ms,
            )
        else:
            err = f"judge failed to return valid JSON: {jres.error or 'parse error'}"
    else:
        err = f"bull or bear failed; bull_err={bull.error}, bear_err={bear.error}"

    result = DebateResult(
        ticker=ticker,
        timestamp=datetime.now(timezone.utc).isoformat(),
        context=context,
        bull=bull,
        bear=bear,
        judge=judge,
        error=err,
    )

    if persist:
        out_dir = transcript_dir or DEFAULT_TRANSCRIPT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        slug = result.timestamp.replace(":", "").replace("-", "")[:15]
        path = out_dir / f"{ticker}_{slug}.json"
        path.write_text(result.to_json())

    return result
