"""Tests for the MintPy step: step order, quick_overview tolerance and reference date check."""

from types import SimpleNamespace

import h5py
import numpy as np
import pytest

from isce_dolphin_mintpy import mintpy_step
from isce_dolphin_mintpy.shell import CommandFailedError


def write_ambiguity_file(mintpy_dir, counts):
    """Write a fake ``numTriNonzeroIntAmbiguity.h5`` with the given counts."""
    with h5py.File(mintpy_dir / mintpy_step.AMBIGUITY_FILE, "w") as target:
        target["mask"] = np.asarray(counts, dtype=np.float32)


def fake_config(mintpy_dir):
    """Smallest config object the step functions need."""
    return SimpleNamespace(mintpy_dir=mintpy_dir, mintpy=SimpleNamespace(template_options={}))


def failing_step(failing_name, calls):
    """Return a replacement for run_smallbaseline_step that fails on one step."""
    def run(config, template_file, step):
        calls.append(step)
        if step == failing_name:
            raise CommandFailedError(f"{step} failed")
    return run


def test_quick_overview_runs_before_unwrapping_error_correction():
    steps = mintpy_step.STEPS_BEFORE_INVERSION
    assert steps.index("quick_overview") < steps.index("correct_unwrap_error")


def test_quick_overview_crash_without_ambiguity_is_tolerated(tmp_path, monkeypatch):
    write_ambiguity_file(tmp_path, [[0, 0], [np.nan, 0]])
    calls = []
    monkeypatch.setattr(mintpy_step, "run_smallbaseline_step", failing_step("quick_overview", calls))
    mintpy_step.run_steps(fake_config(tmp_path), "t.cfg", ["quick_overview", "correct_unwrap_error"])
    assert calls == ["quick_overview", "correct_unwrap_error"]


def test_quick_overview_failure_with_ambiguity_stops(tmp_path, monkeypatch):
    write_ambiguity_file(tmp_path, [[0, 2]])
    monkeypatch.setattr(mintpy_step, "run_smallbaseline_step", failing_step("quick_overview", []))
    with pytest.raises(CommandFailedError):
        mintpy_step.run_steps(fake_config(tmp_path), "t.cfg", ["quick_overview"])


def test_other_step_failures_always_stop(tmp_path, monkeypatch):
    write_ambiguity_file(tmp_path, [[0, 0]])
    monkeypatch.setattr(mintpy_step, "run_smallbaseline_step", failing_step("invert_network", []))
    with pytest.raises(CommandFailedError):
        mintpy_step.run_steps(fake_config(tmp_path), "t.cfg", ["invert_network"])


def write_stack_dates(mintpy_dir, pairs):
    """Write a fake ``inputs/ifgramStack.h5`` with only the date pairs."""
    (mintpy_dir / "inputs").mkdir()
    with h5py.File(mintpy_dir / mintpy_step.STACK_FILE, "w") as stack:
        stack["date"] = np.array(pairs, dtype="S8")


def test_reference_date_of_the_stack_passes(tmp_path):
    write_stack_dates(tmp_path, [["20220101", "20220113"]])
    mintpy_step.check_reference_date_in_stack(tmp_path, {"mintpy.reference.date": "20220113"})


def slc_config(slc_dir, reference_date):
    """Config object with an SLC folder and a MintPy reference date."""
    return SimpleNamespace(
        paths=SimpleNamespace(slc_dir=slc_dir),
        mintpy=SimpleNamespace(template_options={"mintpy.reference.date": reference_date}),
    )


def test_reference_date_without_slc_fails_before_isce(tmp_path):
    for name in ("S1A_IW_SLC__1SDV_20250101T164306_20250101T164334_057000_070000_AAAA.SAFE",
                 "S1A_IW_SLC__1SDV_20250113T164306_20250113T164334_057175_070600_BBBB.zip"):
        (tmp_path / name).mkdir()
    assert mintpy_step.slc_acquisition_dates(tmp_path) == ["20250101", "20250113"]
    mintpy_step.check_reference_date_in_slc_dir(slc_config(tmp_path, "20250113"))
    with pytest.raises(mintpy_step.MintpyStepError, match="20220101"):
        mintpy_step.check_reference_date_in_slc_dir(slc_config(tmp_path, "20220101"))


def test_reference_date_outside_the_stack_fails_early(tmp_path):
    write_stack_dates(tmp_path, [["20250101", "20250113"]])
    with pytest.raises(mintpy_step.MintpyStepError, match="20220101"):
        mintpy_step.check_reference_date_in_stack(tmp_path, {"mintpy.reference.date": "20220101"})
