"""Console output of the daemon.

With ``sbcOutputRedirected`` DSF turns every stderr line into an ``Error:`` console message and
every stdout line into a ``Success:`` one, there is no third kind (DuetPluginService
StartPlugin.cs, MakeOutputHandler). So only errors go to stderr. Warnings are collected until
the command connection is up and then sent with ``write_message(MessageType.Warning, ...)``;
anything DSF refuses falls back to stderr. Pattern from dwc-vigil (verified there against
dsf-python 3.7 on 2026-09-16).
"""

import logging
import sys

PLUGIN_ID = "QualityAssurance"

logger = logging.getLogger("qa")


class StderrHandler(logging.StreamHandler):
    """Writes to whatever sys.stderr is at the time (as logging.lastResort does)."""

    def __init__(self):
        logging.Handler.__init__(self)

    @property
    def stream(self):
        return sys.stderr


class DeferredWarnings(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.pending = []

    def emit(self, record):
        if record.levelno < logging.ERROR:
            self.pending.append(self.format(record))

    def send_to(self, cmd, lock=None):
        """Hand pending warnings to DSF. ``lock`` guards a command connection shared with other threads."""
        if not self.pending:
            return
        from dsf.object_model import LogLevel, MessageType

        while self.pending:
            text = self.pending[0]
            try:
                if lock is not None:
                    with lock:
                        cmd.write_message(MessageType.Warning, f"[{PLUGIN_ID}]: {text}", True, LogLevel.Warn)
                else:
                    cmd.write_message(MessageType.Warning, f"[{PLUGIN_ID}]: {text}", True, LogLevel.Warn)
            except Exception:
                sys.stderr.write(text + "\n")
                sys.stderr.flush()
            del self.pending[0]


deferred_warnings = DeferredWarnings()


def setup():
    logger.setLevel(logging.WARNING)
    for handler in list(logger.handlers):
        if getattr(handler, "_qa_owned", False):
            logger.removeHandler(handler)
    stderr_handler = StderrHandler()
    stderr_handler.setLevel(logging.ERROR)
    for handler in (stderr_handler, deferred_warnings):
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler._qa_owned = True
        logger.addHandler(handler)
    return logger
