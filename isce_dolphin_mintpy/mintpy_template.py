"""MintPy template options: read them from the config, check them, write the template.

The ``mintpy`` section of the pipeline YAML works like a custom MintPy template:
every key that starts with ``mintpy.`` uses the original name from MintPy's
``smallbaselineApp.cfg`` and is written to ``<work_dir>/mintpy/isce_dolphin_mintpy.cfg``
unchanged. MintPy then merges that file into its full ``smallbaselineApp.cfg``
in the same folder, and every option that is not given keeps MintPy's default.

Example (YAML)::

    mintpy:
      dolphin_temporal_coherence_threshold: 0.6  # pipeline option
      mintpy.compute.cluster: local              # MintPy option
      mintpy.reference.lalo: 48.1977, 16.3254
"""

import importlib.util
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

MINTPY_OPTION_PREFIX = "mintpy."

# Options the pipeline always sets: the MintPy inputs are written in ISCE2 format
# by mintpy_inputs.py, and load_data is never run.
FIXED_TEMPLATE_OPTIONS = {"mintpy.load.processor": "isce"}

# Option groups that only MintPy's load_data step reads. load_data is never run
# (mintpy_inputs.py writes the inputs), so they are passed on but have no effect;
# a warning says so.
OPTIONS_WITHOUT_EFFECT = {
    "mintpy.load.": "the MintPy inputs are written by the pipeline (mintpy_inputs.py).",
    "mintpy.subset.": "set the area with 'aoi.bbox' instead; the crop happens before "
                      "ISCE2 and dolphin.",
    "mintpy.multilook.": "set the output pixel size with 'dolphin.strides_x/strides_y' instead.",
}

# Option names of older configs -> their original MintPy name, for a helpful error.
LEGACY_OPTION_NAMES = {
    "reference_lalo": "mintpy.reference.lalo",
    "min_temporal_coherence": "mintpy.networkInversion.minTempCoh",
    "unwrap_error_correction": "mintpy.unwrapError.method",
    "troposphere": "mintpy.troposphericDelay.method",
    "topographic_residual": "mintpy.topographicResidual",
    "deramp": "mintpy.deramp",
    "solid_earth_tides": "mintpy.solidEarthTides",
    "geocode": "mintpy.geocode",
    "geocode_lalo_step": "mintpy.geocode.laloStep",
    "extra_template_options": "the mintpy.* option names directly in the 'mintpy' section",
}

# MintPy files that list every template option (with "auto" values).
MINTPY_DEFAULT_TEMPLATES = ("smallbaselineApp.cfg", "smallbaselineApp_auto.cfg")
OPTION_LINE_PATTERN = re.compile(r"^\s*(mintpy\.[A-Za-z0-9_.]+)\s*=")

# Values MintPy reads as "off" / "use the default".
MINTPY_FALSE_VALUES = ("no", "false")
MINTPY_AUTO_VALUES = ("auto", "")

# MintPy's default for mintpy.networkInversion.minTempCoh ("auto").
MINTPY_DEFAULT_MIN_TEMPORAL_COHERENCE = 0.7


def split_mintpy_section(values):
    """Split the YAML ``mintpy`` section into pipeline options and MintPy options.

    Parameters
    ----------
    values : dict | None
        The raw ``mintpy`` mapping from the YAML file.

    Returns
    -------
    tuple[dict, dict]
        ``(pipeline_options, template_options)``. The template options keep the
        order of the YAML file, and their values are converted to MintPy text
        (see ``format_template_value``).

    Raises
    ------
    ValueError
        If an option name of an older config version is used.
    """
    pipeline_options, template_options = {}, {}
    for key, value in (values or {}).items():
        key = str(key)
        if key.startswith(MINTPY_OPTION_PREFIX):
            template_options[key] = format_template_value(value)
        elif key in LEGACY_OPTION_NAMES:
            raise ValueError(
                f"Option '{key}' was replaced by the original MintPy name. "
                f"Use {LEGACY_OPTION_NAMES[key]} instead."
            )
        else:
            pipeline_options[key] = value
    return pipeline_options, template_options


def format_template_value(value):
    """Turn a YAML value into the text MintPy expects in a template file.

    YAML reads unquoted ``yes``/``no`` as booleans, so booleans become
    ``yes``/``no`` again. Lists become comma separated (``[48.2, 16.3]`` ->
    ``48.2, 16.3``) and ``null`` becomes ``auto``.
    """
    if value is None:
        return "auto"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def find_mintpy_defaults_dir():
    """Return MintPy's ``defaults`` folder, or ``None`` if MintPy is not installed.

    ``find_spec`` only locates the package, it does not import MintPy.
    """
    spec = importlib.util.find_spec("mintpy")
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(list(spec.submodule_search_locations)[0]) / "defaults"


def read_known_option_names(defaults_dir):
    """Return all ``mintpy.*`` option names defined in MintPy's default templates."""
    names = set()
    for template_name in MINTPY_DEFAULT_TEMPLATES:
        template_file = Path(defaults_dir) / template_name
        if not template_file.is_file():
            continue
        for line in template_file.read_text().splitlines():
            match = OPTION_LINE_PATTERN.match(line)
            if match:
                names.add(match.group(1))
    return names


def validate_template_options(template_options, defaults_dir=None):
    """Check that every MintPy option exists; warn about options without effect here.

    Every option of MintPy's ``smallbaselineApp.cfg`` is accepted. MintPy
    silently ignores unknown options, so a typo would otherwise go unnoticed
    until the results look wrong. Options that only ``load_data`` reads (see
    ``OPTIONS_WITHOUT_EFFECT``) are passed on with a warning.

    Parameters
    ----------
    template_options : dict
        Original MintPy option names -> values.
    defaults_dir : Path, optional
        MintPy's ``defaults`` folder. Default: found from the installed MintPy.
        If MintPy is not installed, only the pipeline-specific checks run.

    Raises
    ------
    ValueError
        If options are unknown to MintPy.
    """
    for key in template_options:
        for prefix, reason in OPTIONS_WITHOUT_EFFECT.items():
            if key.startswith(prefix):
                logger.warning("MintPy option '%s' is passed on but has no effect in this "
                               "pipeline (load_data is not run): %s", key, reason)

    defaults_dir = defaults_dir or find_mintpy_defaults_dir()
    if defaults_dir is None:
        logger.warning("MintPy is not installed: MintPy option names are not checked.")
        return
    known_names = read_known_option_names(defaults_dir)
    unknown = [key for key in template_options if key not in known_names]
    if unknown:
        raise ValueError(
            f"Unknown MintPy option(s): {unknown}. Check the spelling against "
            f"{Path(defaults_dir) / MINTPY_DEFAULT_TEMPLATES[0]}."
        )


def build_template_options(mintpy_config):
    """Return all options (name -> text value) for the MintPy template of this run.

    The fixed pipeline options come first, then the user's options in the
    order of the config file.
    """
    options = dict(FIXED_TEMPLATE_OPTIONS)
    options.update(mintpy_config.template_options)
    return options


def write_template(mintpy_config, template_file):
    """Write the MintPy template file used for all ``smallbaselineApp.py`` calls."""
    lines = [f"{key:45s} = {value}" for key, value in build_template_options(mintpy_config).items()]
    Path(template_file).write_text("\n".join(lines) + "\n")


def is_plotting_enabled(template_options):
    """Return True unless ``mintpy.plot`` is switched off (MintPy's default is yes)."""
    value = template_options.get("mintpy.plot", "auto").strip().lower()
    return value not in MINTPY_FALSE_VALUES


def min_temporal_coherence(template_options):
    """Return ``mintpy.networkInversion.minTempCoh`` as a number (MintPy default if auto).

    Raises
    ------
    ValueError
        If the value is neither ``auto`` nor a number.
    """
    value = template_options.get("mintpy.networkInversion.minTempCoh", "auto").strip()
    if value.lower() in MINTPY_AUTO_VALUES:
        return MINTPY_DEFAULT_MIN_TEMPORAL_COHERENCE
    try:
        return float(value)
    except ValueError as error:
        raise ValueError(f"mintpy.networkInversion.minTempCoh must be a number, got '{value}'.") from error


def requested_reference_date(template_options):
    """Return ``mintpy.reference.date`` if it is an explicit date (YYYYMMDD), else ``None``.

    Other values (``auto``, ``no``, a file name) are left to MintPy.
    """
    value = template_options.get("mintpy.reference.date", "auto").strip()
    return value if value.isdigit() else None
