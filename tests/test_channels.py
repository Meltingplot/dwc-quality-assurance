"""Channel extraction and context from a real dsf-python object model."""

import pytest

import qa_channels
import qa_context
import qa_settings

from conftest import make_model

CFG = qa_settings.DEFAULTS


def test_extract_reads_every_channel():
    model = make_model({
        "heat": {"heaters": [{}, {"active": 220, "current": 219.5, "avgPwm": 0.47, "state": "active"}]},
        "sensors": {"filamentMonitors": [{"lastPercentage": 98, "avgPercentage": 97, "totalExtrusion": 100.5,
                                          "calibrated": {"mmPerRev": 25.1, "totalDistance": 98.0}}]},
        "move": {"currentMove": {"extrusionRate": 1.2}},
        "fans": [{"actualValue": 0.5, "rpm": 3000}],
        "global": {"mfm_error_count": 1},
    })
    ch = qa_channels.extract(model)
    assert ch["heater.1.current"] == 219.5
    assert ch["heater.1.avgPwm"] == 0.47
    assert ch["heater.1.state"] == qa_channels.HEATER_STATES["active"]
    assert ch["heater.0.state"] == qa_channels.HEATER_STATES["off"]
    assert ch["fm.0.lastPercentage"] == 98
    assert ch["fm.0.agc"] == 110
    assert ch["fm.0.calibrated.mmPerRev"] == pytest.approx(25.1)
    assert ch["fm.0.calibrated.totalDistance"] == 98.0
    assert ch["fm.0.status"] == 0
    assert ch["axis.Z.machinePosition"] == 0.0
    assert ch["board.0.vIn"] == 24.0
    assert ch["board.0.v12"] == pytest.approx(12.1)
    assert ch["board.1.mcuTemp"] == 35.0
    assert ch["sensor.2.lastReading"] == 28.0
    assert ch["move.currentMove.extrusionRate"] == pytest.approx(1.2)
    assert ch["fan.0.rpm"] == 3000
    assert ch["global.mfm_error_count"] == 1.0
    assert ch["global.mfm_esteps_detected"] == 0.0
    assert "board.1.v12" not in ch


def test_no_tacho_no_rpm_channel():
    assert "fan.0.rpm" not in qa_channels.extract(make_model())


def test_positions_and_workplace():
    model = make_model({"move": {"workplaceNumber": 1, "axes": [
        {"machinePosition": 12.5, "workplaceOffsets": [0, 5]}, {"machinePosition": 7}, {"machinePosition": 0.4}]}})
    pos, workplace, offsets = qa_channels.positions(model)
    assert pos == {"X": 12.5, "Y": 7.0, "Z": 0.4}
    assert workplace == 1
    assert offsets == {"X": 5.0, "Y": 0.0, "Z": 0.0}


def test_heater_roles():
    model = make_model()
    assert qa_channels.nozzle_heaters(model) == {1: 0}
    assert qa_channels.bed_heaters(model) == [0]
    assert qa_channels.chamber_source(model, CFG) == ("sensor", 2)
    assert qa_channels.chamber_channel(model, CFG) == "sensor.2.lastReading"


def test_chamber_heater_wins_and_override():
    model = make_model({"heat": {"chamberHeaterMapping": [[0]]}})
    assert qa_channels.chamber_source(model, CFG) == ("heater", 0)
    cfg = {**CFG, "chamber": {"mode": "sensor", "index": 1, "autoSensorName": "SZP coil"}}
    assert qa_channels.chamber_source(model, cfg) == ("sensor", 1)


def test_nozzle_named_after_single_nozzle_tool():
    model = make_model({"tools": [{"number": 0, "heaters": [1, 2]}, {"number": 1, "heaters": [2], "extruders": [1]},
                                  {"number": 2, "heaters": [1], "extruders": [0]}]})
    # tools sorted by heater count: T1 (heater 2), T2 (heater 1), then T0
    assert qa_channels.nozzle_heaters(model) == {2: 1, 1: 2}


def test_context_snapshot(tmp_path):
    gcode = tmp_path / "a.gcode"
    gcode.write_text("G1 X1\n; CONFIG_BLOCK_START\n; filament_settings_id = \"PLA @0.8\"\n"
                     "; nozzle_diameter = 0.8\n; CONFIG_BLOCK_END\n")
    model = make_model({"job": {"file": {"fileName": "0:/gcodes/a.gcode", "numLayers": 100, "size": 90,
                                         "filament": [1234.5], "generatedBy": "OrcaSlicer 2.3"}}})
    crc = qa_context.file_crc32(str(gcode))
    ctx = qa_context.snapshot(model, CFG, "0.1.0", str(gcode), crc)
    assert ctx["file"]["crc32"] == crc and len(crc) == 8
    assert ctx["file"]["numLayers"] == 100
    assert ctx["slicer"]["config"]["filament_settings_id"] == "PLA @0.8"
    assert ctx["material"] == "PLA @0.8"
    assert ctx["extruders"][0]["pressAdv"] == {"k0": 0.04, "k1": 0.0, "d": None}
    assert ctx["filamentMonitors"][0]["configured"]["mmPerRev"] == 25.3
    assert ctx["filamentMonitors"][0]["calibrated"] is None
    assert [h["role"] for h in ctx["heaters"]] == ["bed", "nozzle"]
    assert ctx["heaters"][1]["model"]["maxPwm"] == 1.0
    assert ctx["chamber"] == ("sensor", 2)
    assert ctx["shaping"]["type"] == "mzv"
    assert ctx["versions"] == {"firmware": "3.7.0", "dsf": "3.7.0-rc.2+mp.6", "plugin": "0.1.0"}
    assert ctx["globalsStart"]["spool_remaining"] == [900.0, 0.0]
    assert ctx["globalsStart"]["nozzle_diameter"] == [0.8, 0.6]
    assert ctx["boards"][1]["canAddress"] == 20
