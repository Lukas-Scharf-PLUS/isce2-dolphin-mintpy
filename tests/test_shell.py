"""Tests for running external commands."""

import pytest

from isce_dolphin_mintpy.shell import CommandFailedError, run_command


def test_output_goes_to_log_and_optionally_to_terminal(tmp_path, capsys):
    log_file = tmp_path / "logs" / "tool.log"
    run_command("echo hello", log_file, echo=True)
    run_command(["echo", "quiet"], log_file)
    assert "hello" in capsys.readouterr().out
    log_text = log_file.read_text()
    assert "hello" in log_text and "quiet" in log_text


def test_failing_command_raises_with_log_path(tmp_path):
    log_file = tmp_path / "fail.log"
    with pytest.raises(CommandFailedError, match="fail.log"):
        run_command("echo boom; exit 3", log_file)
    assert "boom" in log_file.read_text()
