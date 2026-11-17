"""#18: a fresh install must say why accounts are UNMAPPED, and `init` must offer the fix."""

import logging
import re
import stat

import pytest
from click.testing import CliRunner

from tradekit import cli as cli_mod
from tradekit.reporting import ingest
from tradekit.reporting.schema import AccountKind


def _stat(account: str) -> dict:
    return {"account": account, "round_trips": 1, "wins": 1, "losses": 0, "realized": 10.0}


@pytest.fixture
def fresh_install(monkeypatch, tmp_path):
    """No env mapping, no accounts.toml, a private config dir, and the warn-once memory cleared."""
    monkeypatch.delenv("TRADEKIT_ACCOUNT_KINDS", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(ingest, "_warned_unmapped", set())
    return tmp_path / "config" / "tradekit" / "accounts.toml"


# ----- the warning -----


def test_empty_mapping_warns_once_naming_both_fixes(fresh_install, caplog):
    with caplog.at_level(logging.WARNING, logger="tradekit.reporting.ingest"):
        rows = ingest.accounts_from_falcon([_stat("AAA"), _stat("BBB"), _stat("CCC")])
    assert all(r.kind is None for r in rows)  # still never guessed as LIVE
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1, warnings
    assert "TRADEKIT_ACCOUNT_KINDS" in warnings[0]
    assert "tradekit init" in warnings[0]
    assert str(fresh_install) in warnings[0]


def test_account_missing_from_a_mapping_warns_once_per_account(fresh_install, monkeypatch, caplog):
    monkeypatch.setenv("TRADEKIT_ACCOUNT_KINDS", "AAA=LIVE")
    with caplog.at_level(logging.WARNING, logger="tradekit.reporting.ingest"):
        ingest.accounts_from_falcon([_stat("AAA"), _stat("ZZZ"), _stat("ZZZ")])
        ingest.trade_from_dict({"ticker": "X", "account": "ZZZ"})
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1 and "ZZZ" in warnings[0]


def test_fully_mapped_accounts_do_not_warn(fresh_install, monkeypatch, caplog):
    monkeypatch.setenv("TRADEKIT_ACCOUNT_KINDS", "AAA=LIVE,BBB=SIM")
    with caplog.at_level(logging.WARNING, logger="tradekit.reporting.ingest"):
        ingest.accounts_from_falcon([_stat("AAA"), _stat("BBB")])
    assert caplog.records == []


# ----- writing accounts.toml -----


def test_write_accounts_config_round_trips_with_owner_only_mode(fresh_install):
    path = ingest.write_accounts_config({"BBB": AccountKind.SIM, "AAA": AccountKind.LIVE})
    assert path == fresh_install
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert ingest.default_account_kinds() == {"AAA": AccountKind.LIVE, "BBB": AccountKind.SIM}


def test_write_accounts_config_never_overwrites(fresh_install):
    fresh_install.parent.mkdir(parents=True)
    fresh_install.write_text('[accounts]\n"KEEP" = "LIVE"\n', encoding="utf-8")
    with pytest.raises(FileExistsError):
        ingest.write_accounts_config({"AAA": AccountKind.SIM})
    assert "KEEP" in fresh_install.read_text(encoding="utf-8")


# ----- tradekit init -----


def _init(tmp_path, input_text: str, *extra: str):
    return CliRunner().invoke(cli_mod.cli, ["init", "--env-file", str(tmp_path / "test.env"), *extra], input=input_text)


def _plain(output: str) -> str:
    """CLI output as plain text: rich may colour digits and wrap long paths when it sees a terminal."""
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", output).split())


def test_init_offers_and_writes_accounts_toml(fresh_install, tmp_path):
    keys = "\n" * len(cli_mod._INIT_KEYS)  # keep every .env value
    result = _init(tmp_path, keys + "AAA=LIVE, BBB=sim\n")
    assert result.exit_code == 0, result.output
    assert "not configured" in _plain(result.output)
    assert stat.S_IMODE(fresh_install.stat().st_mode) == 0o600
    assert ingest.default_account_kinds() == {"AAA": AccountKind.LIVE, "BBB": AccountKind.SIM}


def test_init_skip_writes_nothing(fresh_install, tmp_path):
    result = _init(tmp_path, "\n" * (len(cli_mod._INIT_KEYS) + 1))
    assert result.exit_code == 0, result.output
    assert not fresh_install.exists()


def test_init_does_not_prompt_when_mapping_exists(fresh_install, tmp_path):
    ingest.write_accounts_config({"AAA": AccountKind.LIVE})
    result = _init(tmp_path, "\n" * len(cli_mod._INIT_KEYS))
    assert result.exit_code == 0, result.output
    out = _plain(result.output)
    assert "present" in out and "(1 accounts)" in out
    assert "Broker accounts as" not in out


def test_init_non_interactive_reports_missing_mapping(fresh_install, tmp_path):
    result = _init(tmp_path, "", "--non-interactive")
    assert result.exit_code == 0, result.output
    assert "not configured" in _plain(result.output)
    assert not fresh_install.exists()
