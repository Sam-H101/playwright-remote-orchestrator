"""gunicorn's settings for the Grid UI. The image's CMD loads them (--config python:ui.gunicorn_conf), so that
gunicorn's own lines (start-up, workers, errors) are written like the Grid UI's: one line a record, in
GRID_UI_LOG_FORMAT. The bind address and the worker count stay on the command line.
"""
from gunicorn import glogging

from . import logs


class Logger(glogging.Logger):
    """gunicorn's logger in the Grid UI's format, and no lower than its level."""

    def setup(self, cfg):
        super().setup(cfg)
        ours = logs.configure()  # the master's logging too: it never imports ui.app
        for handler in self.error_log.handlers:
            handler.setFormatter(ours.formatter)
        # GRID_UI_LOG_LEVEL only raises gunicorn's level (--log-level, info unless given): at debug gunicorn
        # writes its whole configuration and a line per request.
        self.error_log.setLevel(max(self.error_log.level, ours.least))

    def exception(self, msg, *args, **kwargs):
        # A request that fails outside the app is logged with its address, query string and all, and the sign-in
        # callback's carries the authorization code.
        super().exception(msg, *(a.partition("?")[0] if isinstance(a, str) else a for a in args), **kwargs)


logger_class = Logger
