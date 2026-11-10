"""dolphin options: read them from the config, check them, build dolphin's config.

The ``dolphin`` section of the pipeline YAML has the same structure and names as
dolphin's own config file (``dolphin config --print-empty`` shows all of them),
for example::

    dolphin:
      output_options:
        strides: {x: 4, y: 1}
      unwrap_options:
        unwrap_method: whirlwind
        whirlwind_options:
          num_threads: 8

These options are merged over the pipeline defaults (``PIPELINE_DEFAULT_OPTIONS``)
and passed to dolphin's ``DisplacementWorkflow`` unchanged. Options that the
pipeline must control itself (input files, work directory, mask, wavelength)
are rejected, see ``PIPELINE_OWNED_OPTIONS``.
"""

import copy
import importlib.util
import logging

logger = logging.getLogger(__name__)

# Defaults of this pipeline. They differ from dolphin's own defaults where the
# pipeline needs it (e.g. strides 4 x 1 for ~15 m x 14 m pixels, no overviews).
PIPELINE_DEFAULT_OPTIONS = {
    "output_options": {
        "strides": {"x": 4, "y": 1},
        "add_overviews": False,
    },
    "phase_linking": {
        "ministack_size": 15,
        "half_window": {"x": 11, "y": 5},
        "shp_method": "glrt",
    },
    "interferogram_network": {"max_bandwidth": 3},
    "unwrap_options": {
        "run_unwrap": True,
        "unwrap_method": "snaphu",
        "n_parallel_jobs": 2,
        "snaphu_options": {"ntiles": [1, 1]},
    },
    "worker_settings": {
        "threads_per_worker": 4,
        "block_shape": [512, 512],
    },
}

# Options the pipeline sets itself, as paths in dolphin's config -> reason.
PIPELINE_OWNED_OPTIONS = {
    ("cslc_file_list",): "the cropped SLCs of the 'crop' step are used.",
    ("work_directory",): "dolphin always works in <work_dir>/dolphin.",
    ("mask_file",): "set the water mask in the 'water_mask' section.",
    ("keep_paths_relative",): "the pipeline passes absolute paths.",
    ("require_cslc_files",): "internal dolphin option.",
    ("input_options", "wavelength"): "it is read from the ISCE2 reference XML.",
    ("input_options", "cslc_date_fmt"): "the cropped SLCs are named <YYYYMMDD>.slc.vrt.",
}

# MintPy needs dolphin's unwrapped interferograms.
REQUIRED_VALUES = {("unwrap_options", "run_unwrap"): True}

# Time series options that follow the top-level 'inversion' unless set explicitly.
INVERSION_OPTIONS = (("timeseries_options", "run_inversion"),
                     ("timeseries_options", "run_velocity"))

# Python module each dolphin unwrapper needs, and the package that provides it.
UNWRAPPER_MODULES = {"snaphu": "snaphu", "whirlwind": "whirlwind", "spurt": "spurt",
                     "icu": "isce3", "phass": "isce3"}
UNWRAPPER_PACKAGES = {
    "snaphu": "conda package snaphu",
    # The PyPI package called "whirlwind" is unrelated; whirlwind-insar provides the module.
    "whirlwind": "pip package whirlwind-insar, see environment.yml",
    "spurt": "pip package spurt",
    "icu": "conda package isce3",
    "phass": "conda package isce3",
}

# Option names of older configs -> their path in dolphin's config, for a helpful error.
LEGACY_OPTION_NAMES = {
    "strides_x": "output_options.strides.x",
    "strides_y": "output_options.strides.y",
    "half_window_x": "phase_linking.half_window.x",
    "half_window_y": "phase_linking.half_window.y",
    "ministack_size": "phase_linking.ministack_size",
    "shp_method": "phase_linking.shp_method",
    "max_bandwidth": "interferogram_network.max_bandwidth",
    "unwrap_method": "unwrap_options.unwrap_method",
    "snaphu_ntiles": "unwrap_options.snaphu_options.ntiles",
    "n_parallel_unwrap": "unwrap_options.n_parallel_jobs",
    "threads_per_worker": "worker_settings.threads_per_worker",
    "block_shape": "worker_settings.block_shape",
}


def deep_merge(base, override):
    """Return a copy of ``base`` with ``override`` merged in (nested dicts merged key by key)."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def get_nested(options, path):
    """Return the value at ``path`` (tuple of keys) in nested dicts, or ``None``."""
    for key in path:
        if not isinstance(options, dict) or key not in options:
            return None
        options = options[key]
    return options


def set_nested(options, path, value):
    """Set ``value`` at ``path`` (tuple of keys), creating intermediate dicts."""
    for key in path[:-1]:
        options = options.setdefault(key, {})
    options[path[-1]] = value


def merge_with_defaults(user_options):
    """Check the user's dolphin options and merge them over the pipeline defaults.

    Parameters
    ----------
    user_options : dict | None
        The raw ``dolphin`` mapping from the YAML file.

    Returns
    -------
    dict
        Complete dolphin options (without the pipeline-owned ones).

    Raises
    ------
    ValueError
        If an old option name, a pipeline-owned option or a forbidden value is used.
    """
    user_options = user_options or {}
    for key in user_options:
        if key in LEGACY_OPTION_NAMES:
            raise ValueError(f"Option '{key}' was replaced by dolphin's own structure. "
                             f"Use {LEGACY_OPTION_NAMES[key]} instead.")
    for path, reason in PIPELINE_OWNED_OPTIONS.items():
        if get_nested(user_options, path) is not None:
            raise ValueError(f"Option {'.'.join(path)} is set by the pipeline: {reason}")
    for path, required in REQUIRED_VALUES.items():
        value = get_nested(user_options, path)
        if value is not None and value != required:
            raise ValueError(f"Option {'.'.join(path)} must be {required}: "
                             "MintPy needs the unwrapped interferograms.")
    return deep_merge(PIPELINE_DEFAULT_OPTIONS, user_options)


def apply_inversion_mode(options, dolphin_inverts):
    """Return options with dolphin's time series inversion set for the ``inversion`` mode.

    ``timeseries_options.run_inversion``/``run_velocity`` default to True only in
    ``inversion: dolphin`` mode. In ``mintpy`` mode they may be set to
    True explicitly, e.g. to compare both inversions.

    Raises
    ------
    ValueError
        If dolphin must invert but an option switches it off.
    """
    options = copy.deepcopy(options)
    for path in INVERSION_OPTIONS:
        value = get_nested(options, path)
        if value is None:
            set_nested(options, path, dolphin_inverts)
        elif dolphin_inverts and not value:
            raise ValueError(f"Option dolphin.{'.'.join(path)} must not be false when "
                             "'inversion' is 'dolphin'.")
    return options


def validate_with_dolphin(options):
    """Check every option with dolphin's own config model (names, types, ranges).

    The top level of dolphin's config accepts unknown keys silently, so those
    are checked here explicitly; all nested sections reject unknown keys
    themselves. If dolphin is not installed, the check is skipped with a warning.

    Raises
    ------
    ValueError
        With dolphin's error message if an option is invalid.
    """
    try:
        from dolphin.workflows.config import DisplacementWorkflow
    except ImportError:
        logger.warning("dolphin is not installed: dolphin options are not checked.")
        return
    from pydantic import ValidationError

    unknown = sorted(set(options) - set(DisplacementWorkflow.model_fields))
    if unknown:
        raise ValueError(f"Unknown dolphin option(s): {unknown}. "
                         f"Valid top-level options: {sorted(DisplacementWorkflow.model_fields)}")
    try:
        DisplacementWorkflow.model_validate({**options, "require_cslc_files": False})
    except ValidationError as error:
        raise ValueError(f"Invalid dolphin option(s):\n{error}") from error


def check_unwrapper_installed(options):
    """Fail early if the Python module of the chosen unwrapper is missing.

    dolphin imports the unwrapper only when unwrapping starts, i.e. after phase
    linking, which can take hours.

    Raises
    ------
    ValueError
        If the module for ``unwrap_options.unwrap_method`` cannot be found.
    """
    method = str(get_nested(options, ("unwrap_options", "unwrap_method")))
    module = UNWRAPPER_MODULES.get(method)
    if module is None or importlib.util.find_spec(module) is not None:
        return
    raise ValueError(f"unwrap_options.unwrap_method is '{method}', but the Python module "
                     f"'{module}' is not installed ({UNWRAPPER_PACKAGES[method]}).")


REFERENCE_HEADER = """\
# Every dolphin option with its description, as accepted in the 'dolphin' section
# of the pipeline config. Values shown are this pipeline's defaults (dolphin {version}).
# Copy only the options you want to change into your config; nesting stays the same.
# Not listed: options the pipeline sets itself ({owned}).
# timeseries_options.run_inversion/run_velocity follow the top-level 'inversion' unless set.
#
# Generated file. Regenerate after a dolphin update with:
#   python -c "from isce_dolphin_mintpy.dolphin_options import write_options_reference; \\
#              write_options_reference('config/dolphin_options_reference.yaml')"
"""


def write_options_reference(output_file):
    """Write a commented YAML file with every dolphin option and the pipeline defaults.

    Uses dolphin's own comment generator (descriptions, types, choices), drops
    the pipeline-owned options and nests everything under ``dolphin:`` so parts
    can be copied straight into a pipeline config.
    """
    import io

    import dolphin
    from dolphin.workflows.config import DisplacementWorkflow
    from dolphin.workflows.config._yaml_model import _add_comments
    from ruamel.yaml import YAML

    model = DisplacementWorkflow.model_validate(
        {**PIPELINE_DEFAULT_OPTIONS, "require_cslc_files": False})
    yaml_obj = model._to_yaml_obj(by_alias=True)
    for path in PIPELINE_OWNED_OPTIONS:
        parent = get_nested(yaml_obj, path[:-1]) if len(path) > 1 else yaml_obj
        parent.pop(path[-1], None)
    for path in INVERSION_OPTIONS:
        get_nested(yaml_obj, path[:-1]).pop(path[-1], None)
    _add_comments(yaml_obj, model.model_json_schema(by_alias=True), indent_per_level=2)

    yaml_writer = YAML()
    yaml_writer.indent(offset=2, mapping=2, sequence=4)
    buffer = io.StringIO()
    yaml_writer.dump(yaml_obj, buffer)
    nested = "".join(f"  {line}" if line.strip() else line
                     for line in buffer.getvalue().splitlines(keepends=True))
    owned = ", ".join(".".join(path) for path in PIPELINE_OWNED_OPTIONS)
    header = REFERENCE_HEADER.format(version=dolphin.__version__, owned=owned)
    with open(output_file, "w") as stream:
        stream.write(header + "\ndolphin:\n" + nested)


def build_workflow_options(options, slc_files, work_directory, mask_file, wavelength):
    """Return the complete dolphin config: user/default options plus pipeline-owned ones."""
    workflow_options = copy.deepcopy(options)
    workflow_options.update({
        "cslc_file_list": [str(path) for path in slc_files],
        "work_directory": work_directory,
        "mask_file": mask_file,
    })
    input_options = workflow_options.setdefault("input_options", {})
    input_options.update({"wavelength": wavelength, "cslc_date_fmt": "%Y%m%d"})
    return workflow_options
