"""Settings: /opt/dsf/sd/QualityAssurance/settings.json (PLAN.md §5.9).

The file may hold only the values that differ from the defaults, or a complete copy; missing
or invalid values fall back to the defaults and are reported. Written atomically. Changing a
setting through the API takes effect without a restart: readers take ``Settings.current()``
(an immutable snapshot) each time they need it.
"""

import copy
import json
import os
import threading

DEFAULT_DATA_DIR = "/opt/dsf/sd/QualityAssurance"
SETTINGS_FILE = "settings.json"


def data_dir():
    """Data directory, QA_DATA_DIR overrides it (tests, development)."""
    return os.environ.get("QA_DATA_DIR", DEFAULT_DATA_DIR)


DEFAULTS = {
    # Sampling (§3): coarse sample into the DB every sampleIntervalS, ring buffer of every
    # object-model patch ringBufferS back, fine-resolution block postTriggerS past a trigger
    "sampleIntervalS": 5,
    "ringBufferS": 90,
    "postTriggerS": 30,
    "commitIntervalS": 30,
    "thresholds": {
        # a jump between two consecutive samples that triggers a fine block
        "temperatureK": 5,
        "filamentPercentPoints": 15,
        "vInPercent": 10,
        # phantom reading: a jump of at least this within one patch that returns within
        # phantomReturnS to within temperatureK of the value before the jump
        "phantomJumpK": 15,
        "phantomReturnS": 5,
    },
    # lastPercentage outside configured.percentMin/Max (without a firmware error) this long
    "filamentPercentWindowMinS": 5,
    # Same constants as the CHX UI heater-load banner (CHX350/stores/heaterLoad.ts, §5.4.1)
    "heaterLoad": {"high": 0.8, "limit": 0.9, "hysteresis": 0.05, "windowS": 60,
                   "minCoverage": 0.75, "reachedToleranceK": 2},
    "chamber": {"mode": "auto", "index": None, "autoSensorName": "SZP coil"},
    "contextGlobals": ["nozzle_type", "nozzle_diameter", "filament_diameter", "bed_surface",
                       "spool_net_weight", "spool_remaining", "spool_tare", "spool_density",
                       "filament_max_flow_rate", "machine_mode"],
    "machineSignals": {"mfm": True},
    # encoderThreads: SVT-AV1 "lp"; a paused encoder keeps its memory through a print, about
    # 0.57 GB with 2 at 1984×1080 against 0.95 GB with 4 (qa_timelapse docstring); None = SVT decides
    "timelapse": {"enabled": True, "snapshotUrl": None, "trigger": "layer", "minIntervalS": 2,
                  "fps": 30, "keyframeInterval": 30, "crf": None, "preset": None, "encoderThreads": 2,
                  "keepFramesOnFailure": True,
                  "retention": {"jobs": 50, "maxBytes": 10 * 1024 ** 3}},
    "accelerometer": {"enabled": "auto", "intervalMin": 15, "samples": 1000, "axes": "XYZ",
                      "board": None, "referenceAutoCount": 5},
    "retention": {"jobs": 50, "days": 90, "maxDbBytes": 2 * 1024 ** 3},
}

# key path -> (type(s), min, max); None = no bound. Anything not listed only has to match the
# default's type
_NUMBER = (int, float)
BOUNDS = {
    "sampleIntervalS": (_NUMBER, 1, 3600),
    "ringBufferS": (_NUMBER, 10, 600),
    "postTriggerS": (_NUMBER, 1, 600),
    "commitIntervalS": (_NUMBER, 1, 300),
    "thresholds.temperatureK": (_NUMBER, 0.1, 500),
    "thresholds.filamentPercentPoints": (_NUMBER, 1, 1000),
    "thresholds.vInPercent": (_NUMBER, 1, 100),
    "thresholds.phantomJumpK": (_NUMBER, 1, 1000),
    "thresholds.phantomReturnS": (_NUMBER, 0.1, 60),
    "filamentPercentWindowMinS": (_NUMBER, 0, 3600),
    "heaterLoad.high": (_NUMBER, 0, 1),
    "heaterLoad.limit": (_NUMBER, 0, 1),
    "heaterLoad.hysteresis": (_NUMBER, 0, 0.5),
    "heaterLoad.windowS": (_NUMBER, 5, 3600),
    "heaterLoad.minCoverage": (_NUMBER, 0, 1),
    "heaterLoad.reachedToleranceK": (_NUMBER, 0, 50),
    "chamber.mode": (str, None, None),
    "chamber.index": ((int, type(None)), 0, 1000),
    "chamber.autoSensorName": (str, None, None),
    "contextGlobals": (list, None, None),
    "timelapse.snapshotUrl": ((str, type(None)), None, None),
    "timelapse.minIntervalS": (_NUMBER, 0, 3600),
    "timelapse.fps": (_NUMBER, 1, 120),
    "timelapse.keyframeInterval": (int, 1, 1000),
    "timelapse.crf": ((int, type(None)), 0, 63),
    "timelapse.preset": ((int, type(None)), -1, 13),
    "timelapse.encoderThreads": ((int, type(None)), 1, 64),
    "timelapse.retention.jobs": (int, 1, 100000),
    "timelapse.retention.maxBytes": (int, 0, None),
    "accelerometer.enabled": ((bool, str), None, None),
    "accelerometer.board": ((int, type(None)), 0, 1000),
    "retention.jobs": (int, 1, 1000000),
    "retention.days": (_NUMBER, 1, 100000),
    "retention.maxDbBytes": (int, 1024 * 1024, None),
}

ENUMS = {
    "chamber.mode": ("auto", "heater", "sensor"),
    "timelapse.trigger": ("layer",),
    "accelerometer.enabled": (True, False, "auto"),
}


def _check(path, value, default):
    """None if ``value`` is acceptable for ``path``, else the reason."""
    if path in ENUMS:
        return None if value in ENUMS[path] else f"must be one of {list(ENUMS[path])}"
    types, low, high = BOUNDS.get(path, (type(default) if default is not None else object, None, None))
    if isinstance(value, bool) and bool not in (types if isinstance(types, tuple) else (types,)):
        return "wrong type"
    if not isinstance(value, types):
        return "wrong type"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if low is not None and value < low:
            return f"must be >= {low}"
        if high is not None and value > high:
            return f"must be <= {high}"
    if path == "contextGlobals" and not all(isinstance(v, str) and v for v in value):
        return "must be a list of global variable names"
    if path == "timelapse.snapshotUrl" and isinstance(value, str) and value and \
            not value.startswith(("http://", "https://")):
        return "must be an http:// or https:// URL"
    return None


def _merge(defaults, given, prefix, errors):
    result = {}
    for key, default in defaults.items():
        path = f"{prefix}{key}"
        if not isinstance(given, dict) or key not in given:
            result[key] = copy.deepcopy(default)
            continue
        value = given[key]
        if isinstance(default, dict) and path not in BOUNDS:
            result[key] = _merge(default, value if isinstance(value, dict) else {}, path + ".", errors)
            if not isinstance(value, dict):
                errors.append(f"{path}: must be an object")
            continue
        problem = _check(path, value, default)
        if problem:
            errors.append(f"{path}: {problem}")
            result[key] = copy.deepcopy(default)
        else:
            result[key] = copy.deepcopy(value)
    if isinstance(given, dict):
        for key in given:
            if key not in defaults:
                errors.append(f"{prefix}{key}: unknown setting")
    return result


def validate(given):
    """Merge ``given`` over the defaults. Returns ``(settings, errors)``."""
    errors = []
    settings = _merge(DEFAULTS, given if isinstance(given, dict) else {}, "", errors)
    load = settings["heaterLoad"]
    if load["limit"] < load["high"]:
        errors.append("heaterLoad.limit: must be >= heaterLoad.high")
        settings["heaterLoad"] = copy.deepcopy(DEFAULTS["heaterLoad"])
    return settings, errors


def _freeze(value):
    """Deep copy so readers cannot change the shared snapshot by accident."""
    return copy.deepcopy(value)


class Settings:
    """Thread-safe holder of the current settings."""

    def __init__(self, directory=None):
        self._dir = directory or data_dir()
        self._lock = threading.Lock()
        self._current = _freeze(DEFAULTS)
        self.errors = []

    @property
    def path(self):
        return os.path.join(self._dir, SETTINGS_FILE)

    def current(self):
        with self._lock:
            return self._current

    def load(self):
        """Read settings.json. A missing file means defaults; a broken one is reported and ignored."""
        given = {}
        errors = []
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                given = json.load(handle)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            errors.append(f"settings.json unreadable, using defaults: {exc}")
        settings, problems = validate(given)
        with self._lock:
            self._current = _freeze(settings)
            self.errors = errors + problems
        return self.errors

    def update(self, given):
        """Validate and store new settings. Returns ``(settings, errors)``; nothing is stored on errors."""
        settings, errors = validate(given)
        if errors:
            return settings, errors
        os.makedirs(self._dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        _fsync_dir(self._dir)
        with self._lock:
            self._current = _freeze(settings)
            self.errors = []
        return settings, []


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
