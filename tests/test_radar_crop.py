"""Tests for the AOI crop in radar coordinates and the stride-aligned output grid."""

import numpy as np
import pytest
from osgeo import gdal

from isce_dolphin_mintpy.mintpy_inputs import output_shape, sample_to_output_grid
from isce_dolphin_mintpy.radar_crop import (
    CropError,
    _align_axis,
    compute_crop_window,
    find_aoi_window,
)


def write_raster(path, array):
    """Write a float64 GeoTIFF without georeferencing (like a radar raster)."""
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(str(path), array.shape[1], array.shape[0], 1, gdal.GDT_Float64)
    dataset.GetRasterBand(1).WriteArray(array)
    dataset = None
    return path


@pytest.fixture
def lat_lon_files(tmp_path):
    """A 100 x 80 radar grid: latitude grows with row, longitude with column."""
    rows, cols = np.mgrid[0:100, 0:80]
    latitude = 48.0 + rows * 0.001
    longitude = 16.0 + cols * 0.001
    return (write_raster(tmp_path / "lat.tif", latitude),
            write_raster(tmp_path / "lon.tif", longitude))


def test_find_aoi_window_returns_inclusive_pixel_range(lat_lon_files):
    lat_file, lon_file = lat_lon_files
    # rows 10..20 and cols 5..15 lie inside this box
    bbox = [48.0095, 48.0205, 16.0045, 16.0155]
    assert find_aoi_window(lat_file, lon_file, bbox) == (10, 20, 5, 15)


def test_find_aoi_window_rejects_box_outside_stack(lat_lon_files):
    lat_file, lon_file = lat_lon_files
    with pytest.raises(CropError):
        find_aoi_window(lat_file, lon_file, [10.0, 11.0, 10.0, 11.0])


def test_zero_lat_lon_pixels_are_ignored(tmp_path):
    latitude = np.zeros((10, 10))
    longitude = np.zeros((10, 10))
    latitude[5, 5], longitude[5, 5] = 48.0, 16.0
    lat_file = write_raster(tmp_path / "lat.tif", latitude)
    lon_file = write_raster(tmp_path / "lon.tif", longitude)
    # a box around (0, 0) must not pick up the no-data pixels
    with pytest.raises(CropError):
        find_aoi_window(lat_file, lon_file, [-1.0, 1.0, -1.0, 1.0])
    assert find_aoi_window(lat_file, lon_file, [47.9, 48.1, 15.9, 16.1]) == (5, 5, 5, 5)


@pytest.mark.parametrize(
    "first, last, stride, full_size, expected",
    [
        (10, 20, 4, 100, (10, 12)),   # 11 pixels -> grown to 12
        (10, 21, 4, 100, (10, 12)),   # already a multiple
        (95, 99, 4, 100, (92, 8)),    # end hits the edge -> start moves back
        (0, 99, 3, 100, (0, 99)),     # whole axis, cut to a multiple of 3
    ],
)
def test_align_axis_produces_stride_multiples(first, last, stride, full_size, expected):
    start, size = _align_axis(first, last, stride, full_size)
    assert (start, size) == expected
    assert size % stride == 0
    assert start >= 0 and start + size <= full_size


def test_crop_window_size_matches_dolphin_output_grid(lat_lon_files):
    lat_file, lon_file = lat_lon_files
    window = compute_crop_window(lat_file, lon_file, [48.0095, 48.0205, 16.0045, 16.0155],
                                 strides_x=4, strides_y=3)
    assert window.width % 4 == 0 and window.length % 3 == 0
    assert output_shape(window, 4, 3) == (window.length // 3, window.width // 4)


def test_sample_to_output_grid_uses_dolphin_pixel_centers():
    # dolphin docs: size 6 with stride 3 -> output pixels centered on input 1 and 4
    array = np.arange(6 * 8).reshape(6, 8)
    sampled = sample_to_output_grid(array, strides_x=4, strides_y=3, out_length=2, out_width=2)
    assert sampled.tolist() == [[array[1, 2], array[1, 6]], [array[4, 2], array[4, 6]]]
