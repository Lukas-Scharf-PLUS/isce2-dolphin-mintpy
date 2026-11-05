"""Pipeline configuration: load the YAML config file into typed dataclasses.

One YAML file describes a full run. Every section maps to one dataclass below;
see ``config/example.yaml`` for a documented example of every option.
"""

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

import yaml

from .dem_preparation import VALID_VERTICAL_DATUMS

# Network modes that MintPy can invert (see mintpy_step.py).
INVERSION_BY_MINTPY = "mintpy"
INVERSION_BY_DOLPHIN = "dolphin"
VALID_INVERSION_MODES = (INVERSION_BY_MINTPY, INVERSION_BY_DOLPHIN)


class ConfigError(ValueError):
    """Raised when the configuration file is incomplete or inconsistent."""


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
    """Options for dolphin phase linking, interferograms and unwrapping."""

    strides_x: int = 4
    strides_y: int = 1
    half_window_x: int = 11
    half_window_y: int = 5
    ministack_size: int = 15
    shp_method: str = "glrt"
    max_bandwidth: int = 3
    unwrap_method: str = "snaphu"
    snaphu_ntiles: list = field(default_factory=lambda: [1, 1])
    threads_per_worker: int = 4
    n_parallel_unwrap: int = 2
    block_shape: list = field(default_factory=lambda: [512, 512])


@dataclass
class MintpyConfig:
    """Options for the MintPy stage.

    ``inversion`` selects who turns interferograms into a time series:
    ``mintpy`` (network inversion in MintPy) or ``dolphin`` (dolphin's own
    inversion, MintPy then only applies corrections).
    """

    inversion: str = INVERSION_BY_MINTPY
    reference_lalo: Optional[list] = None
    min_temporal_coherence: float = 0.7
    dolphin_temporal_coherence_threshold: Optional[float] = 0.6
    unwrap_error_correction: str = "no"
    troposphere: str = "pyaps"
    topographic_residual: bool = True
    deramp: str = "no"
    solid_earth_tides: bool = False
    geocode: bool = True
    geocode_lalo_step: Optional[list] = None
    extra_template_options: dict = field(default_factory=dict)


@dataclass
class PipelineConfig:
    """Complete pipeline configuration."""

    paths: PathsConfig
    aoi: AoiConfig
    water_mask: WaterMaskConfig
    isce: IsceConfig
    dolphin: DolphinConfig
    mintpy: MintpyConfig

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

    if config.mintpy.inversion not in VALID_INVERSION_MODES:
        raise ConfigError(
            f"mintpy.inversion must be one of {VALID_INVERSION_MODES}, "
            f"got '{config.mintpy.inversion}'."
        )
    if config.dolphin.strides_x < 1 or config.dolphin.strides_y < 1:
        raise ConfigError("dolphin strides must be >= 1.")


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
        raw = yaml.safe_load(stream) or {}

    unknown_sections = set(raw) - {"paths", "aoi", "water_mask", "isce", "dolphin", "mintpy"}
    if unknown_sections:
        raise ConfigError(f"Unknown config section(s): {sorted(unknown_sections)}")
    if "paths" not in raw:
        raise ConfigError("Config section 'paths' is required.")

    config = PipelineConfig(
        paths=_build_section(PathsConfig, raw.get("paths"), "paths"),
        aoi=_build_section(AoiConfig, raw.get("aoi"), "aoi"),
        water_mask=_build_section(WaterMaskConfig, raw.get("water_mask"), "water_mask"),
        isce=_build_section(IsceConfig, raw.get("isce"), "isce"),
        dolphin=_build_section(DolphinConfig, raw.get("dolphin"), "dolphin"),
        mintpy=_build_section(MintpyConfig, raw.get("mintpy"), "mintpy"),
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
