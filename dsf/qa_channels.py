"""Channel extraction: numeric process values from the typed object model (PLAN.md §3 "Kanäle").

A channel is a name and a float. Everything is read by index from the model on every call,
nothing is wired to a fixed number of heaters, extruders or monitors (§5.4 "Mehrere Düsen").
Enum states are stored as numeric codes (tables below, part of the API contract).

Property names are dsf-python's snake_case (with the fixes of qa_patches). Object model
semantics from the Duet3D wiki (Object_Model docs) and DuetAPI (DuetSoftwareFramework v3.7-dev
@ cd3ae65f), 2026-09-26.
"""

import math

import qa_machine

# Heater.state (DuetAPI HeaterState)
HEATER_STATES = {"off": 0, "standby": 1, "active": 2, "fault": 3, "tuning": 4, "offline": 5}
# FilamentMonitor.status (DuetAPI FilamentMonitorStatus)
FM_STATUSES = {"ok": 0, "noMonitor": 1, "noDataReceived": 2, "noFilament": 3, "tooLittleMovement": 4,
               "tooMuchMovement": 5, "sensorError": 6}


def enum_value(value):
    """The wire value of an enum (dsf-python enums are str/int mixins; pseudo-members keep the raw value)."""
    if value is None:
        return None
    return getattr(value, "value", value)


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def items(collection):
    """(index, item) for the non-null items of a model collection."""
    if not collection:
        return []
    return [(i, item) for i, item in enumerate(collection) if item is not None]


def axis_letter(axis):
    letter = enum_value(getattr(axis, "letter", None))
    return letter if isinstance(letter, str) and letter and letter != "none" and letter != "\x00" else None


def extract(model, include_globals=True):
    """All channels of ``model`` as ``{name: float}``."""
    out = {}

    def put(name, value):
        v = number(value)
        if v is not None:
            out[name] = v

    heat = getattr(model, "heat", None)
    for i, heater in items(getattr(heat, "heaters", None)):
        put(f"heater.{i}.current", getattr(heater, "current", None))
        put(f"heater.{i}.active", getattr(heater, "active", None))
        put(f"heater.{i}.standby", getattr(heater, "standby", None))
        put(f"heater.{i}.avgPwm", getattr(heater, "avg_pwm", None))
        state = HEATER_STATES.get(enum_value(getattr(heater, "state", None)))
        if state is not None:
            out[f"heater.{i}.state"] = float(state)

    move = getattr(model, "move", None)
    for i, extruder in items(getattr(move, "extruders", None)):
        put(f"extruder.{i}.position", getattr(extruder, "position", None))
        put(f"extruder.{i}.rawPosition", getattr(extruder, "raw_position", None))
        put(f"extruder.{i}.factor", getattr(extruder, "factor", None))
        put(f"extruder.{i}.stepsPerMm", getattr(extruder, "steps_per_mm", None))
    for axis in getattr(move, "axes", None) or []:
        letter = axis_letter(axis)
        if letter:
            put(f"axis.{letter}.machinePosition", getattr(axis, "machine_position", None))
    current_move = getattr(move, "current_move", None)
    if current_move is not None:
        put("move.currentMove.topSpeed", getattr(current_move, "top_speed", None))
        put("move.currentMove.requestedSpeed", getattr(current_move, "requested_speed", None))
        put("move.currentMove.extrusionRate", getattr(current_move, "extrusion_rate", None))
    put("move.speedFactor", getattr(move, "speed_factor", None))

    job = getattr(model, "job", None)
    put("job.rawExtrusion", getattr(job, "raw_extrusion", None))

    sensors = getattr(model, "sensors", None)
    for i, fm in items(getattr(sensors, "filament_monitors", None)):
        status = FM_STATUSES.get(enum_value(getattr(fm, "status", None)))
        if status is not None:
            out[f"fm.{i}.status"] = float(status)
        for prop, name in (("last_percentage", "lastPercentage"), ("avg_percentage", "avgPercentage"),
                           ("min_percentage", "minPercentage"), ("max_percentage", "maxPercentage"),
                           ("position", "position"), ("total_extrusion", "totalExtrusion"), ("agc", "agc")):
            put(f"fm.{i}.{name}", getattr(fm, prop, None))
        calibrated = getattr(fm, "calibrated", None)
        if calibrated is not None:
            put(f"fm.{i}.calibrated.totalDistance", getattr(calibrated, "total_distance", None))
            put(f"fm.{i}.calibrated.mmPerRev", getattr(calibrated, "mm_per_rev", None))
    for i, sensor in items(getattr(sensors, "analog", None)):
        put(f"sensor.{i}.lastReading", getattr(sensor, "last_reading", None))

    for i, fan in items(getattr(model, "fans", None)):
        put(f"fan.{i}.actualValue", getattr(fan, "actual_value", None))
        put(f"fan.{i}.requestedValue", getattr(fan, "requested_value", None))
        rpm = getattr(fan, "rpm", None)
        if rpm is not None and rpm >= 0:  # -1 = no tacho
            put(f"fan.{i}.rpm", rpm)

    for i, board in items(getattr(model, "boards", None)):
        for prop, name in (("v_in", "vIn"), ("v_12", "v12"), ("mcu_temp", "mcuTemp")):
            reading = getattr(board, prop, None)
            if reading is not None:
                put(f"board.{i}.{name}", getattr(reading, "current", None))

    if include_globals:
        for name, value in qa_machine.mfm_channels(getattr(model, "globals", None)).items():
            put(f"global.{name}", value)
    return out


def is_temperature(name):
    return (name.startswith("heater.") and name.endswith(".current")) or name.endswith(".lastReading")


def is_fm_percentage(name):
    return name.startswith("fm.") and name.endswith(".lastPercentage")


def is_vin(name):
    return name.startswith("board.") and name.endswith(".vIn")


def positions(model):
    """Machine position of every axis, the workplace number and its offsets (§3 "Koordinaten")."""
    move = getattr(model, "move", None)
    workplace = getattr(move, "workplace_number", None)
    pos = {}
    offsets = {}
    for axis in getattr(move, "axes", None) or []:
        letter = axis_letter(axis)
        if not letter:
            continue
        value = number(getattr(axis, "machine_position", None))
        if value is not None:
            pos[letter] = value
        wp = getattr(axis, "workplace_offsets", None) or []
        if workplace is not None and 0 <= workplace < len(wp):
            offset = number(wp[workplace])
            if offset is not None:
                offsets[letter] = offset
    return pos, workplace, offsets


def current_tool(model):
    state = getattr(model, "state", None)
    tool = getattr(state, "current_tool", None)
    return tool if isinstance(tool, int) and tool >= 0 else None


def current_object(model):
    build = getattr(getattr(model, "job", None), "build", None)
    obj = getattr(build, "current_object", None) if build is not None else None
    return obj if isinstance(obj, int) and obj >= 0 else None


def nozzle_heaters(model):
    """Nozzle heaters: heaters of the tools, counted per heater, named after the first tool that
    heats only this nozzle (as CHX350 useTemps().nozzles / heaterLoad.ts). ``{heater: tool}``."""
    tools = [t for t in (getattr(model, "tools", None) or []) if t is not None]
    tools.sort(key=lambda t: (len(getattr(t, "heaters", None) or []), getattr(t, "number", 0)))
    result = {}
    for tool in tools:
        for heater in getattr(tool, "heaters", None) or []:
            if heater not in result:
                result[heater] = getattr(tool, "number", None)
    return result


def bed_heaters(model):
    """Heaters of heat.bedHeaterMapping (RRF 3.7: a slot may hold several heaters; the old
    heat.bedHeaters is -1 on the CHX)."""
    heat = getattr(model, "heat", None)
    result = []
    for slot in getattr(heat, "bed_heater_mapping", None) or []:
        for h in slot or []:
            if isinstance(h, int) and h >= 0 and h not in result:
                result.append(h)
    return result


def chamber_source(model, settings):
    """Where the chamber temperature comes from: ``("heater", i)``, ``("sensor", i)`` or None.

    auto: first heater of heat.chamberHeaterMapping, else the analog sensor named
    ``autoSensorName`` ("SZP coil", as CHX350 useJobAnalysis.chamberChannel), compared trimmed
    and case-insensitive. heater/sensor: the configured index.
    """
    chamber = settings.get("chamber", {})
    mode = chamber.get("mode", "auto")
    index = chamber.get("index")
    if mode == "heater" and index is not None:
        return ("heater", index)
    if mode == "sensor" and index is not None:
        return ("sensor", index)
    heat = getattr(model, "heat", None)
    for slot in getattr(heat, "chamber_heater_mapping", None) or []:
        for h in slot or []:
            if isinstance(h, int) and h >= 0:
                return ("heater", h)
    wanted = (chamber.get("autoSensorName") or "").strip().lower()
    if wanted:
        for i, sensor in items(getattr(getattr(model, "sensors", None), "analog", None)):
            if (getattr(sensor, "name", None) or "").strip().lower() == wanted:
                return ("sensor", i)
    return None


def chamber_channel(model, settings):
    source = chamber_source(model, settings)
    if source is None:
        return None
    kind, index = source
    return f"heater.{index}.current" if kind == "heater" else f"sensor.{index}.lastReading"
