"""Tests for the dolphin options: merging, pipeline-owned options, validation, final config."""

from pathlib import Path

import pytest

from isce_dolphin_mintpy import dolphin_options
from isce_dolphin_mintpy.dolphin_options import (
    apply_inversion_mode,
    build_workflow_options,
    check_unwrapper_installed,
    merge_with_defaults,
    validate_with_dolphin,
)

dolphin_config = pytest.importorskip("dolphin.workflows.config")


def test_user_options_are_merged_over_defaults_key_by_key():
    options = merge_with_defaults({
        "unwrap_options": {"unwrap_method": "whirlwind",
                           "whirlwind_options": {"num_threads": 8}},
        "output_options": {"strides": {"x": 6}},
    })
    assert options["unwrap_options"]["unwrap_method"] == "whirlwind"
    assert options["unwrap_options"]["whirlwind_options"] == {"num_threads": 8}
    assert options["unwrap_options"]["snaphu_options"] == {"ntiles": [1, 1]}  # default kept
    assert options["output_options"]["strides"] == {"x": 6, "y": 1}


def test_pipeline_owned_option_is_rejected():
    with pytest.raises(ValueError, match="input_options.wavelength"):
        merge_with_defaults({"input_options": {"wavelength": 0.05}})


def test_unwrapping_cannot_be_switched_off():
    with pytest.raises(ValueError, match="run_unwrap"):
        merge_with_defaults({"unwrap_options": {"run_unwrap": False}})


def test_old_flat_name_points_to_new_path():
    with pytest.raises(ValueError, match="output_options.strides.x"):
        merge_with_defaults({"strides_x": 4})


def test_inversion_follows_mintpy_mode_unless_set():
    defaults = merge_with_defaults({})
    assert apply_inversion_mode(defaults, True)["timeseries_options"]["run_inversion"] is True
    assert apply_inversion_mode(defaults, False)["timeseries_options"]["run_velocity"] is False
    explicit = merge_with_defaults({"timeseries_options": {"run_inversion": True}})
    assert apply_inversion_mode(explicit, False)["timeseries_options"]["run_inversion"] is True
    switched_off = merge_with_defaults({"timeseries_options": {"run_inversion": False}})
    with pytest.raises(ValueError, match="run_inversion"):
        apply_inversion_mode(switched_off, True)


def test_dolphin_model_accepts_whirlwind_options():
    options = merge_with_defaults({"unwrap_options": {
        "unwrap_method": "whirlwind",
        "whirlwind_options": {"num_threads": 8, "interpolate": True, "conncomp_reliability": 0.3},
    }})
    validate_with_dolphin(options)


@pytest.mark.parametrize("bad_options, message", [
    ({"phase_linkin": {"ministack_size": 10}}, "phase_linkin"),          # top-level typo
    ({"unwrap_options": {"whirlwind_options": {"num_thread": 8}}}, "num_thread"),  # nested typo
    ({"unwrap_options": {"unwrap_method": "magic"}}, "unwrap_method"),   # invalid choice
])
def test_dolphin_model_rejects_invalid_options(bad_options, message):
    with pytest.raises(ValueError, match=message):
        validate_with_dolphin(merge_with_defaults(bad_options))


def test_missing_unwrapper_module_fails_early(monkeypatch):
    monkeypatch.setattr(dolphin_options.importlib.util, "find_spec", lambda name: None)
    options = merge_with_defaults({"unwrap_options": {"unwrap_method": "whirlwind"}})
    with pytest.raises(ValueError, match="whirlwind-insar"):
        check_unwrapper_installed(options)


def test_final_config_contains_user_and_pipeline_options(tmp_path):
    options = apply_inversion_mode(
        merge_with_defaults({"unwrap_options": {"unwrap_method": "whirlwind"}}), False)
    slc_files = [Path(f"/data/slc/{date}.slc.vrt") for date in ("20220101", "20220113")]
    workflow = dolphin_config.DisplacementWorkflow.model_validate(
        build_workflow_options(options, slc_files, tmp_path, None, 0.0555))
    assert workflow.unwrap_options.unwrap_method.value == "whirlwind"
    assert workflow.input_options.wavelength == 0.0555
    assert workflow.output_options.strides.x == 4
    assert workflow.timeseries_options.run_inversion is False
