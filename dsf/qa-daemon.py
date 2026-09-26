#!/usr/bin/env python3
"""Quality Assurance - DSF SBC daemon.

Records process parameters of every print job from the DSF object model, keeps them in a
SQLite database under /opt/dsf/sd/QualityAssurance/ and serves them through HTTP endpoints
under /machine/QualityAssurance/ to the DWC page, the CHX 350 UI and a later Quality Control
plugin. It records only; it never judges.

Targets DSF 3.7 / dsf-python 3.7.0b1 on Python >= 3.11.
"""

import os
import signal
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_log  # noqa: E402
import qa_patches  # noqa: E402

logger = qa_log.setup()
qa_patches.apply()

from dsf.connections import CommandConnection  # noqa: E402

import qa_api  # noqa: E402

PLUGIN_ID = qa_log.PLUGIN_ID

# Keys written with set_plugin_data; every one must be declared in plugin.json#data or DSF
# refuses it (tests/core/plugin-structure.test.js checks the two agree)
PLUGIN_DATA_KEYS = ("status", "currentJobId", "lastJobId", "dbSizeBytes", "lastError")

# DSF may launch the plugin before duetcontrolserver accepts connections (boot, upgrade)
CONNECT_ATTEMPTS = 15
CONNECT_RETRY_DELAY_S = 2.0

_shutdown = threading.Event()


def _signal_handler(_signum, _frame):
    _shutdown.set()


def read_version():
    """Version from the installed manifest next to the plugin directory
    (<plugins>/QualityAssurance.json beside <plugins>/QualityAssurance/dsf/<this file>)."""
    import json

    plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    manifest = os.path.join(os.path.dirname(plugin_dir), f"{PLUGIN_ID}.json")
    try:
        with open(manifest, "r", encoding="utf-8-sig") as handle:
            return str(json.load(handle).get("version") or "unknown")
    except (OSError, ValueError):
        return "unknown"


def connect_with_retry(connection, description, attempts=CONNECT_ATTEMPTS, delay=CONNECT_RETRY_DELAY_S):
    """Connect, retrying while DSF's socket is not there yet. False if shutdown was requested."""
    last_error = None
    for attempt in range(1, attempts + 1):
        if _shutdown.is_set():
            return False
        try:
            connection.connect()
            if attempt > 1:
                logger.warning("%s connected after %d attempts", description, attempt)
            return True
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning("%s connection attempt %d/%d failed: %s", description, attempt, attempts, exc)
            if attempt < attempts:
                _shutdown.wait(delay)
    raise RuntimeError(f"{description} connection failed after {attempts} attempts: {last_error}")


def main():
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    cmd = CommandConnection()
    if not connect_with_retry(cmd, "CommandConnection"):
        return
    qa_log.deferred_warnings.send_to(cmd)

    context = qa_api.ApiContext(version=read_version(), started=time.monotonic())
    endpoints = qa_api.register_endpoints(cmd, context)
    try:
        while not _shutdown.is_set():
            _shutdown.wait(1.0)
            qa_log.deferred_warnings.send_to(cmd, context.cmd_lock)
    finally:
        for endpoint in endpoints:
            try:
                endpoint.close()
            except Exception:  # noqa: BLE001
                pass
        qa_log.deferred_warnings.send_to(cmd, context.cmd_lock)
        try:
            cmd.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        # DSF only reports the exit code; the traceback goes to stderr, i.e. the DWC console
        sys.stderr.write("Quality Assurance daemon terminated with an unhandled exception:\n")
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        sys.exit(1)
