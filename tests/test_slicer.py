"""Ported from the CHX350 backend (dsf/tests/test_gcode.py) plus the lines above the block."""

import qa_slicer

ORCA_TAIL = """G1 X10 Y10
; filament used [mm] = 12526.19
; total filament used [g] = 103.88
; estimated printing time (normal mode) = 1h 14m 55s
; CONFIG_BLOCK_START
; curr_bed_type = High Temp Plate
; filament_diameter = 2.85
; filament_settings_id = "Extrudr PLA NX2 Matt @0.8 nozzle"
; filament_type = PLA
; layer_height = 0.36
; nozzle_diameter = 0.8
; print_sequence = by layer
; printer_model = Meltingplot CHX 350
; printer_settings_id = Meltingplot CHX 350 0.8 nozzle - #1
; required_nozzle_HRC = 3
; some_other_setting = 42
; CONFIG_BLOCK_END
"""


def test_config_block_parsed():
    config, source = qa_slicer.parse_config_block(ORCA_TAIL)
    assert source == "config_block"
    assert config["filament_settings_id"] == "Extrudr PLA NX2 Matt @0.8 nozzle"
    assert config["filament_type"] == "PLA"
    assert config["nozzle_diameter"] == "0.8"
    assert config["required_nozzle_HRC"] == "3"
    assert config["total filament used [g]"] == "103.88"
    assert config["estimated printing time (normal mode)"] == "1h 14m 55s"
    assert "some_other_setting" not in config


def test_plain_tail_fallback():
    config, source = qa_slicer.parse_config_block("; layer_height = 0.2\n; printer_model = X\n")
    assert source == "tail"
    assert config == {"layer_height": "0.2", "printer_model": "X"}


def test_no_config():
    assert qa_slicer.parse_config_block("G1 X1\nG1 X2\n") == ({}, "none")


def test_reads_only_the_tail(tmp_path):
    path = tmp_path / "big.gcode"
    path.write_text("; nozzle_diameter = 0.4\n" + ("G1 X1 Y1\n" * 50000) + ORCA_TAIL)
    result = qa_slicer.slicer_settings(str(path))
    assert result["source"] == "config_block"
    assert result["config"]["nozzle_diameter"] == "0.8"


def test_missing_file():
    assert qa_slicer.slicer_settings("/nonexistent/x.gcode")["source"] == "error"
