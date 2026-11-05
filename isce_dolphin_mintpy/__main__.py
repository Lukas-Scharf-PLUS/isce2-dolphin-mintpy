"""Command line entry point: ``python -m isce_dolphin_mintpy run <config.yaml>``.

Stages run in this order and can be selected with ``--steps``:
    isce     ISCE2 topsStack coregistration (only bursts overlapping the AOI)
    crop     cut the coregistered radar stack to the AOI (VRT windows) and,
             if configured, convert the water mask to dolphin's output grid
    dolphin  phase linking, interferograms, unwrapping
    mintpy   convert to MintPy and run inversion/corrections
"""

import argparse
import logging
import shutil
import sys

from .config import ConfigError, load_config
from .dolphin_step import run_dolphin
from .isce_stack import run_isce_stack
from .mintpy_step import run_mintpy
from .radar_crop import crop_stack_to_aoi
from .water_mask import create_water_mask

def run_crop(config):
    """Crop the radar stack to the AOI, then build the water mask on dolphin's grid."""
    crop_stack_to_aoi(config)
    create_water_mask(config)


STEPS = {
    "isce": run_isce_stack,
    "crop": run_crop,
    "dolphin": run_dolphin,
    "mintpy": run_mintpy,
}


def parse_arguments(argv):
    """Parse the command line arguments."""
    parser = argparse.ArgumentParser(
        prog="python -m isce_dolphin_mintpy",
        description="ISCE2 topsStack -> dolphin -> MintPy InSAR pipeline.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="Run pipeline steps.")
    run_parser.add_argument("config", help="Pipeline configuration YAML file.")
    run_parser.add_argument(
        "--steps", nargs="+", choices=list(STEPS), default=list(STEPS),
        help="Steps to run (default: all, in pipeline order).",
    )
    return parser.parse_args(argv)


def setup_logging(log_file):
    """Log to the console and to ``log_file``.

    ``import isce`` runs ``logging.config.fileConfig``, which removes existing
    handlers and disables all loggers created before it. ISCE2 is therefore
    imported first, then this package's loggers are re-enabled and the handlers
    are (re)installed with ``force=True``.
    """
    try:
        import isce  # noqa: F401
    except ImportError:
        pass  # steps that need ISCE2 report the missing package themselves
    for name, logger in logging.root.manager.loggerDict.items():
        if name.startswith(__package__) and isinstance(logger, logging.Logger):
            logger.disabled = False

    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(log_file)],
        force=True,
    )


def main(argv=None):
    """Run the selected pipeline steps; return a process exit code."""
    arguments = parse_arguments(argv)
    try:
        config = load_config(arguments.config)
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    config.paths.work_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(config.log_dir / "pipeline.log")
    # Keep a copy of the config next to the results for reproducibility.
    shutil.copyfile(arguments.config, config.paths.work_dir / "pipeline_config.yaml")

    selected = [name for name in STEPS if name in arguments.steps]
    for name in selected:
        logging.getLogger(__name__).info("=== Step: %s ===", name)
        STEPS[name](config)
    logging.getLogger(__name__).info("Finished steps: %s", ", ".join(selected))
    return 0


if __name__ == "__main__":
    sys.exit(main())
