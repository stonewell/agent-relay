import pytest

from orch import __version__
from orch.cli.main import main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_unknown_command_fails() -> None:
    assert main(["run"]) == 2
