"""Shared fixtures: a real dsf-python 3.7.0b1 object model (with QA's patches) fed by JSON, and a
database writer on a temporary directory."""

import copy
import json

import pytest

import qa_patches

qa_patches.apply()

from dsf.object_model import ObjectModel  # noqa: E402

import qa_collector  # noqa: E402
import qa_db  # noqa: E402
import qa_gcode  # noqa: E402
import qa_settings  # noqa: E402

# A CHX 350-like machine: bed heater 0, nozzle heater 1 (tool 0), analog sensors bed, nozzle and
# "SZP coil" (the chamber reading), a rotating-magnet monitor on extruder 0, a main board and a
# tool board, the MFM globals of chx350-config.
BASE_MODEL = {
    "boards": [
        {"canAddress": 0, "name": "Duet 3 MB6HC", "shortName": "MB6HC", "firmwareVersion": "3.7.0",
         "vIn": {"current": 24.0, "min": 23.8, "max": 24.2}, "v12": {"current": 12.1, "min": 12, "max": 12.2},
         "mcuTemp": {"current": 40.0, "min": 30, "max": 41}, "drivers": [{"status": 65536}, {"status": 65536}]},
        {"canAddress": 20, "name": "Duet 3 Toolboard 1LC", "shortName": "TOOL1LC", "firmwareVersion": "3.7.0",
         "vIn": {"current": 24.0}, "mcuTemp": {"current": 35.0}, "drivers": [{"status": 0}]},
    ],
    "fans": [{"actualValue": 0.0, "requestedValue": 0.0, "rpm": -1, "name": "part"}],
    "global": {
        "mfm_error_count": 0, "mfm_error_time": 0, "mfm_error_start_pos": None, "mfm_ignore_events": False,
        "mfm_recovery_requested": False, "mfm_recovery_result": -1, "mfm_recovery_resume_time": 0,
        "mfm_esteps_detected": False, "mfm_esteps_drift_since": 0, "mfm_esteps_drift_avg": 0,
        "mfm_esteps_suggested": 0, "mfm_esteps_baseline": 0.0, "mfm_suppress_until": 0,
        "nozzle_diameter": [0.8, 0.6], "filament_diameter": [2.85, 2.85], "spool_remaining": [900.0, 0.0],
        "machine_mode": "default", "door_left_switch_checked": 1431655765,
    },
    "heat": {
        "bedHeaterMapping": [[0]], "chamberHeaterMapping": [],
        "heaters": [
            {"active": 0, "standby": 0, "avgPwm": 0, "current": 25.0, "state": "off", "sensor": 0,
             "model": {"maxPwm": 1.0, "heatingRate": 0.5, "pid": {"p": 10, "i": 0.1, "d": 50, "used": True}},
             "monitors": [{"condition": "tooHigh", "limit": 130, "action": 0, "sensor": -1}]},
            {"active": 0, "standby": 0, "avgPwm": 0, "current": 25.0, "state": "off", "sensor": 1,
             "model": {"maxPwm": 1.0, "heatingRate": 3.0, "pid": {"p": 20, "i": 0.5, "d": 10, "used": True}},
             "monitors": [{"condition": "tooHigh", "limit": 300, "action": 0, "sensor": -1}]},
        ],
    },
    "job": {"duration": None, "layer": None, "lastFileCancelled": False, "lastFileAborted": False,
            "file": {"fileName": "", "size": 0, "numLayers": 0}, "build": {"currentObject": -1, "objects": []},
            "layers": []},
    "move": {
        "workplaceNumber": 0, "speedFactor": 1.0,
        "axes": [
            {"letter": "X", "machinePosition": 0.0, "userPosition": 0.0, "workplaceOffsets": [0, 0], "babystep": 0},
            {"letter": "Y", "machinePosition": 0.0, "userPosition": 0.0, "workplaceOffsets": [0, 0], "babystep": 0},
            {"letter": "Z", "machinePosition": 0.0, "userPosition": 0.0, "workplaceOffsets": [0, 0], "babystep": 0},
        ],
        "extruders": [{"position": 0.0, "rawPosition": 0.0, "factor": 1.0, "stepsPerMm": 790.0, "filament": "PLA",
                       "filamentDiameter": 2.85, "pressAdv": {"k0": 0.04, "k1": 0.0, "d": None},
                       "nonlinear": {"a": 0, "b": 0, "upperLimit": 0.2}}],
        "currentMove": {"topSpeed": 0, "requestedSpeed": 0, "extrusionRate": 0},
        "shaping": {"type": "mzv", "frequency": 42.0, "damping": 0.1},
    },
    "sensors": {
        "analog": [{"name": "bed", "lastReading": 25.0}, {"name": "nozzle", "lastReading": 25.0},
                   {"name": "SZP coil", "lastReading": 28.0}],
        "filamentMonitors": [{
            "type": "rotatingMagnet", "status": "ok", "enableMode": 1, "lastPercentage": None,
            "avgPercentage": None, "minPercentage": None, "maxPercentage": None, "position": 0,
            "totalExtrusion": 0.0, "agc": 110, "calibrated": None,
            "configured": {"allMoves": False, "mmPerRev": 25.3, "percentMin": 60, "percentMax": 180,
                           "sampleDistance": 5}}],
    },
    "state": {"status": "idle", "currentTool": 0, "upTime": 1000, "messageBox": None},
    "tools": [{"number": 0, "name": "T0", "heaters": [1], "extruders": [0], "filamentExtruder": 0,
               "active": [0], "standby": [0],
               # the CHX 350's M207 with PLA NX2 matt (object model, 2026-09-28)
               "retraction": {"length": 0.4, "extraRestart": 0, "speed": 20.8, "unretractSpeed": 14, "zHop": 0}}],
    "sbc": {"dsf": {"version": "3.7.0-rc.2+mp.6"}},
    "plugins": {"QualityAssurance": {"id": "QualityAssurance", "pid": 1234}},
}


def make_model(overrides=None):
    data = copy.deepcopy(BASE_MODEL)
    om = ObjectModel()
    om.update_from_json(json.loads(json.dumps(data)))
    if overrides:
        om.update_from_json(json.loads(json.dumps(overrides)))
    return om


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    directory = tmp_path / "qa"
    directory.mkdir()
    monkeypatch.setenv("QA_DATA_DIR", str(directory))
    return str(directory)


@pytest.fixture
def settings(data_dir):
    s = qa_settings.Settings(data_dir)
    s.load()
    return s


@pytest.fixture
def writer(data_dir):
    w = qa_db.Writer(data_dir, commit_interval_s=30)
    w.start()
    yield w
    w.stop()


@pytest.fixture
def readers(data_dir, writer):
    return qa_db.Readers(data_dir)


GCODE = ";LAYER_CHANGE\nG1 Z0.2\nG1 X10 Y10 E1 F1200\n;LAYER_CHANGE\nG1 Z0.4\nG1 X20 Y20 E1\n"


class Rig:
    def __init__(self, writer, settings, tmp_path, data_dir):
        self.writer = writer
        self.gcode = tmp_path / "a.gcode"
        self.gcode.write_text(GCODE)
        self.frames = []
        self.index = qa_gcode.IndexCache(data_dir)
        self.collector = qa_collector.Collector(
            writer, settings, resolve_path=lambda v: str(self.gcode) if v == "0:/gcodes/a.gcode" else None,
            broadcast=self.frames.append, plugin_version="test", index_cache=self.index)
        self.collector.init_ids()
        self.model = make_model()
        self.t = 1_700_000_000_000
        self.collector.update(self.model, None, self.t)

    def patch(self, data, dt_ms=1000):
        self.t += dt_ms
        patch = json.loads(json.dumps(data))
        self.model.update_from_json(patch)
        self.collector.update(self.model, patch, self.t)

    def tick(self, dt_ms=3000):
        self.t += dt_ms
        self.collector.tick(self.t)

    def rows(self, sql, *args):
        self.writer.flush()
        return [dict(r) for r in qa_db.Readers(self.writer.directory).get().execute(sql, args)]

    def events(self, type_=None):
        rows = self.rows("SELECT * FROM events ORDER BY id")
        for row in rows:
            row["payload"] = qa_db.loads(row["payload"])
        return [r for r in rows if type_ is None or r["type"] == type_]

    def start_job(self):
        self.patch({"state": {"status": "processing"},
                    "job": {"duration": 0, "file": {"fileName": "0:/gcodes/a.gcode", "size": len(GCODE), "numLayers": 2}},
                    "heat": {"heaters": [{"active": 60, "state": "active"}, {"active": 220, "state": "active"}]},
                    "tools": [{"active": [220]}]})


@pytest.fixture
def rig(writer, settings, tmp_path, data_dir):
    return Rig(writer, settings, tmp_path, data_dir)
