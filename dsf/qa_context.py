"""Job context snapshot (PLAN.md §3 "Job-Kontext"): everything that describes how a job was
set up, taken at job start (and the globals again at job end), so the job stays comparable
after the file, the profile or the machine configuration changed.
"""

import zlib

import qa_accel
import qa_channels
import qa_machine
import qa_slicer

CRC_CHUNK = 1024 * 1024


def file_crc32(path):
    """CRC32 of a file as 8 lowercase hex digits (zlib, same polynomial as RRF's M38; QA only
    compares its own values, PLAN.md §7)."""
    crc = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CRC_CHUNK)
            if not chunk:
                break
            crc = zlib.crc32(chunk, crc)
    return f"{crc & 0xFFFFFFFF:08x}"


def _num(value):
    return qa_channels.number(value)


def _list(values):
    return [(_num(v) if not isinstance(v, (list, str)) else v) for v in (values or [])]


def file_info(model):
    info = getattr(getattr(model, "job", None), "file", None)
    if info is None:
        return {}
    modified = getattr(info, "last_modified", None)
    return {
        "fileName": getattr(info, "file_name", None),
        "generatedBy": getattr(info, "generated_by", None),
        "layerHeight": _num(getattr(info, "layer_height", None)),
        "numLayers": getattr(info, "num_layers", None),
        "height": _num(getattr(info, "height", None)),
        "printTime": getattr(info, "print_time", None),
        "simulatedTime": getattr(info, "simulated_time", None),
        "filament": _list(getattr(info, "filament", None)),
        "size": getattr(info, "size", None),
        "lastModified": modified.isoformat() if hasattr(modified, "isoformat") else modified,
        "customInfo": dict(getattr(info, "custom_info", None) or {}),
    }


def extruders(model):
    result = []
    for i, ext in qa_channels.items(getattr(getattr(model, "move", None), "extruders", None)):
        pa = getattr(ext, "press_adv", None)
        nl = getattr(ext, "nonlinear", None)
        result.append({
            "index": i,
            "stepsPerMm": _num(getattr(ext, "steps_per_mm", None)),
            "factor": _num(getattr(ext, "factor", None)),
            "filament": getattr(ext, "filament", None),
            "filamentDiameter": _num(getattr(ext, "filament_diameter", None)),
            "microstepping": microstepping(ext),
            "pressAdv": None if pa is None else {
                "k0": _num(getattr(pa, "k0", None)), "k1": _num(getattr(pa, "k1", None)),
                "d": _num(getattr(pa, "d", None))},
            "nonlinear": None if nl is None else {
                "a": _num(getattr(nl, "a", None)), "b": _num(getattr(nl, "b", None)),
                "upperLimit": _num(getattr(nl, "upper_limit", None))},
        })
    return result


def tools(model):
    result = []
    for tool in getattr(model, "tools", None) or []:
        if tool is None:
            continue
        result.append({
            "number": getattr(tool, "number", None),
            "name": getattr(tool, "name", None),
            "heaters": list(getattr(tool, "heaters", None) or []),
            "extruders": list(getattr(tool, "extruders", None) or []),
            "filamentExtruder": getattr(tool, "filament_extruder", None),
            "active": _list(getattr(tool, "active", None)),
            "standby": _list(getattr(tool, "standby", None)),
            "offsets": _list(getattr(tool, "offsets", None)),
            "retraction": tool_retraction(tool),
        })
    return result


# ToolRetraction in dsf-python 3.7.0b1 (object_model/tools/tool_retraction.py, read 2026-09-28)
RETRACTION_FIELDS = (("length", "length"), ("extra_restart", "extraRestart"), ("speed", "speed"),
                     ("unretract_speed", "unretractSpeed"), ("z_hop", "zHop"))


def tool_retraction(tool):
    """The tool's M207 (firmware retraction, used by G10/G11; speeds in mm/s), None without."""
    return _object_fields(getattr(tool, "retraction", None), RETRACTION_FIELDS)


def _object_fields(obj, fields):
    if obj is None:
        return None
    return {camel: (_num(getattr(obj, snake, None)) if not isinstance(getattr(obj, snake, None), (bool, str))
                    else getattr(obj, snake, None))
            for snake, camel in fields}


def filament_config(fm):
    """``configured`` and ``calibrated`` of a filament monitor, whatever its type."""
    configured = _object_fields(getattr(fm, "configured", None), (
        ("all_moves", "allMoves"), ("mm_per_rev", "mmPerRev"), ("mm_per_pulse", "mmPerPulse"),
        ("calibration_factor", "calibrationFactor"), ("percent_min", "percentMin"),
        ("percent_max", "percentMax"), ("sample_distance", "sampleDistance")))
    calibrated = _object_fields(getattr(fm, "calibrated", None), (
        ("mm_per_rev", "mmPerRev"), ("mm_per_pulse", "mmPerPulse"), ("sensitivity", "sensitivity"),
        ("percent_min", "percentMin"), ("percent_max", "percentMax"), ("total_distance", "totalDistance")))
    for fields in (configured, calibrated):
        if fields:
            for key in [k for k, v in fields.items() if v is None]:
                del fields[key]
    return configured, calibrated


def filament_monitors(model):
    result = []
    for i, fm in qa_channels.items(getattr(getattr(model, "sensors", None), "filament_monitors", None)):
        configured, calibrated = filament_config(fm)
        result.append({
            "index": i,
            "type": qa_channels.enum_value(getattr(fm, "type", None)),
            "enableMode": qa_channels.enum_value(getattr(fm, "enable_mode", None)),
            "configured": configured,
            "calibrated": calibrated,
        })
    return result


def heater_model(heater):
    model = getattr(heater, "model", None)
    if model is None:
        return None
    pid = getattr(model, "pid", None)
    return {
        "maxPwm": _num(getattr(model, "max_pwm", None)),
        "heatingRate": _num(getattr(model, "heating_rate", None)),
        "coolingRate": _num(getattr(model, "cooling_rate", None)),
        "coolingExp": _num(getattr(model, "cooling_exp", None)),
        "deadTime": _num(getattr(model, "dead_time", None)),
        "fanCoolingRate": _num(getattr(model, "fan_cooling_rate", None)),
        "standardVoltage": _num(getattr(model, "standard_voltage", None)),
        "enabled": getattr(model, "enabled", None),
        "pid": None if pid is None else {
            "p": _num(getattr(pid, "p", None)), "i": _num(getattr(pid, "i", None)),
            "d": _num(getattr(pid, "d", None)), "used": getattr(pid, "used", None)},
    }


def heaters(model, settings):
    nozzles = qa_channels.nozzle_heaters(model)
    beds = qa_channels.bed_heaters(model)
    chamber = qa_channels.chamber_source(model, settings)
    analog = getattr(getattr(model, "sensors", None), "analog", None) or []
    result = []
    for i, heater in qa_channels.items(getattr(getattr(model, "heat", None), "heaters", None)):
        sensor = getattr(heater, "sensor", None)
        sensor_name = None
        if isinstance(sensor, int) and 0 <= sensor < len(analog) and analog[sensor] is not None:
            sensor_name = getattr(analog[sensor], "name", None)
        role = "nozzle" if i in nozzles else ("bed" if i in beds else (
            "chamber" if chamber == ("heater", i) else "other"))
        result.append({
            "index": i,
            "role": role,
            "tool": nozzles.get(i),
            "sensor": sensor,
            "sensorName": sensor_name,
            "max": _num(getattr(heater, "max", None)),
            "model": heater_model(heater),
            "monitors": [{
                "condition": qa_channels.enum_value(getattr(m, "condition", None)),
                "limit": _num(getattr(m, "limit", None)),
                "action": qa_channels.enum_value(getattr(m, "action", None)),
                "sensor": getattr(m, "sensor", None),
            } for m in (getattr(heater, "monitors", None) or []) if m is not None],
        })
    return result


def sensors(model):
    return [{"index": i, "name": getattr(s, "name", None), "type": qa_channels.enum_value(getattr(s, "type", None))}
            for i, s in qa_channels.items(getattr(getattr(model, "sensors", None), "analog", None))]


def shaping(model):
    """The input shaper (M593) as RRF reports it: type, frequency, damping, amplitudes and delays
    (RRF 3.7-dev @ 32a84d2 AxisShaper.cpp:53-57, 2026-09-29; RRF 3.7 has no ``durations``)."""
    sh = getattr(getattr(model, "move", None), "shaping", None)
    if sh is None:
        return None
    return {
        "type": qa_channels.enum_value(getattr(sh, "type", None)),
        "frequency": _num(getattr(sh, "frequency", None)),
        "damping": _num(getattr(sh, "damping", None)),
        "amplitudes": _list(getattr(sh, "amplitudes", None)),
        "delays": _list(getattr(sh, "delays", None)),
    }


def microstepping(drive):
    """M350 of an axis or extruder: ``{value, interpolated}`` (RRF 3.7-dev @ 32a84d2 Move.cpp:268, 295,
    320-325, 2026-09-29), None when the model has none."""
    ms = getattr(drive, "microstepping", None) if drive is not None else None
    if ms is None:
        return None
    value = getattr(ms, "value", None)
    return {"value": int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None,
            "interpolated": bool(getattr(ms, "interpolated", False))}


def boards(model):
    return [{
        "index": i,
        "name": getattr(b, "name", None),
        "shortName": getattr(b, "short_name", None),
        "canAddress": getattr(b, "can_address", None),
        "firmwareVersion": getattr(b, "firmware_version", None),
        "firmwareDate": getattr(b, "firmware_date", None),
    } for i, b in qa_channels.items(getattr(model, "boards", None))]


def accelerometers(model):
    """``sensors.accelerometers`` (DSF 3.7 moved them there from ``boards[]``; qa_patches), the
    list index being the M955/M956 P number: what the job's spectra were recorded with."""
    return [{"index": i, "port": getattr(a, "port", None), "board": qa_accel.board_of(getattr(a, "port", None)),
             "orientation": getattr(a, "orientation", None), "samplingRate": getattr(a, "sampling_rate", None),
             "resolution": getattr(a, "resolution", None)}
            for i, a in qa_channels.items(getattr(getattr(model, "sensors", None), "accelerometers", None))]


def axes(model):
    return [{"letter": qa_channels.axis_letter(a), "stepsPerMm": _num(getattr(a, "steps_per_mm", None)),
             "microstepping": microstepping(a), "babystep": _num(getattr(a, "babystep", None))}
            for a in (getattr(getattr(model, "move", None), "axes", None) or []) if qa_channels.axis_letter(a)]


def material(model, slicer_config):
    """What the job prints: the slicer's filament profile, else the loaded filament of the
    first tool's extruder."""
    if slicer_config.get("filament_settings_id"):
        return slicer_config["filament_settings_id"]
    exts = getattr(getattr(model, "move", None), "extruders", None) or []
    for ext in exts:
        name = getattr(ext, "filament", None) if ext is not None else None
        if name:
            return name
    return None


def snapshot(model, settings, plugin_version, file_path=None, crc=None):
    """The context record of a job starting now. ``file_path``: the job file on disk (for the
    slicer settings), may be None."""
    sbc = getattr(model, "sbc", None)
    dsf = getattr(sbc, "dsf", None) if sbc is not None else None
    slicer = qa_slicer.slicer_settings(file_path) if file_path else {"source": "none", "config": {}}
    return {
        "file": {**file_info(model), "crc32": crc},
        "slicer": slicer,
        "extruders": extruders(model),
        "tools": tools(model),
        "filamentMonitors": filament_monitors(model),
        "heaters": heaters(model, settings),
        "sensors": sensors(model),
        "chamber": qa_channels.chamber_source(model, settings),
        "shaping": shaping(model),
        "axes": axes(model),
        "boards": boards(model),
        "accelerometers": accelerometers(model),
        "versions": {
            "firmware": (boards(model) or [{}])[0].get("firmwareVersion"),
            "dsf": getattr(dsf, "version", None) if dsf is not None else None,
            "plugin": plugin_version,
        },
        "globalsStart": qa_machine.context_globals(getattr(model, "globals", None), settings.get("contextGlobals", [])),
        "material": material(model, slicer.get("config", {})),
    }
