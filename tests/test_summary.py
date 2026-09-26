import math

import pytest

import qa_summary


def test_time_weighted_stats():
    tw = qa_summary.TW()
    tw.add(10, 1)
    tw.add(20, 3)
    assert tw.mean == pytest.approx(17.5)
    assert tw.min == 10 and tw.max == 20
    assert tw.std == pytest.approx(math.sqrt(0.25 * 7.5 ** 2 + 0.75 * 2.5 ** 2))
    assert qa_summary.TW().result() is None


def snap(**values):
    base = {"heater.1.current": 220.0, "heater.1.active": 220.0, "heater.1.state": 2.0, "heater.1.avgPwm": 0.5,
            "sensor.2.lastReading": 30.0, "fm.0.lastPercentage": 100.0, "fm.0.totalExtrusion": 0.0,
            "fm.0.calibrated.totalDistance": 0.0, "extruder.0.position": 0.0, "move.currentMove.extrusionRate": 2.0}
    base.update(values)
    return base


def test_layer_accumulator():
    acc = qa_summary.LayerAccumulator(3, 0, snap(), {0: 2.85}, "sensor.2.lastReading", {2: "SZP coil"})
    acc.advance(snap(), 5)
    acc.advance(snap(**{"heater.1.current": 224.0, "fm.0.lastPercentage": 90.0}), 5)
    last = snap(**{"fm.0.totalExtrusion": 100.0, "fm.0.calibrated.totalDistance": 95.0, "extruder.0.position": 102.0,
                   "fm.0.avgPercentage": 96.0})
    record = acc.finish(10_000, last, height=0.2, z=0.6, fraction_printed=0.1, load_stats={"1": {"mean": 0.5}})
    assert record["layer"] == 3 and record["duration_s"] == 10
    assert record["filament"]["0"] == {"commandedMm": 100.0, "measuredMm": 95.0, "extruderMm": 102.0, "ratio": 0.95}
    area = math.pi * (2.85 / 2) ** 2
    assert record["flow"]["0"] == pytest.approx(100 * area / 10, rel=1e-3)
    assert record["temps"]["heaters"]["1"]["max"] == 224.0
    assert record["temps"]["heaters"]["1"]["setpoint"] == 220.0
    assert record["temps"]["sensors"]["2"]["name"] == "SZP coil"
    assert record["temps"]["chamber"]["mean"] == 30.0
    assert record["fm_stats"]["0"]["mean"] == 95.0
    assert record["fm_stats"]["0"]["avgPercentage"] == 96.0
    assert record["pwm_stats"]["1"]["avgPwmMean"] == 0.5
    assert record["load_stats"] == {"1": {"mean": 0.5}}


def test_long_gap_is_not_accumulated():
    acc = qa_summary.LayerAccumulator(1, 0, snap(), {}, None, {})
    acc.advance(snap(), 60)
    assert acc.span_s == 0


def test_job_accumulator_distributions():
    acc = qa_summary.JobAccumulator(snap(), {0: 1.75}, "sensor.2.lastReading")
    for pct in (99, 101, 101, 97):
        acc.advance(snap(**{"fm.0.lastPercentage": float(pct)}), 1, 0)
    totals = acc.filament_totals()["0"]
    classes = {c["from"]: c["s"] for c in totals["percentDistribution"]}
    assert classes == {96: 1.0, 98: 1.0, 100: 2.0}
    flow = 2.0 * math.pi * (1.75 / 2) ** 2
    assert totals["flowVsPercent"][0]["flowFrom"] == math.floor(flow)
    assert totals["flowVsPercent"][0]["mean"] == 99.5


def test_heat_up_time():
    acc = qa_summary.JobAccumulator(snap(), {}, None)
    acc.heater_setpoints(snap(**{"heater.1.current": 25.0, "heater.1.active": 220.0}), 0)
    acc.heater_setpoints(snap(**{"heater.1.current": 150.0, "heater.1.active": 220.0}), 30_000)
    acc.heater_setpoints(snap(**{"heater.1.current": 219.0, "heater.1.active": 220.0}), 62_000)
    assert acc.thermal()["heatUp"]["1"] == [{"setpoint": 220.0, "s": 62.0}]


def test_event_and_mfm_summary():
    events = [
        {"type": "mfm_error_tolerated", "layer": 4},
        {"type": "mfm_error_tolerated", "layer": 9},
        {"type": "mfm_recovery", "subtype": "false_positive", "layer": 9, "payload": {"result": 0}},
        {"type": "mfm_flow_bias", "subtype": "detected", "layer": 12, "payload": {"mfm_esteps_suggested": 810.0}},
        {"type": "setpoint_change", "subtype": "stepsPerMm", "layer": 13, "payload": {"cause": "mfm_flow_bias", "to": 810.0}},
    ]
    summary = qa_summary.event_summary(events)
    assert summary["mfm_error_tolerated"] == {"count": 2, "firstLayer": 4, "lastLayer": 9}
    mfm = qa_summary.mfm_summary(events, {})
    assert mfm == {"toleratedErrors": 2, "recoveries": [{"layer": 9, "result": 0, "subtype": "false_positive"}],
                   "flowBiasDetected": True, "estepsSuggested": 810.0, "estepsApplied": [810.0]}


def test_spool_usage():
    assert qa_summary.spool_usage({"spool_remaining": [900.0, 0]}, {"spool_remaining": [850.5, 0]}) == {"0": 49.5, "1": 0}
    assert qa_summary.spool_usage({}, {}) is None
