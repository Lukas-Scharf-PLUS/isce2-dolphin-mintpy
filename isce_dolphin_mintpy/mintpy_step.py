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
``mintpy_inputs.py``.
"""

import logging
import shutil

import h5py
import numpy as np

from .config import INVERSION_BY_DOLPHIN
from .mintpy_inputs import DOLPHIN_TEMPORAL_COHERENCE_FILE, prepare_mintpy_inputs
from .shell import run_command

logger = logging.getLogger(__name__)

TEMPLATE_FILE = "isce_dolphin_mintpy.cfg"
MASK_FILE = "maskTempCoh.h5"

# MintPy steps before the network inversion. "quick_overview" is left out on
# purpose: it only makes diagnostic plots, and on phase-linked interferograms
# every triplet closes (no non-zero integer ambiguity), which makes MintPy's
# histogram plot crash with "`bins` must be positive" (MintPy 1.6.x).
STEPS_BEFORE_INVERSION = ["modify_network", "reference_point", "correct_unwrap_error"]
STEPS_AFTER_INVERSION = [
    "correct_LOD", "correct_SET", "correct_ionosphere", "correct_troposphere", "deramp",
    "correct_topography", "residual_RMS", "reference_date", "velocity", "geocode",
    "google_earth", "hdfeos5",
]


def _yes_no(flag):
    """Return MintPy's ``yes``/``no`` for a boolean."""
    return "yes" if flag else "no"


def build_template_options(mintpy_config):
    """Return the MintPy template options (key -> value) for this run.

    ``extra_template_options`` from the config override the generated values,
    so any MintPy option can be set without changing code.
    """
    reference_lalo = "auto"
    if mintpy_config.reference_lalo:
        reference_lalo = ", ".join(str(value) for value in mintpy_config.reference_lalo)
    lalo_step = "auto"
    if mintpy_config.geocode_lalo_step:
        lalo_step = ", ".join(str(value) for value in mintpy_config.geocode_lalo_step)

    options = {
        "mintpy.load.processor": "isce",
        "mintpy.reference.lalo": reference_lalo,
        "mintpy.unwrapError.method": mintpy_config.unwrap_error_correction,
        # dolphin connected components: 0 = unreliable unwrapping
        "mintpy.networkInversion.maskDataset": "connectComponent",
        "mintpy.networkInversion.maskThreshold": "0.5",
        "mintpy.networkInversion.minTempCoh": str(mintpy_config.min_temporal_coherence),
        "mintpy.troposphericDelay.method": mintpy_config.troposphere,
        "mintpy.topographicResidual": _yes_no(mintpy_config.topographic_residual),
        "mintpy.deramp": mintpy_config.deramp,
        "mintpy.solidEarthTides": _yes_no(mintpy_config.solid_earth_tides),
        "mintpy.geocode": _yes_no(mintpy_config.geocode),
        "mintpy.geocode.laloStep": lalo_step,
        "mintpy.save.kmz": "no",
        "mintpy.save.hdfEos5": "no",
        "mintpy.plot": "no",
    }
    options.update({str(k): str(v) for k, v in mintpy_config.extra_template_options.items()})
    return options


def write_template(mintpy_config, template_file):
    """Write the MintPy template file used for all smallbaselineApp.py calls."""
    lines = [f"{key:45s} = {value}" for key, value in build_template_options(mintpy_config).items()]
    template_file.write_text("\n".join(lines) + "\n")


def run_smallbaseline_steps(config, template_file, steps):
    """Run the given ``smallbaselineApp.py`` steps one by one with ``--dostep``.

    Steps are run individually (instead of ``--start``/``--end``) so that
    unwanted steps can be left out, see ``STEPS_BEFORE_INVERSION``.
    """
    for step in steps:
        command = ["smallbaselineApp.py", str(template_file), "--dir", str(config.mintpy_dir),
                   "--dostep", step]
        run_command(command, config.log_dir / "mintpy.log", cwd=config.mintpy_dir, echo=True)


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
    with h5py.File(mintpy_dir / "inputs" / "ifgramStack.h5", "r") as stack:
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


def use_dolphin_temporal_coherence(config, template_file):
    """Use dolphin's temporal coherence as MintPy's ``temporalCoherence.h5`` and mask."""
    mintpy_dir = config.mintpy_dir
    shutil.copyfile(mintpy_dir / DOLPHIN_TEMPORAL_COHERENCE_FILE, mintpy_dir / "temporalCoherence.h5")
    threshold = config.mintpy.dolphin_temporal_coherence_threshold
    if threshold is None:
        threshold = config.mintpy.min_temporal_coherence
    run_command(["generate_mask.py", "temporalCoherence.h5", "-m", str(threshold),
                 "-o", MASK_FILE], config.log_dir / "mintpy.log", cwd=mintpy_dir)


def run_mintpy(config):
    """Convert dolphin outputs, then run MintPy in the configured mode."""
    use_dolphin_series = config.mintpy.inversion == INVERSION_BY_DOLPHIN
    config.mintpy_dir.mkdir(parents=True, exist_ok=True)
    prepare_mintpy_inputs(config, include_dolphin_timeseries=use_dolphin_series)

    template_file = config.mintpy_dir / TEMPLATE_FILE
    write_template(config.mintpy, template_file)

    if use_dolphin_series:
        run_smallbaseline_steps(config, template_file, ["modify_network", "reference_point"])
        reference_timeseries_to_stack_point(config.mintpy_dir)
        use_dolphin_temporal_coherence(config, template_file)
    else:
        run_smallbaseline_steps(config, template_file, STEPS_BEFORE_INVERSION + ["invert_network"])
        threshold = config.mintpy.dolphin_temporal_coherence_threshold
        if threshold is not None:
            restrict_mask_with_dolphin_coherence(config.mintpy_dir, threshold)

    run_smallbaseline_steps(config, template_file, STEPS_AFTER_INVERSION)
