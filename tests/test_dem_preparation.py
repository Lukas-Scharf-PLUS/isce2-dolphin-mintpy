"""Tests for cutting and converting the DEM (without network or real geoid grids)."""

import numpy as np
import pytest
from osgeo import gdal, osr

from isce_dolphin_mintpy.dem_preparation import (
    DemPreparationError,
    compute_undulation_grid,
    pixel_center_coordinates,
    read_dem_window,
    to_ellipsoid_heights,
)

STEP = 1.0 / 3600.0  # 1 arc second


@pytest.fixture
def dem_file(tmp_path):
    """A 1-arcsecond lat/lon DEM covering 16-17 E, 48-49 N with height = row index."""
    size = 3600
    path = tmp_path / "dem.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), size, size, 1, gdal.GDT_Float32)
    dataset.SetGeoTransform((16.0, STEP, 0.0, 49.0, 0.0, -STEP))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    dataset.SetProjection(srs.ExportToWkt())
    dataset.GetRasterBand(1).WriteArray(np.repeat(np.arange(size, dtype=np.float32)[:, None], size, 1))
    dataset.GetRasterBand(1).SetNoDataValue(-9999)
    dataset = None
    return path


def test_read_dem_window_covers_requested_box(dem_file):
    heights, geotransform, nodata = read_dem_window(dem_file, [48.4, 48.5, 16.2, 16.3])
    west, north = geotransform[0], geotransform[3]
    east = west + heights.shape[1] * geotransform[1]
    south = north + heights.shape[0] * geotransform[5]
    assert west <= 16.2 and east >= 16.3 and south <= 48.4 and north >= 48.5
    assert nodata == -9999
    # first row of the window is DEM row (49 - north) / STEP
    assert heights[0, 0] == pytest.approx(round((49.0 - north) / STEP))


def test_read_dem_window_rejects_area_outside_dem(dem_file):
    with pytest.raises(DemPreparationError, match="covers"):
        read_dem_window(dem_file, [48.9, 49.2, 16.2, 16.3])


def test_pixel_centers_are_half_a_pixel_inside_the_corner():
    latitudes, longitudes = pixel_center_coordinates((16.0, 0.5, 0, 49.0, 0, -0.5), (2, 3))
    assert longitudes.tolist() == [16.25, 16.75, 17.25]
    assert latitudes.tolist() == [48.75, 48.25]


def test_undulation_grid_interpolates_a_smooth_field():
    latitudes = np.linspace(48.0, 48.5, 40)
    longitudes = np.linspace(16.0, 16.7, 50)

    def linear_field(lon, lat):
        return 40.0 + 2.0 * (lat - 48.0) + 3.0 * (lon - 16.0)

    undulation = compute_undulation_grid(latitudes, longitudes, linear_field)
    expected = linear_field(longitudes[None, :], latitudes[:, None])
    assert undulation.shape == (40, 50)
    assert np.allclose(undulation, expected, atol=1e-4)


def test_unphysical_undulation_is_rejected():
    latitudes, longitudes = np.array([48.0, 48.1]), np.array([16.0, 16.1])
    with pytest.raises(DemPreparationError, match="range"):
        compute_undulation_grid(latitudes, longitudes, lambda lon, lat: np.full_like(lon, np.inf))


def test_no_data_pixels_become_zero_and_valid_pixels_are_shifted():
    heights = np.array([[100.0, -9999.0], [np.nan, 200.0]], dtype=np.float32)
    result = to_ellipsoid_heights(heights, -9999.0, np.full((2, 2), 45.0, dtype=np.float32))
    assert result.tolist() == [[145.0, 0.0], [0.0, 245.0]]
