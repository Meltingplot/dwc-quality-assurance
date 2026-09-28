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


def fm(c, a, pct=100.0, e=None, **values):
    """A snapshot of a monitor mid-print: totalExtrusion c, avgPercentage a (None: no live data)"""
    values.update({"fm.0.totalExtrusion": c, "fm.0.avgPercentage": a, "fm.0.lastPercentage": pct,
                   "fm.0.calibrated.totalDistance": c})  # RRF reports the commanded total there too
    if e is not None:
        values["extruder.0.position"] = e
    return snap(**values)


def test_layer_accumulator():
    acc = qa_summary.LayerAccumulator(3, 0, fm(500.0, 96.0, e=1000.0), {0: 2.85}, "sensor.2.lastReading", {2: "SZP coil"})
    acc.advance(fm(505.0, 96.0, 100.0, e=1005.0), 5)
    acc.advance(fm(510.0, 96.0, 90.0, e=1010.0, **{"heater.1.current": 224.0}), 5)
    last = fm(520.0, 95.0, 95.0, e=1020.0)
    record = acc.finish(10_000, last, height=0.2, z=0.6, fraction_printed=0.1, load_stats={"1": {"mean": 0.5}})
    assert record["layer"] == 3 and record["duration_s"] == 10
    # measured weighted with lastPercentage: 5 × 1.00 + 5 × 0.90 + 10 × 0.95 = 19 of 20 commanded (the integer
    # avgPercentage stepping 96 → 95 would have taken 1 % of 520 mm off this layer)
    assert record["filament"]["0"] == {"commandedMm": 20.0, "measuredMm": 19.0, "extruderMm": 20.0, "ratio": 0.95}
    area = math.pi * (2.85 / 2) ** 2
    assert record["flow"]["0"] == pytest.approx(20 * area / 10, rel=1e-3)
    assert record["temps"]["heaters"]["1"]["max"] == 224.0
    assert record["temps"]["heaters"]["1"]["setpoint"] == 220.0
    assert record["temps"]["sensors"]["2"]["name"] == "SZP coil"
    assert record["temps"]["chamber"]["mean"] == 30.0
    assert record["fm_stats"]["0"]["mean"] == 95.0
    assert record["fm_stats"]["0"]["avgPercentage"] == 95.0
    assert record["pwm_stats"]["1"]["avgPwmMean"] == 0.5
    assert record["load_stats"] == {"1": {"mean": 0.5}}


def test_job_filament_across_restarts():
    """As on the CHX 350 (job 20260928-075236-bddf0026): the monitor's total is stale from the last job
    until it has calibrated again, the print start zeroes the extruder, a pause restarts the monitor."""
    acc = qa_summary.JobAccumulator(fm(6808.0, None, None, e=7308.5), {0: 1.75}, None)
    sequence = [
        fm(6808.0, None, None, e=0.0),     # print start: RRF zeroes the extruder position
        fm(6808.0, None, None, e=30.0),    # the monitor calibrates: no live data, the old total stays
        fm(10.0, 97.0, 97.0, e=45.0),      # calibrated: counts from its restart, 10 mm at 97 %
        fm(510.0, 96.0, 96.0, e=545.0),
        fm(510.0, None, None, e=532.5),    # pause: retract 12.5 mm, the monitor restarts
        fm(510.0, None, None, e=545.0),    # resume
        fm(10.0, 100.0, 100.0, e=560.0),
        fm(110.0, 98.0, 98.0, e=660.0),
    ]
    for s in sequence:
        acc.advance(s, 1, 0)
    totals = acc.filament_totals()["0"]
    assert totals["commandedMm"] == pytest.approx(620.0)
    # the monitor's own integral per span: 9.7 + (489.6 - 9.7) + 10 + (107.8 - 10)
    assert totals["measuredMm"] == pytest.approx(597.4)
    assert totals["ratio"] == pytest.approx(597.4 / 620, abs=1e-4)
    assert totals["extruderMm"] == pytest.approx(660.0)
    assert totals["avgPercentage"] == 98.0


def test_filament_without_live_data():
    acc = qa_summary.LayerAccumulator(1, 0, fm(100.0, None, None), {}, None, {})
    record = acc.finish(1000, fm(100.0, None, None))
    assert record["filament"]["0"] == {"commandedMm": None, "measuredMm": None, "extruderMm": 0.0}


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
