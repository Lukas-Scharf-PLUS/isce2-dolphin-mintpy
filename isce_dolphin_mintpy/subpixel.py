"""Optional stage: sub-pixel position of persistent scatterers (PS) in radar coordinates.

Why
---
Every output pixel is placed at its pixel center, but a point scatterer can sit
anywhere inside the resolution cell (Sentinel-1 IW: ~2.3 m slant range x ~14 m
azimuth pixels). A point scatterer is imaged as a band-limited peak that spreads
over neighbouring pixels; the *shape* of that peak over the pixels tells where
its maximum, the scatterer, really is. This step estimates that offset for PS.
It does not change any phase or displacement; it only adds position layers.

How
---
1. Use the same full-resolution PS pixel that dolphin uses for each output pixel:
   dolphin's PS mask (amplitude dispersion) and, with strides > 1, the PS with
   the highest mean amplitude in the output window (dolphin's ``use_max_ps``).
2. Find the amplitude peak in the 3 x 3 neighbourhood of that pixel in dolphin's
   mean amplitude: a pixel on the flank of a strong scatterer can be a PS too.
3. For every date, fit a parabola to the *log* amplitude of the peak pixel and
   its two neighbours, separately in range and azimuth (a Gaussian through three
   samples). For Sentinel-1-like impulse responses the bias of this estimator
   is below ~0.07 pixel, compared with 0.29 pixel standard error of using the
   pixel center. Dates on which the peak is not the local maximum are skipped.
4. Report the mean offset over dates and its standard deviation. A small
   standard deviation means one dominant scatterer; a large one means several
   interfering scatterers or noise, so the offset should not be used.

Only amplitudes are used, so the TOPS azimuth phase ramp (which would require
deramping before complex interpolation) and burst seams do not matter.

Outputs (``<work_dir>/subpixel``, GeoTIFFs on dolphin's output grid, NaN where
there is no PS or no estimate; float32, latitude/longitude float64):

    range_offset.tif          offset in full-resolution range samples
    azimuth_offset.tif        offset in full-resolution azimuth lines
    range_offset_std.tif      standard deviation of the per-date estimates
    azimuth_offset_std.tif    (needs at least 2 valid dates)
    valid_dates.tif           number of dates used for the estimate
    on_peak.tif               1 = the PS pixel is the amplitude peak itself,
                              0 = it is on the flank of a peak in a neighbouring
                              pixel. A strong scatterer's main lobe covers ~1.4
                              azimuth lines, so neighbouring pixels are often PS
                              too and then point to the *same* scatterer; keep
                              only on_peak = 1 to get one point per scatterer.
    latitude.tif, longitude.tif, height.tif
                              ISCE2 geometry interpolated at the sub-pixel position
                              (on the DEM surface; the DEM error is not applied)

Offsets are relative to the full-resolution pixel whose geometry the MintPy
inputs use for that output pixel, (i*sy + sy//2, j*sx + sx//2), so a consumer
adds them to that pixel position. Positive range = farther from the sensor,
positive azimuth = later in azimuth time (increasing row).
"""

import logging
from dataclasses import dataclass

import numpy as np
from osgeo import gdal

from .mintpy_inputs import output_shape
from .radar_crop import cropped_geometry_file, cropped_slc_files, load_crop_window

logger = logging.getLogger(__name__)
gdal.UseExceptions()

SUBPIXEL_DIRECTORY = "subpixel"
PS_DIRECTORY = ("phase_linking", "PS")
PS_MASK_FILE = "ps_pixels.tif"
AMP_MEAN_FILE = "amp_mean.tif"
# Peaks further than this from the PS pixel belong to another scatterer.
PEAK_SEARCH_RADIUS = 1
# Extra full-resolution rows read around each block (peak search + 3-point fit
# + bilinear interpolation at up to 1.5 pixels away).
HALO_ROWS = 3
ROWS_PER_BLOCK = 512
MIN_DATES_FOR_STD = 2
GEOMETRY_LAYERS = {"latitude": "lat", "longitude": "lon", "height": "hgt"}
# float32 resolves only ~4e-6 degrees (~0.4 m) at 48 degrees, too coarse for
# sub-pixel positions, so coordinates are written as float64.
FLOAT64_LAYERS = ("latitude", "longitude")
OUTPUT_LAYERS = ("range_offset", "azimuth_offset", "range_offset_std", "azimuth_offset_std",
                 "valid_dates", "on_peak", *GEOMETRY_LAYERS)


class SubpixelError(RuntimeError):
    """Raised when inputs of the sub-pixel step are missing or inconsistent."""


@dataclass
class PsPoints:
    """PS used for the output grid: one entry per output pixel that has a PS.

    All arrays have one value per point. Rows/columns are full-resolution
    pixels of the cropped stack.
    """

    out_rows: np.ndarray
    out_cols: np.ndarray
    center_rows: np.ndarray   # pixel whose geometry the MintPy inputs use
    center_cols: np.ndarray
    peak_rows: np.ndarray     # amplitude peak near the PS pixel
    peak_cols: np.ndarray
    on_peak: np.ndarray       # True if the PS pixel is the peak itself


def log_parabola_peak(left, center, right):
    """Return the sub-sample peak offset of three amplitudes (Gaussian fit).

    Parameters
    ----------
    left, center, right : np.ndarray
        Amplitudes at sample -1, 0, +1. Same shape.

    Returns
    -------
    np.ndarray
        Offset of the peak from the center sample, in samples, within [-0.5, 0.5].
        NaN where the center is not a strict local maximum or an amplitude is not positive.
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        log_left, log_center, log_right = np.log(left), np.log(center), np.log(right)
        curvature = log_left - 2.0 * log_center + log_right
        offset = 0.5 * (log_left - log_right) / curvature
    is_peak = (center > left) & (center > right) & (left > 0) & (right > 0)
    return np.where(is_peak & np.isfinite(offset), offset, np.nan)


def select_ps_pixels(ps_mask, amp_mean, strides_x, strides_y):
    """Return, per output pixel, the brightest PS of its stride window (like dolphin).

    Parameters
    ----------
    ps_mask : np.ndarray
        Boolean full-resolution PS mask of the cropped stack.
    amp_mean : np.ndarray
        Mean amplitude of the cropped stack (same shape).
    strides_x, strides_y : int
        dolphin strides in range (columns) and azimuth (rows).

    Returns
    -------
    tuple of np.ndarray
        (out_rows, out_cols, ps_rows, ps_cols) for every output pixel with a PS.
    """
    out_length, out_width = ps_mask.shape[0] // strides_y, ps_mask.shape[1] // strides_x
    mask = ps_mask[:out_length * strides_y, :out_width * strides_x]
    brightness = np.where(mask, amp_mean[:mask.shape[0], :mask.shape[1]], -np.inf)
    # (out_length, sy, out_width, sx) -> one flattened window per output pixel
    windows = brightness.reshape(out_length, strides_y, out_width, strides_x).transpose(0, 2, 1, 3)
    windows = windows.reshape(out_length, out_width, strides_y * strides_x)
    has_ps = np.isfinite(windows).any(axis=2)
    brightest = windows.argmax(axis=2)
    out_rows, out_cols = np.nonzero(has_ps)
    window_row, window_col = np.divmod(brightest[out_rows, out_cols], strides_x)
    return out_rows, out_cols, out_rows * strides_y + window_row, out_cols * strides_x + window_col


def find_amplitude_peaks(amp_mean, rows, cols, radius=PEAK_SEARCH_RADIUS):
    """Return the position of the highest mean amplitude within ``radius`` of each pixel.

    Pixels whose search window would leave the raster keep their own position.
    """
    length, width = amp_mean.shape
    peak_rows, peak_cols = rows.copy(), cols.copy()
    best = np.full(rows.shape, -np.inf)
    for d_row in range(-radius, radius + 1):
        for d_col in range(-radius, radius + 1):
            r, c = rows + d_row, cols + d_col
            inside = (r >= 0) & (r < length) & (c >= 0) & (c < width)
            value = np.full(rows.shape, -np.inf)
            value[inside] = amp_mean[r[inside], c[inside]]
            better = value > best
            best[better] = value[better]
            peak_rows[better], peak_cols[better] = r[better], c[better]
    return peak_rows, peak_cols


def build_ps_points(ps_mask, amp_mean, strides_x, strides_y):
    """Select the PS of every output pixel and locate its amplitude peak.

    Peaks on the raster border are dropped, because the 3-point fit needs both neighbours.
    """
    out_rows, out_cols, ps_rows, ps_cols = select_ps_pixels(ps_mask, amp_mean, strides_x, strides_y)
    peak_rows, peak_cols = find_amplitude_peaks(amp_mean, ps_rows, ps_cols)
    length, width = amp_mean.shape
    interior = (peak_rows > 0) & (peak_rows < length - 1) & (peak_cols > 0) & (peak_cols < width - 1)
    return PsPoints(
        out_rows=out_rows[interior], out_cols=out_cols[interior],
        center_rows=out_rows[interior] * strides_y + strides_y // 2,
        center_cols=out_cols[interior] * strides_x + strides_x // 2,
        peak_rows=peak_rows[interior], peak_cols=peak_cols[interior],
        on_peak=(peak_rows == ps_rows)[interior] & (peak_cols == ps_cols)[interior],
    )


class OffsetAccumulator:
    """Running sum, sum of squares and count of per-date offsets for each point."""

    def __init__(self, num_points):
        self.total = np.zeros(num_points)
        self.total_squared = np.zeros(num_points)
        self.count = np.zeros(num_points, dtype=np.int32)

    def add(self, indices, offsets):
        """Add one date's offsets for the points ``indices`` (NaN = no estimate)."""
        valid = np.isfinite(offsets)
        np.add.at(self.total, indices[valid], offsets[valid])
        np.add.at(self.total_squared, indices[valid], offsets[valid] ** 2)
        np.add.at(self.count, indices[valid], 1)

    def mean(self):
        """Mean offset per point (NaN without estimates)."""
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(self.count > 0, self.total / self.count, np.nan)

    def std(self):
        """Sample standard deviation per point (NaN with fewer than 2 estimates)."""
        with np.errstate(invalid="ignore", divide="ignore"):
            variance = (self.total_squared - self.total ** 2 / self.count) / (self.count - 1)
        return np.where(self.count >= MIN_DATES_FOR_STD, np.sqrt(np.clip(variance, 0.0, None)), np.nan)


def read_rows(path, first_row, num_rows):
    """Read rows ``first_row .. first_row + num_rows`` of band 1 of a raster."""
    dataset = gdal.Open(str(path))
    return dataset.GetRasterBand(1).ReadAsArray(0, first_row, dataset.RasterXSize, num_rows)


def estimate_offsets(slc_files, points, length):
    """Estimate per-date peak offsets for all points, reading the SLCs block by block.

    Returns
    -------
    tuple[OffsetAccumulator, OffsetAccumulator]
        Range and azimuth accumulators. Offsets are in pixels from the peak pixel.
    """
    range_offsets = OffsetAccumulator(len(points.peak_rows))
    azimuth_offsets = OffsetAccumulator(len(points.peak_rows))
    for block_start in range(0, length, ROWS_PER_BLOCK):
        block_end = min(block_start + ROWS_PER_BLOCK, length)
        indices = np.flatnonzero((points.peak_rows >= block_start) & (points.peak_rows < block_end))
        if indices.size == 0:
            continue
        read_start = max(block_start - 1, 0)
        read_end = min(block_end + 1, length)
        rows = points.peak_rows[indices] - read_start
        cols = points.peak_cols[indices]
        for slc_file in slc_files:
            amplitude = np.abs(read_rows(slc_file, read_start, read_end - read_start))
            center = amplitude[rows, cols]
            range_offsets.add(indices, log_parabola_peak(amplitude[rows, cols - 1], center,
                                                         amplitude[rows, cols + 1]))
            azimuth_offsets.add(indices, log_parabola_peak(amplitude[rows - 1, cols], center,
                                                           amplitude[rows + 1, cols]))
        logger.info("Sub-pixel: rows %d-%d done (%d PS)", block_start, block_end - 1, indices.size)
    return range_offsets, azimuth_offsets


def bilinear_at(array, rows, cols):
    """Bilinearly interpolate a 2D array at fractional positions (inside the array)."""
    row0 = np.clip(np.floor(rows).astype(int), 0, array.shape[0] - 2)
    col0 = np.clip(np.floor(cols).astype(int), 0, array.shape[1] - 2)
    d_row, d_col = rows - row0, cols - col0
    top = array[row0, col0] * (1 - d_col) + array[row0, col0 + 1] * d_col
    bottom = array[row0 + 1, col0] * (1 - d_col) + array[row0 + 1, col0 + 1] * d_col
    return top * (1 - d_row) + bottom * d_row


def interpolate_geometry(geometry_file, rows, cols, length):
    """Interpolate one cropped geometry layer at fractional full-resolution positions.

    Reads only the rows that are needed, block by block.
    """
    values = np.full(rows.shape, np.nan)
    valid = np.isfinite(rows) & np.isfinite(cols)
    for block_start in range(0, length, ROWS_PER_BLOCK):
        block_end = min(block_start + ROWS_PER_BLOCK, length)
        selected = valid & (rows >= block_start) & (rows < block_end)
        if not selected.any():
            continue
        read_start = max(block_start - HALO_ROWS, 0)
        read_end = min(block_end + HALO_ROWS, length)
        block = read_rows(geometry_file, read_start, read_end - read_start).astype(np.float64)
        values[selected] = bilinear_at(block, rows[selected] - read_start, cols[selected])
    return values


def write_layer(path, values, points, out_shape, metadata, double_precision=False):
    """Write per-point values as a GeoTIFF on the output grid (NaN elsewhere).

    float32 by default; ``double_precision`` writes float64 (needed for coordinates).
    """
    dtype, gdal_type = (np.float64, gdal.GDT_Float64) if double_precision else (np.float32, gdal.GDT_Float32)
    raster = np.full(out_shape, np.nan, dtype=dtype)
    raster[points.out_rows, points.out_cols] = values
    dataset = gdal.GetDriverByName("GTiff").Create(
        str(path), out_shape[1], out_shape[0], 1, gdal_type,
        options=["COMPRESS=DEFLATE", "TILED=YES"])
    band = dataset.GetRasterBand(1)
    band.SetNoDataValue(float("nan"))
    band.WriteArray(raster)
    dataset.SetMetadata(metadata)
    dataset.FlushCache()


def ps_input_files(dolphin_dir):
    """Return the paths of dolphin's full-resolution PS mask and mean amplitude.

    Raises
    ------
    SubpixelError
        If dolphin's PS files are missing (dolphin step not run yet).
    """
    ps_dir = dolphin_dir.joinpath(*PS_DIRECTORY)
    files = ps_dir / PS_MASK_FILE, ps_dir / AMP_MEAN_FILE
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        raise SubpixelError(f"dolphin PS files missing: {missing}. Run the 'dolphin' step first.")
    return files


def run_subpixel(config):
    """Estimate sub-pixel PS positions and write them to ``<work_dir>/subpixel``.

    Does nothing unless ``subpixel.enabled`` is true in the config.

    Raises
    ------
    SubpixelError
        If the cropped stack or dolphin's PS files are missing or do not match.
    """
    if not config.subpixel.enabled:
        logger.info("Sub-pixel step skipped (subpixel.enabled is false).")
        return None

    strides_x, strides_y = config.dolphin.strides_x, config.dolphin.strides_y
    window = load_crop_window(config.crop_dir)
    slc_files = cropped_slc_files(config.crop_dir)
    if not slc_files:
        raise SubpixelError(f"No cropped SLCs in {config.crop_dir / 'slc'}. Run the 'crop' step first.")
    ps_mask_file, amp_mean_file = ps_input_files(config.dolphin_dir)
    ps_mask = gdal.Open(str(ps_mask_file)).ReadAsArray() == 1
    amp_mean = gdal.Open(str(amp_mean_file)).ReadAsArray().astype(np.float64)
    if ps_mask.shape != (window.length, window.width) or amp_mean.shape != ps_mask.shape:
        raise SubpixelError(
            f"dolphin PS rasters {ps_mask.shape} / {amp_mean.shape} do not match the crop window "
            f"({window.length} x {window.width}). Rerun 'dolphin' after 'crop'."
        )

    points = build_ps_points(ps_mask, amp_mean, strides_x, strides_y)
    logger.info("Sub-pixel: %d output pixels with a PS, %d SLCs, strides x=%d y=%d",
                len(points.peak_rows), len(slc_files), strides_x, strides_y)
    range_offsets, azimuth_offsets = estimate_offsets(slc_files, points, window.length)

    # Offsets relative to the pixel the MintPy geometry uses for each output pixel.
    range_offset = (points.peak_cols - points.center_cols) + range_offsets.mean()
    azimuth_offset = (points.peak_rows - points.center_rows) + azimuth_offsets.mean()
    valid_dates = np.minimum(range_offsets.count, azimuth_offsets.count)
    layers = {
        "range_offset": range_offset,
        "azimuth_offset": azimuth_offset,
        "range_offset_std": range_offsets.std(),
        "azimuth_offset_std": azimuth_offsets.std(),
        "valid_dates": valid_dates.astype(np.float64),
        "on_peak": points.on_peak.astype(np.float64),
    }
    subpixel_rows = points.center_rows + azimuth_offset
    subpixel_cols = points.center_cols + range_offset
    for name, layer in GEOMETRY_LAYERS.items():
        layers[name] = interpolate_geometry(cropped_geometry_file(config.crop_dir, layer),
                                            subpixel_rows, subpixel_cols, window.length)

    out_dir = config.paths.work_dir / SUBPIXEL_DIRECTORY
    out_dir.mkdir(parents=True, exist_ok=True)
    out_shape = output_shape(window, strides_x, strides_y)
    metadata = {
        "DESCRIPTION": "Sub-pixel PS position, see isce_dolphin_mintpy/subpixel.py",
        "OFFSET_REFERENCE_PIXEL": "full-resolution pixel (i*sy + sy//2, j*sx + sx//2) of the crop",
        "STRIDES_X": str(strides_x), "STRIDES_Y": str(strides_y),
        "NUM_SLCS": str(len(slc_files)),
    }
    for name in OUTPUT_LAYERS:
        write_layer(out_dir / f"{name}.tif", layers[name], points, out_shape, metadata,
                    double_precision=name in FLOAT64_LAYERS)

    estimated = valid_dates > 0
    if estimated.any():
        logger.info(
            "Sub-pixel: %d of %d PS estimated (%d on their peak, the others on the flank of a "
            "neighbouring peak); median |offset| range %.2f px, azimuth %.2f px; "
            "median std range %.2f px, azimuth %.2f px",
            int(estimated.sum()), len(estimated), int((estimated & points.on_peak).sum()),
            np.nanmedian(np.abs(range_offset)), np.nanmedian(np.abs(azimuth_offset)),
            np.nanmedian(layers["range_offset_std"]), np.nanmedian(layers["azimuth_offset_std"]),
        )
    else:
        logger.warning("Sub-pixel: no PS could be estimated.")
    logger.info("Sub-pixel layers written to %s", out_dir)
    return out_dir
