"""Heater load (PLAN.md §5.4.1): the same quantity as the heater-load banner of the CHX UI
(DuetWebControl ``src/plugins/CHX350/stores/heaterLoad.ts``, thresholds confirmed by Tim on
2026-09-25), so banner and recording agree.

- load = clamp(avgPwm / model.maxPwm, 0, 1), maxPwm <= 0 counts as 1. avgPwm is RRF's running
  average of the heater PWM over about 5 s, 0..1 (wiki Gcodes.md M573); RRF caps the PWM at
  maxPwm (LocalHeater.cpp), hence the normalisation.
- Counted only while the machine is ``processing``, the heater is ``active`` with a setpoint > 0,
  and only after the setpoint was reached (current >= active - reachedToleranceK). A new
  setpoint or a pause starts over. A temperature that sags after it was reached keeps counting:
  that is the overload case itself.
- Mean over ``windowS`` (60 s), time-weighted (the object model arrives in irregular patches;
  the banner samples once a second). Valid once the window is at least ``minCoverage`` covered
  (the banner wants 45 of 60 samples).
- Levels ``high`` from 0.8, ``limit`` from 0.9; a level clears 0.05 below its threshold.
  Same function as ``loadLevel`` in heaterLoad.ts.
- Events and levels only for nozzle heaters; bed and chamber get layer and job statistics.
"""

import math

# A gap between two updates longer than this is missing data, not a held value
MAX_HOLD_S = 10.0
HIST_BINS = 100


def heater_load(avg_pwm, max_pwm):
    if avg_pwm is None:
        return None
    try:
        avg = float(avg_pwm)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(avg):
        return None
    cap = float(max_pwm) if max_pwm is not None and max_pwm > 0 else 1.0
    return min(1.0, max(0.0, avg / cap))


def load_level(mean, previous, high, limit, hysteresis):
    """As ``loadLevel`` in the CHX UI: a level only clears ``hysteresis`` below its threshold."""
    limit_threshold = limit - hysteresis if previous == "limit" else limit
    high_threshold = high - hysteresis if previous is not None else high
    if mean >= limit_threshold:
        return "limit"
    if mean >= high_threshold:
        return "high"
    return None


class _Stats:
    """Time-weighted statistics of the load at the setpoint."""

    def __init__(self):
        self.time_s = 0.0
        self.sum = 0.0
        self.max = None
        self.hist = [0.0] * HIST_BINS
        self.max_mean = None
        self.mean_valid_s = 0.0
        self.mean_high_s = 0.0
        self.mean_limit_s = 0.0
        self.setpoint = None
        self.by_setpoint = {}  # setpoint -> [time_s, sum]

    def add(self, load, dt, mean, setpoint, high, limit):
        self.time_s += dt
        self.sum += load * dt
        self.max = load if self.max is None else max(self.max, load)
        self.hist[min(HIST_BINS - 1, int(load * HIST_BINS))] += dt
        self.setpoint = setpoint
        bucket = self.by_setpoint.setdefault(setpoint, [0.0, 0.0])
        bucket[0] += dt
        bucket[1] += load * dt
        if mean is not None:
            self.mean_valid_s += dt
            self.max_mean = mean if self.max_mean is None else max(self.max_mean, mean)
            if mean >= high:
                self.mean_high_s += dt
            if mean >= limit:
                self.mean_limit_s += dt

    def percentile(self, q):
        if self.time_s <= 0:
            return None
        target = q * self.time_s
        acc = 0.0
        for index, weight in enumerate(self.hist):
            acc += weight
            if acc >= target:
                return (index + 1) / HIST_BINS
        return 1.0

    def result(self, span_s=None):
        if self.time_s <= 0:
            return None
        out = {
            "mean": round(self.sum / self.time_s, 4),
            "max": round(self.max, 4),
            "p95": round(self.percentile(0.95), 4),
            "maxMean60": None if self.max_mean is None else round(self.max_mean, 4),
            "atSetpointS": round(self.time_s, 1),
            "shareHigh": round(self.mean_high_s / self.time_s, 4),
            "shareLimit": round(self.mean_limit_s / self.time_s, 4),
            "setpoint": self.setpoint,
        }
        if span_s:
            out["shareAtSetpoint"] = round(min(1.0, self.time_s / span_s), 4)
        return out

    def by_setpoint_result(self):
        return [{"setpoint": sp, "mean": round(s / t, 4), "timeS": round(t, 1)}
                for sp, (t, s) in sorted(self.by_setpoint.items()) if t > 0]


class _Track:
    def __init__(self, setpoint):
        self.setpoint = setpoint
        self.reached = False
        self.segments = []  # (t0, t1, load) within the window
        self.last_t = None
        self.last_load = None
        self.mean = None
        self.level = None


class HeaterLoadTracker:
    """Feed ``update(now_s, heaters, ...)`` on every patch and heartbeat.

    ``heaters``: ``{index: (state, active, current, avg_pwm, max_pwm)}`` of every heater.
    Returns level transitions of nozzle heaters: ``[("start"|"change"|"end", heater, info)]``.
    """

    def __init__(self, settings):
        self.configure(settings)
        self._tracks = {}
        self._layer = {}
        self._job = {}
        self.events = {}         # heater -> number of heater_load events this job
        self.first_event_layer = {}

    def configure(self, settings):
        cfg = settings.get("heaterLoad", {})
        self.high = cfg.get("high", 0.8)
        self.limit = cfg.get("limit", 0.9)
        self.hysteresis = cfg.get("hysteresis", 0.05)
        self.window_s = cfg.get("windowS", 60)
        self.min_coverage = cfg.get("minCoverage", 0.75)
        self.tolerance = cfg.get("reachedToleranceK", 2)

    def reset_job(self):
        self._tracks = {}
        self._layer = {}
        self._job = {}
        self.events = {}
        self.first_event_layer = {}

    def reset_layer(self):
        self._layer = {}

    def levels(self):
        return {h: t.level for h, t in self._tracks.items() if t.level is not None}

    def means(self):
        return {h: t.mean for h, t in self._tracks.items() if t.mean is not None}

    def _end(self, heater, track, transitions, nozzles):
        if track.level is not None and heater in nozzles:
            transitions.append(("end", heater, {"level": track.level, "mean": track.mean}))
        track.level = None

    def update(self, now_s, heaters, processing, nozzles, layer=None):
        transitions = []
        for heater in list(self._tracks):
            if heater not in heaters:
                self._end(heater, self._tracks.pop(heater), transitions, nozzles)
        for heater, (state, active, current, avg_pwm, max_pwm) in heaters.items():
            counting = processing and state == "active" and active is not None and active > 0
            track = self._tracks.get(heater)
            if not counting:
                if track is not None:
                    self._end(heater, track, transitions, nozzles)
                    del self._tracks[heater]
                continue
            if track is None or track.setpoint != active:
                if track is not None:
                    self._end(heater, track, transitions, nozzles)
                track = _Track(active)
                self._tracks[heater] = track
            if not track.reached:
                track.reached = current is not None and current >= active - self.tolerance
                if not track.reached:
                    continue
            load = heater_load(avg_pwm, max_pwm)
            # time since the previous update counts with the previous value
            if track.last_t is not None and track.last_load is not None:
                dt = now_s - track.last_t
                if 0 < dt <= MAX_HOLD_S:
                    track.segments.append((track.last_t, now_s, track.last_load))
                    self._accumulate(heater, track.last_load, dt, track.mean, active)
            track.last_t = now_s
            track.last_load = load
            start = now_s - self.window_s
            while track.segments and track.segments[0][1] <= start:
                track.segments.pop(0)
            covered = 0.0
            weighted = 0.0
            for t0, t1, value in track.segments:
                t0 = max(t0, start)
                if t1 > t0 and value is not None:
                    covered += t1 - t0
                    weighted += (t1 - t0) * value
            track.mean = weighted / covered if covered > 0 and covered >= self.min_coverage * self.window_s else None
            if heater not in nozzles:
                continue
            previous = track.level
            track.level = None if track.mean is None else load_level(
                track.mean, previous, self.high, self.limit, self.hysteresis)
            info = {"level": track.level, "mean": track.mean, "setpoint": active, "tool": nozzles.get(heater)}
            if previous is None and track.level is not None:
                self.events[heater] = self.events.get(heater, 0) + 1
                if layer is not None:
                    self.first_event_layer.setdefault(heater, layer)
                transitions.append(("start", heater, info))
            elif previous is not None and track.level is None:
                transitions.append(("end", heater, {**info, "level": previous}))
            elif previous != track.level:
                transitions.append(("change", heater, info))
        return transitions

    def _accumulate(self, heater, load, dt, mean, setpoint):
        if load is None:
            return
        for bucket in (self._layer, self._job):
            stats = bucket.get(heater)
            if stats is None:
                stats = bucket[heater] = _Stats()
            stats.add(load, dt, mean, setpoint, self.high, self.limit)

    def layer_stats(self, span_s=None):
        return {str(h): s.result(span_s) for h, s in self._layer.items() if s.time_s > 0}

    def job_stats(self, nozzles):
        result = {}
        for heater, stats in self._job.items():
            if stats.time_s <= 0:
                continue
            entry = stats.result()
            entry["bySetpoint"] = stats.by_setpoint_result()
            entry["nozzle"] = heater in nozzles
            if heater in nozzles:
                entry["tool"] = nozzles.get(heater)
                entry["events"] = self.events.get(heater, 0)
                entry["firstEventLayer"] = self.first_event_layer.get(heater)
            result[str(heater)] = entry
        return result
