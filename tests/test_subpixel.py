"""Tests for the sub-pixel PS positioning on synthetic point targets with known positions."""

import json
from types import SimpleNamespace

import numpy as np
import pytest
from osgeo import gdal

from isce_dolphin_mintpy.subpixel import (
    OffsetAccumulator,
    log_parabola_peak,
    run_subpixel,
    select_ps_pixels,
)

LENGTH, WIDTH = 40, 60
NUM_DATES = 8
# Oversampling (sampling rate / bandwidth) and Hamming coefficients similar to Sentinel-1 IW.
RANGE_OVERSAMPLING, RANGE_HAMMING = 64.345 / 48.2, 0.75
AZIMUTH_OVERSAMPLING, AZIMUTH_HAMMING = 1.5, 0.70
# Point targets: (row, col) of the true peak, in full-resolution pixels.
TARGETS = [(10.3, 15.8), (25.6, 40.15), (30.0, 22.0)]
# Synthetic geometry: linear in row and column, so bilinear interpolation is exact.
LAT0, LAT_PER_ROW, LAT_PER_COL = 48.2, -1e-4, 2e-5
TOLERANCE_PX = 0.1


def impulse_response(distance, oversampling, hamming):
    """Amplitude of a Hamming-weighted band-limited peak at ``distance`` samples."""
    frequencies = np.linspace(-0.5, 0.5, 801)
    weights = hamming - (1 - hamming) * np.cos(2 * np.pi * (frequencies + 0.5))
    phase = np.exp(2j * np.pi * np.multiply.outer(distance / oversampling, frequencies))
    return np.abs((weights * phase).sum(axis=-1)) / weights.sum()


def synthetic_slc(rng, noise=0.01):
    """Return one complex SLC with all TARGETS plus weak complex clutter."""
    rows, cols = np.arange(LENGTH), np.arange(WIDTH)
    image = noise * (rng.standard_normal((LENGTH, WIDTH)) + 1j * rng.standard_normal((LENGTH, WIDTH)))
    for target_row, target_col in TARGETS:
        azimuth = impulse_response(rows - target_row, AZIMUTH_OVERSAMPLING, AZIMUTH_HAMMING)
        range_ = impulse_response(cols - target_col, RANGE_OVERSAMPLING, RANGE_HAMMING)
        image += np.outer(azimuth, range_) * np.exp(1j * rng.uniform(0, 2 * np.pi))
    return image.astype(np.complex64)


def write_raster(path, array):
    """Write a 2D array as a GeoTIFF (no georeferencing, like radar-coordinate rasters).

    complex64 and float32 keep their type; everything else (e.g. coordinates) is float64.
    """
    data_type = {np.dtype(np.complex64): gdal.GDT_CFloat32, np.dtype(np.float32): gdal.GDT_Float32}.get(
        array.dtype, gdal.GDT_Float64)
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), array.shape[1], array.shape[0], 1, data_type)
    dataset.GetRasterBand(1).WriteArray(array)
    dataset.FlushCache()


def make_run(tmp_path, strides_x=1, strides_y=1, ps_pixels=None):
    """Create a fake cropped stack, geometry and dolphin PS output; return a config stand-in."""
    rng = np.random.default_rng(0)
    crop_dir, dolphin_dir = tmp_path / "crop", tmp_path / "dolphin"
    (crop_dir / "slc").mkdir(parents=True)
    (crop_dir / "geometry").mkdir()
    ps_dir = dolphin_dir / "phase_linking" / "PS"
    ps_dir.mkdir(parents=True)

    slcs = [synthetic_slc(rng) for _ in range(NUM_DATES)]
    for index, slc in enumerate(slcs):
        tif = crop_dir / "slc" / f"2022{index + 1:02d}01.tif"
        write_raster(tif, slc)
        gdal.Translate(str(crop_dir / "slc" / f"2022{index + 1:02d}01.slc.vrt"), str(tif), format="VRT")

    rows, cols = np.mgrid[0:LENGTH, 0:WIDTH].astype(np.float64)
    geometry = {"lat": LAT0 + LAT_PER_ROW * rows + LAT_PER_COL * cols,
                "lon": 16.3 + 3e-5 * cols, "hgt": 200.0 + 0.1 * rows}
    for layer, values in geometry.items():
        write_raster(crop_dir / "geometry" / f"{layer}.tif", values)
        gdal.Translate(str(crop_dir / "geometry" / f"{layer}.vrt"),
                       str(crop_dir / "geometry" / f"{layer}.tif"), format="VRT")

    amp_mean = np.mean([np.abs(slc) for slc in slcs], axis=0)
    ps_mask = np.zeros((LENGTH, WIDTH), np.float32)
    for row, col in ps_pixels if ps_pixels is not None else [(round(r), round(c)) for r, c in TARGETS]:
        ps_mask[row, col] = 1
    write_raster(ps_dir / "ps_pixels.tif", ps_mask)
    write_raster(ps_dir / "amp_mean.tif", amp_mean.astype(np.float32))

    window = {"x_offset": 0, "y_offset": 0, "width": WIDTH, "length": LENGTH,
              "full_width": WIDTH, "full_length": LENGTH}
    (crop_dir / "crop_window.json").write_text(json.dumps(window))
    return SimpleNamespace(
        subpixel=SimpleNamespace(enabled=True),
        dolphin=SimpleNamespace(strides_x=strides_x, strides_y=strides_y),
        crop_dir=crop_dir, dolphin_dir=dolphin_dir, paths=SimpleNamespace(work_dir=tmp_path),
    )


def read_layer(out_dir, name):
    """Read one output layer."""
    return gdal.Open(str(out_dir / f"{name}.tif")).ReadAsArray()


def test_symmetric_peak_has_zero_offset_and_sign_follows_the_brighter_side():
    assert log_parabola_peak(np.array([0.5]), np.array([1.0]), np.array([0.5]))[0] == pytest.approx(0.0)
    assert log_parabola_peak(np.array([0.4]), np.array([1.0]), np.array([0.6]))[0] > 0
    assert log_parabola_peak(np.array([0.6]), np.array([1.0]), np.array([0.4]))[0] < 0


def test_no_estimate_when_the_center_is_not_the_peak():
    assert np.isnan(log_parabola_peak(np.array([1.2]), np.array([1.0]), np.array([0.5]))[0])
    assert np.isnan(log_parabola_peak(np.array([0.0]), np.array([1.0]), np.array([0.5]))[0])


def test_brightest_ps_of_each_stride_window_is_selected_like_dolphin():
    ps_mask = np.zeros((2, 8), bool)
    ps_mask[0, [1, 2]] = True      # output pixel (0, 0): two PS, column 2 is brighter
    ps_mask[1, 6] = True           # output pixel (1, 1): one PS
    amp_mean = np.ones((2, 8))
    amp_mean[0, 2] = 5.0
    out_rows, out_cols, ps_rows, ps_cols = select_ps_pixels(ps_mask, amp_mean, strides_x=4, strides_y=1)
    assert list(zip(out_rows, out_cols, ps_rows, ps_cols)) == [(0, 0, 0, 2), (1, 1, 1, 6)]


def test_std_needs_two_estimates():
    accumulator = OffsetAccumulator(2)
    accumulator.add(np.array([0, 1]), np.array([0.1, 0.2]))
    accumulator.add(np.array([0, 1]), np.array([0.3, np.nan]))
    np.testing.assert_allclose(accumulator.mean(), [0.2, 0.2])
    assert accumulator.std()[0] == pytest.approx(np.std([0.1, 0.3], ddof=1))
    assert np.isnan(accumulator.std()[1])


def test_offsets_of_point_targets_are_recovered(tmp_path):
    out_dir = run_subpixel(make_run(tmp_path))
    range_offset = read_layer(out_dir, "range_offset")
    azimuth_offset = read_layer(out_dir, "azimuth_offset")
    for target_row, target_col in TARGETS:
        row, col = round(target_row), round(target_col)
        assert azimuth_offset[row, col] == pytest.approx(target_row - row, abs=TOLERANCE_PX)
        assert range_offset[row, col] == pytest.approx(target_col - col, abs=TOLERANCE_PX)
        assert read_layer(out_dir, "valid_dates")[row, col] == NUM_DATES
        assert read_layer(out_dir, "azimuth_offset_std")[row, col] < TOLERANCE_PX
    # Only PS pixels get values.
    assert np.isfinite(range_offset).sum() == len(TARGETS)


def test_geometry_is_interpolated_at_the_subpixel_position(tmp_path):
    out_dir = run_subpixel(make_run(tmp_path))
    latitude = read_layer(out_dir, "latitude")
    for target_row, target_col in TARGETS:
        row, col = round(target_row), round(target_col)
        estimated_row = row + read_layer(out_dir, "azimuth_offset")[row, col]
        estimated_col = col + read_layer(out_dir, "range_offset")[row, col]
        expected = LAT0 + LAT_PER_ROW * estimated_row + LAT_PER_COL * estimated_col
        assert latitude[row, col] == pytest.approx(expected, abs=1e-9)


def test_ps_on_the_flank_of_a_peak_points_to_the_peak(tmp_path):
    # Only the pixel right of the first target is marked as PS.
    target_row, target_col = TARGETS[0]
    flank = (round(target_row), round(target_col) + 1)
    out_dir = run_subpixel(make_run(tmp_path, ps_pixels=[flank]))
    range_offset = read_layer(out_dir, "range_offset")[flank]
    assert range_offset == pytest.approx(target_col - flank[1], abs=TOLERANCE_PX)
    assert read_layer(out_dir, "on_peak")[flank] == 0


def test_ps_on_the_peak_is_flagged(tmp_path):
    out_dir = run_subpixel(make_run(tmp_path))
    on_peak = read_layer(out_dir, "on_peak")
    assert all(on_peak[round(row), round(col)] == 1 for row, col in TARGETS)


def test_offsets_refer_to_the_output_pixel_center_with_strides(tmp_path):
    strides_x = 4
    out_dir = run_subpixel(make_run(tmp_path, strides_x=strides_x))
    range_offset = read_layer(out_dir, "range_offset")
    assert range_offset.shape == (LENGTH, WIDTH // strides_x)
    for target_row, target_col in TARGETS:
        out_col = round(target_col) // strides_x
        center_col = out_col * strides_x + strides_x // 2
        assert range_offset[round(target_row), out_col] == pytest.approx(target_col - center_col,
                                                                        abs=TOLERANCE_PX)


def test_disabled_step_writes_nothing(tmp_path):
    config = make_run(tmp_path)
    config.subpixel.enabled = False
    assert run_subpixel(config) is None
    assert not (tmp_path / "subpixel").exists()
