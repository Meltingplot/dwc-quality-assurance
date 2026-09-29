"""Layer aggregates and job summary (PLAN.md §3 "Lagenaggregate", "Job-Zusammenfassung").

Both work on channel snapshots (``{name: value}`` from qa_channels.extract) and are
time-weighted: a value holds until the next object-model patch, so ``advance(snapshot, dt)`` adds
the snapshot that was current during the last ``dt`` seconds.
"""

import math

# a hold longer than this is a gap in the data, not a held value
MAX_HOLD_S = 10.0
PERCENT_CLASS = 2          # % per class of the percentage distribution
FLOW_BIN = 1.0             # mm³/s per bin of the flow-vs-percentage curve


class TW:
    """Time-weighted min/max/mean/std."""

    __slots__ = ("t", "s", "ss", "min", "max")

    def __init__(self):
        self.t = 0.0
        self.s = 0.0
        self.ss = 0.0
        self.min = None
        self.max = None

    def add(self, value, dt):
        if value is None or dt <= 0:
            return
        self.t += dt
        self.s += value * dt
        self.ss += value * value * dt
        self.min = value if self.min is None else min(self.min, value)
        self.max = value if self.max is None else max(self.max, value)

    @property
    def mean(self):
        return self.s / self.t if self.t > 0 else None

    @property
    def std(self):
        if self.t <= 0:
            return None
        variance = self.ss / self.t - (self.s / self.t) ** 2
        return math.sqrt(max(0.0, variance))

    def result(self, digits=3):
        if self.t <= 0:
            return None
        return {"min": round(self.min, digits), "max": round(self.max, digits),
                "mean": round(self.mean, digits), "std": round(self.std, digits)}


def _indices(snapshot, prefix, suffix):
    out = []
    for name in snapshot:
        if name.startswith(prefix) and name.endswith(suffix):
            middle = name[len(prefix):len(name) - len(suffix)]
            if middle.isdigit():
                out.append(int(middle))
    return sorted(set(out))


class _Filament:
    """Commanded, measured and extruder filament of one interval, summed over the counters' restarts.

    The monitor's counters (Duet3Expansion 3.7-dev @ 806ef34 RotatingMagnetFilamentMonitor.cpp:628-643
    and RRF 3.7-dev @ 3638836 Duet3DFilamentMonitor.cpp:46 / RotatingMagnetFilamentMonitor.cpp:46,
    read 2026-09-28): ``totalExtrusion`` and ``calibrated.totalDistance`` are both the *commanded*
    extrusion since calibration started; the measured movement is only in ``avgPercentage`` =
    100 × measured / commanded over the same span, so measured = totalExtrusion × avgPercentage / 100
    (as chx350-config's e-steps and NLE macros use it). While the toolboard calibrates it sends no
    live data (the object model keeps the old total, avgPercentage is null), and it restarts both
    whenever the machine does not print (FilamentMonitor.cpp:318-325, Clear → Reset): at every job
    start and pause. RRF zeroes the extruder positions when a print starts (GCodes.cpp:3861-3864).

    ``avgPercentage`` is an integer, so one step moves the measured total by 1 % of everything since
    the restart: exact enough for a job, far too coarse for a layer. A layer therefore weights its
    commanded millimetres with ``lastPercentage`` (the ratio of the monitor's latest check segment).
    """

    def __init__(self, first):
        self.prev_c = {}      # fm -> last totalExtrusion (also while stale)
        self.prev_m = {}      # fm -> last measured total, None without live data
        self.prev_e = {}      # extruder -> last position
        self.commanded = {}
        self.measured = {}
        self.weighted = {}    # fm -> [commanded with a lastPercentage, measured estimated from it]
        self.extruder = {}
        if first:
            self.observe(first)

    def observe(self, snap):
        for i in _indices(snap, "fm.", ".totalExtrusion"):
            c = snap.get(f"fm.{i}.totalExtrusion")
            if c is None:
                continue
            a = snap.get(f"fm.{i}.avgPercentage")
            prev_c, prev_m = self.prev_c.get(i), self.prev_m.get(i)
            self.prev_c[i] = c
            m = c * a / 100.0 if a is not None else None
            self.prev_m[i] = m
            if prev_c is None or m is None:
                continue          # the base, or no live data while the monitor calibrates
            restarted = c < prev_c - 1.0   # the total counts up in whole millimetres until a restart
            dc = c if restarted else c - prev_c
            dm = m if restarted else (m - prev_m if prev_m is not None else dc * a / 100.0)
            self.commanded[i] = self.commanded.get(i, 0.0) + dc
            self.measured[i] = self.measured.get(i, 0.0) + dm
            pct = snap.get(f"fm.{i}.lastPercentage")
            if pct is not None and dc > 0:
                w = self.weighted.setdefault(i, [0.0, 0.0])
                w[0] += dc
                w[1] += dc * pct / 100.0
        for i in _indices(snap, "extruder.", ".position"):
            e = snap.get(f"extruder.{i}.position")
            if e is None:
                continue
            prev = self.prev_e.get(i)
            self.prev_e[i] = e
            if prev is None:
                continue
            # zeroed by a print start (or G92 E0); a retraction or a pause's retract only steps back
            restarted = e < prev - 5.0 and abs(e) < 0.1 * abs(prev)
            self.extruder[i] = self.extruder.get(i, 0.0) + (e if restarted else e - prev)

    def indices(self):
        return sorted(set(self.prev_c) | set(self.prev_e))

    def entry(self, i, per_layer):
        commanded = self.commanded.get(i)
        extruder = self.extruder.get(i)
        entry = {"commandedMm": None if commanded is None else round(commanded, 3), "measuredMm": None,
                 "extruderMm": None if extruder is None else round(extruder, 3)}
        if per_layer:
            covered, measured = self.weighted.get(i, (0.0, 0.0))
            if covered > 0 and commanded:
                entry["measuredMm"] = round(commanded * measured / covered, 3)
                entry["ratio"] = round(measured / covered, 4)
        elif i in self.measured:
            entry["measuredMm"] = round(self.measured[i], 3)
            if commanded and commanded > 0:
                entry["ratio"] = round(self.measured[i] / commanded, 4)
        return entry


class FeedReference:
    """The e-steps each extruder of a job started feeding with: the value that held when it first fed
    filament inside a layer. Not the job start: the start G-code sets the filament's M92 after QA's
    context snapshot (800 → 801 in job 20260928-134928-118609a9), and a tool's filament config
    only at its tool change. Kept in the job context as ``feedReference``, so a daemon restart
    continues with it instead of taking a value the MFM has corrected meanwhile."""

    def __init__(self, stored=None):
        self.values = {}      # extruder -> {"stepsPerMm", "layer"}
        for key, entry in (stored or {}).items():
            if str(key).isdigit() and isinstance(entry, dict) and entry.get("stepsPerMm"):
                self.values[int(key)] = dict(entry)
        self.changed = False  # a new value the collector has not stored yet

    def get(self, extruder, steps_per_mm, layer):
        """The reference of ``extruder``; the first call with e-steps sets it."""
        entry = self.values.get(extruder)
        if entry is None:
            if not steps_per_mm:
                return None
            entry = self.values[extruder] = {"stepsPerMm": steps_per_mm, "layer": layer}
            self.changed = True
        return entry["stepsPerMm"]

    def to_json(self):
        return {str(i): dict(entry) for i, entry in sorted(self.values.items())}


class _Feed:
    """Feed factor per extruder: filament the gear pushed per millimetre the file asked for, relative
    to the job's ``FeedReference``, i.e. e-steps / reference × extrusion factor (M221); 1.0417 after
    the MFM's M92 801 → 834.38 (job 20260928-155257-118609a9). Counted over the extruder's forward
    movement only, with the values that held meanwhile, so pauses and retractions do not count and a
    change within the layer weighs by the filament fed before and after it.

    ``move.extruders[].position`` is the extruder's machine coordinate, i.e. after the extrusion
    factor, macros included; ``rawPosition`` leaves the macros out (RRF 3.7-dev @ a4b8080 Move.cpp:304-310,
    GCodes.cpp:2064-2094, read 2026-09-29). The file's millimetres are therefore position / factor."""

    def __init__(self, layer, reference, first):
        self.layer = layer
        self.reference = reference
        self.prev = {}        # extruder -> (position, stepsPerMm, factor) of the last snapshot
        self.sums = {}        # extruder -> [Σ position mm, Σ file mm, Σ position mm × e-steps]
        self.range = {}       # extruder -> [min, max] of the feed factor while feeding
        if first:
            self.observe(first)

    def observe(self, snap):
        for i in _indices(snap, "extruder.", ".position"):
            e = snap.get(f"extruder.{i}.position")
            prev = self.prev.get(i)
            self.prev[i] = (e, snap.get(f"extruder.{i}.stepsPerMm"), snap.get(f"extruder.{i}.factor"))
            if prev is None or e is None or prev[0] is None or self.reference is None:
                continue
            fed = e - prev[0]
            steps, factor = prev[1], (prev[2] if prev[2] is not None else 1.0)
            if fed <= 0 or not steps or factor <= 0:
                continue      # retraction, standstill, zeroed by a print start (or G92 E0)
            reference = self.reference.get(i, steps, self.layer)
            feed = steps / reference * factor
            sums = self.sums.setdefault(i, [0.0, 0.0, 0.0])
            sums[0] += fed
            sums[1] += fed / factor
            sums[2] += fed * steps
            span = self.range.setdefault(i, [feed, feed])
            span[0], span[1] = min(span[0], feed), max(span[1], feed)

    def result(self):
        out = {}
        for i, (mm, file_mm, steps_mm) in self.sums.items():
            reference = self.reference.get(i, None, self.layer)
            steps = steps_mm / mm
            out[str(i)] = {"factor": round(steps_mm / (reference * file_mm), 4),
                           "min": round(self.range[i][0], 4), "max": round(self.range[i][1], 4),
                           "stepsPerMm": round(steps, 2), "extrusionFactor": round(mm / file_mm, 3),
                           "reference": reference}
        return out


def cross_section(diameter):
    d = diameter if diameter and diameter > 0 else 1.75
    return math.pi * (d / 2) ** 2


class LayerAccumulator:
    """Aggregates of one layer."""

    def __init__(self, layer, started_ms, first, filament_diameters, chamber_channel, sensor_names,
                 feed_reference=None):
        self.layer = layer
        self.started_ms = started_ms
        self.first = dict(first) if first else {}
        self.diameters = filament_diameters or {}
        self.chamber_channel = chamber_channel
        self.sensor_names = sensor_names or {}
        self.heaters = {}        # i -> TW of current
        self.sensors = {}        # i -> TW of lastReading
        self.chamber = TW()
        self.fm = {}             # i -> TW of lastPercentage
        self.pwm = {}            # i -> (TW avgPwm, TW current) at a constant, reached setpoint
        self.setpoints = {}      # i -> first setpoint seen in the layer (None: changed)
        self.span_s = 0.0
        self.print_z = None      # Z of the last extruding sample (travel Z hops do not count)
        self.filament = _Filament(first)
        self.feed = _Feed(layer, feed_reference, first)

    def advance(self, snap, dt):
        self.filament.observe(snap)  # counters: a gap in the data loses nothing
        self.feed.observe(snap)
        if dt <= 0 or dt > MAX_HOLD_S:
            return
        self.span_s += dt
        if (snap.get("move.currentMove.extrusionRate") or 0) > 0 and snap.get("axis.Z.machinePosition") is not None:
            self.print_z = snap["axis.Z.machinePosition"]
        for i in _indices(snap, "heater.", ".current"):
            self.heaters.setdefault(i, TW()).add(snap.get(f"heater.{i}.current"), dt)
            active = snap.get(f"heater.{i}.active")
            state = snap.get(f"heater.{i}.state")
            if i not in self.setpoints:
                self.setpoints[i] = active
            elif self.setpoints[i] != active:
                self.setpoints[i] = None  # changed within the layer
            current = snap.get(f"heater.{i}.current")
            if state == 2 and active and current is not None and current >= active - 2 and self.setpoints[i] is not None:
                pwm, temp = self.pwm.setdefault(i, (TW(), TW()))
                pwm.add(snap.get(f"heater.{i}.avgPwm"), dt)
                temp.add(current, dt)
        for i in _indices(snap, "sensor.", ".lastReading"):
            self.sensors.setdefault(i, TW()).add(snap.get(f"sensor.{i}.lastReading"), dt)
        if self.chamber_channel:
            self.chamber.add(snap.get(self.chamber_channel), dt)
        for i in _indices(snap, "fm.", ".lastPercentage"):
            self.fm.setdefault(i, TW()).add(snap.get(f"fm.{i}.lastPercentage"), dt)

    def finish(self, ended_ms, last, height=None, z=None, fraction_printed=None, load_stats=None):
        last = last or {}
        duration_s = max(0.0, (ended_ms - self.started_ms) / 1000.0)
        filament = {}
        flow = {}
        self.filament.observe(last)
        self.feed.observe(last)
        for i in self.filament.indices():
            entry = self.filament.entry(i, per_layer=True)
            filament[str(i)] = entry
            # the extruder position, not the monitor's total: that one advances a check segment at a
            # time (5 mm on the CHX 350), so most small layers showed flow 0 (commandedMm 0/5/10 per
            # layer in job 20260928-155257-118609a9, 2026-09-28)
            commanded, extruder = entry["commandedMm"], entry["extruderMm"]
            basis = extruder if extruder is not None else commanded
            if basis is not None and duration_s > 0:
                flow[str(i)] = round(max(0.0, basis) * cross_section(self.diameters.get(i)) / duration_s, 3)
        temps = {"heaters": {}, "sensors": {}, "chamber": self.chamber.result(2)}
        for i, tw in self.heaters.items():
            entry = tw.result(2)
            if entry:
                entry["setpoint"] = last.get(f"heater.{i}.active")
                temps["heaters"][str(i)] = entry
        for i, tw in self.sensors.items():
            entry = tw.result(2)
            if entry:
                entry["name"] = self.sensor_names.get(i)
                temps["sensors"][str(i)] = entry
        fm_stats = {}
        for i, tw in self.fm.items():
            entry = tw.result(2)
            if entry:
                entry["avgPercentage"] = last.get(f"fm.{i}.avgPercentage")
                entry["mmPerRev"] = last.get(f"fm.{i}.calibrated.mmPerRev")
                fm_stats[str(i)] = entry
        pwm_stats = {}
        for i, (pwm, temp) in self.pwm.items():
            if pwm.t > 0:
                pwm_stats[str(i)] = {"avgPwmMean": round(pwm.mean, 4), "avgPwmStd": round(pwm.std, 4),
                                     "currentStd": round(temp.std, 3) if temp.t > 0 else None,
                                     "timeS": round(pwm.t, 1)}
        return {
            "layer": self.layer,
            "started_at": self.started_ms,
            "ended_at": ended_ms,
            "duration_s": round(duration_s, 2),
            "height": height,
            "z": z if z is not None else self.print_z,
            "fraction_printed": fraction_printed,
            "filament": filament,
            "flow": flow,
            "feed": self.feed.result(),
            "temps": temps,
            "fm_stats": fm_stats,
            "pwm_stats": pwm_stats,
            "load_stats": load_stats or {},
        }


class JobAccumulator:
    """Job-level statistics that are not a sum of layers."""

    def __init__(self, first, filament_diameters, chamber_channel):
        self.first = dict(first) if first else {}
        self.last = dict(first) if first else {}
        self.diameters = filament_diameters or {}
        self.chamber_channel = chamber_channel
        self.chamber = TW()
        self.percent_hist = {}   # fm -> {class_start: seconds}
        self.flow_curve = {}     # fm -> {flow_bin: TW of lastPercentage}
        self.pwm_at_setpoint = {}  # heater -> TW avgPwm while reached
        self.heat_up = {}        # heater -> {"since": ms, "setpoint": v, "seconds": [..]}
        self.filament = _Filament(first)

    def advance(self, snap, dt, now_ms):
        self.filament.observe(snap)
        if dt <= 0 or dt > MAX_HOLD_S:
            self.last = dict(snap)
            return
        if self.chamber_channel:
            self.chamber.add(snap.get(self.chamber_channel), dt)
        rate = snap.get("move.currentMove.extrusionRate")
        for i in _indices(snap, "fm.", ".lastPercentage"):
            pct = snap.get(f"fm.{i}.lastPercentage")
            if pct is None:
                continue
            hist = self.percent_hist.setdefault(i, {})
            cls = int(pct // PERCENT_CLASS) * PERCENT_CLASS
            hist[cls] = hist.get(cls, 0.0) + dt
            if rate and rate > 0:
                flow = rate * cross_section(self.diameters.get(i))
                b = int(flow // FLOW_BIN)
                self.flow_curve.setdefault(i, {}).setdefault(b, TW()).add(pct, dt)
        for i in _indices(snap, "heater.", ".current"):
            active = snap.get(f"heater.{i}.active")
            current = snap.get(f"heater.{i}.current")
            state = snap.get(f"heater.{i}.state")
            if state == 2 and active and current is not None and current >= active - 2:
                self.pwm_at_setpoint.setdefault(i, TW()).add(snap.get(f"heater.{i}.avgPwm"), dt)
        self.last = dict(snap)

    def heater_setpoints(self, snap, now_ms):
        """Heat-up time per heater: from a new setpoint (state active) to current >= setpoint - 2 K."""
        for i in _indices(snap, "heater.", ".current"):
            active = snap.get(f"heater.{i}.active")
            state = snap.get(f"heater.{i}.state")
            current = snap.get(f"heater.{i}.current")
            track = self.heat_up.setdefault(i, {"since": None, "setpoint": None, "seconds": []})
            if state != 2 or not active:
                track["since"] = None
                track["setpoint"] = None
                continue
            if track["setpoint"] != active:
                track["setpoint"] = active
                track["since"] = now_ms if (current is None or current < active - 2) else None
            elif track["since"] is not None and current is not None and current >= active - 2:
                track["seconds"].append({"setpoint": active, "s": round((now_ms - track["since"]) / 1000.0, 1)})
                track["since"] = None

    def filament_totals(self):
        result = {}
        self.filament.observe(self.last)
        for i in self.filament.indices():
            entry = self.filament.entry(i, per_layer=False)
            entry["avgPercentage"] = self.last.get(f"fm.{i}.avgPercentage")
            entry["mmPerRev"] = self.last.get(f"fm.{i}.calibrated.mmPerRev")
            hist = self.percent_hist.get(i)
            if hist:
                entry["percentDistribution"] = [{"from": c, "to": c + PERCENT_CLASS, "s": round(s, 1)}
                                                for c, s in sorted(hist.items())]
            curve = self.flow_curve.get(i)
            if curve:
                entry["flowVsPercent"] = [{"flowFrom": b * FLOW_BIN, "flowTo": (b + 1) * FLOW_BIN,
                                           "mean": round(tw.mean, 2), "std": round(tw.std, 2), "s": round(tw.t, 1)}
                                          for b, tw in sorted(curve.items()) if tw.t > 0]
            result[str(i)] = entry
        return result

    def thermal(self):
        return {
            "chamber": self.chamber.result(2),
            "avgPwmAtSetpoint": {str(i): round(tw.mean, 4) for i, tw in self.pwm_at_setpoint.items() if tw.t > 0},
            "heatUp": {str(i): t["seconds"] for i, t in self.heat_up.items() if t["seconds"]},
        }


def event_summary(events):
    """``events``: list of dicts with type, layer, subtype, payload. Count per type, first/last layer, and
    how many stayed unconfirmed (a driver's open load that cleared within 500 ms, qa_collector)."""
    result = {}
    for event in events:
        entry = result.setdefault(event["type"], {"count": 0, "firstLayer": None, "lastLayer": None})
        entry["count"] += 1
        if (event.get("payload") or {}).get("confirmed") is False:
            entry["unconfirmed"] = entry.get("unconfirmed", 0) + 1
        layer = event.get("layer")
        if layer is not None:
            entry["firstLayer"] = layer if entry["firstLayer"] is None else min(entry["firstLayer"], layer)
            entry["lastLayer"] = layer if entry["lastLayer"] is None else max(entry["lastLayer"], layer)
    return result


def mfm_summary(events, globals_end):
    tolerated = [e for e in events if e["type"] == "mfm_error_tolerated"]
    recoveries = [{"layer": e.get("layer"), "result": (e.get("payload") or {}).get("result"), "subtype": e.get("subtype")}
                  for e in events if e["type"] == "mfm_recovery" and e.get("subtype") != "requested"]
    bias = [e for e in events if e["type"] == "mfm_flow_bias"]
    applied = [e for e in events if e["type"] == "setpoint_change" and e.get("subtype") == "stepsPerMm"
               and (e.get("payload") or {}).get("cause") == "mfm_flow_bias"]
    if not (tolerated or recoveries or bias or applied or globals_end):
        return None
    suggested = None
    for e in bias:
        suggested = (e.get("payload") or {}).get("mfm_esteps_suggested", suggested)
    return {
        "toleratedErrors": len(tolerated),
        "recoveries": recoveries,
        "flowBiasDetected": bool(bias),
        "estepsSuggested": suggested,
        "estepsApplied": [(e.get("payload") or {}).get("to") for e in applied],
    }


def spool_usage(globals_start, globals_end):
    """Grams per tool from global.spool_remaining at start and end (chx350-config books
    consumption into it)."""
    start = (globals_start or {}).get("spool_remaining")
    end = (globals_end or {}).get("spool_remaining")
    if not isinstance(start, list) or not isinstance(end, list):
        return None
    result = {}
    for tool, (a, b) in enumerate(zip(start, end)):
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
            result[str(tool)] = round(a - b, 2)
    return result or None
