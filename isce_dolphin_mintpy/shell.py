"""Small helpers to run external commands with logging.

All external tools (stackSentinel.py, dolphin, smallbaselineApp.py, ...) are
started through ``run_command`` so that every call is logged the same way and a
failing command always stops the pipeline with a clear error.

Output always goes to a log file. With ``echo=True`` it is also shown live in
the terminal, which is used for the long sequential tools (dolphin, MintPy).
Commands that run in parallel (ISCE2 run files) are not echoed, because their
output would be mixed.
"""

import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


class CommandFailedError(RuntimeError):
    """Raised when an external command exits with a non-zero status."""


def run_command(command, log_file, cwd=None, env=None, echo=False):
    """Run one external command and append its output to a log file.

    Parameters
    ----------
    command : list[str] | str
        Command as argument list, or as a shell string (run with ``bash -c``).
    log_file : Path
        File that receives stdout and stderr of the command (appended).
    cwd : Path, optional
        Working directory for the command.
    env : dict, optional
        Full environment for the command. Defaults to the current environment.
    echo : bool
        Also print the command output live to the terminal.

    Raises
    ------
    CommandFailedError
        If the command returns a non-zero exit code. The message points to the log.
    """
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    use_shell = isinstance(command, str)
    printable = command if use_shell else " ".join(str(part) for part in command)
    logger.info("Running: %s", printable)

    with open(log_file, "a") as log:
        log.write(f"\n$ {printable}\n")
        log.flush()
        process = subprocess.Popen(
            ["bash", "-c", command] if use_shell else [str(p) for p in command],
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        for line in process.stdout:
            log.write(line)
            if echo:
                sys.stdout.write(line)
                sys.stdout.flush()
        return_code = process.wait()

    if return_code != 0:
        raise CommandFailedError(
            f"Command failed with exit code {return_code}: {printable}\n"
            f"See log file: {log_file}"
        )
