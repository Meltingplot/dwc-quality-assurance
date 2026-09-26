"""Machine globals: the CHX 350's own filament monitor (MFM) evaluation (PLAN.md §5.4.2) and the
globals copied into the job context (§5.9 ``contextGlobals``).

chx350-config evaluates the rotating-magnet monitor itself and keeps its state in globals
(declared in ``sys/meltingplot/globals.g``, semantics in its CLAUDE.md "MFM"; read at
chx350-config a69dae7, 2026-09-26):

- ``mfm_error_count``          tolerated P=4/P=5 errors of the current sequence (0..3)
- ``mfm_error_time``           upTime of the last one; ``mfm_error_start_pos`` extruder position at
                               the first error of the sequence (null = none)
- ``mfm_recovery_requested``   armed by filament-error.g right before its M25, consumed by pause.g
- ``mfm_recovery_result``      verdict of the recovery run by pause.g: -1 did not run,
                               0 false positive, 1+ real issue; ``mfm_recovery_resume_time`` upTime
                               of the last auto-recovery + auto-resume
- ``mfm_esteps_detected``      set once per print when the flow-bias detector found a settled
                               drift; ``mfm_esteps_suggested`` bounded (±5 %) target steps/mm,
                               ``mfm_esteps_drift_avg``/``_since`` the settle window,
                               ``mfm_esteps_baseline`` e-steps before the correction (0 = not applied)
- ``mfm_suppress_until``, ``mfm_ignore_events``   suppression (context only)

The MFM globals are plain booleans and numbers. Only the CE flags are "hardened" (0x55555555 =
true, 0xAAAAAAAA = false, chx350-config CLAUDE.md "Bit-flip hardened states"); ``decode``
handles both, so a context global is readable whichever it is. On a machine without these
globals nothing here produces anything.
"""

import math

HARDENED_TRUE = 1431655765   # 0x55555555
HARDENED_FALSE = 2863311530  # 0xAAAAAAAA

# Globals that become channels (numbers; booleans as 0/1)
MFM_CHANNEL_GLOBALS = (
    "mfm_error_count",
    "mfm_recovery_requested",
    "mfm_recovery_result",
    "mfm_esteps_detected",
    "mfm_esteps_suggested",
    "mfm_esteps_baseline",
    "mfm_esteps_drift_avg",
)

# Globals carried in the payload of MFM events
MFM_CONTEXT_GLOBALS = (
    "mfm_error_count", "mfm_error_time", "mfm_error_start_pos",
    "mfm_recovery_requested", "mfm_recovery_result", "mfm_recovery_resume_time",
    "mfm_esteps_detected", "mfm_esteps_suggested", "mfm_esteps_baseline",
    "mfm_esteps_drift_avg", "mfm_esteps_drift_since",
    "mfm_suppress_until", "mfm_ignore_events",
)

# How close (relative) a new stepsPerMm has to be to the suggested/baseline value to be
# attributed to it; the object model reports the value rounded
STEPS_MATCH_TOLERANCE = 0.0005


def decode(value):
    """Hardened booleans to bool; everything else unchanged (lists element by element)."""
    if isinstance(value, bool):
        return value
    if value == HARDENED_TRUE:
        return True
    if value == HARDENED_FALSE:
        return False
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


def _get(globals_, name):
    if globals_ is None:
        return None
    try:
        return globals_.get(name)
    except AttributeError:
        return None


def as_number(value):
    value = decode(value)
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    return None


def has_mfm(globals_):
    return _get(globals_, "mfm_error_count") is not None


def mfm_channels(globals_):
    result = {}
    if not has_mfm(globals_):
        return result
    for name in MFM_CHANNEL_GLOBALS:
        value = as_number(_get(globals_, name))
        if value is not None:
            result[name] = value
    return result


def mfm_context(globals_):
    return {name: decode(_get(globals_, name)) for name in MFM_CONTEXT_GLOBALS
            if globals_ is not None and name in globals_}


def context_globals(globals_, names):
    """The listed globals that exist, decoded (missing ones are skipped, §5.9)."""
    result = {}
    if globals_ is None:
        return result
    for name in names:
        if name in globals_:
            result[name] = decode(globals_.get(name))
    return result


def steps_cause(new_steps, globals_):
    """Why stepsPerMm changed, from the MFM globals: ``mfm_flow_bias`` when it is the suggested
    correction (M92 in filament-error.g / resume.g), ``baseline_restore`` when it is the baseline
    again (M92 in print/finish.g), else None."""
    if new_steps is None:
        return None
    suggested = as_number(_get(globals_, "mfm_esteps_suggested"))
    baseline = as_number(_get(globals_, "mfm_esteps_baseline"))
    if suggested and abs(new_steps - suggested) <= STEPS_MATCH_TOLERANCE * abs(suggested):
        return "mfm_flow_bias"
    if baseline and abs(new_steps - baseline) <= STEPS_MATCH_TOLERANCE * abs(baseline):
        return "baseline_restore"
    return None


class MfmWatcher:
    """Turns changes of the MFM globals into events (§5.4.2). Feed ``update(globals)`` every patch;
    it returns ``[(type, subtype, payload)]``."""

    def __init__(self):
        self._prev = None

    def reset(self):
        self._prev = None

    def update(self, globals_):
        if not has_mfm(globals_):
            self._prev = None
            return []
        now = {name: decode(_get(globals_, name)) for name in MFM_CONTEXT_GLOBALS}
        prev = self._prev
        self._prev = now
        if prev is None:
            return []
        events = []
        context = mfm_context(globals_)

        count, prev_count = as_number(now["mfm_error_count"]), as_number(prev["mfm_error_count"])
        if count is not None and prev_count is not None and count > prev_count:
            events.append(("mfm_error_tolerated", None, {"count": int(count), **context}))

        requested = now["mfm_recovery_requested"] is True
        if requested and prev["mfm_recovery_requested"] is not True:
            events.append(("mfm_recovery", "requested", context))

        result, prev_result = as_number(now["mfm_recovery_result"]), as_number(prev["mfm_recovery_result"])
        if result is not None and result >= 0 and result != prev_result:
            subtype = "false_positive" if result == 0 else "real_issue"
            events.append(("mfm_recovery", subtype, {"result": int(result), **context}))

        if now["mfm_esteps_detected"] is True and prev["mfm_esteps_detected"] is not True:
            events.append(("mfm_flow_bias", "detected", context))
        return events

    def recovery_requested(self):
        return self._prev is not None and self._prev.get("mfm_recovery_requested") is True
