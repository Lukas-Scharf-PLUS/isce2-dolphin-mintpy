"""Tests for loading and validating the pipeline configuration."""

import pytest
import yaml

from isce_dolphin_mintpy.config import ConfigError, load_config
from isce_dolphin_mintpy.mintpy_step import build_template_options


@pytest.fixture
def minimal_config(tmp_path):
    """Write a minimal valid config with fake input paths; return its path."""
    (tmp_path / "slc").mkdir()
    (tmp_path / "orbits").mkdir()
    (tmp_path / "dem.tif").write_text("")
    content = {
        "paths": {
            "slc_dir": str(tmp_path / "slc"),
            "orbit_dir": str(tmp_path / "orbits"),
            "dem_file": str(tmp_path / "dem.tif"),
            "dem_vertical_datum": "EGM2008",
            "work_dir": str(tmp_path / "work"),
        },
        "aoi": {"bbox": [48.1, 48.2, 16.3, 16.4]},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(content))
    return path


def rewrite(path, update):
    """Apply ``update(content)`` to the YAML file at ``path``."""
    content = yaml.safe_load(path.read_text())
    update(content)
    path.write_text(yaml.safe_dump(content))


def test_minimal_config_loads_with_defaults(minimal_config):
    config = load_config(minimal_config)
    assert config.dolphin.strides_x == 4
    assert config.mintpy.inversion == "mintpy"
    assert config.isce_dir == config.paths.work_dir / "isce"


def test_unknown_option_is_rejected(minimal_config):
    rewrite(minimal_config, lambda c: c.update({"dolphin": {"stride_x": 3}}))
    with pytest.raises(ConfigError, match="stride_x"):
        load_config(minimal_config)


def test_bbox_order_is_checked(minimal_config):
    rewrite(minimal_config, lambda c: c["aoi"].update({"bbox": [48.2, 48.1, 16.3, 16.4]}))
    with pytest.raises(ConfigError, match="bbox"):
        load_config(minimal_config)


def test_dem_vertical_datum_is_normalized(minimal_config):
    assert load_config(minimal_config).paths.dem_vertical_datum == "egm2008"


def test_missing_dem_vertical_datum_is_rejected(minimal_config):
    rewrite(minimal_config, lambda c: c["paths"].pop("dem_vertical_datum"))
    with pytest.raises(ConfigError, match="dem_vertical_datum"):
        load_config(minimal_config)


def test_invalid_inversion_mode_is_rejected(minimal_config):
    rewrite(minimal_config, lambda c: c.update({"mintpy": {"inversion": "both"}}))
    with pytest.raises(ConfigError, match="inversion"):
        load_config(minimal_config)


def test_extra_template_options_override_defaults(minimal_config):
    rewrite(minimal_config, lambda c: c.update(
        {"mintpy": {"troposphere": "no", "extra_template_options": {"mintpy.deramp": "linear"}}}))
    options = build_template_options(load_config(minimal_config).mintpy)
    assert options["mintpy.troposphericDelay.method"] == "no"
    assert options["mintpy.deramp"] == "linear"
    assert options["mintpy.networkInversion.maskDataset"] == "connectComponent"
