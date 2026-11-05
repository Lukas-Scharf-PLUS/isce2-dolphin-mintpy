"""Tests for converting a geocoded water mask to water fractions on dolphin's grid."""

import numpy as np
import pytest
from osgeo import gdal, osr

from isce_dolphin_mintpy.water_mask import (
    GeocodedMask,
    WaterMaskError,
    compute_water_fraction,
    subsample_positions,
)

MASK_STEP = 0.0001  # degrees


@pytest.fixture
def mask_file(tmp_path):
    """Lat/lon mask over 16.0-16.1 E, 48.0-48.1 N: water (0) west of 16.05 E, land (1) east.

    The northernmost 10 rows are no-data (255).
    """
    size = 1000
    values = np.ones((size, size), dtype=np.uint8)
    values[:, :500] = 0
    values[:10, :] = 255
    path = tmp_path / "water.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), size, size, 1, gdal.GDT_Byte)
    dataset.SetGeoTransform((16.0, MASK_STEP, 0.0, 48.1, 0.0, -MASK_STEP))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    dataset.SetProjection(srs.ExportToWkt())
    dataset.GetRasterBand(1).WriteArray(values)
    dataset.GetRasterBand(1).SetNoDataValue(255)
    dataset = None
    return path


def radar_grid(length, width, lat0, lon0, dlat, dlon):
    """Full-resolution lat/lon arrays of a simple, axis-aligned 'radar' grid."""
    rows, cols = np.mgrid[0:length, 0:width]
    return lat0 - rows * dlat, lon0 + cols * dlon


def test_sample_points_are_centered_in_each_output_pixel():
    positions = subsample_positions(num_out=3, stride=2, subsamples_per_pixel=2, full_size=100)
    # output pixel 1 covers [1.5, 3.5], pixel 2 covers [3.5, 5.5]
    assert positions[4:8].mean() == pytest.approx(2.5)
    assert positions[8:].mean() == pytest.approx(4.5)
    assert np.all(np.diff(positions) >= 0)


def test_sample_points_are_clipped_to_the_grid():
    # pixel 0 reaches half a pixel outside the grid; there is no lat/lon there
    positions = subsample_positions(num_out=1, stride=2, subsamples_per_pixel=2, full_size=2)
    assert positions.min() == 0.0 and positions.max() == 1.0


def test_water_fraction_follows_the_shoreline(mask_file):
    # 20 x 40 full-res pixels, 0.0005 deg wide: columns 0-19 west of 16.05 (water), 20-39 east
    latitude, longitude = radar_grid(20, 40, 48.09, 16.04 + 0.00025, 0.0005, 0.0005)
    mask = GeocodedMask(mask_file, [48.0, 48.1, 16.0, 16.1])
    fraction = compute_water_fraction(latitude, longitude, mask, water_value=0,
                                      strides_x=4, strides_y=1, out_shape=(20, 10))
    assert np.allclose(fraction[:, :5], 1.0)
    assert np.allclose(fraction[:, 5:], 0.0)


def test_mixed_pixel_gets_partial_fraction(mask_file):
    # one output pixel of 4 columns, centered on the shoreline: half water
    latitude, longitude = radar_grid(1, 4, 48.05, 16.05 - 0.0015, 0.0005, 0.001)
    mask = GeocodedMask(mask_file, [48.0, 48.1, 16.0, 16.1])
    fraction = compute_water_fraction(latitude, longitude, mask, 0, 4, 1, (1, 1))
    assert fraction[0, 0] == pytest.approx(0.5, abs=0.05)


def test_no_data_and_outside_points_are_ignored(mask_file):
    # pixels in the no-data rows (north edge) and north of the mask: no valid samples
    latitude, longitude = radar_grid(2, 4, 48.1005, 16.01, 0.0006, 0.0005)
    mask = GeocodedMask(mask_file, [48.0, 48.1, 16.0, 16.1])
    fraction = compute_water_fraction(latitude, longitude, mask, 0, 4, 1, (2, 1))
    assert np.all(fraction == 0.0)


def test_mask_outside_aoi_raises(mask_file):
    with pytest.raises(WaterMaskError, match="overlap"):
        GeocodedMask(mask_file, [10.0, 11.0, 10.0, 11.0])
