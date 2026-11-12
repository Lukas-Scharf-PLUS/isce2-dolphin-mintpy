"""Tests for comparing the main MintPy run with its variants."""

import json

import h5py
import numpy as np
import pytest

from isce_dolphin_mintpy import variant_comparison


def write_rms_file(run_dir, rms_by_date, name="rms_timeseriesResidual_ramp.txt"):
    """Write a residual RMS text file in MintPy's format (values in meters)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    lines = ["# Root Mean Square in space for each acquisition of time-series", "# Date\t\tRMS (m)"]
    lines += [f"{date}\t{rms}" for date, rms in rms_by_date.items()]
    (run_dir / name).write_text("\n".join(lines) + "\n")


def write_velocity_and_mask(run_dir, velocity_std, mask):
    """Write fake velocity.h5 (velocityStd in m/year) and maskTempCoh.h5."""
    with h5py.File(run_dir / "velocity.h5", "w") as target:
        target["velocityStd"] = np.asarray(velocity_std, dtype=np.float32)
    with h5py.File(run_dir / "maskTempCoh.h5", "w") as target:
        target["mask"] = np.asarray(mask, dtype=bool)


def test_residual_rms_is_read_per_date(tmp_path):
    write_rms_file(tmp_path, {"20220101": 0.004, "20220113": 0.006})
    assert variant_comparison.read_residual_rms(tmp_path) == {"20220101": 0.004, "20220113": 0.006}


def test_missing_residual_rms_explains_the_cause(tmp_path):
    with pytest.raises(variant_comparison.VariantComparisonError, match="topographicResidual"):
        variant_comparison.read_residual_rms(tmp_path)


def test_lowest_median_wins_and_dates_are_counted():
    rms = {
        "main":     {"d1": 5.0, "d2": 5.0, "d3": 5.0},
        "no_tropo": {"d1": 6.0, "d2": 6.0, "d3": 4.0},
        "opera":    {"d1": 4.0, "d2": 4.5, "d3": 5.5},
    }
    selected, scores = variant_comparison.compare_runs(rms)
    assert selected == "opera"
    assert scores["opera"]["dates_better_than_main"] == 2
    assert scores["main"]["dates_better_than_main"] is None
    assert scores["no_tropo"]["dates_lowest_rms"] == 1


def test_only_shared_dates_are_compared():
    rms = {"main": {"d1": 5.0, "d2": 1.0}, "other": {"d1": 4.0}}
    selected, scores = variant_comparison.compare_runs(rms)
    assert selected == "other"
    assert scores["main"]["median_residual_rms"] == 5.0


def test_main_run_is_kept_on_a_tie():
    selected, _ = variant_comparison.compare_runs({"main": {"d1": 5.0}, "other": {"d1": 5.0}})
    assert selected == "main"


def test_rounding_noise_counts_as_a_tie():
    # Seen with 3 dates: the temporal model fits exactly, residuals are ~1e-9 m.
    rms = {"main": {"d1": 1.9e-9, "d2": 2.4e-9}, "other": {"d1": 1.8e-9, "d2": 2.3e-9}}
    selected, scores = variant_comparison.compare_runs(rms)
    assert selected == "main"
    assert scores["other"]["dates_better_than_main"] == 0
    assert scores["main"]["dates_lowest_rms"] == 2


def test_median_velocity_std_uses_the_mask(tmp_path):
    write_velocity_and_mask(tmp_path, [[0.001, 0.002], [0.003, 1.0]], [[1, 1], [1, 0]])
    assert variant_comparison.median_velocity_std(tmp_path) == pytest.approx(2.0)


def test_report_is_written(tmp_path):
    main_dir, variant_dir = tmp_path / "main", tmp_path / "no_tropo"
    write_rms_file(main_dir, {"20220101": 0.003, "20220113": 0.004})
    write_rms_file(variant_dir, {"20220101": 0.005, "20220113": 0.006})
    (variant_dir / "exclude_date.txt").write_text("20220113\n")
    output = tmp_path / "variant_comparison.json"

    selected = variant_comparison.write_comparison(
        {"main": main_dir, "no_tropo": variant_dir}, output,
        {"no_tropo": {"mintpy.troposphericDelay.method": "no"}})

    report = json.loads(output.read_text())
    assert selected == report["selected"] == "main"
    assert report["runs"]["main"]["median_residual_rms_mm"] == pytest.approx(3.5)
    assert report["runs"]["no_tropo"]["excluded_dates"] == ["20220113"]
    assert report["runs"]["no_tropo"]["options"] == {"mintpy.troposphericDelay.method": "no"}
    assert report["runs"]["main"]["median_velocity_std_mm_per_year"] is None
