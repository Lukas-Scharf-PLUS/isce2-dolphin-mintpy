"""Tests for reading topsStack run files."""

from isce_dolphin_mintpy.isce_stack import list_run_files, read_run_file_commands


def test_run_file_commands_run_in_foreground(tmp_path):
    run_file = tmp_path / "run_07_pairs_misreg"
    run_file.write_text(
        "SentinelWrapper.py -c configs/config_a &\n"
        "\n"
        "# comment\n"
        "SentinelWrapper.py -c configs/config_b&\n"
        "wait\n"
        "SentinelWrapper.py -c configs/config_c\n"
    )
    assert read_run_file_commands(run_file) == [
        "SentinelWrapper.py -c configs/config_a",
        "SentinelWrapper.py -c configs/config_b",
        "SentinelWrapper.py -c configs/config_c",
    ]


def test_run_files_are_sorted_by_step_number(tmp_path):
    run_dir = tmp_path / "run_files"
    run_dir.mkdir()
    for name in ("run_10_fullBurst_resample", "run_02_unpack_secondary_slc",
                 "run_01_unpack_topo_reference", "run_01_unpack_topo_reference.job"):
        (run_dir / name).write_text("")
    assert [p.name for p in list_run_files(tmp_path)] == [
        "run_01_unpack_topo_reference", "run_02_unpack_secondary_slc", "run_10_fullBurst_resample",
    ]

