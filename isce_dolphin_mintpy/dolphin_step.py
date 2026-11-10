"""Stage 3: phase linking, interferograms and unwrapping with dolphin.

dolphin reads the cropped ISCE2 SLC VRTs (radar coordinates) directly through
GDAL. The date of each SLC is parsed from its file name (``<YYYYMMDD>.slc.vrt``).

The radar wavelength is taken from the ISCE2 reference XML and handed to
dolphin, so its time series / velocity outputs are in meters (positive = motion
toward the satellite, the same convention as MintPy).

Every other dolphin option (phase linking, network, unwrapper incl. whirlwind,
...) comes from the config's ``dolphin`` section in dolphin's own structure.
The complete config is saved as ``dolphin/dolphin_config.yaml``.
"""

import logging
import xml.etree.ElementTree as ElementTree

from .dolphin_options import apply_inversion_mode, build_workflow_options
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

    All dolphin options come from the config's ``dolphin`` section (merged over
    the pipeline defaults, see ``dolphin_options.py``); the input files, work
    directory, water mask and wavelength are added here.

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

    options = apply_inversion_mode(config.dolphin.options, config.dolphin_inverts)
    return DisplacementWorkflow.model_validate(build_workflow_options(
        options, slc_files, config.dolphin_dir, dolphin_mask_file(config), wavelength))


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
