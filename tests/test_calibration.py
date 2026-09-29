"""Bed and probe calibration per job (qa_calibration): values from the real dsf-python model, the height
map file and the two replies the model lacks. The numbers are the CHX 350's print_start of 2026-09-29
(G32 twice, M558.2, M558.1, G29 with 840 points; Tim 2026-09-29)."""

import os

import qa_calibration
import qa_collector
import qa_db

from conftest import make_model

T0 = 1_700_000_000_000

MESH = {"type": "mesh", "file": "0:/sys/heightmap.csv", "fadeHeight": 5,
        "liveGrid": {"axes": ["X", "Y"], "mins": [23.2, 12.3], "maxs": [856.8, 409.7], "spacings": [21.4, 19.9],
                     "radius": -1},
        "meshDeviation": {"mean": -0.121, "deviation": 0.145}}
NO_MESH = {"type": "none", "file": None, "liveGrid": None, "meshDeviation": None}   # G29 S2
LEVELLING = {"calibration": {"initial": {"mean": -0.319, "deviation": 0.128}, "final": {"mean": 0, "deviation": 0},
                             "numFactors": 4},
             "kinematics": {"name": "coreXYU", "tiltCorrection": {"lastCorrections": [-0.103, -0.529, -0.213, -0.429]}}}
LEVELLING_2 = {"calibration": {"initial": {"mean": -0.00742, "deviation": 0.021},
                               "final": {"mean": 2.563e-19, "deviation": 4.258e-18}, "numFactors": 4},
               "kinematics": {"tiltCorrection": {"lastCorrections": [-0.00721, -0.0345, 0.0354, -0.0249]}}}
PROBE = {"type": 11, "isCalibrated": True, "threshold": 8031, "triggerHeight": 3, "calibrationTemperature": 25,
         "temperatureCoefficients": [0, 0],
         "scanCoefficients": [-0.006, -2.339e-3, 1.195e-6, -4.462e-10]}
PROBE_2 = {"threshold": 8924, "scanCoefficients": [-0.006131041, -0.002150733, 1.0283316e-06, -4.4146634e-10]}
FIT_REPLY = ("Scanning probe offset: -0.006mm, A: -2.151e-3, B: 1.028e-6, C: -4.415e-10, "
             "reading at trigger height 8924, rms error 0.003mm")
DRIVE_REPLY = "Calibration successful, sensor drive current is 16, offset is 133919"


def height_map(deviation=0.145, stats="min error -0.492, max error 0.200, mean -0.121"):
    """A 3 × 2 map as RRF 3.7 writes it (Grid.cpp:367-420), one point not probed"""
    return (f"RepRapFirmware height map file v2 generated at 2026-09-29 10:01, {stats}, deviation {deviation:.3f}\n"
            "axis0,axis1,min0,max0,min1,max1,radius,spacing0,spacing1,num0,num1\n"
            "X,Y,23.20,856.80,12.30,409.70,-1.00,416.80,397.40,3,2\n"
            " -0.115, -0.120,      0\n"
            " -0.492,  0.000,  0.200\n")


def message(content):
    return {"messages": [{"content": content, "type": 0, "time": "2026-09-29T11:00:00"}]}


def write_map(path, text, mtime_ms):
    path.write_text(text)
    os.utime(path, (mtime_ms / 1000, mtime_ms / 1000))


def test_read_height_map(tmp_path):
    path = tmp_path / "heightmap.csv"
    path.write_text(height_map())
    info = qa_calibration.read_height_map(str(path))
    assert info == {
        "generatedAt": "2026-09-29 10:01", "minError": -0.492, "maxError": 0.2, "mean": -0.121, "deviation": 0.145,
        "grid": {"axes": ["X", "Y"], "mins": [23.2, 12.3], "maxs": [856.8, 409.7], "spacings": [416.8, 397.4],
                 "radius": -1.0, "nums": [3, 2]},
        "points": 5, "heights": [[-0.115, -0.12, None], [-0.492, 0.0, 0.2]]}
    path.write_text("RepRapFirmware height map file v1\nxmin,xmax,ymin,ymax,radius,spacing,xnum,ynum\n")
    assert qa_calibration.read_height_map(str(path)) is None


def machine_rig(rig, tmp_path):
    heightmap = tmp_path / "heightmap.csv"
    rig.collector.resolve_path = lambda v: str(heightmap) if v == "0:/sys/heightmap.csv" else str(rig.gcode)
    return heightmap


def context(rig):
    return qa_db.loads(rig.rows("SELECT context FROM jobs")[0]["context"])["calibration"]


def test_context_holds_the_calibration_from_before_the_job(writer, settings, tmp_path):
    """What the machine had when QA started has no time; the mesh's file fills in min, max and heights."""
    heightmap = tmp_path / "heightmap.csv"
    write_map(heightmap, height_map(), T0 - 3_600_000)
    gcode = tmp_path / "a.gcode"
    gcode.write_text("G1 X1\n")
    model = make_model({"move": {"compensation": MESH, **LEVELLING}, "sensors": {"probes": [PROBE]}})
    collector = qa_collector.Collector(writer, settings, plugin_version="test",
                                       resolve_path=lambda v: str(heightmap) if v.startswith("0:/sys") else str(gcode))
    collector.init_ids()
    collector.update(model, None, T0)
    model.update_from_json({"state": {"status": "processing"}, "job": {"duration": 0, "file": {"fileName": "0:/gcodes/a.gcode"}}})
    collector.update(model, {}, T0 + 1000)
    writer.flush()
    row = qa_db.Readers(writer.directory).get().execute("SELECT context FROM jobs").fetchone()
    cal = qa_db.loads(row["context"])["calibration"]
    assert cal["mesh"] == {"file": "0:/sys/heightmap.csv", "fadeHeight": 5.0, "mean": -0.121, "deviation": 0.145,
                           "grid": {"axes": ["X", "Y"], "mins": [23.2, 12.3], "maxs": [856.8, 409.7],
                                    "spacings": [416.8, 397.4], "radius": -1.0, "nums": [3, 2]},
                           "ts": None, "minError": -0.492, "maxError": 0.2, "points": 5, "generatedAt": "2026-09-29 10:01",
                           "heights": [[-0.115, -0.12, None], [-0.492, 0.0, 0.2]]}
    assert cal["levelling"] == {"numFactors": 4, "before": {"mean": -0.319, "deviation": 0.128},
                                "after": {"mean": 0.0, "deviation": 0.0}, "corrections": [-0.103, -0.529, -0.213, -0.429],
                                "ts": None}
    assert cal["probes"] == [{"index": 0, "type": 11, "isCalibrated": True,
                              "scanCoefficients": [-0.006, -2.339e-3, 1.195e-6, -4.462e-10], "threshold": 8031,
                              "triggerHeight": 3.0, "calibrationTemperature": 25.0, "temperatureCoefficients": [0.0, 0.0],
                              "ts": None}]
    assert cal["probeDrive"] is None


def test_print_start_calibrations_are_events_and_context(rig, tmp_path):
    """The CHX 350's print_start: bed.g clears the mesh (G29 S2), levels twice (G32), M558.2 and M558.1
    calibrate the probe, G29 probes and then writes the height map."""
    heightmap = machine_rig(rig, tmp_path)
    write_map(heightmap, height_map(deviation=0.151), T0 - 86_400_000)   # yesterday's
    rig.patch({"move": {"compensation": {**MESH, "meshDeviation": {"mean": -0.1, "deviation": 0.151}}},
               "sensors": {"probes": [PROBE]}})
    rig.tick(5000)                                       # a loaded mesh: its older file is taken after 5 s
    rig.start_job()
    assert context(rig)["mesh"]["points"] == 5          # yesterday's mesh, from before the job
    rig.patch({"move": {"compensation": NO_MESH}})       # G29 S2: no event, the job has no mesh for now
    assert rig.events("calibration") == [] and context(rig)["mesh"] is None
    rig.patch({"move": LEVELLING})
    rig.patch({"move": LEVELLING_2})
    rig.patch(message(DRIVE_REPLY))
    rig.patch({"sensors": {"probes": [PROBE_2]}})        # the model before the reply
    rig.patch(message(FIT_REPLY))
    rig.patch({"move": {"compensation": MESH}})          # G29: the model first, the old file still there
    mesh_ms = rig.t
    assert "points" not in rig.events("calibration")[-1]["payload"]
    write_map(heightmap, height_map(), mesh_ms + 400)
    rig.tick(1000)

    events = rig.events("calibration")
    assert [(e["subtype"], e["device"]) for e in events] == [
        ("levelling", None), ("levelling", None), ("probeDrive", None), ("probe", 0), ("mesh", None)]
    assert all(e["block_id"] is None for e in events)
    first, second = events[0]["payload"], events[1]["payload"]
    assert (first["corrections"], first["before"], first["after"]) == (
        [-0.103, -0.529, -0.213, -0.429], {"mean": -0.319, "deviation": 0.128}, {"mean": 0.0, "deviation": 0.0})
    assert second["corrections"] == [-0.00721, -0.0345, 0.0354, -0.0249] and second["after"]["deviation"] == 4.258e-18
    assert events[2]["payload"] == {"current": 16, "offset": 133919, "ts": events[2]["ts_ms"]}
    probe = events[3]["payload"]
    # 6 significant digits: dsf-python makes json.dumps write floats with %g (object_model/model_object.py:9-17)
    assert (probe["threshold"], probe["scanCoefficients"], probe["rmsError"]) == (
        8924, [-0.00613104, -0.00215073, 1.02833e-06, -4.41466e-10], 0.003)
    mesh = events[4]["payload"]
    assert (mesh["mean"], mesh["deviation"], mesh["minError"], mesh["maxError"], mesh["points"], mesh["ts"]) == (
        -0.121, 0.145, -0.492, 0.2, 5, mesh_ms)
    assert "heights" not in mesh

    cal = context(rig)
    assert cal["mesh"]["heights"] == [[-0.115, -0.12, None], [-0.492, 0.0, 0.2]] and cal["mesh"]["ts"] == mesh_ms
    assert cal["levelling"]["corrections"] == [-0.00721, -0.0345, 0.0354, -0.0249]
    assert cal["probes"][0]["rmsError"] == 0.003 and cal["probes"][0]["threshold"] == 8924
    assert cal["probeDrive"]["current"] == 16
    assert rig.collector.calibration.tick(rig.t + 60_000) == []   # nothing pending any more


def test_a_fit_reply_before_the_model_and_a_loaded_mesh(rig, tmp_path):
    """The M558.1 reply may come first; G29 S1 loads a file older than the change."""
    heightmap = machine_rig(rig, tmp_path)
    rig.patch({"sensors": {"probes": [PROBE]}})
    rig.start_job()
    rig.patch(message(FIT_REPLY))
    rig.patch({"sensors": {"probes": [PROBE_2]}})
    assert rig.events("calibration")[0]["payload"]["rmsError"] == 0.003
    write_map(heightmap, height_map(), T0 - 86_400_000)
    rig.patch({"move": {"compensation": MESH}})
    rig.tick(1000)
    assert "points" not in rig.events("calibration")[-1]["payload"]     # it might still be rewritten
    rig.tick(5000)
    assert rig.events("calibration")[-1]["payload"]["points"] == 5


def test_a_height_map_that_does_not_match_is_left_out(rig, tmp_path):
    heightmap = machine_rig(rig, tmp_path)
    rig.start_job()
    write_map(heightmap, height_map(deviation=0.2), T0 + 60_000)
    rig.patch({"move": {"compensation": MESH}})
    for _ in range(12):
        rig.tick(3000)
    mesh = rig.events("calibration")[0]["payload"]
    assert (mesh["deviation"], "points" in mesh) == (0.145, False)
    assert rig.collector.calibration._pending is None


def test_calibrations_between_jobs_reach_the_next_context(rig, tmp_path):
    machine_rig(rig, tmp_path)
    rig.patch({"move": LEVELLING})
    rig.patch(message(DRIVE_REPLY))
    levelled_ms = rig.t - 1000
    rig.start_job()
    cal = context(rig)
    assert cal["levelling"]["ts"] == levelled_ms and cal["probeDrive"]["offset"] == 133919
    assert rig.events("calibration") == []
