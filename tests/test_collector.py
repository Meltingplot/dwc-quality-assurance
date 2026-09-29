"""The collector end to end: a real dsf-python model fed by DSF-style JSON patches, a real
database writer, and the timings of a job as DSF reports them."""

import json
import threading

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
    assert context["tools"][0]["retraction"] == {"length": 0.4, "extraRestart": 0, "speed": 20.8,
                                                 "unretractSpeed": 14, "zHop": 0}
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



def test_jump_across_a_null_reading(rig):
    """RRF reports lastPercentage as null while the monitor has no live data. The drop 91 → 64 %
    across such a patch (L140 of job 20260928-155257-118609a9) opens a block; a value older than
    ringBufferS is no base."""
    def close_block():   # postTriggerS after its last trigger
        for _ in range(35):
            rig.patch({}, dt_ms=1000)

    rig.start_job()
    rig.patch({"sensors": {"filamentMonitors": [{"lastPercentage": 91}]}})
    close_block()        # the job_start block
    rig.patch({"sensors": {"filamentMonitors": [{"lastPercentage": None}]}})
    assert "fm.0.lastPercentage" not in rig.collector._current
    rig.patch({"sensors": {"filamentMonitors": [{"lastPercentage": 64}]}})
    close_block()
    rig.patch({"sensors": {"filamentMonitors": [{"lastPercentage": None}]}})
    rig.patch({}, dt_ms=91_000)
    rig.patch({"sensors": {"filamentMonitors": [{"lastPercentage": 91}]}})
    blocks = [[t["reason"] for t in qa_db.loads(b["triggers"])] for b in rig.rows("SELECT * FROM blocks ORDER BY id")]
    assert blocks == [["job_start"], ["jump:fm.0.lastPercentage"]]


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



def test_level_event_for_a_drop_inside_the_window(rig):
    """As at L140 of job 20260928-155257-118609a9: the monitor read about 90 %, then 64 %, above
    percentMin 60, so no window event; its own level in the job flags the drop. A null reading does
    not end it."""
    def reading(pct, dt_ms=1000):
        rig.patch({"sensors": {"filamentMonitors": [{"lastPercentage": pct}]}}, dt_ms=dt_ms)

    rig.start_job()
    reading(90)
    for _ in range(40):          # 200 s of readings: no level yet (filamentLevelMinS 300)
        reading(90, 5000)
    reading(64)
    rig.tick(3000)
    rig.tick(3000)
    assert rig.events("filament_percent_level") == []
    reading(90)
    for _ in range(25):
        reading(90, 5000)
    reading(64)
    rig.tick(3000)
    reading(None)
    rig.tick(3000)
    reading(41)
    events = rig.events("filament_percent_level")
    assert [(e["subtype"], e["device"], e["payload"]["level"], e["payload"]["threshold"]) for e in events] == [
        ("low", 0, 91.0, 20)]
    assert events[0]["end_ms"] is None and events[0]["block_id"] is not None
    reading(88)
    event = rig.events("filament_percent_level")[0]
    assert event["end_ms"] is not None
    assert (event["payload"]["extreme"], event["payload"]["returnedTo"], event["payload"]["durationS"]) == (41, 88, 9.0)
    assert rig.events("filament_percent_window") == []



def test_drift_event_for_a_slow_drop_over_layers(rig):
    """As in job 20260928-134928-118609a9 (L136-150: 91 → 79 %, never a jump): layers whose monitor
    mean stays 6 points or more below its level, three in a row, are one filament_percent_drift event
    from the first one's start to the start of the first layer back; two such layers are none."""
    def layer(n, pct, seconds=100):
        rig.patch({"job": {"layer": n}, "sensors": {"filamentMonitors": [{"lastPercentage": pct}]}})
        for _ in range(seconds // 5):
            rig.patch({"sensors": {"filamentMonitors": [{"lastPercentage": pct}]}}, dt_ms=5000)

    rig.start_job()
    for n, pct in enumerate((90, 90, 90, 90, 82, 82, 90, 83, 84, 82, 80, 90, 90), 1):
        layer(n, pct)
    layer(14, 90)                # finishes layer 13
    events = rig.events("filament_percent_drift")
    assert len(events) == 1
    event = events[0]
    started = {r["layer"]: r["started_at"] for r in rig.rows("SELECT layer, started_at FROM job_layers")}
    assert (event["subtype"], event["layer"], event["ts_ms"], event["block_id"]) == ("low", 8, started[8], None)
    payload = event["payload"]
    assert (payload["level"], payload["threshold"], payload["firstLayer"], payload["lastLayer"], payload["layers"],
            payload["extreme"]) == (91.0, 6, 8, 11, 4, 80.0)
    assert event["end_ms"] == started[12] and payload["durationS"] == (started[12] - started[8]) / 1000


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
    rig.patch({"tools": [{"retraction": {"length": 0.8}}]})   # M207 S0.8, e.g. by a filament's config
    types = [(e["type"], e["subtype"]) for e in rig.events()]
    assert ("setpoint_change", "heater.active") in types
    assert ("heater_monitor", "tooHigh") in types
    assert ("heater_fault", None) in types
    assert ("setpoint_change", "pressAdv.k0") in types
    assert ("babystep", None) in types
    retraction = [e for e in rig.events("setpoint_change") if e["subtype"] == "tool.retraction"]
    assert [(e["device"], e["payload"]["from"]["length"], e["payload"]["to"]["length"]) for e in retraction] == [
        (0, 0.4, 0.8)]
    fault = rig.events("heater_fault")[0]
    assert fault["device"] == 1 and fault["tool"] == 0 and fault["layer"] is None



def test_input_shaping_and_microstepping_are_setpoints(rig):
    """M593 P"mzv" F32.8 S0.07 and M350, e.g. from a filament config or print_start, are setpoint changes
    during a job; the context holds both as at the job start (Tim 2026-09-29)."""
    rig.start_job()
    context = qa_db.loads(rig.rows("SELECT context FROM jobs")[0]["context"])
    assert context["shaping"] == {"type": "mzv", "frequency": 42.0, "damping": 0.1, "amplitudes": [], "delays": []}
    assert [a["microstepping"] for a in context["axes"]] == [{"value": 16, "interpolated": False}] * 3
    assert context["extruders"][0]["microstepping"] == {"value": 16, "interpolated": False}
    rig.patch({"move": {"shaping": {"frequency": 32.8, "damping": 0.07}}})
    rig.patch({"move": {"axes": [{}, {"microstepping": {"value": 64, "interpolated": False}}],
                        "extruders": [{"microstepping": {"value": 16, "interpolated": True}}]}})
    changes = {(e["subtype"], e["payload"]["index"]): e for e in rig.events("setpoint_change")}
    shaping = changes[("shaping", None)]
    assert (shaping["device"], shaping["payload"]["from"]["frequency"], shaping["payload"]["to"]) == (
        None, 42.0, {"type": "mzv", "frequency": 32.8, "damping": 0.07, "amplitudes": [], "delays": []})
    assert changes[("axis.microstepping", "Y")]["payload"]["to"] == {"value": 64, "interpolated": False}
    extruder = changes[("extruder.microstepping", 0)]
    assert (extruder["device"], extruder["payload"]["from"]["interpolated"], extruder["payload"]["to"]["interpolated"]) == (
        0, False, True)
    assert ("axis.microstepping", "X") not in changes


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



# --- feed factor ------------------------------------------------------------------------------------

def feed_layers(rig):
    return {r["layer"]: qa_db.loads(r["feed"]) for r in rig.rows("SELECT layer, feed FROM job_layers")}


def test_feed_factor_relative_to_the_first_feeding(rig):
    """The start G-code sets the filament's e-steps after the context snapshot (790 → 801); the MFM
    raises them during layer 2 (801 → 834.38, as in job 20260928-155257-118609a9), M221 90 % in layer 3."""
    rig.start_job()
    for data in ({"move": {"extruders": [{"stepsPerMm": 801.0}]}},       # print_start: the filament's M92
                 {"move": {"extruders": [{"position": 20.0}]}},          # purge line, no layer yet
                 {"job": {"layer": 1}}, {"move": {"extruders": [{"position": 30.0}]}},
                 {"job": {"layer": 2}}, {"move": {"extruders": [{"position": 40.0}]}},
                 {"move": {"extruders": [{"stepsPerMm": 834.38}]}}, {"move": {"extruders": [{"position": 50.0}]}},
                 {"job": {"layer": 3}}, {"move": {"extruders": [{"factor": 0.9}]}},
                 {"move": {"extruders": [{"position": 59.0}]}},
                 {"job": {"layer": 4}}):
        rig.patch(data)
    layers = feed_layers(rig)
    assert layers[1]["0"] == {"factor": 1.0, "min": 1.0, "max": 1.0, "stepsPerMm": 801.0, "extrusionFactor": 1.0,
                              "reference": 801.0}
    assert (layers[2]["0"]["factor"], layers[2]["0"]["min"], layers[2]["0"]["max"]) == (
        round((10 * 801 + 10 * 834.38) / (801 * 20), 4), 1.0, round(834.38 / 801, 4))
    assert (layers[3]["0"]["factor"], layers[3]["0"]["extrusionFactor"]) == (round(834.38 / 801 * 0.9, 4), 0.9)
    context = qa_db.loads(rig.rows("SELECT context FROM jobs")[0]["context"])
    assert context["extruders"][0]["stepsPerMm"] == 790.0
    assert context["feedReference"] == {"0": {"stepsPerMm": 801.0, "layer": 1}}
    assert "extruder.0.stepsPerMm" in {r["name"] for r in rig.rows("SELECT name FROM channels")}


def test_feed_reference_survives_a_daemon_restart(rig, writer, settings):
    """A restart after the MFM's correction keeps comparing with the e-steps the job started with."""
    rig.start_job()
    for data in ({"move": {"extruders": [{"stepsPerMm": 801.0}]}}, {"job": {"layer": 1}},
                 {"move": {"extruders": [{"position": 10.0}]}}, {"job": {"layer": 2}},
                 {"move": {"extruders": [{"stepsPerMm": 834.38}]}}):
        rig.patch(data)
    rig.collector.shutdown(rig.t)
    rig.collector = qa_collector.Collector(writer, settings, resolve_path=lambda v: str(rig.gcode))
    rig.collector.init_ids()
    rig.patch({})
    rig.patch({"move": {"extruders": [{"position": 20.0}]}})
    rig.patch({"job": {"layer": 3}})
    layer = feed_layers(rig)[2]["0"]
    assert (layer["factor"], layer["reference"]) == (round(834.38 / 801, 4), 801.0)


# --- filament path through the extruder gear ------------------------------------------------------

PHOTO = "M83\nG10\nG1 E-2 F2000\nG1 E2 F2000\nG11\n"   # take-photo.g's retraction as of chx350-config 4d2bf0e
SMALL = "".join(f"G1 X{x} Y5 E0.25\nG10\nG1 X{x} Y9\nG11\n" for x in range(1, 5))


def gear_layers():
    """A big layer (2.2 passes), two small ones retracting every 0.25 mm (9 and 13 passes), a big one."""
    big = 'G10\nM98 P"photo.g"\nG1 Z{z}\nG11\nG1 X10 Y10 E4 F1200\n'
    small = 'G10\nM98 P"photo.g"\nG1 Z{z}\nG11\n' + SMALL
    return "M83\n" + "".join(";LAYER_CHANGE\n" + part.format(z=0.2 * n)
                             for n, part in enumerate((big, small, small, big), 1))


def gear_rig(rig, tmp_path, ready=True):
    rig.gcode.write_text(gear_layers())
    macro = tmp_path / "photo.g"
    macro.write_text(PHOTO)
    files = {"0:/gcodes/a.gcode": str(rig.gcode), "0:/sys/photo.g": str(macro)}
    rig.collector.resolve_path = files.get
    rig.start_job()
    crc = rig.rows("SELECT file_crc32 FROM jobs")[0]["file_crc32"]
    for _ in range(200):
        if rig.index.get(crc)[1] == "ready":
            break
        threading.Event().wait(0.01)
    return macro


def gear_print(rig, between=None):
    for layer in (1, 2, 3, 4):
        rig.patch({"job": {"layer": layer, "duration": layer * 20}}, dt_ms=20_000)
        if between:
            between(layer)
    rig.patch({"state": {"status": "idle"}, "job": {"duration": None, "layer": None}}, dt_ms=20_000)
    rig.collector.resolve_pending_end(rig.t, force=True)


def test_gear_passes_with_the_values_valid_during_the_print(rig, tmp_path):
    """M207 and e-steps change during the print; each layer counts with the values at its start."""
    macro = gear_rig(rig, tmp_path)

    def change(layer):
        if layer == 2:   # a filament's config sets M207 S0.8, the MFM its e-steps
            rig.patch({"tools": [{"retraction": {"length": 0.8}}], "move": {"extruders": [{"stepsPerMm": 834.38}]}})
        if layer == 4:
            macro.write_text(PHOTO.replace("E-2", "E-1"))   # edited while printing
    gear_print(rig, change)
    layers = {r["layer"]: qa_db.loads(r["filament_path"]) for r in rig.rows("SELECT * FROM job_layers")}
    # layer 1: 4 mm printed, one G10 of 0.4 mm, the macro's 4 mm (its G10 skipped: already retracted)
    assert layers[1] == {"mm": 8.8, "netMm": 4.0, "gearPasses": 2.2, "stepsPerMm": 790.0, "motorSteps": 6952,
                         "retraction": {"length": 0.4, "extraRestart": 0}}
    assert (layers[2]["mm"], layers[2]["gearPasses"]) == (9.0, 9.0)
    assert (layers[3]["gearPasses"], layers[3]["stepsPerMm"], layers[3]["retraction"]["length"]) == (13.0, 834.38, 0.8)
    assert layers[4]["gearPasses"] == 2.4   # 4 mm + one 0.8 mm cycle + the macro, 4 mm net
    events = rig.events("gear_passes")
    assert [(e["layer"], e["payload"]["firstLayer"], e["payload"]["lastLayer"], e["payload"]["max"],
             e["payload"]["maxLayer"]) for e in events] == [(2, 2, 3, 13.0, 3)]
    assert events[0]["end_ms"] is not None and events[0]["payload"]["threshold"] == 5
    context = qa_db.loads(rig.rows("SELECT context FROM jobs")[0]["context"])
    photo = context["macros"]['photo.g']
    assert (photo["pathMm"], photo["fwRetracts"]) == (4.0, 1) and photo["crc32End"] != photo["crc32"]


def test_gear_passes_wait_for_the_layer_index(rig, tmp_path, monkeypatch):
    """Layers that finish while the index is being built get their path once it is ready."""
    gear_rig(rig, tmp_path)
    real = rig.index.get
    building = {"on": True}
    monkeypatch.setattr(rig.index, "get", lambda crc: (None, "building") if building["on"] else real(crc))

    def release(layer):
        if layer == 3:
            building["on"] = False
    gear_print(rig, release)
    layers = {r["layer"]: qa_db.loads(r["filament_path"]) for r in rig.rows("SELECT * FROM job_layers")}
    assert [layers[n]["gearPasses"] for n in (1, 2, 3, 4)] == [2.2, 9.0, 9.0, 2.2]
    assert [(e["payload"]["firstLayer"], e["payload"]["lastLayer"]) for e in rig.events("gear_passes")] == [(2, 3)]


def test_gear_passes_forecast_at_the_first_layer(rig, tmp_path):
    """Every layer in advance, with the M207 valid at the first layer: layers 2-3 at 9 passes are one
    forecast run, before they print. A later M207 change does not touch it."""
    gear_rig(rig, tmp_path)

    def change(layer):
        if layer == 1:
            forecasts = rig.events("gear_passes_forecast")
            assert len(forecasts) == 1 and forecasts[0]["layer"] == 1 and forecasts[0]["block_id"] is None
            assert forecasts[0]["payload"] == {"threshold": 5, "runs": [
                {"firstLayer": 2, "lastLayer": 3, "max": 9.0, "maxLayer": 2}], "layers": 2, "max": 9.0, "maxLayer": 2,
                "retraction": {"length": 0.4, "extraRestart": 0, "speed": 20.8, "unretractSpeed": 14, "zHop": 0}}
        if layer == 2:
            rig.patch({"tools": [{"retraction": {"length": 0.8}}]})
    gear_print(rig, change)
    assert len(rig.events("gear_passes_forecast")) == 1
    forecast = qa_db.loads(rig.rows("SELECT context FROM jobs")[0]["context"])["gearForecast"]
    assert (forecast["atLayer"], forecast["layers"], forecast["runs"][0]["lastLayer"]) == (
        1, {"1": 2.2, "2": 9.0, "3": 9.0, "4": 2.2}, 3)


def test_gear_passes_forecast_waits_for_the_layer_index(rig, tmp_path, monkeypatch):
    gear_rig(rig, tmp_path)
    real = rig.index.get
    building = {"on": True}
    monkeypatch.setattr(rig.index, "get", lambda crc: (None, "building") if building["on"] else real(crc))

    def release(layer):
        if layer == 1:
            assert rig.events("gear_passes_forecast") == []
            building["on"] = False
    gear_print(rig, release)
    forecasts = rig.events("gear_passes_forecast")
    assert [(e["layer"], e["payload"]["runs"][0]["firstLayer"]) for e in forecasts] == [(2, 2)]
