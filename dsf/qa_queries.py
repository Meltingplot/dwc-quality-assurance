"""Read side of the database for the HTTP API (PLAN.md §5.6, contract with the CHX UI §5.10).

Pure functions on a read-only SQLite connection; every one answers from indexed ranges so an
endpoint stays well below the 10 s the HMI's haproxy gives a busy backend.
"""

import datetime
import json

import qa_db
import qa_severity

MAX_POINTS_PER_CHANNEL = 20_000

# QA result -> CHX UI history result (useJobHistory.ts: running|finished|cancelled|aborted).
# "unknown" = the daemon was not running when the job ended.
CHX_RESULT = {"running": "running", "completed": "finished", "cancelled": "cancelled", "aborted": "aborted",
              "unknown": "unknown"}


def iso(ms):
    if ms is None:
        return None
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _loads(row, *columns):
    out = dict(row)
    for column in columns:
        if column in out:
            out[column] = qa_db.loads(out[column])
    return out


def job_key(con, job_id):
    row = con.execute("SELECT key FROM jobs WHERE id=?", (job_id,)).fetchone()
    return row[0] if row else None


def _summary_excerpt(summary):
    if not summary:
        return None
    heater_load = (summary.get("thermal") or {}).get("heaterLoad") or {}
    filament = summary.get("filament") or {}
    return {
        "abortReason": summary.get("abortReason"),
        "layers": summary.get("layers"),
        "events": {k: v.get("count") for k, v in (summary.get("events") or {}).items()},
        "filamentRatio": {k: v.get("ratio") for k, v in filament.items() if v.get("ratio") is not None},
        "avgPercentage": {k: v.get("avgPercentage") for k, v in filament.items() if v.get("avgPercentage") is not None},
        "heaterLoadMean": {k: v.get("mean") for k, v in heater_load.items() if v.get("nozzle")},
        "heaterLoadEvents": {k: v.get("events") for k, v in heater_load.items() if v.get("nozzle")},
        "spoolUsageG": summary.get("spoolUsageG"),
    }


def event_levels(con, rows, ranges=None):
    """Job key -> ``{"info", "warning", "error"}`` counts of its events (qa_severity) for the job rows."""
    keys = [row["key"] for row in rows]
    ended = {row["key"]: row["ended_at"] for row in rows}
    result = {key: dict.fromkeys(qa_severity.LEVELS, 0) for key in keys}
    for start in range(0, len(keys), 500):
        chunk = keys[start:start + 500]
        for event in con.execute(f"SELECT job_key, type, subtype, ts_ms, payload FROM events "
                                 f"WHERE job_key IN ({','.join('?' * len(chunk))})", chunk):
            entry = {"type": event["type"], "subtype": event["subtype"], "ts_ms": event["ts_ms"],
                     "payload": qa_db.loads(event["payload"])}
            result[event["job_key"]][qa_severity.level(entry, ranges, ended[event["job_key"]])] += 1
    return result


def job_list_entry(row, levels=None):
    """``levels``: the job's event counts per level (event_levels)."""
    summary = qa_db.loads(row["summary"])
    ended, started = row["ended_at"], row["started_at"]
    return {
        # CHX UI contract (useJobHistory: HistoryEntry + analysable)
        "id": row["id"],
        "file": row["file_name"],
        "result": CHX_RESULT.get(row["result"], row["result"]),
        "printTimeS": row["duration_s"],
        "timestamp": iso(ended if ended is not None else started),
        "analysable": True,
        # QA
        "qaResult": row["result"],
        "startedAt": iso(started),
        "endedAt": iso(ended),
        "partial": bool(row["partial"]),
        "numLayers": row["num_layers"],
        "material": row["material"],
        "rawPruned": bool(row["raw_pruned"]),
        "summary": _summary_excerpt(summary),
        # warnings and errors rate the job, info does not (qa_severity)
        "events": levels,
    }


def jobs(con, limit=50, offset=0, result=None, material=None, ranges=None):
    where = []
    args = []
    if result:
        where.append("result=?")
        args.append({"finished": "completed"}.get(result, result))
    if material:
        where.append("material=?")
        args.append(material)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = con.execute(f"SELECT COUNT(*) FROM jobs {clause}", args).fetchone()[0]
    rows = con.execute(f"SELECT * FROM jobs {clause} ORDER BY started_at DESC LIMIT ? OFFSET ?",
                       (*args, limit, offset)).fetchall()
    levels = event_levels(con, rows, ranges)
    return {"total": total, "offset": offset, "jobs": [job_list_entry(r, levels[r["key"]]) for r in rows]}


def job(con, job_id, ranges=None):
    row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return None
    out = job_list_entry(row, event_levels(con, [row], ranges)[row["key"]])
    out.update({
        "fileCrc32": row["file_crc32"],
        "startLayer": row["start_layer"],
        "durationS": row["duration_s"],
        "warmupS": row["warmup_s"],
        "pauseS": row["pause_s"],
        "context": qa_db.loads(row["context"]),
        "summary": qa_db.loads(row["summary"]),
    })
    return out


def layers(con, job_id, load_thresholds=None):
    """Layer aggregates (§5.10: the CHX analysis channels come from these)."""
    key = job_key(con, job_id)
    if key is None:
        return None
    ctx_row = con.execute("SELECT context FROM jobs WHERE key=?", (key,)).fetchone()
    context = qa_db.loads(ctx_row[0]) or {}
    rows = con.execute("SELECT * FROM job_layers WHERE job_key=? ORDER BY layer", (key,)).fetchall()
    result = []
    for row in rows:
        entry = _loads(row, "filament", "flow", "temps", "fm_stats", "pwm_stats", "load_stats", "filament_path",
                       "feed")
        entry.pop("job_key", None)
        entry["startedAt"] = iso(entry.pop("started_at"))
        entry["endedAt"] = iso(entry.pop("ended_at"))
        entry["durationS"] = entry.pop("duration_s")
        entry["fractionPrinted"] = entry.pop("fraction_printed")
        entry["fmStats"] = entry.pop("fm_stats")
        entry["pwmStats"] = entry.pop("pwm_stats")
        entry["loadStats"] = entry.pop("load_stats")
        entry["filamentPath"] = entry.pop("filament_path")
        result.append(entry)
    return {
        "jobId": job_id,
        "meta": {
            "sensors": context.get("sensors", []),
            "heaters": [{"index": h["index"], "role": h["role"], "tool": h.get("tool"), "sensor": h.get("sensor"),
                         "sensorName": h.get("sensorName")} for h in context.get("heaters", [])],
            "chamber": context.get("chamber"),
            "filamentDiameters": {str(e["index"]): e.get("filamentDiameter") for e in context.get("extruders", [])},
            "heaterLoad": load_thresholds or {"high": 0.8, "limit": 0.9},
        },
        "layers": result,
    }


def events(con, job_id, type_=None, ranges=None):
    """The job's events, each with its ``severity`` (qa_severity)."""
    row = con.execute("SELECT key, ended_at FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return None
    key, ended_ms = row["key"], row["ended_at"]
    sql = "SELECT * FROM events WHERE job_key=?"
    args = [key]
    if type_:
        types = [t for t in type_.split(",") if t]
        sql += f" AND type IN ({','.join('?' * len(types))})"
        args += types
    rows = con.execute(sql + " ORDER BY ts_ms, id", args).fetchall()
    out = []
    for row in rows:
        entry = _loads(row, "positions", "offsets", "payload")
        entry.pop("job_key", None)
        entry["ts"] = iso(entry["ts_ms"])
        entry["severity"] = qa_severity.level(entry, ranges, ended_ms)
        out.append(entry)
    return {"jobId": job_id, "events": out}


def blocks(con, job_id):
    key = job_key(con, job_id)
    if key is None:
        return None
    rows = con.execute("SELECT id, start_ms, end_ms, triggers FROM blocks WHERE job_key=? ORDER BY start_ms", (key,))
    return {"jobId": job_id, "blocks": [_loads(r, "triggers") for r in rows]}


def channels(con):
    names = [r[0] for r in con.execute("SELECT name FROM channels ORDER BY name")]
    derived = sorted({n.replace(".avgPwm", ".load") for n in names if n.startswith("heater.") and n.endswith(".avgPwm")})
    return {"channels": names, "derived": derived}


def _max_pwm_timeline(con, key, context, heater):
    """[(from_ms, maxPwm)] for a heater: the job-start value, then every change event."""
    start = None
    for h in (context or {}).get("heaters", []):
        if h["index"] == heater:
            start = ((h.get("model") or {}).get("maxPwm"))
    timeline = [(0, start if start and start > 0 else 1.0)]
    for row in con.execute("SELECT ts_ms, payload FROM events WHERE job_key=? AND type='setpoint_change' "
                           "AND subtype='heater.maxPwm' AND device=? ORDER BY ts_ms", (key, heater)):
        value = (qa_db.loads(row["payload"]) or {}).get("to")
        timeline.append((row["ts_ms"], value if value and value > 0 else 1.0))
    return timeline


def _series(con, key, channel, start, end, resolution):
    row = con.execute("SELECT id FROM channels WHERE name=?", (channel,)).fetchone()
    if row is None:
        return []
    sql = "SELECT ts_ms, value, resolution FROM samples WHERE job_key=? AND channel=? AND ts_ms BETWEEN ? AND ?"
    args = [key, row[0], start, end]
    if resolution == "coarse":
        sql += " AND resolution=0"
    elif resolution == "fine":
        sql += " AND block_id IS NOT NULL"
    return con.execute(sql + " ORDER BY ts_ms", args).fetchall()


def samples(con, job_id, names, start=None, end=None, resolution="auto"):
    """Time series ``{channel: [[ts_ms, value], ...]}``; ``heater.<n>.load`` is derived from
    avgPwm and the maxPwm valid at the time (§3 "Abgeleitete Kanäle"). Downsampled by stride
    beyond MAX_POINTS_PER_CHANNEL."""
    row = con.execute("SELECT key, context, started_at, ended_at FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return None
    key = row["key"]
    start = row["started_at"] if start is None else start
    end = (row["ended_at"] or 2 ** 62) if end is None else end
    context = None
    result = {}
    downsampled = False
    for name in names:
        if name.startswith("heater.") and name.endswith(".load"):
            heater = int(name.split(".")[1])
            if context is None:
                context = qa_db.loads(row["context"])
            timeline = _max_pwm_timeline(con, key, context, heater)
            points = []
            index = 0
            for r in _series(con, key, f"heater.{heater}.avgPwm", start, end, resolution):
                while index + 1 < len(timeline) and timeline[index + 1][0] <= r["ts_ms"]:
                    index += 1
                points.append([r["ts_ms"], round(min(1.0, max(0.0, r["value"] / timeline[index][1])), 4)])
        else:
            points = [[r["ts_ms"], r["value"]] for r in _series(con, key, name, start, end, resolution)]
        if len(points) > MAX_POINTS_PER_CHANNEL:
            stride = -(-len(points) // MAX_POINTS_PER_CHANNEL)
            points = points[::stride]
            downsampled = True
        result[name] = points
    return {"jobId": job_id, "from": start, "to": end, "resolution": resolution, "downsampled": downsampled,
            "channels": result}


def timelapse(con, job_id):
    """The job's timelapse row with its layer index, None when none was recorded."""
    row = con.execute("SELECT t.*, j.id AS job_id FROM timelapse t JOIN jobs j ON j.key = t.job_key WHERE j.id=?",
                      (job_id,)).fetchone()
    if row is None:
        return None
    entry = _loads(row, "layer_frames")
    entry["layer_frames"] = entry.get("layer_frames") or []
    return entry


def layer_frame(entries, layer):
    """Frame number of ``layer`` in a timelapse index (the latest one if it was taken twice)."""
    frames = [e["frame"] for e in entries if e.get("layer") == layer and e.get("frame") is not None]
    return frames[-1] if frames else None


def spectra(con, job_id):
    key = job_key(con, job_id)
    if key is None:
        return None
    rows = con.execute("SELECT * FROM spectra WHERE job_key=? ORDER BY ts_ms", (key,)).fetchall()
    out = []
    for row in rows:
        entry = _loads(row, "freqs", "amplitudes", "fans")
        entry.pop("job_key", None)
        out.append(entry)
    return {"jobId": job_id, "spectra": out}


def _spectra_rows(con, where, args):
    rows = con.execute(f"SELECT s.*, j.id AS job_id FROM spectra s JOIN jobs j ON j.key = s.job_key WHERE {where}",
                       args).fetchall()
    return [_loads(r, "freqs", "amplitudes", "fans") for r in rows]


def references(con, auto_count=5):
    """The reference spectrum of each axis. ``manual``: the chosen spectrum. ``auto``: the
    element-wise median of the first ``auto_count`` spectra of the axis (interpolated onto the grid
    of the first, qa_accel.median_spectrum); ``complete`` false while there are fewer."""
    import qa_accel

    settings = {r["axis"]: dict(r) for r in con.execute("SELECT * FROM reference_spectra")}
    out = []
    for axis in ("X", "Y", "Z"):
        setting = settings.get(axis) or {"mode": "auto", "spectrum_id": None, "set_at": None}
        if setting["mode"] == "manual":
            spectra = _spectra_rows(con, "s.id=?", (setting["spectrum_id"],))
        else:
            spectra = _spectra_rows(con, "s.axis=? AND s.freqs IS NOT NULL ORDER BY s.ts_ms, s.id LIMIT ?",
                                    (axis, auto_count))
        entry = {"axis": axis, "mode": setting["mode"], "setAt": iso(setting["set_at"]),
                 "spectrumIds": [s["id"] for s in spectra], "jobIds": sorted({s["job_id"] for s in spectra}),
                 "complete": setting["mode"] == "manual" or len(spectra) >= auto_count,
                 "freqs": [], "amplitudes": [], "peakHz": None, "rms": None}
        usable = [s for s in spectra if s.get("freqs")]
        if usable:
            freqs, amplitudes = qa_accel.median_spectrum(usable)
            peaks = [(a, f) for f, a in zip(freqs, amplitudes) if f >= qa_accel.PEAK_MIN_HZ]
            rms = sorted(s["rms"] for s in usable if s.get("rms") is not None)
            entry.update({"freqs": freqs, "amplitudes": amplitudes, "peakHz": max(peaks)[1] if peaks else None,
                          "rms": rms[len(rms) // 2] if rms else None})
        out.append(entry)
    return {"references": out, "autoCount": auto_count}


def spectra_latest(con, axis, limit=5):
    """The newest spectrum of each of the last ``limit`` jobs that have one for ``axis``."""
    rows = con.execute(
        "SELECT MAX(s.id) AS id FROM spectra s JOIN jobs j ON j.key = s.job_key WHERE s.axis=? AND s.freqs IS NOT NULL "
        "GROUP BY s.job_key ORDER BY MAX(j.started_at) DESC LIMIT ?", (axis, limit)).fetchall()
    ids = [r["id"] for r in rows]
    if not ids:
        return {"axis": axis, "spectra": []}
    spectra = _spectra_rows(con, f"s.id IN ({','.join('?' * len(ids))})", ids)
    order = {sid: i for i, sid in enumerate(ids)}
    for s in spectra:
        s.pop("job_key", None)
        s["ts"] = iso(s["ts_ms"])
    return {"axis": axis, "spectra": sorted(spectra, key=lambda s: order[s["id"]])}


def export(con, job_id, ranges=None):
    detail = job(con, job_id, ranges)
    if detail is None:
        return None
    return {
        "exportedAt": iso(qa_db.now_ms()),
        "format": "dwc-quality-assurance/job/1",
        "job": detail,
        "layers": layers(con, job_id)["layers"],
        "events": events(con, job_id, ranges=ranges)["events"],
        "blocks": blocks(con, job_id)["blocks"],
        "spectra": spectra(con, job_id)["spectra"],
    }


# --- trends -------------------------------------------------------------------------------------

def _context_value(context, path):
    value = context
    for part in path:
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and isinstance(part, int) and 0 <= part < len(value):
            value = value[part]
        else:
            return None
    return value


def _nozzle_diameter(context, tool):
    slicer = _context_value(context, ["slicer", "config", "nozzle_diameter"])
    if slicer:
        try:
            return float(str(slicer).split(",")[0])
        except ValueError:
            pass
    value = _context_value(context, ["globalsStart", "nozzle_diameter"])
    if isinstance(value, list) and tool is not None and 0 <= tool < len(value):
        return value[tool]
    return None


TREND_METRICS = ("heater_load_mean", "fm_avg_percentage", "filament_ratio", "esteps_suggested", "mm_per_rev",
                 "heat_up_s", "duration_s", "events", "spectrum_peak_hz", "spectrum_rms", "fan_rpm", "fan_amplitude")


def trends(con, metric, limit=100, material=None, ranges=None):
    """One point per job (newest first), grouped where the plan asks for it (§5.4.1 Trends)."""
    if metric not in TREND_METRICS:
        raise ValueError(f"unknown metric {metric}; one of {', '.join(TREND_METRICS)}")
    sql = ("SELECT key, id, started_at, ended_at, result, material, duration_s, context, summary FROM jobs "
           "WHERE result != 'running'")
    args = []
    if material:
        sql += " AND material=?"
        args.append(material)
    rows = con.execute(sql + " ORDER BY started_at DESC LIMIT ?", (*args, limit)).fetchall()
    levels = event_levels(con, rows, ranges) if metric == "events" else {}
    points = []
    for row in rows:
        summary = qa_db.loads(row["summary"]) or {}
        context = qa_db.loads(row["context"]) or {}
        base = {"jobId": row["id"], "ts": iso(row["started_at"]), "result": row["result"], "material": row["material"]}
        if metric == "heater_load_mean":
            for heater, stats in ((summary.get("thermal") or {}).get("heaterLoad") or {}).items():
                if not stats.get("nozzle"):
                    continue
                for group in stats.get("bySetpoint") or []:
                    points.append({**base, "heater": int(heater), "tool": stats.get("tool"),
                                   "setpoint": group["setpoint"], "value": group["mean"], "timeS": group["timeS"],
                                   "nozzleDiameter": _nozzle_diameter(context, stats.get("tool"))})
        elif metric in ("fm_avg_percentage", "filament_ratio", "mm_per_rev"):
            field = {"fm_avg_percentage": "avgPercentage", "filament_ratio": "ratio", "mm_per_rev": "mmPerRev"}[metric]
            for monitor, stats in (summary.get("filament") or {}).items():
                if stats.get(field) is not None:
                    points.append({**base, "monitor": int(monitor), "value": stats[field]})
        elif metric == "esteps_suggested":
            mfm = summary.get("mfm") or {}
            if mfm.get("estepsSuggested"):
                steps = (context.get("extruders") or [{}])[0].get("stepsPerMm")
                points.append({**base, "value": mfm["estepsSuggested"], "stepsPerMm": steps,
                               "applied": mfm.get("estepsApplied")})
        elif metric == "heat_up_s":
            for heater, runs in ((summary.get("thermal") or {}).get("heatUp") or {}).items():
                if runs:
                    points.append({**base, "heater": int(heater), "value": runs[0]["s"], "setpoint": runs[0]["setpoint"]})
        elif metric == "duration_s":
            if row["duration_s"] is not None:
                points.append({**base, "value": row["duration_s"]})
        elif metric in ("spectrum_peak_hz", "spectrum_rms"):
            field = "peakHzMean" if metric == "spectrum_peak_hz" else "rmsMean"
            for axis, stats in (summary.get("mechanics") or {}).items():
                if stats.get(field) is not None:
                    points.append({**base, "axis": axis, "value": stats[field], "spectra": stats.get("spectra")})
        elif metric in ("fan_rpm", "fan_amplitude"):
            field = "rpmMean" if metric == "fan_rpm" else "amplitudeMean"
            for fan, stats in (summary.get("fans") or {}).items():
                if stats.get(field) is not None:
                    points.append({**base, "fan": int(fan), "name": stats.get("name"), "value": stats[field],
                                   "pwm": stats.get("pwmMean"), "stalled": stats.get("stalled"),
                                   "amplitudeMax": stats.get("amplitudeMax"), "recordings": stats.get("recordings")})
        elif metric == "events":
            counts = {k: v.get("count") for k, v in (summary.get("events") or {}).items()}
            by_level = levels[row["key"]]
            points.append({**base, "value": qa_severity.counted(by_level), "byLevel": by_level, "byType": counts})
    return {"metric": metric, "points": points}


def dumps(value):
    return json.dumps(value, separators=(",", ":"))
