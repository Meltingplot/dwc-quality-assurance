"""M240 "trigger camera" for the timelapse (PLAN.md §5.11, Tim 2026-09-28).

The slicer calls a macro at each layer change that parks the head, sends M240 and returns; or it
sends M240 alone for a photo in place. QA holds M240 in DSF's code pipeline, takes the photo while
the machine stands still (once the camera's lagging picture stands still as well: ``Timelapse.photo``)
and resolves the code, so RRF never sees it. Facts (DuetSoftwareFramework
v3.7-dev @ cd3ae65f, RepRapFirmware 3.7-dev @ 3638836, dsf-python 3.7.0b1; read 2026-09-28):

- A Pre interceptor holds the code: nothing after it on that channel runs until QA answers, and
  DSF has no timeout for that (IPC/Processors/CodeInterception.cs:289). So every code is resolved in
  ``finally``, whatever happened; a lost connection lets the held code continue (:203-215).
- ``auto_flush`` hands M240 over once RRF acknowledged every earlier code of the channel, which is
  not standstill: RRF acknowledges a move when it is queued. M400 sent on the SBC channel over the
  intercept connection waits for the whole motion system, the file's queued moves included (RRF
  GCodes5.cpp:63-65). Codes for the held channel itself must not be sent: DSF cancels a code
  inserted into a job file's channel.
- A code from a macro runs on the caller's channel (File) and is intercepted like any other
  (CodeInterception.cs:224-251 does not look at IsFromMacro). M240 typed in the console runs on
  HTTP and is not intercepted (RRF runs /sys/M240.g or warns "not supported").
- Resolving needs the ``codeInterceptionReadWrite`` permission (Commands/Resolve.cs:13), M400
  ``commandExecution``.
- RRF does not implement M240 (GCodes2.cpp HandleMcode); unintercepted it runs /sys/M240.g, else
  prints "M240: Command is not supported". While QA is down the machine therefore wants an empty
  /sys/M240.g (chx350-config).
"""

import threading
import time

from qa_log import logger

FILTER = "M240"
RETRY_S = 10


class CameraTrigger:
    """One thread with an intercept connection. ``current()`` returns ``(job_key, layer, simulating)``
    or None without a job; ``connect()`` returns a connected dsf-python InterceptConnection;
    ``sbc`` is dsf-python's CodeChannel.SBC."""

    def __init__(self, timelapse, current, connect, sbc):
        self.timelapse = timelapse
        self.current = current
        self.connect = connect
        self.sbc = sbc
        self._stopping = threading.Event()
        self._conn = None
        self._thread = None
        self.handled = 0

    def start(self):
        self._thread = threading.Thread(target=self._run, name="qa-m240", daemon=True)
        self._thread.start()

    def stop(self, timeout=2):
        self._stopping.set()
        conn = self._conn
        if conn is not None:
            try:
                if getattr(conn, "socket", None) is not None:
                    conn.socket.shutdown(2)   # SHUT_RDWR: the blocked receive ends with ConnectionError
                else:
                    conn.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self):
        failing = False
        while not self._stopping.is_set():
            conn = None
            try:
                conn = self._conn = self.connect()
                failing = False
                while not self._stopping.is_set():
                    code = conn.receive_code()
                    try:
                        self._handle(conn, code)
                    except Exception as exc:  # noqa: BLE001 - the print must go on
                        logger.error("M240 photo failed: %s", exc)
                    finally:
                        conn.resolve_code()
            except Exception as exc:  # noqa: BLE001
                if self._stopping.is_set():
                    break
                if not failing:  # once per outage, not every RETRY_S
                    logger.error("M240 interception lost (%s); retrying every %d s", exc, RETRY_S)
                failing = True
            finally:
                self._conn = None
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
            self._stopping.wait(RETRY_S)

    def _handle(self, conn, _code):
        self.handled += 1
        state = self.current()
        if state is None:
            return
        job_key, layer, simulating = state
        if simulating:
            return
        conn.perform_simple_code("M400", self.sbc)
        result = self.timelapse.photo(job_key, layer, int(time.time() * 1000))
        logger.debug("M240 at layer %s: %s", layer, result)
