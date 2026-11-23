"""Read-only DAS CMD API connection and probe (#9), against a fake DAS on a local socket."""

import asyncio
import json

import pytest

from tradekit.depth import das_cmd
from tradekit.depth.das_cmd import ReadOnlyViolation, WatchConnection, check_command, login_line, subscribe_line

# ----- the allowlist -----


@pytest.mark.parametrize(
    "line",
    [
        "NEWORDER 1 B AAPL SMRTL 100 Limit 345.00 DAY",
        "neworder 1 b aapl",
        "CANCEL ALL",
        "CANCEL 123",
        "SCRIPT MONTAGE1 SYMBOL AAPL",  # hotkey scripts can place orders
        "POSREFRESH",
        "GET ORDERS",
        "",
        "SB AAPL Lv2\r\nNEWORDER 1 B AAPL",  # smuggled second command
        "SB AAPL Lv2\nCANCEL ALL",
        "LOGIN user pw acct",  # bare login: not WATCH
        "LOGIN user pw acct 1 1",  # #63 review: ends in " 1" but isn't the WATCH form
        "LOGIN user pw acct 1 extra",
        "LOGIN user pw 1",  # account missing: "1" would be read as the account
        "LOGIN user pw acct 0",
    ],
)
def test_anything_but_watch_login_and_market_data_is_refused(line):
    with pytest.raises(ReadOnlyViolation):
        check_command(line)


@pytest.mark.parametrize("line", ["LOGIN u p a 1", "SB AAPL Lv2", "sb aapl tms", "UNSB AAPL Lv1"])
def test_watch_login_and_subscriptions_pass(line):
    assert check_command(line) == line


def test_login_is_always_watch_mode():
    assert login_line("user", "pw", "TR4425") == "LOGIN user pw TR4425 1"
    for bad in [("", "pw", "a"), ("u", "p w", "a"), ("u", "pw", "a\n")]:
        with pytest.raises(ValueError):
            login_line(*bad)


def test_subscribe_line_validates_symbol_and_kind():
    assert subscribe_line("aapl", "Lv2") == "SB AAPL Lv2"
    for sym, kind in [("AAPL;X", "Lv2"), ("A A", "Lv2"), ("AAPL", "Lv2 X"), ("", "Lv1")]:
        with pytest.raises(ValueError):
            subscribe_line(sym, kind)


# ----- against a fake DAS -----


class FakeDAS:
    """Replies to LOGIN with ``login_reply`` and to each SB with ``replies(symbol, kind)``."""

    def __init__(self, login_reply, replies=lambda s, k: [], close_after_login=False):
        self.login_reply, self.replies, self.close_after_login = login_reply, replies, close_after_login
        self.received: list[str] = []

    async def handle(self, reader, writer):
        while raw := await reader.readline():
            line = raw.decode().rstrip("\r\n")
            self.received.append(line)
            if line.startswith("LOGIN"):
                writer.write(f"{self.login_reply}\r\n".encode())
                if self.close_after_login:
                    await writer.drain()
                    writer.close()
                    return
            elif line.startswith("SB "):
                _, sym, kind = line.split()
                for out in self.replies(sym, kind):
                    writer.write(f"{out}\r\n".encode())
            await writer.drain()
        writer.close()


def run_probe(fake, tmp_path, **kwargs):
    async def main():
        server = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        conn = await WatchConnection.open("127.0.0.1", port)
        try:
            return await das_cmd.probe(
                conn,
                login=login_line("me", "s3cret", "TR1"),
                password="s3cret",
                user="me",
                account="TR1",
                capture=tmp_path / "cap.jsonl",
                login_wait=0.2,
                listen=0.3,
                pace=0.02,
                **kwargs,
            )
        finally:
            server.close()
            await server.wait_closed()

    return asyncio.run(main())


def lv2(sym, kind):
    if kind == "Lv2":
        return [f"$Lv2 {sym} B 345.10 300 NSDQ", f"$Lv2 {sym} A 345.12 200 ARCA"]
    if kind == "Lv1":
        return [f"$Quote {sym} B:345.10 A:345.12"]
    return [f"#ERROR unknown kind {kind}"]


def test_probe_subscribes_and_records_everything_without_the_password(tmp_path):
    fake = FakeDAS("#LOGIN SUCCESSED", lv2)
    result = run_probe(fake, tmp_path, symbols=["aapl", "nvda"], kinds=["Lv1", "Lv2", "tms"])
    assert fake.received[0] == "LOGIN me s3cret TR1 1"  # WATCH on the wire
    assert fake.received[1:] == [f"SB {s} {k}" for s in ("AAPL", "NVDA") for k in ("Lv1", "Lv2", "tms")]
    assert result.login_reply == ["#LOGIN SUCCESSED"] and not result.login_refused
    assert result.first_tokens["$Lv2"] == 4 and result.lines_per_symbol["NVDA"] == 3  # 2 Lv2 + 1 Lv1

    text = (tmp_path / "cap.jsonl").read_text(encoding="utf-8")
    assert "s3cret" not in text
    rows = [json.loads(r) for r in text.splitlines()]
    assert rows[0] == {**rows[0], "dir": "sent", "line": "LOGIN <user> *** <account> 1"}
    assert {r["dir"] for r in rows} == {"sent", "recv"}


def test_a_refused_login_subscribes_nothing(tmp_path):
    fake = FakeDAS("#LOGIN FAILED invalid password", lv2)
    result = run_probe(fake, tmp_path, symbols=["AAPL"])
    assert result.login_refused
    assert fake.received == ["LOGIN me s3cret TR1 1"]


def test_server_closing_after_login_is_reported(tmp_path):
    fake = FakeDAS("bye", close_after_login=True)
    result = run_probe(fake, tmp_path, symbols=["AAPL"])
    assert result.closed_by_server and result.sent == ["LOGIN <user> *** <account> 1"]


def test_send_refuses_an_order_before_writing_a_byte(tmp_path):
    fake = FakeDAS("#LOGIN SUCCESSED")

    async def main():
        server = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
        conn = await WatchConnection.open("127.0.0.1", server.sockets[0].getsockname()[1])
        with pytest.raises(ReadOnlyViolation):
            await conn.send("NEWORDER 1 B AAPL SMRTL 100 Limit 1.00 DAY")
        await conn.close()
        await asyncio.sleep(0.05)
        server.close()
        await server.wait_closed()

    asyncio.run(main())
    assert fake.received == []


def test_the_module_never_builds_an_order():
    """No order verb appears in das_cmd outside the docstrings that name what is refused."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(das_cmd))
    docstrings = {
        id(n.body[0].value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and n.body
        and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)
    }
    strings = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]
    import re

    for verb in ("NEWORDER", "CANCEL", "SCRIPT"):
        assert not any(re.search(rf"\b{verb}\b", s.upper()) for s in strings), verb


def test_cli_names_every_missing_setting(monkeypatch):
    from click.testing import CliRunner

    from tradekit import cli as cli_mod
    from tradekit.config import DataSettings

    monkeypatch.setitem(DataSettings.model_config, "env_file", [])
    for name in ("DAS_HOST", "DAS_PORT", "DAS_USER", "DAS_PASSWORD", "DAS_ACCOUNT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli_mod, "get_settings", lambda: __import__("tradekit.config").config.Settings())
    result = CliRunner().invoke(cli_mod.cli, ["depth", "das-probe", "AAPL"])
    assert result.exit_code == 2
    for name in ("DAS_HOST", "DAS_PORT", "DAS_USER", "DAS_PASSWORD", "DAS_ACCOUNT"):
        assert name in result.output
    assert "127.0.0.1" in result.output


def test_account_and_trader_in_received_rows_are_masked_too(tmp_path):
    """DAS's order and trade rows carry account and trader columns (seen on #9's capture headers)."""
    rows = {"Lv1": ["%IORDER 1 tok AAPL B L 100 0 0 345.1 SMRTL Executed 09:31:00 0 TR1 me API DAY"]}
    fake = FakeDAS("#LOGIN SUCCESSED", lambda s, k: rows.get(k, []))
    result = run_probe(fake, tmp_path, symbols=["AAPL"], kinds=["Lv1"])
    text = (tmp_path / "cap.jsonl").read_text(encoding="utf-8")
    assert "TR1" not in text and " me " not in text and "s3cret" not in text
    assert "0 <account> <user> API DAY" in text
    assert result.sent[0] == "LOGIN <user> *** <account> 1"


def test_redaction_never_rewrites_market_data_or_parts_of_numbers():
    from tradekit.depth.das_cmd import redactor

    redact = redactor("pw", user="me", account="34")
    # market data is left exactly as received: the size 34 is data, not the account
    assert redact("$Lv2 AAPL B BOAT 348.34 34 REMOVED 21:00:41") == "$Lv2 AAPL B BOAT 348.34 34 REMOVED 21:00:41"
    # elsewhere, whole tokens only: 348.34 and 1340 keep their digits
    assert redact("%IORDER 7 AAPL B 348.34 1340 34 me") == "%IORDER 7 AAPL B 348.34 1340 <account> <user>"
    assert redact("acct 34. user me; pw=pw") == "acct <account>. user <user>; ***=***"
