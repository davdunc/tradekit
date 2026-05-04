"""Subprocess wrapper around the PAI Inference tool.

Per AISTEERINGRULES, all LLM calls go through `bun ~/.claude/PAI/Tools/Inference.ts`
rather than importing the Anthropic SDK directly. This keeps billing on the user's
Claude Code subscription and centralizes model selection (fast/standard/smart).
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path

INFERENCE_TS = Path.home() / ".claude" / "PAI" / "Tools" / "Inference.ts"

Level = str  # "fast" | "standard" | "smart"


@dataclass
class InferenceResult:
    success: bool
    output: str
    parsed: dict | list | None
    error: str | None
    latency_ms: int
    level: Level


async def call(
    system_prompt: str,
    user_prompt: str,
    level: Level = "standard",
    expect_json: bool = False,
    timeout_s: float = 90.0,
) -> InferenceResult:
    """Invoke PAI Inference asynchronously."""
    if not INFERENCE_TS.exists():
        return InferenceResult(False, "", None, f"Inference.ts not found at {INFERENCE_TS}", 0, level)

    args = ["bun", str(INFERENCE_TS), "--level", level, "--timeout", str(int(timeout_s * 1000))]
    if expect_json:
        args.append("--json")
    args.extend([system_prompt, user_prompt])

    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("CLAUDECODE", None)

    import time

    start = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        return InferenceResult(False, "", None, f"timeout after {timeout_s}s", int((time.time() - start) * 1000), level)

    latency_ms = int((time.time() - start) * 1000)
    output = stdout.decode().strip()
    err = stderr.decode().strip()

    if proc.returncode != 0:
        return InferenceResult(False, output, None, err or f"exit code {proc.returncode}", latency_ms, level)

    parsed: dict | list | None = None
    if expect_json:
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            # Inference.ts already attempts to extract — if it failed, return raw
            pass

    return InferenceResult(True, output, parsed, None, latency_ms, level)


def call_sync(
    system_prompt: str,
    user_prompt: str,
    level: Level = "standard",
    expect_json: bool = False,
    timeout_s: float = 90.0,
) -> InferenceResult:
    """Synchronous wrapper — convenience for CLI use."""
    return asyncio.run(call(system_prompt, user_prompt, level, expect_json, timeout_s))
