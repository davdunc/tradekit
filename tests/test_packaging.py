"""The RPM spec's advertised command list must match the registered CLI (SPEC.md § CLI, gap G4)."""

from __future__ import annotations

import re
from pathlib import Path

from tradekit.cli import cli

SPEC = Path(__file__).resolve().parents[1] / "packaging" / "tradekit.spec"


def _spec_commands() -> set[str]:
    text = SPEC.read_text()
    m = re.search(r"^Commands:(.*?)\.\s*$", text, re.S | re.M)
    assert m, "packaging/tradekit.spec has no 'Commands:' list in %description"
    return {c.strip() for c in m.group(1).replace("\n", " ").split(",") if c.strip()}


def test_rpm_spec_commands_match_cli_help():
    advertised = _spec_commands()
    registered = set(cli.commands)
    assert advertised - registered == set(), f"spec advertises unregistered commands: {advertised - registered}"
    assert registered - advertised == set(), f"spec omits registered commands: {registered - advertised}"
