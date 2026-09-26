"""dsf-python 3.7 compatibility patches.

QA reads the object model through dsf-python's typed model classes (``get_object_model()``
once, then ``update_from_json()`` per patch). dsf-python lags behind DSF, and a few of its
model classes drop or reject values QA needs. Every patch here is applied once by
``apply()`` before the first connection, swallows its own failure (an unpatchable library
must not stop the daemon; most patches guard inputs that may never occur) and is covered
by tests/test_patches.py against the real library.

QA targets DSF/DWC 3.7 only. The patches below were taken over from dwc-vigil
(``dsf/vigil-daemon.py``, the ones tagged [both] or [3.7 only]) or are new; each comment
says which. Verified against dsf-python 3.7.0b1 (PyPI; git 3.7.0-beta.1 + b1af5bb) and
DuetSoftwareFramework v3.7-dev @ cd3ae65f on 2026-09-26.
"""

import json
import logging
import socket
import sys
import time

logger = logging.getLogger("qa")

APPLIED = []


def _patch_property_setter(cls, name, setter):
    """Replace the setter of a model property, keeping its getter (`model_prop` stores in `_<name>`)."""
    prop = getattr(cls, name, None)
    if not isinstance(prop, property):
        return False
    setattr(cls, name, prop.setter(setter))
    return True


# --- Greeting ---------------------------------------------------------------------------------

def json_object_end(buffer):
    """End index (exclusive) of the first complete JSON object in ``buffer``, -1 if incomplete.

    String-aware (a brace inside a string does not count), as in the CHX350 backend.
    """
    depth = 0
    in_string = False
    escape = False
    started = False
    for index, char in enumerate(buffer):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
            started = True
        elif char == "}":
            depth -= 1
            if depth < 0:
                return -1
            if depth == 0 and started:
                return index + 1
    return -1


def read_json_object(recv, timeout=3, buff_size=4096):
    """Read until a complete JSON object arrived. Returns ``(json_string, leftover)``."""
    buffer = ""
    start = time.monotonic()
    while True:
        end = json_object_end(buffer)
        if end > 0:
            return buffer[:end], buffer[end:]
        if timeout and time.monotonic() - start > timeout:
            raise TimeoutError("Timeout while waiting for the DSF greeting")
        chunk = recv(buff_size)
        if not chunk:
            raise ConnectionError("Connection closed before a complete JSON object was received")
        buffer += chunk.decode("utf8") if isinstance(chunk, (bytes, bytearray)) else chunk


def _patch_greeting():
    """[from Vigil] dsf-python reads DSF's greeting with a fixed ``socket.recv(50)``; DSF 3.7
    sends a longer one (it carries a GUID), the JSON is cut and ``json.loads`` fails with
    "Unterminated string". dsf-python 3.7 calls the method ``_connect``."""
    from dsf.connections.base_connection import BaseConnection
    from dsf.connections.exceptions import IncompatibleVersionException
    from dsf.connections.init_messages.server_init_message import ServerInitMessage

    def _patched_connect(self, init_message, socket_file):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.connect(socket_file)
        self.socket.settimeout(self.timeout if self.timeout > 0 else None)
        json_string, leftover = read_json_object(self.socket.recv, self.timeout)
        self.input = leftover
        server_init = ServerInitMessage.from_json(json.loads(json_string))
        if not server_init.is_compatible():
            raise IncompatibleVersionException(
                f"Incompatible API version (need {server_init.PROTOCOL_VERSION}, got {server_init.version})"
            )
        self.id = server_init.id
        self.send(init_message)
        response = self.receive_response()
        if not getattr(response, "success", True):
            raise Exception(
                f"Could not set connection type {init_message.mode} "
                f"({response.error_type}: {response.error_message})"
            )

    patched = False
    for name in ("connect", "_connect"):
        if hasattr(BaseConnection, name):
            setattr(BaseConnection, name, _patched_connect)
            patched = True
    return patched


# --- Nulls for dictionaries and collections ---------------------------------------------------

def _patch_set_model_prop_none():
    """[from Vigil] Non-nullable ``model_prop``s over a ModelDictionary/ModelCollection
    (``GCodeFileInfo.custom_info``, ``ObjectModel.globals``, ``PluginManifest.data``) have no
    ``None`` branch; DSF sends ``"customInfo": null`` in patches, which raises a TypeError and
    drops the whole patch. ``update_from_json(None)`` means "clear" for a dictionary."""
    import dsf.object_model.utils as om_utils
    from dsf.object_model.model_collection import ModelCollection
    from dsf.object_model.model_dictionary import ModelDictionary

    original = om_utils._set_model_prop
    if getattr(original, "_qa_patched", False):
        return True

    def _patched(instance, name, runtime_type, current_value, value):
        if value is None:
            if isinstance(current_value, ModelDictionary):
                current_value.update_from_json(None)
                return
            if isinstance(current_value, ModelCollection):
                current_value.update_from_json([])
                return
        return original(instance, name, runtime_type, current_value, value)

    _patched._qa_patched = True
    # model_prop's setter looks `_set_model_prop` up in the utils module at call time
    om_utils._set_model_prop = _patched
    return True


# --- Enum values dsf-python does not know ------------------------------------------------------

def _patch_board_state():
    """[from Vigil] ``BoardState`` lacks values DSF reports (``timedOut``); the setter's
    ValueError takes down the whole ``get_object_model()``."""
    from dsf.object_model.boards.boards import Board, BoardState

    def _safe_state_setter(self, value):
        try:
            if value is None or isinstance(value, BoardState):
                self._state = value
            else:
                self._state = BoardState(value)
        except (ValueError, KeyError, TypeError):
            self._state = BoardState("unknown")

    return _patch_property_setter(Board, "state", _safe_state_setter)


def _patch_axis_letter():
    """[from Vigil] ``Axis.letter`` crashes on ``'\\x00'`` from axes the firmware has not
    configured yet (daemon started before config.g ran)."""
    from dsf.object_model.move.axis import Axis, AxisLetter

    def _safe_letter_setter(self, value):
        try:
            if value is None:
                self._letter = AxisLetter.none
            elif isinstance(value, AxisLetter):
                self._letter = value
            else:
                self._letter = AxisLetter(value)
        except (ValueError, KeyError, TypeError):
            self._letter = AxisLetter.none

    return _patch_property_setter(Axis, "letter", _safe_letter_setter)


_seen_unknown_enum_values = set()


def _mint_pseudo_member(cls, value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None  # let Enum raise its usual ValueError
    cache = getattr(cls, "_value2member_map_", None)
    if cache is not None and value in cache:
        return cache[value]
    member_type = getattr(cls, "_member_type_", object)
    member = object.__new__(cls) if member_type is object else member_type.__new__(cls, value)
    member._name_ = str(value)
    member._value_ = value
    if cache is not None:
        member = cache.setdefault(value, member)
    if (cls.__name__, value) not in _seen_unknown_enum_values:
        _seen_unknown_enum_values.add((cls.__name__, value))
        logger.warning("dsf-python's %s has no value %r; keeping it as-is", cls.__name__, value)
    return member


def _add_enum_member(cls, name, value):
    if name in cls.__members__:
        return
    cache = getattr(cls, "_value2member_map_", None)
    member = cache.get(value) if cache is not None else None
    if member is None:
        member_type = getattr(cls, "_member_type_", object)
        member = object.__new__(cls) if member_type is object else member_type.__new__(cls, value)
        member._value_ = value
    member._name_ = name
    # The class attribute first: EnumType.__setattr__ refuses names already in _member_map_
    setattr(cls, name, member)
    cls._member_map_[name] = member
    cls._member_names_.append(name)
    if cache is not None:
        cache[value] = member


# Values DSF reports that dsf-python 3.7.0b1 lacks, added as real members under their
# DuetAPI names. EndstopType.MotorStallEncoder: DuetSoftwareFramework v3.7-dev @ 46886f5
# (dwc-vigil, 2026-09-16); still missing in dsf-python 3.7.0b1 (2026-09-26).
KNOWN_ENUM_ADDITIONS = {
    "EndstopType": (("MotorStallEncoder", "motorStallEncoder"),),
}


def _patch_enum_missing():
    """[from Vigil] DSF grows its enums faster than dsf-python: one unknown string in one
    sub-object aborts the whole object-model update. Every enum under ``dsf.object_model``
    gets a ``_missing_`` hook that keeps the raw value as a pseudo-member (warned once)."""
    import importlib
    import pkgutil
    from enum import Enum

    import dsf.object_model as om_pkg

    for info in pkgutil.walk_packages(getattr(om_pkg, "__path__", []), om_pkg.__name__ + "."):
        try:
            importlib.import_module(info.name)
        except Exception:
            pass
    enums = {}
    for mod_name, mod in list(sys.modules.items()):
        if not mod_name.startswith("dsf.object_model") or mod is None:
            continue
        for obj in list(vars(mod).values()):
            if (isinstance(obj, type) and issubclass(obj, Enum)
                    and getattr(obj, "__module__", "").startswith("dsf.")):
                enums[obj.__name__] = obj
                if "_missing_" not in vars(obj):
                    obj._missing_ = classmethod(_mint_pseudo_member)
    for enum_name, additions in KNOWN_ENUM_ADDITIONS.items():
        if enum_name in enums:
            for member_name, member_value in additions:
                _add_enum_member(enums[enum_name], member_name, member_value)
    return bool(enums)


# --- Properties dsf-python drops ---------------------------------------------------------------

def _model_object_classes():
    """Every ModelObject subclass defined under dsf.object_model (modules already imported)."""
    from dsf.object_model.model_object import ModelObject

    seen = set()
    stack = [ModelObject]
    while stack:
        cls = stack.pop()
        for sub in cls.__subclasses__():
            if sub not in seen and sub.__module__.startswith("dsf."):
                seen.add(sub)
                stack.append(sub)
    return seen


def _patch_digit_property_names():
    """[new] dsf-python maps a JSON key to its property with ``camel_to_snake``, which splits
    digits off: ``k0`` → ``k_0``, ``m486Names`` → ``m_486_names``. Properties declared with the
    digit attached (``ExtruderPressureAdvance.k0/k1``, ``Build.m486_names/m486_numbers``) are
    therefore never written; ``move.extruders[].pressAdv.k0`` stays 0.0 whatever RRF reports.
    Register the property object a second time under the name the deserializer looks for
    (same storage, so reading ``k0`` returns the value). Generic: any property whose name
    does not survive the ``snake_to_camel`` → ``camel_to_snake`` round trip gets an alias."""
    from dsf.utils import camel_to_snake, snake_to_camel

    aliases = []
    for cls in _model_object_classes():
        for name, value in list(vars(cls).items()):
            if not isinstance(value, property) or value.fset is None or name.startswith("_"):
                continue
            wire = camel_to_snake(snake_to_camel(name))
            if wire != name and wire not in vars(cls):
                setattr(cls, wire, value)
                aliases.append(f"{cls.__name__}.{wire}")
    return aliases


# (module, class, property, nullable, type name, default). DuetAPI declares these, dsf-python
# 3.7.0b1 does not, so update_from_json() skips them silently:
#   RotatingMagnetFilamentMonitor.Agc (int?), RotatingMagnetFilamentMonitorCalibrated.MmPerRev
#   (float; dsf-python has mm_per_pulse instead), BuildObject.Cancelled (bool; dsf-python spells
#   it `canceled`), FilamentMonitor.FilamentPresent (bool?).
# DuetSoftwareFramework v3.7-dev @ cd3ae65f, DuetAPI/ObjectModel/Sensors/FilamentMonitors/*.cs,
# Job/BuildObject.cs; dsf-python 3.7.0b1; compared 2026-09-26.
KNOWN_PROPERTY_ADDITIONS = (
    ("dsf.object_model.sensors.filament_monitors.rotating_magnet_filament_monitor",
     "RotatingMagnetFilamentMonitor", "agc", True, int, None),
    ("dsf.object_model.sensors.filament_monitors.rotating_magnet_filament_monitor",
     "RotatingMagnetFilamentMonitorCalibrated", "mm_per_rev", False, float, 0.0),
    ("dsf.object_model.sensors.filament_monitors.filament_monitor",
     "FilamentMonitor", "filament_present", True, bool, None),
)

# (module, class, alias, existing property): JSON name → property dsf-python spells differently
KNOWN_PROPERTY_ALIASES = (
    ("dsf.object_model.job.build_object", "BuildObject", "cancelled", "canceled"),
)


def _patch_missing_properties():
    """[new] Add the DuetAPI properties dsf-python lacks (see KNOWN_PROPERTY_ADDITIONS)."""
    import importlib

    from dsf.object_model.utils import model_prop, nullable_model_prop

    added = []
    for module_name, class_name, prop, nullable, prop_type, default in KNOWN_PROPERTY_ADDITIONS:
        try:
            cls = getattr(importlib.import_module(module_name), class_name)
        except Exception:
            continue
        if isinstance(getattr(cls, prop, None), property):
            continue  # upstream has it by now
        descriptor = nullable_model_prop(prop, prop_type) if nullable else model_prop(prop, prop_type, default)
        setattr(cls, prop, descriptor)
        added.append(f"{class_name}.{prop}")
    for module_name, class_name, alias, existing in KNOWN_PROPERTY_ALIASES:
        try:
            cls = getattr(importlib.import_module(module_name), class_name)
        except Exception:
            continue
        target = getattr(cls, existing, None)
        if isinstance(target, property) and not isinstance(getattr(cls, alias, None), property):
            setattr(cls, alias, target)
            added.append(f"{class_name}.{alias}")
    return added


PATCHES = (
    ("greeting", _patch_greeting),
    ("set_model_prop_none", _patch_set_model_prop_none),
    ("board_state", _patch_board_state),
    ("axis_letter", _patch_axis_letter),
    ("enum_missing", _patch_enum_missing),
    ("missing_properties", _patch_missing_properties),
    # after missing_properties, so an added property with a digit gets its alias too
    ("digit_property_names", _patch_digit_property_names),
)


def apply():
    """Apply every patch once. Returns the names of those that took effect."""
    if APPLIED:
        return list(APPLIED)
    for name, patch in PATCHES:
        try:
            if patch():
                APPLIED.append(name)
        except Exception as exc:  # noqa: BLE001 - never fatal
            logger.debug("patch %s not applied: %s", name, exc)
    return list(APPLIED)
