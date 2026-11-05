"""Stage 4a: write MintPy input files from dolphin outputs and ISCE2 metadata.

Why this module exists instead of MintPy's ``load_data``:
dolphin writes its radar-coordinate results as GeoTIFFs. When MintPy reads
them with ``load_data`` it derives X_FIRST/Y_FIRST from the GeoTIFF transform,
decides the data are geocoded and then fails looking for ``geometryGeo.h5``
(dolphin discussion #677, MintPy forum thread "ISCE2 --> dolphin --> Mintpy").
Here the MintPy HDF5 files are written directly, with metadata taken from the
ISCE2 reference XML, so the stack is unambiguously in radar coordinates and
MintPy can geocode it later with the ISCE2 latitude/longitude lookup.

Grid convention (checked against dolphin's ``compute_out_shape``): with strides
(sy, sx) dolphin output pixel (i, j) is centered on input pixel
(i*sy + sy//2, j*sx + sx//2) of the cropped stack. The geometry is sampled at
exactly those pixels.

Sign convention: dolphin forms interferograms as ``ref * conj(sec)`` like ISCE2,
so unwrapped phase is used unchanged. dolphin and MintPy both convert phase to
displacement with ``-wavelength / (4 pi)`` (positive = toward the satellite).

Files written (``<work_dir>/mintpy``):
    inputs/geometryRadar.h5        latitude, longitude, height, angles, slant range
    inputs/ifgramStack.h5          unwrapPhase, coherence, connectComponent, bperp
    dolphinTemporalCoherence.h5    dolphin phase-linking temporal coherence
    timeseries.h5                  only for ``mintpy.inversion: dolphin``
"""

import logging
import re
from pathlib import Path

import numpy as np
from osgeo import gdal

from .isce_stack import find_reference_xml
from .radar_crop import cropped_geometry_file, load_crop_window

logger = logging.getLogger(__name__)
gdal.UseExceptions()

DATE_PAIR_PATTERN = re.compile(r"(\d{8})_(\d{8})")
DOLPHIN_TEMPORAL_COHERENCE_FILE = "dolphinTemporalCoherence.h5"


class MintpyInputError(RuntimeError):
    """Raised when dolphin outputs are missing or do not match the expected grid."""


# --------------------------------------------------------------------------- #
# Grid helpers
# --------------------------------------------------------------------------- #
def output_shape(window, strides_x, strides_y):
    """Return the (length, width) of the dolphin output grid for a crop window."""
    return window.length // strides_y, window.width // strides_x


def sample_to_output_grid(array, strides_x, strides_y, out_length, out_width):
    """Pick the full-resolution pixels that sit at the dolphin output pixel centers.

    Parameters
    ----------
    array : np.ndarray
        2D array on the cropped full-resolution grid.
    strides_x, strides_y : int
        dolphin strides in range (columns) and azimuth (rows).
    out_length, out_width : int
        dolphin output shape.
    """
    sampled = array[strides_y // 2::strides_y, strides_x // 2::strides_x]
    return sampled[:out_length, :out_width]


def read_raster(path, band=1):
    """Read one band of a GDAL raster into a numpy array."""
    dataset = gdal.Open(str(path))
    return dataset.GetRasterBand(band).ReadAsArray()


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #
def build_radar_metadata(reference_xml, window, strides_x, strides_y, latitude, longitude,
                         incidence_angle):
    """Build MintPy attributes for the cropped, strided radar grid.

    Starts from MintPy's own ISCE2/topsStack metadata reader and then adapts
    pixel sizes, looks, starting range and corner coordinates to the dolphin grid.

    Parameters
    ----------
    reference_xml : Path
        ISCE2 ``reference/IW*.xml`` of the first processed swath.
    window : CropWindow
        Crop window inside the ISCE2 merged grid.
    strides_x, strides_y : int
        dolphin strides.
    latitude, longitude, incidence_angle : np.ndarray
        Geometry already sampled on the dolphin output grid.

    Returns
    -------
    dict
        MintPy metadata with all values as strings.
    """
    from mintpy.utils import readfile
    from mintpy.utils.isce_utils import extract_tops_metadata

    meta, _ = extract_tops_metadata(str(reference_xml))

    full_range_pixel_size = float(meta["rangePixelSize"])
    full_azimuth_pixel_size = float(meta["azimuthPixelSize"])

    # STARTING_RANGE is the slant range of the center of output column 0.
    first_full_res_column = window.x_offset + strides_x // 2
    meta["startingRange"] = float(meta["startingRange"]) + first_full_res_column * full_range_pixel_size
    meta["rangePixelSize"] = full_range_pixel_size * strides_x
    meta["azimuthPixelSize"] = full_azimuth_pixel_size * strides_y
    meta["RLOOKS"] = strides_x
    meta["ALOOKS"] = strides_y

    # Number of independent looks, used by MintPy for coherence-based weighting
    # (same formula as mintpy.utils.isce_utils.extract_multilook_number).
    range_oversampling = float(meta["rangeResolution"]) / full_range_pixel_size
    azimuth_oversampling = float(meta["azimuthResolution"]) / full_azimuth_pixel_size
    meta["NCORRLOOKS"] = strides_x * strides_y / (range_oversampling * azimuth_oversampling)

    out_length, out_width = latitude.shape
    meta["LENGTH"] = out_length
    meta["WIDTH"] = out_width
    meta["LAT_REF1"], meta["LAT_REF2"] = latitude[0, 0], latitude[0, -1]
    meta["LAT_REF3"], meta["LAT_REF4"] = latitude[-1, 0], latitude[-1, -1]
    meta["LON_REF1"], meta["LON_REF2"] = longitude[0, 0], longitude[0, -1]
    meta["LON_REF3"], meta["LON_REF4"] = longitude[-1, 0], longitude[-1, -1]
    meta["CENTER_INCIDENCE_ANGLE"] = incidence_angle[out_length // 2, out_width // 2]

    meta["PROCESSOR"] = "isce"
    meta["ANTENNA_SIDE"] = -1
    # Traceability: where this grid sits in the ISCE2 merged full-resolution grid.
    meta["ISCE_CROP_X0"] = window.x_offset
    meta["ISCE_CROP_Y0"] = window.y_offset
    meta["DOLPHIN_STRIDES_X"] = strides_x
    meta["DOLPHIN_STRIDES_Y"] = strides_y

    meta = {key: str(value) for key, value in meta.items()}
    return readfile.standardize_metadata(meta)


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
def read_strided_geometry(crop_dir, strides_x, strides_y, out_length, out_width):
    """Read the cropped ISCE2 geometry and sample it onto the dolphin output grid.

    Returns
    -------
    dict[str, np.ndarray]
        MintPy geometry dataset names -> arrays.
    """
    def sample(layer, band=1):
        array = read_raster(cropped_geometry_file(crop_dir, layer), band)
        return sample_to_output_grid(array, strides_x, strides_y, out_length, out_width)

    geometry = {
        "latitude": sample("lat").astype(np.float32),
        "longitude": sample("lon").astype(np.float32),
        "height": sample("hgt").astype(np.float32),
        # ISCE2 los.rdr: band 1 = incidence angle, band 2 = azimuth angle of the
        # ground-to-satellite LOS, from north, anti-clockwise; same as MintPy.
        "incidenceAngle": sample("los", band=1).astype(np.float32),
        "azimuthAngle": sample("los", band=2).astype(np.float32),
    }
    if cropped_geometry_file(crop_dir, "shadowMask").is_file():
        geometry["shadowMask"] = sample("shadowMask") != 0
    return geometry


def write_geometry_file(out_file, geometry, metadata):
    """Write ``geometryRadar.h5`` including a slant range distance layer."""
    from mintpy.utils import writefile

    out_length, out_width = geometry["latitude"].shape
    starting_range = float(metadata["STARTING_RANGE"])
    range_pixel_size = float(metadata["RANGE_PIXEL_SIZE"])
    slant_range_row = starting_range + range_pixel_size * np.arange(out_width, dtype=np.float32)
    datasets = dict(geometry)
    datasets["slantRangeDistance"] = np.tile(slant_range_row, (out_length, 1)).astype(np.float32)

    geometry_metadata = dict(metadata)
    geometry_metadata["FILE_TYPE"] = "geometry"
    writefile.write(datasets, out_file=str(out_file), metadata=geometry_metadata)


# --------------------------------------------------------------------------- #
# dolphin outputs
# --------------------------------------------------------------------------- #
def parse_date_pair(path):
    """Return (date1, date2) from a file name containing ``YYYYMMDD_YYYYMMDD``.

    Raises
    ------
    MintpyInputError
        If the file name contains no date pair.
    """
    match = DATE_PAIR_PATTERN.search(Path(path).name)
    if match is None:
        raise MintpyInputError(f"No YYYYMMDD_YYYYMMDD date pair in file name: {path}")
    return match.group(1), match.group(2)


def find_dolphin_interferograms(dolphin_dir):
    """Collect matching unwrapped phase, coherence and connected component files.

    Returns
    -------
    list[dict]
        One dict per interferogram with keys ``dates``, ``unw``, ``cor``, ``conncomp``,
        sorted by date pair.

    Raises
    ------
    MintpyInputError
        If dolphin produced no unwrapped interferograms or a companion file is missing.
    """
    unwrapped_files = sorted((dolphin_dir / "unwrapped").glob("*.unw.tif"))
    if not unwrapped_files:
        raise MintpyInputError(f"No unwrapped interferograms in {dolphin_dir / 'unwrapped'}")

    interferograms = []
    for unw_file in unwrapped_files:
        dates = parse_date_pair(unw_file)
        pair_name = f"{dates[0]}_{dates[1]}"
        cor_file = dolphin_dir / "interferograms" / f"{pair_name}.int.cor.tif"
        conncomp_file = unw_file.with_name(f"{pair_name}.unw.conncomp.tif")
        for required in (cor_file, conncomp_file):
            if not required.is_file():
                raise MintpyInputError(f"Missing dolphin output for {pair_name}: {required}")
        interferograms.append(
            {"dates": dates, "unw": unw_file, "cor": cor_file, "conncomp": conncomp_file}
        )
    return sorted(interferograms, key=lambda item: item["dates"])


def find_dolphin_temporal_coherence(dolphin_dir):
    """Return dolphin's average temporal coherence raster (stitched or per ministack).

    Raises
    ------
    MintpyInputError
        If no temporal coherence raster exists.
    """
    for pattern in ("interferograms/temporal_coherence_average*.tif",
                    "linked_phase/temporal_coherence_average*.tif",
                    "interferograms/temporal_coherence*.tif",
                    "linked_phase/temporal_coherence*.tif"):
        matches = sorted(dolphin_dir.glob(pattern))
        if matches:
            return matches[-1]
    raise MintpyInputError(f"No temporal coherence raster found in {dolphin_dir}")


def _check_shape(path, expected_shape):
    """Raise if a raster does not have the expected (length, width)."""
    dataset = gdal.Open(str(path))
    shape = (dataset.RasterYSize, dataset.RasterXSize)
    if shape != tuple(expected_shape):
        raise MintpyInputError(
            f"{path} has shape {shape}, expected {tuple(expected_shape)} from the crop "
            "window and strides. Were crop and dolphin run with the same strides?"
        )


def read_perpendicular_baselines(isce_dir):
    """Return {date: bperp} relative to the ISCE2 reference date (meters).

    The top/bottom values of the topsStack baseline files are averaged, like
    MintPy does for ISCE2 stacks.
    """
    from mintpy.utils.isce_utils import read_baseline_timeseries

    baseline_dict = read_baseline_timeseries(str(isce_dir / "baselines"), processor="tops")
    if baseline_dict is None:
        raise MintpyInputError(f"No baseline files found in {isce_dir / 'baselines'}")
    return {date: float(np.mean(values)) for date, values in baseline_dict.items()}


def write_ifgram_stack(out_file, interferograms, baselines, metadata, out_shape):
    """Write ``ifgramStack.h5`` one interferogram at a time (low memory use).

    Raises
    ------
    MintpyInputError
        If a perpendicular baseline is missing for a date of the network.
    """
    from mintpy.utils import writefile

    num_ifgs = len(interferograms)
    date_pairs = np.array([item["dates"] for item in interferograms], dtype=np.bytes_)
    bperp = []
    for item in interferograms:
        date1, date2 = item["dates"]
        if date1 not in baselines or date2 not in baselines:
            raise MintpyInputError(f"No perpendicular baseline for {date1} or {date2}")
        bperp.append(baselines[date2] - baselines[date1])

    stack_shape = (num_ifgs, *out_shape)
    dataset_layout = {
        # fixed-length strings (S8 for YYYYMMDD): h5py cannot store unsized np.bytes_
        "date": [date_pairs.dtype, (num_ifgs, 2), date_pairs],
        "dropIfgram": [np.bool_, (num_ifgs,), np.ones(num_ifgs, dtype=np.bool_)],
        "bperp": [np.float32, (num_ifgs,), np.array(bperp, dtype=np.float32)],
        "unwrapPhase": [np.float32, stack_shape, None],
        "coherence": [np.float32, stack_shape, None],
        "connectComponent": [np.int16, stack_shape, None],
    }
    stack_metadata = dict(metadata)
    stack_metadata["FILE_TYPE"] = "ifgramStack"
    stack_metadata["UNIT"] = "radian"
    writefile.layout_hdf5(str(out_file), dataset_layout, metadata=stack_metadata)

    for index, item in enumerate(interferograms):
        for key in ("unw", "cor", "conncomp"):
            _check_shape(item[key], out_shape)
        block = [index, index + 1, 0, out_shape[0], 0, out_shape[1]]
        # dolphin marks invalid pixels with NaN; MintPy expects 0 there.
        unwrapped = np.nan_to_num(read_raster(item["unw"]).astype(np.float32), nan=0.0)
        coherence = np.nan_to_num(read_raster(item["cor"]).astype(np.float32), nan=0.0)
        conncomp = np.clip(read_raster(item["conncomp"]), 0, np.iinfo(np.int16).max)
        writefile.write_hdf5_block(str(out_file), unwrapped[None], "unwrapPhase", block, print_msg=False)
        writefile.write_hdf5_block(str(out_file), coherence[None], "coherence", block, print_msg=False)
        writefile.write_hdf5_block(str(out_file), conncomp.astype(np.int16)[None],
                                   "connectComponent", block, print_msg=False)
    logger.info("Wrote %d interferograms to %s", num_ifgs, out_file)


def write_temporal_coherence(out_file, coherence_file, metadata, out_shape):
    """Write dolphin's temporal coherence as a MintPy ``temporalCoherence`` file."""
    from mintpy.utils import writefile

    _check_shape(coherence_file, out_shape)
    coherence = read_raster(coherence_file).astype(np.float32)
    coherence = np.nan_to_num(coherence, nan=0.0)
    coherence_metadata = dict(metadata)
    coherence_metadata["FILE_TYPE"] = "temporalCoherence"
    coherence_metadata["UNIT"] = "1"
    writefile.write({"temporalCoherence": coherence}, out_file=str(out_file),
                    metadata=coherence_metadata)


def write_dolphin_timeseries(out_file, dolphin_dir, baselines, metadata, out_shape):
    """Convert dolphin's single-reference displacement rasters to ``timeseries.h5``.

    dolphin writes ``timeseries/<ref>_<date>.tif`` in meters relative to its
    first date; the reference date itself is added as an all-zero epoch.

    Raises
    ------
    MintpyInputError
        If no dolphin time series rasters exist or they use several reference dates.
    """
    from mintpy.objects import timeseries

    series_files = sorted(
        path for path in (dolphin_dir / "timeseries").glob("*.tif")
        if DATE_PAIR_PATTERN.fullmatch(path.name.split(".")[0])
    )
    if not series_files:
        raise MintpyInputError(
            f"No dolphin time series in {dolphin_dir / 'timeseries'}. "
            "Was dolphin run with mintpy.inversion: dolphin?"
        )
    reference_dates = {parse_date_pair(path)[0] for path in series_files}
    if len(reference_dates) != 1:
        raise MintpyInputError(f"dolphin time series has several reference dates: {reference_dates}")
    reference_date = reference_dates.pop()

    dates = [reference_date] + [parse_date_pair(path)[1] for path in series_files]
    displacement = np.zeros((len(dates), *out_shape), dtype=np.float32)
    for index, path in enumerate(series_files, start=1):
        _check_shape(path, out_shape)
        displacement[index] = np.nan_to_num(read_raster(path), nan=0.0)

    bperp = np.array([baselines[d] - baselines[reference_date] for d in dates], dtype=np.float32)
    series_metadata = dict(metadata)
    series_metadata["FILE_TYPE"] = "timeseries"
    series_metadata["UNIT"] = "m"
    series_metadata["REF_DATE"] = reference_date
    timeseries(str(out_file)).write2hdf5(displacement, dates=np.array(dates, dtype=np.bytes_),
                                          bperp=bperp, metadata=series_metadata)


def prepare_mintpy_inputs(config, include_dolphin_timeseries):
    """Write all MintPy input files for the dolphin results of this run.

    Parameters
    ----------
    config : PipelineConfig
    include_dolphin_timeseries : bool
        Also convert dolphin's own time series to ``timeseries.h5``.

    Returns
    -------
    dict
        The metadata written to the files.
    """
    window = load_crop_window(config.crop_dir)
    strides_x, strides_y = config.dolphin.strides_x, config.dolphin.strides_y
    out_shape = output_shape(window, strides_x, strides_y)

    geometry = read_strided_geometry(config.crop_dir, strides_x, strides_y, *out_shape)
    metadata = build_radar_metadata(
        find_reference_xml(config.isce_dir), window, strides_x, strides_y,
        geometry["latitude"], geometry["longitude"], geometry["incidenceAngle"],
    )

    inputs_dir = config.mintpy_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    write_geometry_file(inputs_dir / "geometryRadar.h5", geometry, metadata)

    baselines = read_perpendicular_baselines(config.isce_dir)
    interferograms = find_dolphin_interferograms(config.dolphin_dir)
    write_ifgram_stack(inputs_dir / "ifgramStack.h5", interferograms, baselines, metadata, out_shape)

    write_temporal_coherence(
        config.mintpy_dir / DOLPHIN_TEMPORAL_COHERENCE_FILE,
        find_dolphin_temporal_coherence(config.dolphin_dir), metadata, out_shape,
    )
    if include_dolphin_timeseries:
        write_dolphin_timeseries(config.mintpy_dir / "timeseries.h5", config.dolphin_dir,
                                 baselines, metadata, out_shape)
    return metadata
