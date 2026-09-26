"""Heater load (PLAN.md §5.4.1): same definition as the CHX UI banner."""

import pytest

import qa_heaterload
import qa_settings

CFG = qa_settings.DEFAULTS
NOZZLES = {1: 0}


def heaters(load, active=220.0, current=220.0, state="active", max_pwm=1.0):
    return {1: (state, active, current, load * max_pwm, max_pwm)}


def run(tracker, start, end, load, step=1.0, **kwargs):
    transitions = []
    t = start
    while t <= end + 1e-9:
        transitions += tracker.update(t, heaters(load, **kwargs), True, NOZZLES, layer=3)
        t += step
    return transitions


@pytest.mark.parametrize("avg,max_pwm,expected", [
    (0.45, 1.0, 0.45), (0.45, 0.9, 0.5), (1.2, 1.0, 1.0), (0.5, 0.0, 0.5), (-0.1, 1.0, 0.0), (None, 1.0, None),
])
def test_heater_load(avg, max_pwm, expected):
    result = qa_heaterload.heater_load(avg, max_pwm)
    assert result == (pytest.approx(expected) if expected is not None else None)


def test_load_level_matches_the_banner():
    # heaterLoad.ts loadLevel: limit clears at 0.85, high at 0.75
    level = lambda mean, prev: qa_heaterload.load_level(mean, prev, 0.8, 0.9, 0.05)  # noqa: E731
    assert level(0.79, None) is None
    assert level(0.80, None) == "high"
    assert level(0.90, None) == "limit"
    assert level(0.86, "limit") == "limit"
    assert level(0.84, "limit") == "high"
    assert level(0.76, "high") == "high"
    assert level(0.74, "high") is None


def test_mean_needs_75_percent_of_the_window():
    tracker = qa_heaterload.HeaterLoadTracker(CFG)
    run(tracker, 0, 44, 0.95)
    assert tracker.means() == {}          # 44 s covered < 45 s
    run(tracker, 45, 46, 0.95)
    assert tracker.means()[1] == pytest.approx(0.95)


def test_high_level_event_with_hysteresis():
    tracker = qa_heaterload.HeaterLoadTracker(CFG)
    transitions = run(tracker, 0, 120, 0.85)
    assert [t[0] for t in transitions] == ["start"]
    assert transitions[0][2]["level"] == "high"
    assert tracker.levels() == {1: "high"}
    # 0.78 is below 0.8 but above 0.75: the level stays
    transitions = run(tracker, 121, 240, 0.78)
    assert transitions == []
    transitions = run(tracker, 241, 400, 0.5)
    assert [t[0] for t in transitions] == ["end"]
    assert tracker.events == {1: 1}
    assert tracker.first_event_layer == {1: 3}


def test_escalation_to_limit_is_a_change():
    tracker = qa_heaterload.HeaterLoadTracker(CFG)
    run(tracker, 0, 90, 0.82)
    transitions = run(tracker, 91, 200, 0.97)
    assert [t[0] for t in transitions] == ["change"]
    assert transitions[0][2]["level"] == "limit"


def test_counting_starts_when_the_setpoint_is_reached():
    tracker = qa_heaterload.HeaterLoadTracker(CFG)
    run(tracker, 0, 100, 1.0, current=150.0)  # heating up at full power
    assert tracker.means() == {}
    assert tracker.job_stats(NOZZLES) == {}


def test_sag_after_reaching_keeps_counting():
    tracker = qa_heaterload.HeaterLoadTracker(CFG)
    run(tracker, 0, 10, 0.6)
    run(tracker, 11, 80, 1.0, current=205.0)  # overload: the temperature sags
    assert tracker.levels() == {1: "limit"}


def test_setpoint_change_and_pause_start_over():
    tracker = qa_heaterload.HeaterLoadTracker(CFG)
    run(tracker, 0, 90, 0.95)
    transitions = tracker.update(91, heaters(0.95, active=230.0, current=221.0), True, NOZZLES)
    assert [t[0] for t in transitions] == ["end"]
    assert tracker.means() == {}
    run(tracker, 92, 200, 0.95, active=230.0, current=230.0)
    transitions = tracker.update(201, heaters(0.95, active=230.0), False, NOZZLES)  # paused
    assert [t[0] for t in transitions] == ["end"]
    assert tracker.levels() == {}


def test_bed_gets_statistics_but_no_events():
    tracker = qa_heaterload.HeaterLoadTracker(CFG)
    transitions = []
    for t in range(0, 120):
        transitions += tracker.update(t, {0: ("active", 60.0, 60.0, 0.95, 1.0)}, True, NOZZLES)
    assert transitions == []
    stats = tracker.job_stats(NOZZLES)["0"]
    assert stats["nozzle"] is False
    assert stats["mean"] == pytest.approx(0.95)


def test_layer_and_job_statistics():
    tracker = qa_heaterload.HeaterLoadTracker(CFG)
    run(tracker, 0, 60, 0.5)
    run(tracker, 61, 120, 0.95)
    layer = tracker.layer_stats(span_s=120)["1"]
    assert layer["mean"] == pytest.approx(0.725, abs=0.01)
    assert layer["max"] == pytest.approx(0.95)
    assert layer["p95"] == pytest.approx(0.96, abs=0.011)
    assert layer["shareAtSetpoint"] == pytest.approx(1.0, abs=0.01)
    assert 0 < layer["shareHigh"] < 0.5
    job = tracker.job_stats(NOZZLES)["1"]
    assert job["tool"] == 0
    assert job["bySetpoint"] == [{"setpoint": 220.0, "mean": pytest.approx(0.725, abs=0.01), "timeS": 120.0}]
    tracker.reset_layer()
    assert tracker.layer_stats() == {}


def test_gap_is_missing_data_not_a_held_value():
    tracker = qa_heaterload.HeaterLoadTracker(CFG)
    tracker.update(0, heaters(0.9), True, NOZZLES)
    tracker.update(50, heaters(0.9), True, NOZZLES)  # 50 s without data
    assert tracker.means() == {}
