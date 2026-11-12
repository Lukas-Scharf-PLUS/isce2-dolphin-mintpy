"""MintPy variants: rerun the correction steps with other options for comparison.

A variant is a named set of MintPy options that replace the main run's options
for the steps from ``correct_troposphere`` on (troposphere, deramp, DEM error,
residual RMS, velocity, geocoding). Everything before that (network inversion,
solid earth tides, ionosphere) is identical for all runs, so it runs only once
in ``<work_dir>/mintpy`` and each variant continues from there in
``<work_dir>/mintpy/variants/<name>/``.

Example (YAML)::

    mintpy:
      mintpy.troposphericDelay.method: pyaps     # main run: ERA5
      variants:
        no_tropo: {mintpy.troposphericDelay.method: "no"}
        opera:    {mintpy.troposphericDelay.method: opera}

How a variant folder is prepared (``prepare_variant_dir``):

- The time series files written so far (``timeseries.h5``, ``timeseries_SET.h5``,
  ...) are *copied*, because MintPy's ``reference_date`` step rewrites every
  time series file of its folder in place. Symlinks would let a variant change
  the main run's files.
- ``inputs/`` and the other HDF5 files of the main folder (masks, coherence)
  are only read by the later steps, so they are symlinked (only the files in
  ``SHARED_READ_ONLY_FILES``). Results of the per-run steps (``velocity.h5``,
  ``demErr.h5``, ``timeseries*.h5``) are never linked. Weather model delays
  that MintPy writes to ``inputs/`` (e.g. ``inputs/ERA5.h5``) are therefore
  computed once and reused by every run with the same model.
"""

import logging
import re
import shutil

from .mintpy_template import format_template_value, validate_template_options

logger = logging.getLogger(__name__)

VARIANTS_DIR_NAME = "variants"
MAIN_RUN_NAME = "main"
VARIANT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

# Options of the steps that run separately for each variant (correct_troposphere
# ... hdfeos5). All other options act on the shared steps, so a variant value
# would have no effect and is rejected.
VARIANT_OPTION_PREFIXES = (
    "mintpy.troposphericDelay.",
    "mintpy.deramp",
    "mintpy.topographicResidual",
    "mintpy.residualRMS.",
    "mintpy.timeFunc.",
    "mintpy.geocode",
    "mintpy.save.",
    "mintpy.plot",
)

# Time series files of the shared steps: timeseries.h5 plus the suffixes of
# correct_LOD (_LOD), correct_SET (_SET) and correct_ionosphere (_ion), in
# MintPy's naming, e.g. timeseries_SET_ion.h5.
SHARED_TIMESERIES_PATTERN = re.compile(r"^timeseries(_(LOD|SET|ion))*\.h5$")
# Files of the shared steps that the per-run steps only read: masks for deramp
# and residual_RMS, coherence for geocode, the rest for MintPy's plots. Only
# these are linked. A result of a per-run step (velocity.h5, demErr.h5,
# timeseriesResidual.h5, ...) must never be linked: the main folder still holds
# it from an earlier run, and MintPy would write the variant's result through
# the link into the main run's file. A list of allowed files is used because a
# missing entry makes MintPy fail visibly, while a missing exclusion would not.
SHARED_READ_ONLY_FILES = (
    "maskTempCoh.h5",
    "maskConnComp.h5",
    "waterMask.h5",
    "temporalCoherence.h5",
    "avgSpatialCoh.h5",
    "avgPhaseVelocity.h5",
    "numInvIfgram.h5",
    "numTriNonzeroIntAmbiguity.h5",
)
INPUTS_DIR_NAME = "inputs"


def parse_variants(raw_variants):
    """Check the ``variants`` mapping of the config and convert values to MintPy text.

    Parameters
    ----------
    raw_variants : dict | None
        ``{variant_name: {mintpy_option: value}}`` from the YAML file.

    Returns
    -------
    dict
        ``{variant_name: {mintpy_option: text_value}}`` in file order.

    Raises
    ------
    ValueError
        If a name is invalid, a variant is empty, or an option is unknown to
        MintPy or belongs to a step that all runs share.
    """
    if raw_variants is None:
        return {}
    if not isinstance(raw_variants, dict):
        raise ValueError("'variants' must be a mapping {name: {mintpy.option: value}}.")

    variants = {}
    for name, options in raw_variants.items():
        name = str(name)
        _check_variant_name(name)
        if not isinstance(options, dict) or not options:
            raise ValueError(f"Variant '{name}' must be a non-empty mapping of mintpy.* options.")
        overrides = {str(key): format_template_value(value) for key, value in options.items()}
        _check_variant_options(name, overrides)
        variants[name] = overrides
    return variants


def _check_variant_name(name):
    """Raise ValueError if ``name`` cannot be used as a folder name or is reserved."""
    if not VARIANT_NAME_PATTERN.match(name):
        raise ValueError(f"Variant name '{name}' may only contain letters, digits, '_' and '-'.")
    if name == MAIN_RUN_NAME:
        raise ValueError(f"Variant name '{MAIN_RUN_NAME}' is reserved for the main run.")


def _check_variant_options(name, overrides):
    """Raise ValueError if an option is unknown or acts on a shared step."""
    shared_step_options = [key for key in overrides
                           if not key.startswith(VARIANT_OPTION_PREFIXES)]
    if shared_step_options:
        raise ValueError(
            f"Variant '{name}': {shared_step_options} act on steps that all runs share "
            f"(before correct_troposphere). Allowed option groups: {list(VARIANT_OPTION_PREFIXES)}."
        )
    try:
        validate_template_options(overrides)
    except ValueError as error:
        raise ValueError(f"Variant '{name}': {error}") from error


def variant_dir(mintpy_dir, name):
    """Return the work folder of variant ``name``."""
    return mintpy_dir / VARIANTS_DIR_NAME / name


def is_shared_timeseries_file(path):
    """Return True if ``path`` is a time series file written by the shared steps."""
    return bool(SHARED_TIMESERIES_PATTERN.match(path.name))


def prepare_variant_dir(mintpy_dir, target_dir):
    """Set up a variant folder so MintPy can continue at ``correct_troposphere``.

    Must be called after the shared steps and *before* the main run's later
    steps, because the main run's ``reference_date`` step changes the shared
    time series files in place.

    Parameters
    ----------
    mintpy_dir : Path
        The main MintPy folder.
    target_dir : Path
        The variant folder (created if needed). Existing links are replaced and
        time series files are copied again, so a rerun starts from the current
        main results; MintPy then recomputes the later steps because their
        inputs are newer.

    Raises
    ------
    FileNotFoundError
        If the main folder has no ``timeseries.h5`` or ``inputs/`` yet.
    """
    if not (mintpy_dir / "timeseries.h5").is_file():
        raise FileNotFoundError(f"{mintpy_dir / 'timeseries.h5'} is missing; variants need the "
                                "time series of the shared MintPy steps.")
    if not (mintpy_dir / INPUTS_DIR_NAME).is_dir():
        raise FileNotFoundError(f"{mintpy_dir / INPUTS_DIR_NAME} is missing.")
    target_dir.mkdir(parents=True, exist_ok=True)
    # Links of an earlier preparation are made again below; removing them all
    # first also drops links that must not exist (any more), see SHARED_READ_ONLY_FILES.
    for old_link in target_dir.iterdir():
        if old_link.is_symlink():
            old_link.unlink()

    _replace_with_symlink(target_dir / INPUTS_DIR_NAME, mintpy_dir / INPUTS_DIR_NAME)
    for source in sorted(mintpy_dir.glob("*.h5")):
        if is_shared_timeseries_file(source):
            # copyfile (not copy2) gives the copy a new modification time, so
            # MintPy sees its later outputs as outdated and recomputes them.
            shutil.copyfile(source, target_dir / source.name)
        elif source.name in SHARED_READ_ONLY_FILES:
            _replace_with_symlink(target_dir / source.name, source)
    logger.info("Variant folder prepared: %s", target_dir)


def _replace_with_symlink(link, source):
    """Create ``link`` pointing to ``source``, replacing an existing link or file."""
    if link.is_symlink() or link.is_file():
        link.unlink()
    link.symlink_to(source.resolve())
