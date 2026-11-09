"""Tests for reading, checking and writing the MintPy template options."""

import pytest

from isce_dolphin_mintpy.mintpy_template import (
    format_template_value,
    is_plotting_enabled,
    min_temporal_coherence,
    requested_reference_date,
    split_mintpy_section,
    validate_template_options,
)


@pytest.fixture
def fake_mintpy_defaults(tmp_path):
    """A MintPy ``defaults`` folder with two known options."""
    (tmp_path / "smallbaselineApp.cfg").write_text(
        "mintpy.deramp          = auto  #[no / linear / quadratic]\n"
        "## comment line\n"
        "mintpy.reference.lalo  = auto\n"
    )
    return tmp_path


def test_split_keeps_pipeline_options_and_mintpy_options_apart():
    pipeline, template = split_mintpy_section({
        "inversion": "mintpy",
        "mintpy.reference.lalo": [48.19767, 16.32543],
        "mintpy.plot": True,
    })
    assert pipeline == {"inversion": "mintpy"}
    assert template == {"mintpy.reference.lalo": "48.19767, 16.32543", "mintpy.plot": "yes"}


def test_yaml_values_become_mintpy_text():
    assert format_template_value(False) == "no"
    assert format_template_value(None) == "auto"
    assert format_template_value(20220101) == "20220101"
    assert format_template_value("bridging+phase_closure") == "bridging+phase_closure"


def test_old_option_name_is_rejected_with_new_name():
    with pytest.raises(ValueError, match="mintpy.deramp"):
        split_mintpy_section({"deramp": "linear"})


def test_known_options_pass_and_typos_fail(fake_mintpy_defaults):
    validate_template_options({"mintpy.deramp": "quadratic"}, fake_mintpy_defaults)
    with pytest.raises(ValueError, match="mintpy.derampp"):
        validate_template_options({"mintpy.derampp": "quadratic"}, fake_mintpy_defaults)


def test_load_options_are_accepted_with_warning(fake_mintpy_defaults, caplog):
    (fake_mintpy_defaults / "smallbaselineApp.cfg").write_text("mintpy.load.unwFile = auto\n")
    validate_template_options({"mintpy.load.unwFile": "x"}, fake_mintpy_defaults)
    assert "no effect" in caplog.text


def test_plotting_is_on_unless_switched_off():
    assert is_plotting_enabled({})
    assert is_plotting_enabled({"mintpy.plot": "auto"})
    assert not is_plotting_enabled({"mintpy.plot": "no"})


def test_min_temporal_coherence_uses_mintpy_default_for_auto():
    assert min_temporal_coherence({}) == 0.7
    assert min_temporal_coherence({"mintpy.networkInversion.minTempCoh": "0.5"}) == 0.5


def test_only_explicit_reference_dates_are_returned():
    assert requested_reference_date({"mintpy.reference.date": "20220101"}) == "20220101"
    assert requested_reference_date({"mintpy.reference.date": "auto"}) is None
    assert requested_reference_date({}) is None
