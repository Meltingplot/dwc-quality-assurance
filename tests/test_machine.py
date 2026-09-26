import qa_machine


def g(**overrides):
    base = {"mfm_error_count": 0, "mfm_error_time": 0, "mfm_error_start_pos": None, "mfm_ignore_events": False,
            "mfm_recovery_requested": False, "mfm_recovery_result": -1, "mfm_recovery_resume_time": 0,
            "mfm_esteps_detected": False, "mfm_esteps_drift_since": 0, "mfm_esteps_drift_avg": 0,
            "mfm_esteps_suggested": 0, "mfm_esteps_baseline": 0.0, "mfm_suppress_until": 0}
    base.update(overrides)
    return base


def test_decode_hardened_booleans():
    assert qa_machine.decode(1431655765) is True
    assert qa_machine.decode(2863311530) is False
    assert qa_machine.decode([1431655765, 3]) == [True, 3]
    assert qa_machine.decode(0.8) == 0.8


def test_channels_only_with_mfm_globals():
    assert qa_machine.mfm_channels({"nozzle_diameter": [0.8]}) == {}
    channels = qa_machine.mfm_channels(g(mfm_error_count=2, mfm_esteps_detected=True))
    assert channels["mfm_error_count"] == 2.0
    assert channels["mfm_esteps_detected"] == 1.0
    assert channels["mfm_recovery_result"] == -1.0


def test_watcher_events():
    w = qa_machine.MfmWatcher()
    assert w.update(g()) == []
    events = w.update(g(mfm_error_count=1, mfm_error_time=500))
    assert events[0][0] == "mfm_error_tolerated"
    assert events[0][2]["count"] == 1
    assert events[0][2]["mfm_error_time"] == 500
    assert w.update(g(mfm_error_count=0)) == []  # a reset is no event
    events = w.update(g(mfm_recovery_requested=True))
    assert events == [("mfm_recovery", "requested", events[0][2])]
    assert w.recovery_requested()
    events = w.update(g(mfm_recovery_requested=False, mfm_recovery_result=0))
    assert events[0][:2] == ("mfm_recovery", "false_positive")
    events = w.update(g(mfm_recovery_result=2))
    assert events[0][:2] == ("mfm_recovery", "real_issue")
    events = w.update(g(mfm_recovery_result=2, mfm_esteps_detected=True, mfm_esteps_suggested=812.5))
    assert events[0][:2] == ("mfm_flow_bias", "detected")
    assert events[0][2]["mfm_esteps_suggested"] == 812.5


def test_watcher_on_a_machine_without_mfm():
    w = qa_machine.MfmWatcher()
    assert w.update({}) == []
    assert w.update(None) == []


def test_steps_cause():
    assert qa_machine.steps_cause(812.5, g(mfm_esteps_suggested=812.5)) == "mfm_flow_bias"
    assert qa_machine.steps_cause(790.0, g(mfm_esteps_suggested=812.5, mfm_esteps_baseline=790.0)) == "baseline_restore"
    assert qa_machine.steps_cause(800.0, g(mfm_esteps_suggested=812.5, mfm_esteps_baseline=790.0)) is None
    assert qa_machine.steps_cause(800.0, g()) is None


def test_context_globals_skip_missing():
    result = qa_machine.context_globals({"nozzle_diameter": [0.8], "door": 1431655765},
                                        ["nozzle_diameter", "bed_surface", "door"])
    assert result == {"nozzle_diameter": [0.8], "door": True}
