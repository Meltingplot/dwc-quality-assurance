"""Event levels: info, warning, error (Tim 2026-10-01).

A job's number of events is a quality criterion, so a job with more events is a worse print. That number
counts warnings and errors, never info.

A level is a reading of the recording, not part of it. It is worked out whenever events are read (API, live
frames) from type, subtype and payload, so every job is counted by the same rules, and a changed rule or
range applies to old jobs too. The later Quality Control plugin can take the rules over.

One incident often leaves several events. Only the observed problem counts, and what follows from it is
info (Tim 2026-10-01): a filament error is ``filament_status`` (warning); the machine tolerating it is
``mfm_error_tolerated``, the pause it causes is ``pause``, the M92 of a flow-bias correction is a
``setpoint_change`` with a ``cause`` (all info); the recovery's verdict is ``mfm_recovery`` (real_issue
error, false_positive info). A setpoint change is info while its new value lies in its expected range
(settings ``expectedRanges``, e.g. pressure advance), and a warning outside it.
"""

INFO, WARNING, ERROR = "info", "warning", "error"
LEVELS = (INFO, WARNING, ERROR)
COUNTED = (WARNING, ERROR)

# type -> level, or {subtype: level, None: the level of every other subtype}. A type missing here counts
# as a warning, so a new event type counts until it is placed (tests/test_severity.py checks the catalog)
RULES = {
    "job_start": INFO,
    "job_end": INFO,                    # the result is a criterion of its own; what ended the job counts
    "daemon_started_mid_job": INFO,
    "pause": INFO,                      # its cause is an event of its own
    "resume": INFO,
    "babystep": INFO,                   # outside its expected range: warning
    "setpoint_change": INFO,            # likewise
    "calibration": INFO,
    "gear_passes_forecast": INFO,       # gear_passes counts the layers when they print
    "mfm_error_tolerated": INFO,        # the filament_status it tolerated counts
    "timelapse_failed": INFO,           # the recording failed, not the print
    "accelerometer_failed": INFO,
    "firmware_restart": {"during_job": ERROR, None: INFO},     # before_job explains gaps in the recording
    "mfm_recovery": {"real_issue": ERROR, None: INFO},         # requested: its verdict follows
    "machine_mode": {"default": ERROR, None: INFO},            # see END_SEQUENCE_MS
    "heater_fault": ERROR,
    "heater_monitor": ERROR,
    "heater_load": {"limit": ERROR, None: WARNING},
    "driver_error": {"error": ERROR, None: WARNING},           # an open load cleared within 500 ms: info
    "filament_status": WARNING,
    "filament_percent_window": WARNING,
    "filament_percent_level": WARNING,
    "filament_percent_drift": WARNING,
    "mfm_flow_bias": WARNING,
    "voltage_dip": WARNING,
    "phantom_reading": WARNING,
    "gear_passes": WARNING,
    "frame_change": WARNING,
}

# stop.g and cancel.g switch the CHX 350 into its restrictive default mode (chx350-config operating-mode/
# default.g); that switch came 1.3 s before job_end in job 20260929-125727 and 5 ms after it in 20260930-075116.
# A switch this close to the job end is the end sequence, not doors opened during the print.
END_SEQUENCE_MS = 30_000


def outside(value, bounds):
    """Whether a number lies outside ``[low, high]`` (None: no bound on that side)."""
    if bounds is None or not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    low, high = bounds
    return (low is not None and value < low) or (high is not None and value > high)


def level(event, ranges=None, ended_ms=None):
    """The level of ``event`` (a dict with type, subtype, ts_ms, payload). ``ranges``: the settings'
    expectedRanges; ``ended_ms``: its job's end, None while the job runs."""
    type_ = event.get("type")
    subtype = event.get("subtype")
    payload = event.get("payload") or {}
    rule = RULES.get(type_, WARNING)
    result = rule.get(subtype, rule[None]) if isinstance(rule, dict) else rule
    if type_ == "driver_error" and payload.get("confirmed") is False:
        return INFO
    if type_ == "machine_mode" and result == ERROR and ended_ms is not None and \
            event.get("ts_ms") is not None and event["ts_ms"] >= ended_ms - END_SEQUENCE_MS:
        return INFO
    if type_ in ("setpoint_change", "babystep") and payload.get("cause") is None:
        name = subtype if type_ == "setpoint_change" else "babystep"
        if outside(payload.get("to"), (ranges or {}).get(name)):
            return WARNING
    return result


def counts(events, ranges=None, ended_ms=None):
    """``{"info": n, "warning": n, "error": n}`` of ``events``."""
    result = dict.fromkeys(LEVELS, 0)
    for event in events:
        result[level(event, ranges, ended_ms)] += 1
    return result


def counted(levels):
    """The number that rates a job: its warnings and errors."""
    return sum(levels.get(name, 0) for name in COUNTED)
