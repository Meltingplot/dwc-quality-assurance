"""The collector end to end: a real dsf-python model fed by DSF-style JSON patches, a real
database writer, and the timings of a job as DSF reports them."""

import json

import pytest

import qa_collector
import qa_db

from conftest import GCODE, make_model

def test_job_start_records_context_and_event(rig):
    rig.start_job()
    jobs = rig.rows("SELECT * FROM jobs")
    assert len(jobs) == 1
    job = jobs[0]
    assert job["result"] == "running" and job["file_name"] == "0:/gcodes/a.gcode"
    assert job["file_crc32"] and job["id"].endswith("-" + job["id"][-8:])
    context = qa_db.loads(job["context"])
    assert context["file"]["crc32"] == job["file_crc32"]
    assert context["versions"]["plugin"] == "test"
    assert [e["type"] for e in rig.events()] == ["job_start"]
    assert rig.collector.status()["currentJobId"] == job["id"]
    # the layer index is built in the background
    for _ in range(100):
        if rig.index.get(job["file_crc32"])[1] == "ready":
            break
        import time
        time.sleep(0.01)
    assert rig.index.get(job["file_crc32"])[0]["numLayers"] == 2


def test_context_names_the_accelerometers(rig):
    """DSF 3.7 lists them under sensors (qa_patches), not under boards any more."""
    rig.patch({"sensors": {"accelerometers": [{"orientation": 25, "port": "60.i2c.lis", "resolution": 14,
                                               "samplingRate": 800, "runs": 0, "points": 0}]}})
    rig.start_job()
    context = qa_db.loads(rig.rows("SELECT context FROM jobs")[0]["context"])
    assert context["accelerometers"] == [{"index": 0, "port": "60.i2c.lis", "board": 60, "orientation": 25,
                                          "samplingRate": 800, "resolution": 14}]
    assert "accelerometer" not in context["boards"][0]


def test_simulation_is_ignored(rig):
    rig.patch({"state": {"status": "simulating"}, "job": {"duration": 0, "file": {"fileName": "0:/gcodes/a.gcode"}}})
    rig.patch({"job": {"duration": 5}})
    rig.patch({"state": {"status": "idle"}, "job": {"duration": None}})
    assert rig.rows("SELECT * FROM jobs") == []


def test_outcome_waits_for_dsf_flags(rig):
    rig.start_job()
    rig.patch({"job": {"duration": 60}})
    rig.patch({"state": {"status": "idle"}, "job": {"duration": None}})  # RRF ended the job
    assert rig.rows("SELECT result FROM jobs")[0]["result"] == "running"
    rig.patch({"job": {"lastFileCancelled": True}})                   # DSF's flag arrives later
    job = rig.rows("SELECT * FROM jobs")[0]
    assert job["result"] == "cancelled"
    assert job["ended_at"] is not None
    summary = qa_db.loads(job["summary"])
    assert summary["result"] == "cancelled" and summary["abortReason"] == "user"
    assert rig.events("job_end")[0]["subtype"] == "cancelled"


def test_completed_after_grace(rig, monkeypatch):
    rig.start_job()
    rig.patch({"state": {"status": "idle"}, "job": {"duration": None}})
    now = [qa_collector.time.monotonic()]
    monkeypatch.setattr(qa_collector.time, "monotonic", lambda: now[0])
    now[0] += qa_collector.JOB_OUTCOME_GRACE_S + 1
    rig.tick()
    assert rig.rows("SELECT result FROM jobs")[0]["result"] == "completed"


def test_layers_samples_and_heater_load(rig):
    rig.start_job()
    for layer in (1, 2, 3):
        rig.patch({"job": {"layer": layer, "filePosition": layer * 10},
                   "move": {"axes": [{}, {}, {"machinePosition": 0.2 * layer}], "extruders": [{"position": 10.0 * layer}],
                            "currentMove": {"extrusionRate": 1.0}},
                   "heat": {"heaters": [{"current": 60}, {"current": 220, "avgPwm": 0.5}]},
                   "sensors": {"filamentMonitors": [{"lastPercentage": 95, "avgPercentage": 95, "totalExtrusion": 10.0 * layer,
                                                     "calibrated": {"mmPerRev": 25.0, "totalDistance": 10.0 * layer}}]}})
        for _ in range(20):
            rig.patch({"heat": {"heaters": [{}, {"avgPwm": 0.5}]}}, dt_ms=1000)
    layers = rig.rows("SELECT * FROM job_layers ORDER BY layer")
    assert [r["layer"] for r in layers] == [1, 2]  # layer 3 still printing
    first = layers[0]
    assert qa_db.loads(first["filament"])["0"]["ratio"] == pytest.approx(0.95)
    assert qa_db.loads(first["temps"])["heaters"]["1"]["mean"] == pytest.approx(220)
    assert qa_db.loads(first["load_stats"])["1"]["mean"] == pytest.approx(0.5)
    assert first["z"] == pytest.approx(0.2)
    coarse = rig.rows("SELECT COUNT(DISTINCT ts_ms) AS n FROM samples WHERE resolution=0")[0]["n"]
    assert 10 <= coarse <= 16  # every 5 s over ~64 s
    assert any(f["type"] == "sample" for f in rig.frames)
    assert any(f["type"] == "layer" for f in rig.frames)


def test_filament_status_event_with_block_and_return(rig):
    rig.start_job()
    for _ in range(5):
        rig.patch({"sensors": {"filamentMonitors": [{"lastPercentage": 100}]}})
    rig.patch({"sensors": {"filamentMonitors": [{"status": "tooLittleMovement", "lastPercentage": 20}]}})
    rig.patch({"sensors": {"filamentMonitors": [{"status": "ok", "lastPercentage": 95}]}}, dt_ms=4000)
    event = rig.events("filament_status")[0]
    assert event["subtype"] == "tooLittleMovement"
    assert event["payload"]["durationS"] == 4.0 and event["end_ms"] is not None
    blocks = rig.rows("SELECT * FROM blocks")
    assert len(blocks) == 1 and event["block_id"] == blocks[0]["id"]
    fine = rig.rows("SELECT COUNT(*) AS n FROM samples WHERE resolution=1 AND block_id=?", blocks[0]["id"])[0]["n"]
    assert fine > 20  # the ring buffer before the trigger plus what followed


def test_block_closes_after_post_trigger(rig):
    rig.start_job()
    rig.patch({"heat": {"heaters": [{}, {"current": 212}]}})  # an 8 K jump from 220? no: from 25
    for _ in range(40):
        rig.patch({"heat": {"heaters": [{}, {"current": 212.5}]}}, dt_ms=1000)
    block = rig.rows("SELECT * FROM blocks")[0]
    assert block["end_ms"] is not None
    assert "jump:heater.1.current" in json.dumps(qa_db.loads(block["triggers"]))


def test_percent_window_needs_min_duration(rig):
    rig.start_job()
    rig.patch({"sensors": {"filamentMonitors": [{"lastPercentage": 50}]}})
    rig.patch({"sensors": {"filamentMonitors": [{"lastPercentage": 100}]}}, dt_ms=2000)
    assert rig.events("filament_percent_window") == []
    rig.patch({"sensors": {"filamentMonitors": [{"lastPercentage": 200}]}})
    rig.tick(3000)
    rig.tick(3000)
    events = rig.events("filament_percent_window")
    assert len(events) == 1 and events[0]["subtype"] == "high"
    rig.patch({"sensors": {"filamentMonitors": [{"lastPercentage": 100}]}})
    assert rig.events("filament_percent_window")[0]["end_ms"] is not None


def test_mfm_correction_and_pause_cause(rig):
    rig.start_job()
    rig.patch({"global": {"mfm_error_count": 1}})
    rig.patch({"global": {"mfm_esteps_detected": True, "mfm_esteps_suggested": 812.5}})
    rig.patch({"move": {"extruders": [{"stepsPerMm": 812.5}]}, "global": {"mfm_esteps_baseline": 790.0}})
    rig.patch({"global": {"mfm_recovery_requested": True}})
    rig.patch({"state": {"status": "pausing"}})
    rig.patch({"state": {"status": "paused"}, "global": {"mfm_recovery_requested": False, "mfm_recovery_result": 0}})
    rig.patch({"state": {"status": "resuming"}}, dt_ms=30_000)
    rig.patch({"state": {"status": "processing"}})
    types = [e["type"] for e in rig.events()]
    assert types == ["job_start", "mfm_error_tolerated", "mfm_flow_bias", "setpoint_change", "mfm_recovery",
                     "pause", "mfm_recovery", "resume"]
    steps = rig.events("setpoint_change")[0]
    assert steps["subtype"] == "stepsPerMm" and steps["payload"]["cause"] == "mfm_flow_bias"
    assert steps["payload"]["from"] == 790.0 and steps["payload"]["to"] == 812.5
    pause = rig.events("pause")[0]
    assert pause["subtype"] == "filament_monitor" and pause["end_ms"] is not None
    assert rig.events("resume")[0]["payload"]["pausedS"] == 31.0


def test_pause_by_user_and_message_box(rig):
    rig.start_job()
    rig.patch({"state": {"status": "paused"}})
    rig.patch({"state": {"status": "processing"}})
    rig.patch({"state": {"status": "paused", "messageBox": {"title": "Nozzle check", "message": "?"}}}, dt_ms=20_000)
    causes = [e["subtype"] for e in rig.events("pause")]
    assert causes == ["user", "message: Nozzle check"]


def test_heater_fault_monitor_and_setpoints(rig):
    rig.start_job()
    rig.patch({"heat": {"heaters": [{"active": 70}, {"current": 301}]}})
    rig.patch({"heat": {"heaters": [{}, {"state": "fault", "current": 305}]}})
    rig.patch({"move": {"extruders": [{"pressAdv": {"k0": 0.06}}], "axes": [{}, {}, {"babystep": 0.02}]}})
    types = [(e["type"], e["subtype"]) for e in rig.events()]
    assert ("setpoint_change", "heater.active") in types
    assert ("heater_monitor", "tooHigh") in types
    assert ("heater_fault", None) in types
    assert ("setpoint_change", "pressAdv.k0") in types
    assert ("babystep", None) in types
    fault = rig.events("heater_fault")[0]
    assert fault["device"] == 1 and fault["tool"] == 0 and fault["layer"] is None


def test_heater_monitor_only_while_the_heater_regulates(rig):
    """The job end on a CHX 350: heaters off, then the CE default mode caps them (M143 S50 A2) while still hot"""
    rig.start_job()
    rig.patch({"heat": {"heaters": [{"state": "off", "active": 0}, {"state": "off", "active": 0, "current": 203.9}]}})
    rig.patch({"heat": {"heaters": [{"monitors": [{"condition": "tooHigh", "limit": 50, "action": 2, "sensor": -1}]},
                                    {"monitors": [{"condition": "tooHigh", "limit": 50, "action": 2, "sensor": -1}]}]}})
    assert rig.events("heater_monitor") == []
    rig.patch({"heat": {"heaters": [{}, {"state": "active", "active": 60}]}})  # switched on above its cap
    assert [(e["device"], e["payload"]["limit"]) for e in rig.events("heater_monitor")] == [(1, 50)]


def test_driver_status_bits_and_messages(rig):
    rig.start_job()
    rig.patch({"boards": [{"drivers": [{"status": 65536 | 2}, {}]}, {}]})     # over temperature shutdown
    rig.patch({"boards": [{"drivers": [{"status": 65536 | 2}, {}]}, {}]})     # unchanged: nothing
    rig.patch({"boards": [{"drivers": [{"status": 65536}, {"status": 65536 | (1 << 8)}]}, {}]})  # stall on driver 1
    rig.patch({"messages": [{"content": "Driver 20.0 warning: phase A may be disconnected", "type": 1,
                             "time": "2026-09-26T10:00:00"}]})
    events = rig.events("driver_error")
    assert [(e["subtype"], e["device"], e["payload"]["source"]) for e in events] == [
        ("error", 0, "status"), ("stall", 1, "status"), ("warning", 0, "message")]
    assert events[0]["payload"]["bits"] == ["over temperature shutdown"]
    assert events[2]["payload"]["canAddress"] == 20


def test_open_load_is_recorded_and_confirmed_after_500_ms(rig):
    """Job 20260928-075236-bddf0026: board 20's extruder driver flickered "phase A/B may be disconnected" in
    the raw status; the board raises its own event only after 500 ms. QA keeps every episode, unconfirmed
    until it lasted that long (Tim 2026-09-28: boards with many transients must show)."""
    rig.start_job()
    rig.patch({"move": {"currentMove": {"extrusionRate": 0.4, "topSpeed": 60}},
               "boards": [{}, {"drivers": [{"status": 65536 | 128}]}]}, dt_ms=100)   # phase B, transient
    rig.patch({"boards": [{}, {"drivers": [{"status": 65536}]}]}, dt_ms=300)
    transient = rig.events("driver_error")
    assert len(transient) == 1 and transient[0]["subtype"] == "warning"
    assert transient[0]["payload"]["confirmed"] is False and transient[0]["payload"]["canAddress"] == 20
    assert transient[0]["end_ms"] - transient[0]["ts_ms"] == 300 and transient[0]["block_id"] is None
    assert (transient[0]["payload"]["extrusionRate"], transient[0]["payload"]["topSpeed"]) == (0.4, 60)
    rig.patch({"boards": [{}, {"drivers": [{"status": 65536 | 64}]}]}, dt_ms=100)    # phase A, persists
    rig.patch({"boards": [{}, {"drivers": [{"status": 65536 | 128}]}]}, dt_ms=300)   # B takes over
    assert rig.events("driver_error")[1]["payload"]["confirmed"] is False
    rig.tick(3000)                                                                  # no patch: the heartbeat
    persistent = rig.events("driver_error")[1]
    assert persistent["payload"]["confirmed"] is True and persistent["block_id"] is not None
    assert persistent["payload"]["bits"] == ["phase A may be disconnected", "phase B may be disconnected"]
    assert persistent["end_ms"] is None                                             # still open
    rig.patch({"boards": [{}, {"drivers": [{"status": 65536}]}]}, dt_ms=100)
    assert rig.events("driver_error")[1]["end_ms"] is not None
    assert len(rig.events("driver_error")) == 2


def test_voltage_dip_and_phantom(rig):
    rig.start_job()
    for _ in range(10):
        rig.patch({"boards": [{"vIn": {"current": 24.0}}, {}]})
    rig.patch({"boards": [{"vIn": {"current": 20.0}}, {}]})
    rig.patch({"boards": [{"vIn": {"current": 24.0}}, {}]})
    dip = rig.events("voltage_dip")[0]
    assert dip["payload"]["median90s"] == 24.0 and dip["end_ms"] is not None
    rig.patch({"sensors": {"analog": [{}, {}, {"lastReading": 60.0}]}}, dt_ms=250)
    rig.patch({"sensors": {"analog": [{}, {}, {"lastReading": 28.5}]}}, dt_ms=250)
    phantom = rig.events("phantom_reading")[0]
    assert phantom["subtype"] == "sensor" and phantom["device"] == 2 and phantom["payload"]["peak"] == 60.0


def test_restart_mid_job_resumes_the_job(rig, writer, settings):
    rig.start_job()
    rig.patch({"job": {"duration": 30, "layer": 5}})
    job_id = rig.collector.job.id
    rig.collector.shutdown(rig.t)
    # a new daemon: new collector on the same database, the job is still running
    fresh = qa_collector.Collector(writer, settings, resolve_path=lambda v: str(rig.gcode))
    fresh.init_ids()
    fresh.update(rig.model, None, rig.t + 5000)
    assert fresh.job is not None and fresh.job.id == job_id
    assert rig.events("daemon_started_mid_job")[0]["payload"]["resumed"] is True
    assert len(rig.rows("SELECT * FROM jobs")) == 1


def test_daemon_start_during_unknown_job_is_partial(writer, settings, tmp_path):
    gcode = tmp_path / "b.gcode"
    gcode.write_text(GCODE)
    model = make_model({"state": {"status": "processing"},
                        "job": {"duration": 500, "layer": 12, "file": {"fileName": "0:/gcodes/b.gcode"}}})
    collector = qa_collector.Collector(writer, settings, resolve_path=lambda v: str(gcode))
    collector.init_ids()
    collector.update(model, None, 1_700_000_000_000)
    writer.flush()
    job = dict(qa_db.Readers(writer.directory).get().execute("SELECT * FROM jobs").fetchone())
    assert job["partial"] == 1 and job["start_layer"] == 12


def test_stale_running_job_becomes_unknown(writer, settings):
    key = writer.call("job_insert", {"id": "old", "file_name": "0:/gcodes/x.gcode", "started_at": 1})
    writer.submit("samples", key, [(5000, "a", 1.0)], qa_db.RESOLUTION_COARSE, urgent=True)
    collector = qa_collector.Collector(writer, settings)
    collector.init_ids()
    collector.update(make_model(), None, 1_700_000_000_000)
    writer.flush()
    row = qa_db.Readers(writer.directory).get().execute("SELECT result, ended_at FROM jobs").fetchone()
    assert (row["result"], row["ended_at"]) == ("unknown", 5000)


def test_decode_driver_status():
    assert qa_collector.decode_driver_status(65536 | 1 | 256) == ["over temperature warning", "motor stall", "standstill"]


def test_job_id_format():
    assert qa_collector.job_id(0, "0:/gcodes/a.gcode", "deadbeef").startswith("19700101-000000-")
    assert len(qa_collector.job_id(0, "a", None).split("-")[-1]) == 8
