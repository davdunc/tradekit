"""``analysis`` is pure computation: no network, no data layer, no provider calls (SPEC, #49).

The SPEC's module table describes ``analysis`` as "pure computation over DataFrames", and import
rule 2 says it "never imports data". Until #49 nothing checked either: ``gex`` opened HTTP
connections itself, ``earnings`` fetched through it, and ``comps`` called a provider passed in.
None of that broke the import graph, so this test reads each module's source tree instead.
"""

import ast
import pathlib

import pytest

ANALYSIS = pathlib.Path(__file__).resolve().parents[1] / "src" / "tradekit" / "analysis"

#: Top-level packages that do I/O over the network (or wrap a broker/vendor API that does).
NETWORK = {
    "aiohttp",
    "alpaca",
    "boto3",
    "botocore",
    "finvizfinance",
    "http",
    "httpx",
    "ib_async",
    "mcp",
    "requests",
    "socket",
    "urllib",
    "urllib3",
    "websockets",
    "yfinance",
}
#: tradekit layers that analysis must not reach into.
FORBIDDEN_TRADEKIT = ("tradekit.data", "tradekit.cli", "tradekit.reports", "tradekit.screener")
#: Calls that fetch: provider methods and raw URL opening.
FETCH_CALLS = {
    "get_quote",
    "get_premarket",
    "get_history",
    "get_multiple_quotes",
    "get_multiple_premarket",
    "urlopen",
}


def violations(path: pathlib.Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in NETWORK or alias.name.startswith(FORBIDDEN_TRADEKIT):
                    found.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in NETWORK or node.module.startswith(FORBIDDEN_TRADEKIT):
                found.append(f"line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            if name in FETCH_CALLS:
                found.append(f"line {node.lineno}: {name}()")
    return found


MODULES = sorted(ANALYSIS.glob("*.py"))


def test_the_analysis_package_is_where_the_test_expects():
    assert {p.name for p in MODULES} >= {"comps.py", "gex.py", "earnings.py", "risk.py"}


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_analysis_module_does_no_io(path):
    assert violations(path) == [], f"{path.name} reaches outside pure computation"


@pytest.mark.parametrize(
    ("source", "caught"),
    [
        ("import urllib.request", "import urllib.request"),
        ("from tradekit.data.cboe import fetch_cboe_chain", "from tradekit.data.cboe"),
        ("def f(p):\n    return p.get_quote('SPY')", "get_quote()"),
        ("import yfinance as yf", "import yfinance"),
    ],
)
def test_the_checker_catches_each_kind_of_violation(tmp_path, source, caught):
    module = tmp_path / "bad.py"
    module.write_text(source)
    assert any(caught in v for v in violations(module))
