"""Stage 4b: run MintPy on the converted dolphin results.

Two modes (config ``mintpy.inversion``):

``mintpy`` (default)
    MintPy inverts dolphin's interferogram network (modify_network ...
    invert_network). Because phase-linked interferograms have no closure phase,
    MintPy's own temporal coherence is close to 1 almost everywhere, so the
    pixel mask is additionally restricted with dolphin's phase-linking temporal
    coherence (``dolphin_temporal_coherence_threshold``). Then all MintPy
    corrections, velocity and geocoding run.

``dolphin``
    dolphin's own time series inversion is used. MintPy only selects the
    reference point on the interferogram stack, the dolphin time series is
    re-referenced to that point and then goes through the same corrections.

In both modes ``load_data`` is never run: the inputs are written by
``mintpy_inputs.py``. The MintPy template is written from the ``mintpy.*``
options of the config (see ``mintpy_template.py``). Unless ``mintpy.plot`` is
``no``, every step is followed by MintPy's plotting, so ``mintpy/pic/`` always
holds the figures of all steps run so far, even if a later step fails.
"""

import logging
import shutil

import h5py
import numpy as np

from .config import INVERSION_BY_DOLPHIN
from .mintpy_inputs import DOLPHIN_TEMPORAL_COHERENCE_FILE, prepare_mintpy_inputs
from .mintpy_template import (
    is_plotting_enabled,
    min_temporal_coherence,
    requested_reference_date,
    write_template,
)
from .shell import CommandFailedError, run_command

logger = logging.getLogger(__name__)

TEMPLATE_FILE = "isce_dolphin_mintpy.cfg"
MASK_FILE = "maskTempCoh.h5"
STACK_FILE = "inputs/ifgramStack.h5"
SLC_NAME_PATTERN = "S1*_IW_SLC_*"
QUICK_OVERVIEW_STEP = "quick_overview"
# Written by quick_overview: number of triplets with non-zero integer ambiguity per pixel.
AMBIGUITY_FILE = "numTriNonzeroIntAmbiguity.h5"

# MintPy steps before the network inversion, in smallbaselineApp.py order.
STEPS_BEFORE_INVERSION = ["modify_network", "reference_point", QUICK_OVERVIEW_STEP,
                          "correct_unwrap_error"]
# In dolphin mode there is no network inversion, so unwrapping errors are not corrected.
STEPS_BEFORE_DOLPHIN_SERIES = ["modify_network", "reference_point", QUICK_OVERVIEW_STEP]
STEPS_AFTER_INVERSION = [
    "correct_LOD", "correct_SET", "correct_ionosphere", "correct_troposphere", "deramp",
    "correct_topography", "residual_RMS", "reference_date", "velocity", "geocode",
    "google_earth", "hdfeos5",
]


class MintpyStepError(RuntimeError):
    """Raised when the MintPy options do not fit the processed stack."""


def run_smallbaseline_step(config, template_file, step):
    """Run one ``smallbaselineApp.py`` step with ``--dostep``.

    Steps are run individually (instead of ``--start``/``--end``) so that the
    pipeline's own processing can run between them. ``--plot`` makes MintPy
    plot every result file that exists so far into ``pic/`` after the step.

    Raises
    ------
    CommandFailedError
        If the step (or its plotting) fails.
    """
    command = ["smallbaselineApp.py", str(template_file), "--dir", str(config.mintpy_dir),
               "--dostep", step]
    if is_plotting_enabled(config.mintpy.template_options):
        command.append("--plot")
    run_command(command, config.log_dir / "mintpy.log", cwd=config.mintpy_dir, echo=True)


def has_no_closure_ambiguity(mintpy_dir):
    """Return True if quick_overview found no triplet with non-zero integer ambiguity.

    MintPy 1.6 writes ``numTriNonzeroIntAmbiguity.h5`` first and then plots its
    histogram with ``bins=max``, which crashes ("`bins` must be positive") when
    the maximum is 0. That happens on phase-linked interferograms where every
    triplet closes, so it is a sign of *no* unwrapping errors, not a failure.
    """
    ambiguity_file = mintpy_dir / AMBIGUITY_FILE
    if not ambiguity_file.is_file():
        return False
    with h5py.File(ambiguity_file, "r") as source:
        counts = source[list(source.keys())[0]][:]
    valid = counts[~np.isnan(counts)]
    return valid.size == 0 or valid.max() == 0


def run_steps(config, template_file, steps):
    """Run the given MintPy steps in order.

    Only the known histogram crash of ``quick_overview`` is tolerated (see
    ``has_no_closure_ambiguity``); it is logged as a warning. Every other
    failure stops the pipeline.
    """
    for step in steps:
        try:
            run_smallbaseline_step(config, template_file, step)
        except CommandFailedError:
            if step != QUICK_OVERVIEW_STEP or not has_no_closure_ambiguity(config.mintpy_dir):
                raise
            logger.warning(
                "quick_overview: no triplet with non-zero integer ambiguity (no unwrapping "
                "errors found by phase closure). MintPy 1.6 cannot plot that histogram; "
                "%s and avgPhaseVelocity.h5 were written, continuing.", AMBIGUITY_FILE)


def check_reference_date_in_stack(mintpy_dir, template_options):
    """Fail early if ``mintpy.reference.date`` is not an acquisition date of the stack.

    MintPy only checks this in the ``reference_date`` step near the end, after
    the inversion and all corrections have run.

    Raises
    ------
    MintpyStepError
        If the requested date is not in ``inputs/ifgramStack.h5``.
    """
    reference_date = requested_reference_date(template_options)
    if reference_date is None:
        return
    with h5py.File(mintpy_dir / STACK_FILE, "r") as stack:
        stack_dates = sorted({date.decode() for date in stack["date"][:].flatten()})
    if reference_date not in stack_dates:
        raise MintpyStepError(
            f"mintpy.reference.date = {reference_date} is not an acquisition date of this "
            f"stack ({stack_dates[0]} ... {stack_dates[-1]}, {len(stack_dates)} dates). "
            "Use one of the stack dates or 'auto'."
        )


def slc_acquisition_dates(slc_dir):
    """Return the sorted acquisition dates (YYYYMMDD) of the Sentinel-1 SAFE folders/zips in ``slc_dir``.

    The date is the 6th ``_``-separated part of the SAFE name, e.g.
    ``S1A_IW_SLC__1SDV_20220101T164306_...`` -> ``20220101``.
    """
    safes = list(slc_dir.glob(SLC_NAME_PATTERN + ".SAFE")) + list(slc_dir.glob(SLC_NAME_PATTERN + ".zip"))
    return sorted({path.name.split("_")[5][:8] for path in safes})


def check_reference_date_in_slc_dir(config):
    """Fail before ISCE2 starts if ``mintpy.reference.date`` has no SLC in ``paths.slc_dir``.

    The final check against the interferogram stack happens again in the
    ``mintpy`` step (``check_reference_date_in_stack``), because MintPy's
    network modification can still drop dates.

    Raises
    ------
    MintpyStepError
        If SLCs exist but none is from the requested reference date.
    """
    reference_date = requested_reference_date(config.mintpy.template_options)
    if reference_date is None:
        return
    slc_dates = slc_acquisition_dates(config.paths.slc_dir)
    if slc_dates and reference_date not in slc_dates:
        raise MintpyStepError(
            f"mintpy.reference.date = {reference_date} has no SLC in {config.paths.slc_dir} "
            f"({slc_dates[0]} ... {slc_dates[-1]}, {len(slc_dates)} dates). "
            "Use one of these dates or 'auto'."
        )


def restrict_mask_with_dolphin_coherence(mintpy_dir, threshold):
    """AND the MintPy temporal coherence mask with dolphin's temporal coherence mask.

    Parameters
    ----------
    mintpy_dir : Path
    threshold : float
        Minimum dolphin temporal coherence for a pixel to stay valid.
    """
    with h5py.File(mintpy_dir / DOLPHIN_TEMPORAL_COHERENCE_FILE, "r") as source:
        dolphin_coherent = source["temporalCoherence"][:] >= threshold
    with h5py.File(mintpy_dir / MASK_FILE, "r+") as mask_file:
        mask = mask_file["mask"][:] & dolphin_coherent
        mask_file["mask"][:] = mask
        mask_file.attrs["DOLPHIN_TEMPORAL_COHERENCE_THRESHOLD"] = str(threshold)
    logger.info("Mask restricted with dolphin temporal coherence >= %.2f: %d valid pixels",
                threshold, int(mask.sum()))


def reference_timeseries_to_stack_point(mintpy_dir):
    """Re-reference dolphin's ``timeseries.h5`` to MintPy's reference point.

    MintPy's ``reference_point`` step stores REF_Y/REF_X (and REF_LAT/REF_LON)
    in ``inputs/ifgramStack.h5``; the same point is subtracted from every epoch.

    Raises
    ------
    RuntimeError
        If the interferogram stack has no reference point yet.
    """
    with h5py.File(mintpy_dir / STACK_FILE, "r") as stack:
        reference_attributes = {key: stack.attrs[key] for key in
                                ("REF_Y", "REF_X", "REF_LAT", "REF_LON") if key in stack.attrs}
    if "REF_Y" not in reference_attributes:
        raise RuntimeError("ifgramStack.h5 has no REF_Y/REF_X; run MintPy reference_point first.")

    ref_y, ref_x = int(reference_attributes["REF_Y"]), int(reference_attributes["REF_X"])
    with h5py.File(mintpy_dir / "timeseries.h5", "r+") as series:
        data = series["timeseries"][:]
        data -= data[:, ref_y, ref_x][:, None, None]
        series["timeseries"][:] = data
        for key, value in reference_attributes.items():
            series.attrs[key] = value
    logger.info("dolphin time series re-referenced to pixel y=%d, x=%d", ref_y, ref_x)


def use_dolphin_temporal_coherence(config):
    """Use dolphin's temporal coherence as MintPy's ``temporalCoherence.h5`` and mask.

    The mask threshold is ``dolphin_temporal_coherence_threshold``, or
    ``mintpy.networkInversion.minTempCoh`` if that is ``null``.
    """
    mintpy_dir = config.mintpy_dir
    shutil.copyfile(mintpy_dir / DOLPHIN_TEMPORAL_COHERENCE_FILE, mintpy_dir / "temporalCoherence.h5")
    threshold = config.mintpy.dolphin_temporal_coherence_threshold
    if threshold is None:
        threshold = min_temporal_coherence(config.mintpy.template_options)
    run_command(["generate_mask.py", "temporalCoherence.h5", "-m", str(threshold),
                 "-o", MASK_FILE], config.log_dir / "mintpy.log", cwd=mintpy_dir)


def run_mintpy(config):
    """Convert dolphin outputs, then run MintPy in the configured mode.

    Raises
    ------
    MintpyStepError
        If ``mintpy.reference.date`` is not a date of the stack.
    CommandFailedError
        If a MintPy command fails (details in ``logs/mintpy.log``).
    """
    use_dolphin_series = config.mintpy.inversion == INVERSION_BY_DOLPHIN
    config.mintpy_dir.mkdir(parents=True, exist_ok=True)
    prepare_mintpy_inputs(config, include_dolphin_timeseries=use_dolphin_series)
    check_reference_date_in_stack(config.mintpy_dir, config.mintpy.template_options)

    template_file = config.mintpy_dir / TEMPLATE_FILE
    write_template(config.mintpy, template_file)

    if use_dolphin_series:
        run_steps(config, template_file, STEPS_BEFORE_DOLPHIN_SERIES)
        reference_timeseries_to_stack_point(config.mintpy_dir)
        use_dolphin_temporal_coherence(config)
    else:
        run_steps(config, template_file, STEPS_BEFORE_INVERSION + ["invert_network"])
        threshold = config.mintpy.dolphin_temporal_coherence_threshold
        if threshold is not None:
            restrict_mask_with_dolphin_coherence(config.mintpy_dir, threshold)

    run_steps(config, template_file, STEPS_AFTER_INVERSION)
