"""Stage 3: phase linking, interferograms and unwrapping with dolphin.

dolphin reads the cropped ISCE2 SLC VRTs (radar coordinates) directly through
GDAL. The date of each SLC is parsed from its file name (``<YYYYMMDD>.slc.vrt``).

The radar wavelength is taken from the ISCE2 reference XML and handed to
dolphin, so its time series / velocity outputs are in meters (positive = motion
toward the satellite, the same convention as MintPy).
"""

import logging
import xml.etree.ElementTree as ElementTree

from .config import INVERSION_BY_DOLPHIN
from .isce_stack import find_reference_xml
from .radar_crop import cropped_slc_files
from .water_mask import valid_mask_file
from .shell import run_command

logger = logging.getLogger(__name__)

DOLPHIN_CONFIG_FILE = "dolphin_config.yaml"


class DolphinStepError(RuntimeError):
    """Raised when dolphin inputs are missing or outputs were not produced."""


def read_radar_wavelength(reference_xml):
    """Read the radar wavelength in meters from an ISCE2 reference IW*.xml file.

    Raises
    ------
    DolphinStepError
        If the XML has no ``radarwavelength`` property.
    """
    tree = ElementTree.parse(reference_xml)
    for prop in tree.iter("property"):
        if prop.get("name", "").lower() == "radarwavelength":
            value = prop.find("value")
            if value is not None and value.text:
                return float(value.text)
    raise DolphinStepError(f"No radarwavelength found in {reference_xml}")


def dolphin_mask_file(config):
    """Return the water mask for dolphin (1 = keep, 0 = water), or None.

    dolphin 0.42 applies ``mask_file`` in unwrapping (and to its own time
    series), on its output grid, which is where ``water_mask.py`` writes it.

    Raises
    ------
    DolphinStepError
        If a water mask is configured but the crop step has not created it yet.
    """
    if config.water_mask.file is None or not config.water_mask.use_in_dolphin:
        return None
    mask_file = valid_mask_file(config)
    if not mask_file.is_file():
        raise DolphinStepError(f"Water mask {mask_file} is missing. Run the 'crop' step first.")
    return mask_file


def build_dolphin_config(config, slc_files, wavelength):
    """Create a validated dolphin ``DisplacementWorkflow`` for the cropped stack.

    Parameters
    ----------
    config : PipelineConfig
    slc_files : list[Path]
        Cropped SLC VRTs, one per date.
    wavelength : float
        Radar wavelength in meters.

    Returns
    -------
    dolphin.workflows.config.DisplacementWorkflow
    """
    from dolphin.workflows.config import DisplacementWorkflow

    options = config.dolphin
    run_dolphin_inversion = config.mintpy.inversion == INVERSION_BY_DOLPHIN
    return DisplacementWorkflow(
        cslc_file_list=[str(path) for path in slc_files],
        mask_file=dolphin_mask_file(config),
        work_directory=config.dolphin_dir,
        input_options={"wavelength": wavelength, "cslc_date_fmt": "%Y%m%d"},
        output_options={
            "strides": {"x": options.strides_x, "y": options.strides_y},
            "add_overviews": False,
        },
        phase_linking={
            "ministack_size": options.ministack_size,
            "half_window": {"x": options.half_window_x, "y": options.half_window_y},
            "shp_method": options.shp_method,
        },
        interferogram_network={"max_bandwidth": options.max_bandwidth},
        unwrap_options={
            "run_unwrap": True,
            "unwrap_method": options.unwrap_method,
            "n_parallel_jobs": options.n_parallel_unwrap,
            "snaphu_options": {"ntiles": options.snaphu_ntiles},
        },
        timeseries_options={
            "run_inversion": run_dolphin_inversion,
            "run_velocity": run_dolphin_inversion,
        },
        worker_settings={
            "threads_per_worker": options.threads_per_worker,
            "block_shape": options.block_shape,
        },
    )


def run_dolphin(config):
    """Write the dolphin config for the cropped stack and run ``dolphin run``.

    Raises
    ------
    DolphinStepError
        If fewer than 2 cropped SLCs exist.
    CommandFailedError
        If dolphin fails (details in ``logs/dolphin.log``).
    """
    slc_files = cropped_slc_files(config.crop_dir)
    if len(slc_files) < 2:
        raise DolphinStepError(
            f"Need at least 2 cropped SLCs in {config.crop_dir / 'slc'}, found {len(slc_files)}. "
            "Run the 'crop' step first."
        )
    wavelength = read_radar_wavelength(find_reference_xml(config.isce_dir))

    config.dolphin_dir.mkdir(parents=True, exist_ok=True)
    dolphin_config = build_dolphin_config(config, slc_files, wavelength)
    config_file = config.dolphin_dir / DOLPHIN_CONFIG_FILE
    dolphin_config.to_yaml(config_file)
    logger.info("dolphin config written to %s (%d SLCs)", config_file, len(slc_files))

    run_command(["dolphin", "run", str(config_file)], config.log_dir / "dolphin.log",
                cwd=config.dolphin_dir, echo=True)
