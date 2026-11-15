import json
from importlib.metadata import version

import pytest

from bellhop.cli import main


def test_version_prints_the_installed_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out == f"bellhop {version('bellhop')}\n"


def test_option_prefixes_are_not_expanded(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--vers"]) == 2
    assert capsys.readouterr().err.startswith("bellhop: unrecognized arguments: --vers")


def test_missing_command_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert capsys.readouterr().err == "bellhop: missing command; see 'bellhop --help'\n"


def test_unknown_option_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--bogus"]) == 2
    assert capsys.readouterr().err.startswith("bellhop: unrecognized arguments: --bogus")


def test_usage_error_with_json_prints_the_error_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--bogus", "--json"]) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 1
    assert payload["status"] == "error"
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"
    assert captured.err == ""
