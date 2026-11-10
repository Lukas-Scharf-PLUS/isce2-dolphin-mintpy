"""Record the software versions of a run in ``<work_dir>/logs/versions.json``.

The file is read by the post-processing (mintpy_postprocessing), so every
output file can say which ISCE2, dolphin, MintPy and pipeline version made it.
"""

import importlib.metadata
import json
import os
import platform
import subprocess
from pathlib import Path

VERSIONS_FILE = "versions.json"
# Set in the Docker image from the git commit it was built from.
GIT_COMMIT_VARIABLE = "PIPELINE_GIT_COMMIT"
# Distribution names (pip/conda), e.g. whirlwind-insar provides the module "whirlwind".
PACKAGES = ("dolphin", "mintpy", "snaphu", "whirlwind-insar", "pyaps3", "numpy", "gdal")


def pipeline_version():
    """Return the git commit of this pipeline code, or None if unknown.

    The Docker image stores it in ``PIPELINE_GIT_COMMIT``; in a git checkout
    the current commit is used, with ``-dirty`` if there are uncommitted changes.
    """
    if os.environ.get(GIT_COMMIT_VARIABLE):
        return os.environ[GIT_COMMIT_VARIABLE]
    repository = Path(__file__).resolve().parent.parent
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository,
                                capture_output=True, text=True, check=True).stdout.strip()
        changes = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                                 cwd=repository, capture_output=True, text=True,
                                 check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return f"{commit}-dirty" if changes else commit


def collect_versions():
    """Return a dict with the versions of ISCE2, the main packages and this pipeline."""
    versions = {"python": platform.python_version(), "isce_dolphin_mintpy": pipeline_version()}
    try:
        import isce

        versions["isce2"] = getattr(isce, "__version__", None)
    except ImportError:
        versions["isce2"] = None
    for package in PACKAGES:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def write_versions(log_dir):
    """Write ``versions.json`` into ``log_dir`` and return its path."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / VERSIONS_FILE
    path.write_text(json.dumps(collect_versions(), indent=2) + "\n")
    return path
