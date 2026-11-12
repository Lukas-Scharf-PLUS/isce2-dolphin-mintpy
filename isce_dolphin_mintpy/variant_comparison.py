"""Compare the main MintPy run with its variants and select the least noisy one.

The main quality measure is MintPy's **residual RMS**: the ``residual_RMS``
step writes, for every date, the spatial RMS of ``timeseriesResidual.h5`` after
removing a ramp. That residual is what the temporal model of the DEM error step
(velocity, DEM error, optional periodic terms) cannot explain. Tropospheric
noise changes randomly from date to date, so the model cannot absorb it and it
ends up in the residual: a better correction gives a lower residual RMS on most
dates (Yunjun et al., 2019, Computers & Geosciences).

The selected run is the one with the lowest median residual RMS over the dates
that all runs share. The report also lists, per run, on how many dates it has
the lowest RMS and on how many it beats the main run, so a correction that
wins on average but makes single dates worse is visible.

Output: ``<work_dir>/mintpy/variant_comparison.json``.
"""

import json
import logging
import statistics

import h5py
import numpy as np

from .mintpy_variants import MAIN_RUN_NAME

logger = logging.getLogger(__name__)

COMPARISON_FILE_NAME = "variant_comparison.json"
# Written by MintPy's residual_RMS step; the name ends in "_ramp" when a ramp is
# removed first (mintpy.residualRMS.deramp), so all spellings are accepted.
RESIDUAL_RMS_FILE_PATTERN = "rms_timeseriesResidual*.txt"
EXCLUDE_DATE_FILE_NAME = "exclude_date.txt"
VELOCITY_FILE_NAME = "velocity.h5"
VELOCITY_STD_DATASET = "velocityStd"
MASK_FILE_NAME = "maskTempCoh.h5"
METERS_TO_MILLIMETERS = 1000.0
DECIMALS = 3


class VariantComparisonError(RuntimeError):
    """Raised when a run has no residual RMS to compare."""


def read_residual_rms(run_dir):
    """Return ``{date: rms_in_meters}`` from MintPy's residual RMS text file.

    If several files exist (reruns with different ``residualRMS.deramp``), the
    most recently written one is used.

    Raises
    ------
    VariantComparisonError
        If the file is missing, i.e. ``correct_topography`` or ``residual_RMS``
        did not run.
    """
    candidates = sorted(run_dir.glob(RESIDUAL_RMS_FILE_PATTERN), key=lambda path: path.stat().st_mtime)
    if not candidates:
        raise VariantComparisonError(
            f"No {RESIDUAL_RMS_FILE_PATTERN} in {run_dir}. The comparison needs MintPy's "
            "correct_topography and residual_RMS steps (mintpy.topographicResidual must not be 'no')."
        )
    rows = np.loadtxt(candidates[-1], dtype=str, comments="#", ndmin=2)
    return {date: float(rms) for date, rms in rows[:, :2]}


def read_excluded_dates(run_dir):
    """Return the dates MintPy excluded from the velocity fit (``exclude_date.txt``), or ``[]``."""
    exclude_file = run_dir / EXCLUDE_DATE_FILE_NAME
    if not exclude_file.is_file():
        return []
    return [line.strip() for line in exclude_file.read_text().splitlines() if line.strip()]


def median_velocity_std(run_dir):
    """Return the median ``velocityStd`` inside ``maskTempCoh.h5`` in mm/year, or ``None``.

    ``None`` if the velocity or mask file or the dataset does not exist, or the
    mask is empty.
    """
    velocity_file = run_dir / VELOCITY_FILE_NAME
    mask_file = run_dir / MASK_FILE_NAME
    if not velocity_file.is_file() or not mask_file.is_file():
        return None
    with h5py.File(velocity_file, "r") as velocity:
        if VELOCITY_STD_DATASET not in velocity:
            return None
        velocity_std = velocity[VELOCITY_STD_DATASET][:]
    with h5py.File(mask_file, "r") as mask_source:
        mask = mask_source["mask"][:].astype(bool)
    values = velocity_std[mask & np.isfinite(velocity_std)]
    if values.size == 0:
        return None
    return float(np.median(values)) * METERS_TO_MILLIMETERS


def compare_runs(residual_rms_by_run):
    """Rank runs by their residual RMS on the dates all of them share.

    Parameters
    ----------
    residual_rms_by_run : dict
        ``{run_name: {date: rms}}``, the main run first.

    Returns
    -------
    tuple[str, dict]
        ``(selected_run, scores)`` where ``scores[run]`` holds
        ``median_residual_rms``, ``dates_lowest_rms`` and
        ``dates_better_than_main`` (``None`` for the main run). On equal
        medians the earlier run wins, so the main run is kept on a tie.

    Raises
    ------
    VariantComparisonError
        If the runs share no date.
    """
    run_names = list(residual_rms_by_run)
    common_dates = sorted(set.intersection(*(set(rms) for rms in residual_rms_by_run.values())))
    if not common_dates:
        raise VariantComparisonError("The runs share no date with a residual RMS.")

    main_name = run_names[0]
    scores = {}
    for name in run_names:
        rms = residual_rms_by_run[name]
        scores[name] = {
            "median_residual_rms": statistics.median(rms[date] for date in common_dates),
            "dates_lowest_rms": 0,
            "dates_better_than_main": None if name == main_name else sum(
                rms[date] < residual_rms_by_run[main_name][date] for date in common_dates),
        }
    for date in common_dates:
        best = min(run_names, key=lambda name: residual_rms_by_run[name][date])
        scores[best]["dates_lowest_rms"] += 1

    selected = min(run_names, key=lambda name: scores[name]["median_residual_rms"])
    return selected, scores


def write_comparison(run_dirs, output_file, variant_options):
    """Compare all runs, write the JSON report and log a summary table.

    Parameters
    ----------
    run_dirs : dict
        ``{run_name: work_folder}``, the main run first.
    output_file : Path
        Where the JSON report is written.
    variant_options : dict
        ``{variant_name: {mintpy_option: value}}``, stored in the report so it
        shows what each variant changed.

    Returns
    -------
    str
        Name of the selected run.

    Raises
    ------
    VariantComparisonError
        If a run has no residual RMS or the runs share no date.
    """
    residual_rms_by_run = {name: read_residual_rms(path) for name, path in run_dirs.items()}
    selected, scores = compare_runs(residual_rms_by_run)

    runs = {}
    for name, path in run_dirs.items():
        velocity_std = median_velocity_std(path)
        runs[name] = {
            "directory": str(path),
            "options": variant_options.get(name, {}),
            "median_residual_rms_mm": _to_mm(scores[name]["median_residual_rms"]),
            "dates_lowest_rms": scores[name]["dates_lowest_rms"],
            "dates_better_than_main": scores[name]["dates_better_than_main"],
            "median_velocity_std_mm_per_year": (None if velocity_std is None
                                                else round(velocity_std, DECIMALS)),
            "excluded_dates": read_excluded_dates(path),
            "residual_rms_mm": {date: _to_mm(rms) for date, rms in residual_rms_by_run[name].items()},
        }
    report = {
        "metric": "median over the shared dates of MintPy's per-date residual RMS "
                  "(rms_timeseriesResidual*.txt); lowest wins",
        "selected": selected,
        "runs": runs,
    }
    output_file.write_text(json.dumps(report, indent=2) + "\n")
    _log_summary(runs, selected, output_file)
    return selected


def _to_mm(value_in_meters):
    """Convert meters to millimeters, rounded for the report."""
    return round(value_in_meters * METERS_TO_MILLIMETERS, DECIMALS)


def _log_summary(runs, selected, output_file):
    """Log one line per run with the comparison numbers."""
    logger.info("Variant comparison (%s):", output_file)
    for name, run in runs.items():
        better = run["dates_better_than_main"]
        logger.info(
            "  %-12s median residual RMS %.2f mm | lowest on %d dates | better than %s on %s dates"
            " | median velocityStd %s mm/yr%s",
            name, run["median_residual_rms_mm"], run["dates_lowest_rms"], MAIN_RUN_NAME,
            "-" if better is None else better,
            "-" if run["median_velocity_std_mm_per_year"] is None
            else f"{run['median_velocity_std_mm_per_year']:.2f}",
            "   <- selected" if name == selected else "",
        )
