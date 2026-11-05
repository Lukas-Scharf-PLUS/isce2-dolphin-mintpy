"""Water mask on dolphin's output grid (radar coordinates).

The water mask is a geocoded raster (e.g. EPSG:4326, 1 = land, 0 = water), but
dolphin and MintPy work in radar coordinates. For every dolphin output pixel
(strides_y x strides_x full-resolution pixels) this module computes the
*fraction* of its area that is water:

1. Regularly spaced sample points are placed inside the pixel's footprint in
   the full-resolution radar grid (``SUBSAMPLES_PER_PIXEL_*`` per full-resolution
   pixel, i.e. a few meters apart on the ground).
2. Their lat/lon are interpolated bilinearly from the cropped ISCE2 lat/lon rasters.
3. The mask is looked up at each point. Mask no-data and points outside the
   mask are ignored: fraction = water samples / valid samples (0 if none valid).

A pixel counts as water only if the fraction is above ``max_water_fraction``
(default 0.8). Being conservative keeps mixed shore pixels that may contain a
strong scatterer (quay wall, bridge pier); such pixels can still be removed
later with coherence masks, whereas masked pixels cannot be recovered.

Files written to ``<work_dir>/crop/water``:
    water_fraction.tif   float32, 0..1 per output pixel
    valid_mask.tif       uint8, 1 = keep, 0 = water (dolphin ``mask_file`` convention)
"""

import logging

import numpy as np
from osgeo import gdal

from .radar_crop import cropped_geometry_file, load_crop_window

logger = logging.getLogger(__name__)
gdal.UseExceptions()

# Sample points per full-resolution Sentinel-1 IW pixel (about 4 m ground range
# x 14 m azimuth), giving a spacing of roughly 1.3-2 m, similar to a fine
# (cadastral) water mask.
SUBSAMPLES_PER_PIXEL_RANGE = 3
SUBSAMPLES_PER_PIXEL_AZIMUTH = 7
# Upper limit of sample points processed at once (controls memory use).
MAX_SAMPLES_PER_BLOCK = 4_000_000
# Margin (degrees) around the AOI when reading the water mask window.
MASK_READ_MARGIN_DEG = 0.002

WATER_FRACTION_FILE = "water_fraction.tif"
VALID_MASK_FILE = "valid_mask.tif"


class WaterMaskError(RuntimeError):
    """Raised when the water mask cannot be used (wrong CRS, no overlap)."""


class GeocodedMask:
    """In-memory window of a geocoded lat/lon mask raster with point lookup."""

    def __init__(self, mask_file, bbox):
        """Read the part of ``mask_file`` covering the [S, N, W, E] box.

        Raises
        ------
        WaterMaskError
            If the mask is not in geographic coordinates or does not overlap the box.
        """
        dataset = gdal.Open(str(mask_file))
        spatial_ref = dataset.GetSpatialRef()
        if spatial_ref is None or not spatial_ref.IsGeographic():
            raise WaterMaskError(f"Water mask must be in geographic lat/lon coordinates: {mask_file}")
        geotransform = dataset.GetGeoTransform()
        band = dataset.GetRasterBand(1)
        self.nodata = band.GetNoDataValue()

        south, north, west, east = bbox
        first_col = max(0, int(np.floor((west - geotransform[0]) / geotransform[1])))
        last_col = min(dataset.RasterXSize, int(np.ceil((east - geotransform[0]) / geotransform[1])))
        first_row = max(0, int(np.floor((north - geotransform[3]) / geotransform[5])))
        last_row = min(dataset.RasterYSize, int(np.ceil((south - geotransform[3]) / geotransform[5])))
        if last_col <= first_col or last_row <= first_row:
            raise WaterMaskError(f"Water mask {mask_file} does not overlap the AOI {bbox}.")

        self.values = band.ReadAsArray(first_col, first_row, last_col - first_col, last_row - first_row)
        self.west = geotransform[0] + first_col * geotransform[1]
        self.north = geotransform[3] + first_row * geotransform[5]
        self.step_x, self.step_y = geotransform[1], geotransform[5]

    def lookup(self, latitude, longitude):
        """Return (values, inside) for points; ``inside`` is False outside the window."""
        cols = np.floor((longitude - self.west) / self.step_x).astype(np.int64)
        rows = np.floor((latitude - self.north) / self.step_y).astype(np.int64)
        length, width = self.values.shape
        inside = (rows >= 0) & (rows < length) & (cols >= 0) & (cols < width)
        values = np.zeros(latitude.shape, dtype=self.values.dtype)
        values[inside] = self.values[rows[inside], cols[inside]]
        return values, inside


def subsample_positions(num_out, stride, subsamples_per_pixel, full_size):
    """Return full-resolution pixel coordinates of the sample points along one axis.

    Output pixel ``i`` covers full-resolution pixels ``i*stride .. (i+1)*stride-1``,
    i.e. the coordinate interval ``[i*stride - 0.5, (i+1)*stride - 0.5]``. That
    interval is split into ``stride * subsamples_per_pixel`` equal parts and one
    point is placed in the center of each part.

    Returns
    -------
    np.ndarray, shape (num_out * stride * subsamples_per_pixel,)
    """
    samples_per_out = stride * subsamples_per_pixel
    offsets = (np.arange(samples_per_out) + 0.5) / subsamples_per_pixel - 0.5
    positions = (np.arange(num_out)[:, None] * stride + offsets[None, :]).ravel()
    return np.clip(positions, 0, full_size - 1)


def compute_water_fraction(latitude, longitude, mask, water_value, strides_x, strides_y,
                           out_shape):
    """Compute the water fraction of every output pixel.

    Parameters
    ----------
    latitude, longitude : np.ndarray
        Full-resolution lat/lon of the cropped radar grid.
    mask : GeocodedMask
    water_value : int
        Mask value that means water.
    strides_x, strides_y : int
        dolphin strides (range, azimuth).
    out_shape : tuple[int, int]
        dolphin output shape (length, width).

    Returns
    -------
    np.ndarray (float32), values 0..1
    """
    from scipy.ndimage import map_coordinates

    out_length, out_width = out_shape
    full_length, full_width = latitude.shape
    samples_az = strides_y * SUBSAMPLES_PER_PIXEL_AZIMUTH
    samples_rg = strides_x * SUBSAMPLES_PER_PIXEL_RANGE
    col_positions = subsample_positions(out_width, strides_x, SUBSAMPLES_PER_PIXEL_RANGE, full_width)

    rows_per_block = max(1, MAX_SAMPLES_PER_BLOCK // (samples_az * col_positions.size))
    fraction = np.zeros(out_shape, dtype=np.float32)
    for row_start in range(0, out_length, rows_per_block):
        num_rows = min(rows_per_block, out_length - row_start)
        row_positions = subsample_positions(row_start + num_rows, strides_y,
                                            SUBSAMPLES_PER_PIXEL_AZIMUTH, full_length)
        row_positions = row_positions[row_start * samples_az:]
        grid_rows, grid_cols = np.meshgrid(row_positions, col_positions, indexing="ij")
        coordinates = [grid_rows.ravel(), grid_cols.ravel()]
        sample_lat = map_coordinates(latitude, coordinates, order=1)
        sample_lon = map_coordinates(longitude, coordinates, order=1)

        values, inside = mask.lookup(sample_lat, sample_lon)
        valid = inside if mask.nodata is None else inside & (values != mask.nodata)
        is_water = valid & (values == water_value)

        block_shape = (num_rows, samples_az, out_width, samples_rg)
        water_count = is_water.reshape(block_shape).sum(axis=(1, 3))
        valid_count = valid.reshape(block_shape).sum(axis=(1, 3))
        fraction[row_start:row_start + num_rows] = np.where(
            valid_count > 0, water_count / np.maximum(valid_count, 1), 0.0
        )
    return fraction


def write_radar_raster(path, array, gdal_type):
    """Write a 2D array as GeoTIFF without georeferencing (radar grid)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    dataset = gdal.GetDriverByName("GTiff").Create(
        str(path), array.shape[1], array.shape[0], 1, gdal_type, options=["COMPRESS=DEFLATE"]
    )
    dataset.GetRasterBand(1).WriteArray(array)
    dataset = None


def water_fraction_file(config):
    """Path of the water fraction raster on dolphin's output grid."""
    return config.water_dir / WATER_FRACTION_FILE


def valid_mask_file(config):
    """Path of the dolphin mask (1 = keep, 0 = water) on dolphin's output grid."""
    return config.water_dir / VALID_MASK_FILE


def create_water_mask(config):
    """Compute the water fraction and binary mask on dolphin's output grid.

    Does nothing if no water mask file is configured.

    Returns
    -------
    Path or None
        The dolphin mask file, or None without a water mask.
    """
    if config.water_mask.file is None:
        return None

    window = load_crop_window(config.crop_dir)
    strides_x, strides_y = config.dolphin.strides_x, config.dolphin.strides_y
    out_shape = (window.length // strides_y, window.width // strides_x)

    latitude = gdal.Open(str(cropped_geometry_file(config.crop_dir, "lat"))).ReadAsArray().astype(np.float64)
    longitude = gdal.Open(str(cropped_geometry_file(config.crop_dir, "lon"))).ReadAsArray().astype(np.float64)
    bbox = [latitude.min() - MASK_READ_MARGIN_DEG, latitude.max() + MASK_READ_MARGIN_DEG,
            longitude.min() - MASK_READ_MARGIN_DEG, longitude.max() + MASK_READ_MARGIN_DEG]
    mask = GeocodedMask(config.water_mask.file, bbox)

    fraction = compute_water_fraction(latitude, longitude, mask, config.water_mask.water_value,
                                      strides_x, strides_y, out_shape)
    keep = (fraction <= config.water_mask.max_water_fraction).astype(np.uint8)

    write_radar_raster(water_fraction_file(config), fraction, gdal.GDT_Float32)
    write_radar_raster(valid_mask_file(config), keep, gdal.GDT_Byte)
    logger.info(
        "Water mask on dolphin grid %s: %.1f%% of pixels have water, %.1f%% masked (> %.0f%% water)",
        out_shape, 100 * float((fraction > 0).mean()), 100 * float((keep == 0).mean()),
        100 * config.water_mask.max_water_fraction,
    )
    return valid_mask_file(config)
