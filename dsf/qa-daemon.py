#!/usr/bin/env python3
"""Quality Assurance - DSF SBC daemon.

Records process parameters of every print job from the DSF object model, keeps them in a
SQLite database under /opt/dsf/sd/QualityAssurance/ and serves them through HTTP endpoints
under /machine/QualityAssurance/ to the DWC page, the CHX 350 UI and a later Quality Control
plugin. It records only; it never judges.

Threads (PLAN.md §5.3): the main thread runs the object-model subscription and the collector;
the database writer has its own thread; dsf-python serves every HTTP endpoint from its own
thread and asyncio loop; the G-code layer index is built in a background thread; the timelapse
has a capture thread and an encoder thread (qa_timelapse); accelerometer recordings run in their
own thread (qa_accel).

Targets DSF 3.7 / dsf-python 3.7.0b1 on Python >= 3.11.
"""

import json
import os
import signal
import socket
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_log  # noqa: E402
import qa_patches  # noqa: E402

logger = qa_log.setup()
qa_patches.apply()

from dsf.commands.code_channel import CodeChannel  # noqa: E402
from dsf.connections import (  # noqa: E402
    CommandConnection, InterceptConnection, InterceptionMode, SubscribeConnection, SubscriptionMode)

import qa_accel  # noqa: E402
import qa_api  # noqa: E402
import qa_collector  # noqa: E402
import qa_db  # noqa: E402
import qa_gcode  # noqa: E402
import qa_intercept  # noqa: E402
import qa_journal  # noqa: E402
import qa_settings  # noqa: E402
import qa_timelapse  # noqa: E402

PLUGIN_ID = qa_log.PLUGIN_ID

# Keys written with set_plugin_data; every one must be declared in plugin.json#data or DSF
# refuses it (tests/core/plugin-structure.test.js checks the two agree)
PLUGIN_DATA_KEYS = ("status", "currentJobId", "lastJobId", "dbSizeBytes", "lastError")

# DSF may launch the plugin before duetcontrolserver accepts connections (boot, upgrade)
CONNECT_ATTEMPTS = 15
CONNECT_RETRY_DELAY_S = 2.0
# Consecutive subscription errors after which the daemon exits; sbcAutoRestart starts it again
MAX_SUBSCRIBE_ERRORS = 10
RETENTION_INTERVAL_S = 600
PLUGIN_DATA_INTERVAL_S = 5

_shutdown = threading.Event()
# Closes the subscription socket, so a SIGTERM ends the main loop at once instead of after the
# subscription's 3 s timeout: DSF kills a plugin 4 s after SIGTERM (StopPlugin.cs:63-75)
_wake_main = []


def _signal_handler(_signum, _frame):
    _shutdown.set()
    for wake in list(_wake_main):
        try:
            wake()
        except Exception:  # noqa: BLE001 - a signal handler must not raise
            pass


def read_version():
    """Version from the installed manifest next to the plugin directory
    (<plugins>/QualityAssurance.json beside <plugins>/QualityAssurance/dsf/<this file>)."""
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


class PluginData:
    """Status summary in the object model (plugins.QualityAssurance.data), written on change."""

    def __init__(self, cmd, lock):
        self._cmd = cmd
        self._lock = lock
        self._last = {}

    def set(self, values):
        for key, value in values.items():
            if key not in PLUGIN_DATA_KEYS:
                continue
            text = "" if value is None else str(value)
            if self._last.get(key) == text:
                continue
            try:
                with self._lock:
                    self._cmd.set_plugin_data(PLUGIN_ID, key, text)
                self._last[key] = text
            except Exception as exc:  # noqa: BLE001
                logger.debug("set_plugin_data %s failed: %s", key, exc)


def now_ms():
    return int(time.time() * 1000)


def main():
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    settings = qa_settings.Settings()
    for problem in settings.load():
        logger.warning("settings: %s", problem)
    data_dir = qa_settings.data_dir()
    writer = qa_db.Writer(data_dir, settings.current()["commitIntervalS"])
    prepared = writer.start()
    if prepared.startswith("restored") or prepared == "replaced":
        logger.warning("database was damaged: %s", prepared)

    cmd = CommandConnection()
    if not connect_with_retry(cmd, "CommandConnection"):
        writer.stop()
        return
    qa_log.deferred_warnings.send_to(cmd)

    timelapse = qa_timelapse.Timelapse(writer, settings, data_dir)
    ctx = qa_api.ApiContext(version=read_version(), started=time.monotonic(), settings=settings,
                            writer=writer, readers=qa_db.Readers(data_dir), data_dir=data_dir,
                            index_cache=qa_gcode.IndexCache(data_dir), prepare_result=prepared,
                            timelapse=timelapse)

    def resolve_path(virtual):
        with ctx.cmd_lock:
            response = cmd.resolve_path(virtual)
        real = getattr(response, "result", response)
        return real if isinstance(real, str) else None

    ctx.resolve_path = resolve_path

    def send_code(code):
        # SBC channel: runs alongside the print's file channel (qa_accel docstring)
        with ctx.cmd_lock:
            return cmd.perform_simple_code(code)

    accel = qa_accel.Recorder(writer, settings, send_code, resolve_path)
    ctx.accel = accel

    def model_snapshot():
        with ctx.cmd_lock:
            return cmd.get_serialized_object_model()

    journal = qa_journal.Writer(data_dir, settings, model_snapshot)
    plugin_data = PluginData(cmd, ctx.cmd_lock)
    collector = qa_collector.Collector(writer, settings, resolve_path=resolve_path, broadcast=ctx.live.publish,
                                       plugin_version=ctx.version, index_cache=ctx.index_cache, timelapse=timelapse,
                                       accel=accel, journal=journal)
    ctx.collector = collector
    collector.init_ids()
    timelapse.on_event = collector.external_event
    timelapse.start()
    accel.on_event = collector.external_event
    accel.start()

    def connect_interceptor():
        # both file channels: a job on the second motion system sends M240 on File2
        conn = InterceptConnection(InterceptionMode.PRE, channels=[CodeChannel.File, CodeChannel.File2],
                                   filters=[qa_intercept.FILTER], auto_flush=True, auto_evaluate_expression=True)
        conn.connect()
        return conn

    camera = qa_intercept.CameraTrigger(timelapse, collector.camera_state, connect_interceptor, CodeChannel.SBC)
    camera.start()

    endpoints = []
    sub = None
    try:
        endpoints = qa_api.register_endpoints(cmd, ctx)
        sub = SubscribeConnection(SubscriptionMode.PATCH)
        if not connect_with_retry(sub, "SubscribeConnection"):
            return
        _wake_main.append(lambda: sub.socket.shutdown(socket.SHUT_RDWR))  # receive_json: ConnectionError
        model = sub.get_object_model()
        collector.update(model, None, now_ms())
        qa_log.deferred_warnings.send_to(cmd, ctx.cmd_lock)

        errors = 0
        last_retention = 0.0
        last_data = 0.0
        while not _shutdown.is_set():
            try:
                raw = sub.get_object_model_patch()
                patch = json.loads(raw)
                model.update_from_json(patch)
                now = now_ms()
                collector.update(model, patch, now)
                journal.patch(raw, now)   # the text as DSF sent it (qa_journal)
                errors = 0
            except TimeoutError:
                # the 3 s subscription timeout is the heartbeat
                now = now_ms()
                collector.tick(now)
                journal.tick(now)
            except Exception as exc:  # noqa: BLE001
                if _shutdown.is_set():
                    break
                errors += 1
                logger.error("subscription error (%d/%d): %s", errors, MAX_SUBSCRIBE_ERRORS, exc)
                if errors >= MAX_SUBSCRIBE_ERRORS:
                    raise
                _shutdown.wait(1.0)
                continue

            mono = time.monotonic()
            if mono - last_retention >= RETENTION_INTERVAL_S:
                last_retention = mono
                retention = settings.current()["retention"]
                writer.submit("retention", retention["jobs"], retention["days"], retention["maxDbBytes"])
                writer.submit("checkpoint")
                timelapse.request_retention()
                try:
                    kept = {row[0] for row in ctx.readers.get().execute("SELECT id FROM jobs WHERE raw_pruned=0")}
                    journal.retention(kept, settings.current()["journal"]["maxBytes"])
                except Exception as exc:  # noqa: BLE001
                    logger.error("journal retention failed: %s", exc)
            if mono - last_data >= PLUGIN_DATA_INTERVAL_S:
                last_data = mono
                status = collector.status()
                plugin_data.set({
                    "status": status["state"],
                    "currentJobId": status["currentJobId"],
                    "lastJobId": status["lastJobId"],
                    "dbSizeBytes": qa_db.file_size(data_dir),
                    "lastError": writer.last_error or "",
                })
            qa_log.deferred_warnings.send_to(cmd, ctx.cmd_lock)
    finally:
        try:
            collector.shutdown(now_ms())
        except Exception as exc:  # noqa: BLE001
            logger.error("collector shutdown failed: %s", exc)
        camera.stop()
        timelapse.stop()
        accel.stop()
        writer.stop()
        ctx.live.close()
        for endpoint in endpoints:
            try:
                endpoint.close()
            except Exception:  # noqa: BLE001
                pass
        qa_log.deferred_warnings.send_to(cmd, ctx.cmd_lock)
        for connection in (sub, cmd):
            if connection is not None:
                try:
                    connection.close()
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
