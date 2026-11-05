"""Stage 1: coregister the Sentinel-1 SLC stack with ISCE2 topsStack.

stackSentinel.py is run with the SLC workflow (``-W slc``): it only coregisters
the bursts (geometry + ESD + fine coregistration) and merges them into
full-resolution SLCs. No interferograms are formed here; dolphin does that.

Only bursts that overlap the (buffered) AOI box are processed, because
stackSentinel.py selects bursts by the ``-b`` bounding box.

Outputs used by later stages (inside ``<work_dir>/isce``):
    merged/SLC/<date>/<date>.slc.full(.vrt)    coregistered SLCs, radar coordinates
    merged/geom_reference/*.rdr.full(.vrt)     lat, lon, hgt, los, shadowMask
    reference/IW*.xml                          reference metadata (MintPy attributes)
    baselines/<ref>_<sec>/<ref>_<sec>.txt      perpendicular baselines
"""

import glob
import logging
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .aoi import get_aoi_bbox
from .shell import run_command
from .dem_preparation import prepare_isce_dem
from .swath_selection import select_swaths

logger = logging.getLogger(__name__)

# Geometry layers that later stages need from merged/geom_reference.
GEOMETRY_LAYERS = ("lat", "lon", "hgt", "los", "shadowMask", "incLocal")

# File name suffixes tried when looking for an ISCE raster, full resolution first.
# topsStack writes "<name>.full" when multilooking is on, and "<name>" otherwise.
ISCE_RASTER_SUFFIXES = (".full.vrt", ".full", ".vrt", "")

RUN_FILE_PATTERN = re.compile(r"run_(\d+)_")


class IsceStackError(RuntimeError):
    """Raised when ISCE2 topsStack is not installed or produced no usable output."""


def find_topsstack_dir(configured_dir=None):
    """Locate the directory holding stackSentinel.py and the other topsStack scripts.

    Search order: config value, an existing stackSentinel.py on PATH, and the
    location used by the conda-forge isce2 package (``$CONDA_PREFIX/share/isce2``).

    Raises
    ------
    IsceStackError
        If no directory with stackSentinel.py is found.
    """
    candidates = []
    if configured_dir is not None:
        candidates.append(Path(configured_dir))
    on_path = shutil.which("stackSentinel.py")
    if on_path:
        candidates.append(Path(on_path).parent)
    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix:
        candidates.append(Path(conda_prefix) / "share" / "isce2" / "topsStack")
    try:
        import isce  # noqa: F401  (only used to find the installation)

        isce_package_dir = Path(isce.__file__).parent
        candidates.append(isce_package_dir / "contrib" / "stack" / "topsStack")
    except ImportError:
        pass

    for candidate in candidates:
        if (candidate / "stackSentinel.py").is_file():
            return candidate.resolve()
    raise IsceStackError(
        "Cannot find the ISCE2 topsStack scripts (stackSentinel.py). Searched: "
        + ", ".join(str(c) for c in candidates)
        + "\nSet 'isce.topsstack_dir' in the config, e.g. to "
        "<isce2-source>/contrib/stack/topsStack."
    )


def find_isce_applications_dir():
    """Return the ISCE2 ``applications`` directory (fixImageXml.py, looks.py, ...)."""
    import isce

    return Path(isce.__file__).parent / "applications"


def build_isce_environment(topsstack_dir, omp_threads):
    """Return an environment dict with topsStack and ISCE2 applications on PATH.

    The topsStack scripts import each other (e.g. ``from Stack import ...``),
    so both the script directory and its parent must be importable.
    """
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join(
        [str(topsstack_dir), str(find_isce_applications_dir()), environment.get("PATH", "")]
    )
    python_path = [str(topsstack_dir), str(topsstack_dir.parent)]
    if environment.get("PYTHONPATH"):
        python_path.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(python_path)
    for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        environment[variable] = str(omp_threads)
    return environment


def build_stack_sentinel_command(config, bbox, swaths, dem_file):
    """Build the stackSentinel.py argument list for the SLC-only workflow.

    Parameters
    ----------
    config : PipelineConfig
    bbox : list[float]
        Buffered AOI [S, N, W, E]; stackSentinel.py keeps bursts overlapping it.
    swaths : list[int]
        Swaths to process (see swath_selection.py).
    dem_file : Path
        ISCE DEM with ellipsoid heights (see dem_preparation.py).

    Multilooking is switched off (``-z 1 -r 1``) because dolphin works on
    full-resolution SLCs and decides the output resolution with its strides.
    """
    south, north, west, east = bbox
    aux_dir = config.paths.aux_dir or (config.isce_dir / "aux")
    isce = config.isce
    command = [
        "stackSentinel.py",
        "-s", str(config.paths.slc_dir),
        "-o", str(config.paths.orbit_dir),
        "-a", str(aux_dir),
        "-d", str(dem_file),
        "-w", str(config.isce_dir),
        "-b", f"{south:.6f} {north:.6f} {west:.6f} {east:.6f}",
        "-W", "slc",
        "-C", isce.coregistration,
        "-p", isce.polarization,
        "-z", "1",
        "-r", "1",
        "-e", str(isce.esd_coherence_threshold),
        "-O", str(isce.num_overlap_connections),
        "--num_proc", str(isce.num_proc),
        "--num_proc4topo", str(isce.num_proc4topo),
    ]
    if isce.reference_date:
        command += ["-m", isce.reference_date]
    command += ["-n", " ".join(str(swath) for swath in swaths)]
    return command


def list_run_files(isce_dir):
    """Return the topsStack run files sorted by their step number."""
    run_files = [Path(p) for p in glob.glob(str(isce_dir / "run_files" / "run_*"))]
    run_files = [p for p in run_files if RUN_FILE_PATTERN.match(p.name) and p.suffix == ""]
    return sorted(run_files, key=lambda p: int(RUN_FILE_PATTERN.match(p.name).group(1)))


def read_run_file_commands(run_file):
    """Return the commands of one topsStack run file, ready to run in the foreground.

    topsStack ends each line with ``&`` (and may add ``wait`` lines) so that
    the file can be run as a shell script. Run through ``bash -c`` that would
    send the command to the background and report success immediately, so the
    next step would start before this one finished. The trailing ``&`` is
    therefore removed and ``wait`` lines are skipped; parallelism is handled by
    :func:`execute_run_file` instead.
    """
    commands = []
    for line in run_file.read_text().splitlines():
        command = line.strip()
        if not command or command.startswith("#") or command == "wait":
            continue
        if command.endswith("&"):
            command = command[:-1].rstrip()
        commands.append(command)
    return commands


def execute_run_file(run_file, log_dir, environment, num_parallel):
    """Execute all commands of one topsStack run file, ``num_parallel`` at a time.

    Every command gets its own log file so parallel output is not mixed.

    Raises
    ------
    CommandFailedError
        If any command of the run file fails (after all started commands end).
    """
    commands = read_run_file_commands(run_file)
    logger.info("ISCE2 %s: %d command(s)", run_file.name, len(commands))

    def run_one(indexed_command):
        index, command = indexed_command
        log_file = log_dir / f"{run_file.name}_{index:03d}.log"
        run_command(command, log_file, cwd=run_file.parent.parent, env=environment)
        logger.info("ISCE2 %s: command %d/%d finished (log: %s)",
                    run_file.name, index + 1, len(commands), log_file.name)

    with ThreadPoolExecutor(max_workers=max(1, num_parallel)) as pool:
        # list() re-raises the first exception from the worker threads
        list(pool.map(run_one, enumerate(commands)))


def run_isce_stack(config):
    """Run stackSentinel.py and all generated run files, skipping finished steps.

    A step is marked finished with ``<isce_dir>/run_status/<run file>.done``, so
    an interrupted run can simply be started again.
    """
    isce_dir = config.isce_dir
    isce_dir.mkdir(parents=True, exist_ok=True)
    (config.paths.aux_dir or (isce_dir / "aux")).mkdir(parents=True, exist_ok=True)
    log_dir = config.log_dir / "isce"
    status_dir = isce_dir / "run_status"
    status_dir.mkdir(exist_ok=True)

    topsstack_dir = find_topsstack_dir(config.isce.topsstack_dir)
    environment = build_isce_environment(topsstack_dir, config.isce.omp_threads)
    logger.info("Using ISCE2 topsStack from %s", topsstack_dir)

    if not list_run_files(isce_dir):
        bbox = get_aoi_bbox(config.aoi, with_buffer=True)
        selection = select_swaths(config, bbox)
        dem_file = prepare_isce_dem(config, selection.dem_bbox)
        run_command(
            build_stack_sentinel_command(config, bbox, selection.swaths, dem_file),
            log_dir / "stackSentinel.log",
            cwd=isce_dir,
            env=environment,
            echo=True,
        )
    run_files = list_run_files(isce_dir)
    if not run_files:
        raise IsceStackError(f"stackSentinel.py created no run files in {isce_dir}")

    for run_file in run_files:
        done_marker = status_dir / f"{run_file.name}.done"
        if done_marker.exists():
            logger.info("ISCE2 %s already done, skipping", run_file.name)
            continue
        execute_run_file(run_file, log_dir, environment, config.isce.num_proc)
        done_marker.touch()


def find_isce_raster(base_path):
    """Return the GDAL-readable file for an ISCE raster, preferring full resolution.

    Parameters
    ----------
    base_path : Path
        Raster path without suffix, e.g. ``merged/geom_reference/lat.rdr``.

    Returns
    -------
    Path or None
        The first existing file of ``ISCE_RASTER_SUFFIXES``, or None.
    """
    for suffix in ISCE_RASTER_SUFFIXES:
        candidate = Path(str(base_path) + suffix)
        if candidate.is_file():
            return candidate
    return None


def find_merged_slcs(isce_dir):
    """Return {date: path} for all merged, coregistered SLCs of the stack.

    Raises
    ------
    IsceStackError
        If no merged SLCs exist (topsStack did not finish the merge step).
    """
    slcs = {}
    for date_dir in sorted((isce_dir / "merged" / "SLC").glob("[0-9]" * 8)):
        date = date_dir.name
        slc_file = find_isce_raster(date_dir / f"{date}.slc")
        if slc_file is not None:
            slcs[date] = slc_file
    if not slcs:
        raise IsceStackError(f"No merged SLCs found in {isce_dir / 'merged' / 'SLC'}")
    return slcs


def find_geometry_files(isce_dir):
    """Return {layer: path} for the full-resolution reference geometry rasters.

    Raises
    ------
    IsceStackError
        If latitude or longitude are missing; they are needed for the crop.
    """
    geometry_dir = isce_dir / "merged" / "geom_reference"
    geometry = {}
    for layer in GEOMETRY_LAYERS:
        raster = find_isce_raster(geometry_dir / f"{layer}.rdr")
        if raster is not None:
            geometry[layer] = raster
    for required in ("lat", "lon", "hgt", "los"):
        if required not in geometry:
            raise IsceStackError(f"Geometry layer '{required}' missing in {geometry_dir}")
    return geometry


def find_reference_xml(isce_dir):
    """Return the reference metadata XML of the first processed swath (IW1/2/3).

    The merged products start at the first processed swath, so its starting
    range and timing describe column 0 of the merged grid.

    Raises
    ------
    IsceStackError
        If no reference/IW*.xml exists.
    """
    xml_files = sorted((isce_dir / "reference").glob("IW[1-3].xml"))
    if not xml_files:
        raise IsceStackError(f"No reference/IW*.xml found in {isce_dir}")
    return xml_files[0]
