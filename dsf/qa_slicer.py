"""Slicer settings from the tail of a G-code file (PLAN.md §5.7).

OrcaSlicer/BambuStudio write their complete configuration between ``; CONFIG_BLOCK_START`` and
``; CONFIG_BLOCK_END`` at the end of the file; PrusaSlicer/SuperSlicer append ``; key = value``
lines without markers. DSF parses ``;customInfo`` only in the header, and DSF's web server has
no range requests, so the daemon reads the last 256 KiB itself.

Taken over from the CHX350 backend (DuetWebControl ``src/plugins/CHX350/dsf/chx350_gcode.py``,
Meltingplot fork v3.7-dev @ 1fb6a51, 2026-09-26) as a copy, so QA does not depend on the CHX350
plugin. The result goes into the job context at job start, because the file may be gone later.
"""

import os
import re

TAIL_BYTES = 256 * 1024

# Keys kept in the job context. The config block holds several hundred settings; these are the
# ones the CHX UI checks plus what the analysis groups by (material, nozzle) and the flow limit.
WANTED_KEYS = (
    "filament_settings_id",
    "filament_type",
    "filament_vendor",
    "nozzle_diameter",
    "printer_model",
    "printer_settings_id",
    "print_settings_id",
    "curr_bed_type",
    "print_sequence",
    "layer_height",
    "first_layer_height",
    "filament_density",
    "filament_diameter",
    "filament_colour",
    "filament_max_volumetric_speed",
    "required_nozzle_HRC",
    "nozzle_temperature",
    "nozzle_temperature_initial_layer",
    "total filament used [g]",
    "estimated printing time (normal mode)",
)

_LINE_RE = re.compile(r"^;\s*(?P<key>[^=]+?)\s*=\s*(?P<value>.*?)\s*$")


def read_tail(path, size=TAIL_BYTES):
    """The last ``size`` bytes of ``path`` decoded as UTF-8 (errors replaced)."""
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        length = handle.tell()
        handle.seek(max(0, length - size))
        data = handle.read()
    return data.decode("utf-8", errors="replace")


def _clean(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
    return value


def parse_config_block(text):
    """``(config, source)``: source ``config_block`` (Orca markers), ``tail`` (plain lines) or ``none``."""
    start = text.rfind("; CONFIG_BLOCK_START")
    end = text.rfind("; CONFIG_BLOCK_END")
    if start >= 0 and end > start:
        block = text[start:end]
        source = "config_block"
    else:
        block = text
        source = "tail"
    config = {}
    for line in block.splitlines():
        match = _LINE_RE.match(line)
        if not match:
            continue
        key = match.group("key").strip()
        if key in WANTED_KEYS:
            config[key] = _clean(match.group("value"))
    if source == "config_block":
        # "total filament used [g]" and the time estimate sit above the block
        for line in text[:start].splitlines()[-200:]:
            match = _LINE_RE.match(line)
            if match and match.group("key").strip() in WANTED_KEYS:
                config.setdefault(match.group("key").strip(), _clean(match.group("value")))
    if not config:
        return {}, "none"
    return config, source


def slicer_settings(path):
    """``{"source": ..., "config": {...}}`` for a file, or an error record."""
    try:
        config, source = parse_config_block(read_tail(path))
    except OSError as exc:
        return {"source": "error", "error": str(exc), "config": {}}
    return {"source": source, "config": config}
