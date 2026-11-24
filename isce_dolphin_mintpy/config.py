"""Pipeline configuration: load the YAML config file into typed dataclasses.

One YAML file describes a full run. Every section maps to one dataclass below;
see ``config/example.yaml`` for a documented example of every option.
"""

import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

import yaml

from .dem_preparation import VALID_VERTICAL_DATUMS
from .dolphin_options import (
    apply_inversion_mode,
    check_unwrapper_installed,
    merge_with_defaults,
    validate_with_dolphin,
)
from .mintpy_template import (
    min_temporal_coherence,
    split_mintpy_section,
    validate_template_options,
)

# Network modes that MintPy can invert (see mintpy_step.py).
INVERSION_BY_MINTPY = "mintpy"
INVERSION_BY_DOLPHIN = "dolphin"
VALID_INVERSION_MODES = (INVERSION_BY_MINTPY, INVERSION_BY_DOLPHIN)


class ConfigError(ValueError):
    """Raised when the configuration file is incomplete or inconsistent."""


class PipelineYamlLoader(yaml.SafeLoader):
    """Safe YAML loader that recognises numbers like YAML 1.2, not YAML 1.1.

    PyYAML follows YAML 1.1, which reads ``20080520_20090817`` as the integer
    2008052020090817 (underscores are digit separators) and ``1:5`` as 65
    (base-60 notation). Both are normal MintPy values (``excludeDate12``,
    ``excludeIfgIndex``), so only plain numbers like ``16``, ``0.7`` or
    ``1e-5`` become numbers here; everything else stays text.
    """


_INT_TAG = "tag:yaml.org,2002:int"
_FLOAT_TAG = "tag:yaml.org,2002:float"
PipelineYamlLoader.yaml_implicit_resolvers = {
    first_char: [(tag, regexp) for tag, regexp in resolvers if tag not in (_INT_TAG, _FLOAT_TAG)]
    for first_char, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
PipelineYamlLoader.add_implicit_resolver(
    _INT_TAG, re.compile(r"^[-+]?[0-9]+$"), list("-+0123456789"))
PipelineYamlLoader.add_implicit_resolver(
    _FLOAT_TAG,
    re.compile(r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
               r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"),
    list("-+0123456789."),
)


@dataclass
class PathsConfig:
    """Input data and working directory locations."""

    slc_dir: Path
    orbit_dir: Path
    dem_file: Path
    # Vertical reference of the DEM heights: egm2008, egm96 or ellipsoid.
    # Required, because DEM files rarely state it in their metadata.
    dem_vertical_datum: Optional[str] = None
    work_dir: Optional[Path] = None
    aux_dir: Optional[Path] = None


@dataclass
class AoiConfig:
    """Area of interest as lat/lon bounding box or vector file.

    ``bbox`` is ``[south, north, west, east]`` in degrees (same order as
    stackSentinel.py ``-b``). If ``file`` is given, its bounding box is used instead.
    ``buffer_m`` extends the AOI on all sides before the radar crop.
    """

    bbox: Optional[list] = None
    file: Optional[Path] = None
    buffer_m: float = 500.0


@dataclass
class WaterMaskConfig:
    """Optional water mask (geocoded raster, e.g. EPSG:4326 GeoTIFF).

    The mask is converted to dolphin's output grid as a *water fraction* per
    pixel. A pixel counts as water only if more than ``max_water_fraction`` of
    it is water, so mixed shore pixels (quay walls, bridge piers) are kept;
    bad pixels can still be removed later with the coherence masks.
    """

    file: Optional[Path] = None
    water_value: int = 0
    max_water_fraction: float = 0.8
    use_in_dolphin: bool = True


@dataclass
class IsceConfig:
    """Options passed to ISCE2 topsStack (stackSentinel.py)."""

    reference_date: Optional[str] = None
    swaths: Optional[list] = None
    polarization: str = "vv"
    coregistration: str = "NESD"
    esd_coherence_threshold: float = 0.85
    num_overlap_connections: int = 3
    num_proc: int = 4
    num_proc4topo: int = 2
    omp_threads: int = 2
    topsstack_dir: Optional[Path] = None


@dataclass
class DolphinConfig:
    """Options for dolphin phase linking, interferograms and unwrapping.

    ``options`` holds dolphin's own config structure (same names and nesting as
    dolphin's YAML), already merged over the pipeline defaults; see
    ``dolphin_options.py``. The output grid (strides) is also needed by the
    crop and MintPy steps, so it is available as properties.
    """

    options: dict = field(default_factory=dict)

    @property
    def strides_x(self):
        """Output pixel spacing in range, in full-resolution pixels."""
        return int(self.options["output_options"]["strides"]["x"])

    @property
    def strides_y(self):
        """Output pixel spacing in azimuth, in full-resolution pixels."""
        return int(self.options["output_options"]["strides"]["y"])


@dataclass
class MintpyConfig:
    """Options for the MintPy stage.

    Two kinds of options share the YAML ``mintpy`` section:

    - Pipeline option ``dolphin_temporal_coherence_threshold``: adds dolphin's
      phase-linking quality to MintPy's pixel mask (``null`` = off).
    - MintPy template options: every key that starts with ``mintpy.``, using the
      original names from MintPy's ``smallbaselineApp.cfg``. They are kept in
      ``template_options`` (in file order) and written to the MintPy template
      unchanged, just like a custom MintPy template. Options that are not given
      keep MintPy's own default (``auto``).

    Who inverts the time series is the top-level ``inversion`` option, because
    it changes both the dolphin and the MintPy step.
    """

    dolphin_temporal_coherence_threshold: Optional[float] = 0.6
    template_options: dict = field(default_factory=dict)


@dataclass
class PipelineConfig:
    """Complete pipeline configuration.

    ``inversion`` (top-level YAML option) selects who turns the interferograms
    into a time series: ``mintpy`` (MintPy network inversion) or ``dolphin``
    (dolphin's own inversion; MintPy then only applies corrections).
    """

    paths: PathsConfig
    aoi: AoiConfig
    water_mask: WaterMaskConfig
    isce: IsceConfig
    dolphin: DolphinConfig
    mintpy: MintpyConfig
    inversion: str = INVERSION_BY_MINTPY

    @property
    def dolphin_inverts(self):
        """True if dolphin's time series is used (``inversion: dolphin``)."""
        return self.inversion == INVERSION_BY_DOLPHIN

    @property
    def isce_dir(self):
        """Directory for ISCE2 topsStack processing."""
        return self.paths.work_dir / "isce"

    @property
    def crop_dir(self):
        """Directory for the AOI-cropped radar stack (VRTs + geometry)."""
        return self.paths.work_dir / "crop"

    @property
    def dolphin_dir(self):
        """Directory for dolphin processing."""
        return self.paths.work_dir / "dolphin"

    @property
    def mintpy_dir(self):
        """Directory for MintPy processing."""
        return self.paths.work_dir / "mintpy"

    @property
    def water_dir(self):
        """Directory for the water fraction and mask on dolphin's output grid."""
        return self.crop_dir / "water"

    @property
    def log_dir(self):
        """Directory for logs of all external commands."""
        return self.paths.work_dir / "logs"


def _build_section(section_class, values, section_name):
    """Create one config dataclass from a YAML mapping, rejecting unknown keys.

    Raises
    ------
    ConfigError
        If ``values`` contains keys the dataclass does not define.
    """
    values = values or {}
    known_keys = {f.name for f in fields(section_class)}
    unknown_keys = set(values) - known_keys
    if unknown_keys:
        raise ConfigError(
            f"Unknown option(s) in section '{section_name}': {sorted(unknown_keys)}. "
            f"Valid options: {sorted(known_keys)}"
        )
    try:
        return section_class(**values)
    except TypeError as error:
        raise ConfigError(f"Section '{section_name}': {error}") from error


def _build_dolphin_section(values):
    """Create the dolphin config: the user's dolphin options merged over the pipeline defaults.

    Whether dolphin also inverts the time series depends on the ``mintpy``
    section, so the full check with dolphin's own model happens in ``_validate``.

    Raises
    ------
    ConfigError
        If an old option name or a pipeline-owned option is used.
    """
    if values is not None and not isinstance(values, dict):
        raise ConfigError("Section 'dolphin' must be a mapping of dolphin options.")
    try:
        return DolphinConfig(options=merge_with_defaults(values))
    except ValueError as error:
        raise ConfigError(f"Section 'dolphin': {error}") from error


def _build_mintpy_section(values):
    """Create the MintPy config from pipeline options and original MintPy template options.

    Raises
    ------
    ConfigError
        If a pipeline option is unknown or a MintPy option is invalid
        (see ``mintpy_template.validate_template_options``).
    """
    try:
        pipeline_options, template_options = split_mintpy_section(values)
        validate_template_options(template_options)
    except ValueError as error:
        raise ConfigError(f"Section 'mintpy': {error}") from error
    if "template_options" in pipeline_options:
        raise ConfigError("Section 'mintpy': write the mintpy.* options directly into the "
                          "section, not under 'template_options'.")
    if "inversion" in pipeline_options:
        raise ConfigError("Option 'mintpy.inversion' moved to the top level of the config: "
                          "write 'inversion: mintpy' or 'inversion: dolphin' after the "
                          "'isce' section.")
    mintpy_config = _build_section(MintpyConfig, pipeline_options, "mintpy")
    mintpy_config.template_options = template_options
    return mintpy_config


def _convert_paths(paths_config):
    """Turn all path strings of the paths section into absolute ``Path`` objects."""
    for field_info in fields(paths_config):
        value = getattr(paths_config, field_info.name)
        if value is not None and field_info.name != "dem_vertical_datum":
            setattr(paths_config, field_info.name, Path(value).expanduser().resolve())
    if paths_config.dem_vertical_datum is not None:
        paths_config.dem_vertical_datum = str(paths_config.dem_vertical_datum).lower()


def _validate(config):
    """Check the loaded configuration for missing inputs and invalid choices.

    Raises
    ------
    ConfigError
        On the first problem found.
    """
    for name in ("slc_dir", "orbit_dir"):
        directory = getattr(config.paths, name)
        if not directory.is_dir():
            raise ConfigError(f"paths.{name} does not exist: {directory}")
    if config.paths.work_dir is None:
        raise ConfigError("paths.work_dir is required.")
    if not config.paths.dem_file.is_file():
        raise ConfigError(f"paths.dem_file does not exist: {config.paths.dem_file}")
    if config.paths.dem_vertical_datum not in VALID_VERTICAL_DATUMS:
        raise ConfigError(
            f"paths.dem_vertical_datum must be one of {VALID_VERTICAL_DATUMS}, got "
            f"'{config.paths.dem_vertical_datum}'. DEM files rarely state their vertical "
            "reference, so it has to be given explicitly (Copernicus DEM: egm2008, "
            "SRTM: egm96, already converted: ellipsoid)."
        )

    if config.aoi.bbox is None and config.aoi.file is None:
        raise ConfigError("aoi: set either 'bbox' [S, N, W, E] or 'file'.")
    if config.aoi.bbox is not None:
        if len(config.aoi.bbox) != 4:
            raise ConfigError("aoi.bbox must be [south, north, west, east].")
        south, north, west, east = config.aoi.bbox
        if not (south < north and west < east):
            raise ConfigError(f"aoi.bbox is not ordered [S, N, W, E]: {config.aoi.bbox}")

    if config.water_mask.file is not None and not config.water_mask.file.is_file():
        raise ConfigError(f"water_mask.file does not exist: {config.water_mask.file}")
    if not 0.0 < config.water_mask.max_water_fraction <= 1.0:
        raise ConfigError("water_mask.max_water_fraction must be in (0, 1].")

    if config.inversion not in VALID_INVERSION_MODES:
        raise ConfigError(
            f"inversion must be one of {VALID_INVERSION_MODES}, got '{config.inversion}'."
        )
    try:
        # Read again in the MintPy step; checked here so a typo fails before ISCE2 runs.
        min_temporal_coherence(config.mintpy.template_options)
    except ValueError as error:
        raise ConfigError(str(error)) from error
    try:
        # The dolphin options with the time series settings of the chosen mode are
        # checked with dolphin's own model, so a typo fails before ISCE2 runs.
        config.dolphin.options = apply_inversion_mode(config.dolphin.options,
                                                      config.dolphin_inverts)
        validate_with_dolphin(config.dolphin.options)
        check_unwrapper_installed(config.dolphin.options)
    except ValueError as error:
        raise ConfigError(f"Section 'dolphin': {error}") from error


def load_config(config_file):
    """Load and validate a pipeline configuration YAML file.

    Parameters
    ----------
    config_file : str | Path
        Path to the YAML configuration.

    Returns
    -------
    PipelineConfig
        Validated configuration with absolute paths.

    Raises
    ------
    ConfigError
        If sections are missing, unknown options are given or inputs do not exist.
    """
    with open(config_file) as stream:
        raw = yaml.load(stream, Loader=PipelineYamlLoader) or {}

    unknown_sections = set(raw) - {"paths", "aoi", "water_mask", "isce", "inversion",
                                   "dolphin", "mintpy"}
    if unknown_sections:
        raise ConfigError(f"Unknown config section(s): {sorted(unknown_sections)}")
    if "paths" not in raw:
        raise ConfigError("Config section 'paths' is required.")

    config = PipelineConfig(
        paths=_build_section(PathsConfig, raw.get("paths"), "paths"),
        aoi=_build_section(AoiConfig, raw.get("aoi"), "aoi"),
        water_mask=_build_section(WaterMaskConfig, raw.get("water_mask"), "water_mask"),
        isce=_build_section(IsceConfig, raw.get("isce"), "isce"),
        dolphin=_build_dolphin_section(raw.get("dolphin")),
        mintpy=_build_mintpy_section(raw.get("mintpy")),
        inversion=str(raw.get("inversion", INVERSION_BY_MINTPY)).lower(),
    )
    _convert_paths(config.paths)
    if config.aoi.file is not None:
        config.aoi.file = Path(config.aoi.file).expanduser().resolve()
    if config.water_mask.file is not None:
        config.water_mask.file = Path(config.water_mask.file).expanduser().resolve()
    if config.isce.reference_date is not None:
        config.isce.reference_date = str(config.isce.reference_date)
    if config.isce.topsstack_dir is not None:
        config.isce.topsstack_dir = Path(config.isce.topsstack_dir).expanduser()
    _validate(config)
    return config
