"""Stage 2: cut the coregistered radar-coordinate stack down to the AOI.

dolphin's ``output_options.bounds`` only masks pixels and needs geocoded input,
so for ISCE2 radar-coordinate stacks we crop ourselves:

1. Scan the full-resolution ISCE2 latitude/longitude rasters block by block and
   find the smallest row/column window that contains the (buffered) AOI box.
2. Grow the window so its size is a multiple of dolphin's strides. Then the
   dolphin output grid is exactly ``(height / strides_y, width / strides_x)``.
3. Write GDAL VRT files that point into the original ISCE2 rasters with that
   window (no data is copied): one per SLC and one per geometry layer.

The window is saved as ``crop_window.json`` so the MintPy stage can translate
cropped/strided pixel positions back to the ISCE2 grid (e.g. slant range).
"""

import json
import logging
from dataclasses import asdict, dataclass

import numpy as np
from osgeo import gdal

from .aoi import get_aoi_bbox
from .isce_stack import find_geometry_files, find_merged_slcs

logger = logging.getLogger(__name__)
gdal.UseExceptions()

ROWS_PER_SCAN_BLOCK = 512
CROP_WINDOW_FILE = "crop_window.json"


class CropError(RuntimeError):
    """Raised when the AOI does not overlap the radar stack."""


@dataclass
class CropWindow:
    """Pixel window of the crop inside the full ISCE2 merged grid."""

    x_offset: int
    y_offset: int
    width: int
    length: int
    full_width: int
    full_length: int

    def to_srcwin(self):
        """Return the window as GDAL ``srcWin`` [xoff, yoff, xsize, ysize]."""
        return [self.x_offset, self.y_offset, self.width, self.length]


def _bbox_contains(latitude, longitude, bbox):
    """Return a boolean mask of pixels inside the [S, N, W, E] box.

    Pixels with latitude and longitude both 0 are ISCE no-data (gaps between
    swaths/bursts) and are never inside.
    """
    south, north, west, east = bbox
    valid = ~((latitude == 0) & (longitude == 0))
    return (
        valid
        & (latitude >= south) & (latitude <= north)
        & (longitude >= west) & (longitude <= east)
    )


def find_aoi_window(lat_file, lon_file, bbox):
    """Find the smallest pixel window of the lat/lon rasters that covers the AOI box.

    Returns
    -------
    tuple[int, int, int, int]
        (first_row, last_row, first_col, last_col), all inclusive.

    Raises
    ------
    CropError
        If no pixel of the rasters falls inside the box.
    """
    lat_dataset = gdal.Open(str(lat_file))
    lon_dataset = gdal.Open(str(lon_file))
    width, length = lat_dataset.RasterXSize, lat_dataset.RasterYSize
    if (lon_dataset.RasterXSize, lon_dataset.RasterYSize) != (width, length):
        raise CropError(f"lat and lon rasters differ in size: {lat_file}, {lon_file}")

    rows_inside = []
    cols_inside = np.zeros(width, dtype=bool)
    for row_start in range(0, length, ROWS_PER_SCAN_BLOCK):
        num_rows = min(ROWS_PER_SCAN_BLOCK, length - row_start)
        latitude = lat_dataset.GetRasterBand(1).ReadAsArray(0, row_start, width, num_rows)
        longitude = lon_dataset.GetRasterBand(1).ReadAsArray(0, row_start, width, num_rows)
        inside = _bbox_contains(latitude, longitude, bbox)
        row_hits = np.flatnonzero(inside.any(axis=1))
        if row_hits.size:
            rows_inside.extend((row_hits + row_start).tolist())
            cols_inside |= inside.any(axis=0)

    if not rows_inside:
        raise CropError(f"The AOI box {bbox} does not overlap the radar stack ({lat_file}).")
    col_hits = np.flatnonzero(cols_inside)
    return rows_inside[0], rows_inside[-1], int(col_hits[0]), int(col_hits[-1])


def _align_axis(first, last, stride, full_size):
    """Grow [first, last] so its size is a multiple of ``stride`` within [0, full_size).

    The end is extended first; if that hits the raster edge, the start moves back.
    The result may be shortened to a multiple of ``stride`` only when the whole
    raster axis is smaller than the requested window.
    """
    size = last - first + 1
    size = int(np.ceil(size / stride) * stride)
    size = min(size, full_size - full_size % stride)
    first = min(first, full_size - size)
    return first, size


def compute_crop_window(lat_file, lon_file, bbox, strides_x, strides_y):
    """Compute the stride-aligned crop window for an AOI box.

    Returns
    -------
    CropWindow
    """
    first_row, last_row, first_col, last_col = find_aoi_window(lat_file, lon_file, bbox)
    dataset = gdal.Open(str(lat_file))
    full_width, full_length = dataset.RasterXSize, dataset.RasterYSize
    y_offset, length = _align_axis(first_row, last_row, strides_y, full_length)
    x_offset, width = _align_axis(first_col, last_col, strides_x, full_width)
    return CropWindow(x_offset, y_offset, width, length, full_width, full_length)


def write_window_vrt(source_file, output_file, window):
    """Write a VRT that exposes only ``window`` of ``source_file`` (no data copy)."""
    output_file.parent.mkdir(parents=True, exist_ok=True)
    gdal.Translate(str(output_file), str(source_file), format="VRT", srcWin=window.to_srcwin())


def save_crop_window(window, crop_dir):
    """Write the crop window to ``<crop_dir>/crop_window.json``."""
    with open(crop_dir / CROP_WINDOW_FILE, "w") as stream:
        json.dump(asdict(window), stream, indent=2)


def load_crop_window(crop_dir):
    """Read the crop window written by :func:`save_crop_window`."""
    with open(crop_dir / CROP_WINDOW_FILE) as stream:
        return CropWindow(**json.load(stream))


def cropped_slc_files(crop_dir):
    """Return the sorted list of cropped SLC VRTs (``<crop_dir>/slc/<date>.slc.vrt``)."""
    return sorted((crop_dir / "slc").glob("*.slc.vrt"))


def cropped_geometry_file(crop_dir, layer):
    """Return the cropped VRT of one geometry layer, e.g. ``lat`` or ``los``."""
    return crop_dir / "geometry" / f"{layer}.vrt"


def crop_stack_to_aoi(config):
    """Create the cropped SLC and geometry VRTs for the AOI and return the window.

    Raises
    ------
    CropError
        If the AOI does not overlap the radar stack.
    """
    geometry_files = find_geometry_files(config.isce_dir)
    slc_files = find_merged_slcs(config.isce_dir)
    bbox = get_aoi_bbox(config.aoi, with_buffer=True)

    window = compute_crop_window(
        geometry_files["lat"],
        geometry_files["lon"],
        bbox,
        config.dolphin.strides_x,
        config.dolphin.strides_y,
    )
    logger.info(
        "AOI crop window in the ISCE2 grid (%d x %d): x=%d..%d, y=%d..%d",
        window.full_width, window.full_length,
        window.x_offset, window.x_offset + window.width - 1,
        window.y_offset, window.y_offset + window.length - 1,
    )

    for date, slc_file in slc_files.items():
        dataset = gdal.Open(str(slc_file))
        if (dataset.RasterXSize, dataset.RasterYSize) != (window.full_width, window.full_length):
            raise CropError(
                f"SLC {slc_file} ({dataset.RasterXSize} x {dataset.RasterYSize}) does not match the "
                f"geometry grid ({window.full_width} x {window.full_length})."
            )

    crop_dir = config.crop_dir
    crop_dir.mkdir(parents=True, exist_ok=True)
    for date, slc_file in slc_files.items():
        write_window_vrt(slc_file, crop_dir / "slc" / f"{date}.slc.vrt", window)
    for layer, geometry_file in geometry_files.items():
        write_window_vrt(geometry_file, cropped_geometry_file(crop_dir, layer), window)
    save_crop_window(window, crop_dir)
    return window
