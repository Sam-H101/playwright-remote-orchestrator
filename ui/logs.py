"""The Grid UI's log: every record as one line on stderr, as text or as JSON, from the level asked for.

Left alone, Python's logging lets only warnings and worse out, bare. configure() sets it up: ui.app calls it as
it is imported (by a gunicorn worker, or run as python -m ui.app), and ui/gunicorn_conf.py for gunicorn's master.
"""
import datetime
import json
import logging
import os
import re
import sys

log = logging.getLogger(__name__)

LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}
FORMATS = ("text", "json")
# The request log's logger (GRID_UI_ACCESS_LOG in ui/app.py). It is asked for by name, so its lines are written
# whatever the level.
ACCESS = "ui.access"

# What ends a line, or moves about on a terminal: the control characters (C0, DEL, C1), Unicode's line and
# paragraph separators, and lone surrogates, which no stream can encode. A name from an identity provider may
# hold any of them.
_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]")
# The colours werkzeug's development server puts in its lines.
_COLOURS = re.compile(r"\x1b\[[0-9;]*m")


def _one_line(text):
    return _UNSAFE.sub(lambda found: found.group().encode("unicode_escape").decode("ascii"), text)


def _utc(seconds):
    moment = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _Formatter(logging.Formatter):
    """A record as exactly one line, whatever its message, its values or its traceback hold.

    json: {"time", "level", "logger", "message"}, "exception" when there is one, and a request line's own keys
    (see access). text: the same facts as "<time> <level> <logger>: <message>", with what would end the line
    written as its escape (a traceback's line ends as \\n)."""

    def __init__(self, style):
        super().__init__()
        self.style = style

    def format(self, record):
        fields = {"time": _utc(record.created), "level": record.levelname, "logger": record.name,
                  "message": _COLOURS.sub("", record.getMessage())}
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)  # kept for the next handler, as logging does
        exception = "\n".join(text for text in (record.exc_text, record.stack_info) if text)
        if self.style == "json":
            if exception:
                fields["exception"] = exception
            fields.update(getattr(record, "fields", {}))
            # json.dumps writes every control character, and every character past ASCII, as its escape.
            return json.dumps(fields, separators=(",", ":"))
        line = "{time} {level} {logger}: {message}".format(**fields)
        return _one_line(line + "\n" + exception) if exception else _one_line(line)


class _Handler(logging.StreamHandler):
    """Writes to what sys.stderr is when a record comes, as logging's own last resort does, so a stream put in
    its place later (pytest's capture) gets the lines."""
    grid_ui = True  # what configure() knows its handler by, also once this module has been loaded again

    def __init__(self):
        logging.Handler.__init__(self)
        self.least = logging.INFO
        self.asked = None

    @property
    def stream(self):
        return sys.stderr

    def filter(self, record):
        # The level is kept here, not as the handler's own or the root logger's alone: werkzeug adds a handler
        # of its own unless one takes its info lines, and a logger with a level of its own (werkzeug's,
        # gunicorn's) isn't held to the root logger's.
        if record.levelno < self.least and record.name != ACCESS:
            return False
        return super().filter(record)


def _setting(name, values, default):
    """(The setting, or the default when it isn't one of values; what it was instead, None when it was fine)."""
    text = (os.environ.get(name) or default).strip().lower()
    return (text, None) if text in values else (default, text)


def configure():
    """Set this process's logging up from GRID_UI_LOG_LEVEL and GRID_UI_LOG_FORMAT, and return the handler.

    The root logger gets one handler however often this runs: gunicorn's master runs it and every worker's import
    of ui.app runs it again, with the master's handler already there. Other handlers (pytest's) are left alone.
    A setting that changed since is taken up; a wrong one is warned about once per change."""
    root = logging.getLogger()
    handler = next((h for h in root.handlers if getattr(h, "grid_ui", False)), None)
    asked = (os.environ.get("GRID_UI_LOG_LEVEL"), os.environ.get("GRID_UI_LOG_FORMAT"))
    if handler is None:
        handler = _Handler()
        root.addHandler(handler)
    elif handler.asked == asked:
        return handler
    level, wrong_level = _setting("GRID_UI_LOG_LEVEL", LEVELS, "info")
    style, wrong_style = _setting("GRID_UI_LOG_FORMAT", FORMATS, "text")
    handler.setFormatter(_Formatter(style))
    handler.least = LEVELS[level]
    handler.asked = asked
    root.setLevel(LEVELS[level])
    logging.getLogger(ACCESS).setLevel(logging.INFO)
    # Said last, so they come out like every other line.
    if wrong_level is not None:
        log.warning("GRID_UI_LOG_LEVEL is debug, info, warning or error, not %r: taking info", wrong_level)
    if wrong_style is not None:
        log.warning("GRID_UI_LOG_FORMAT is text or json, not %r: taking text", wrong_style)
    return handler


def access(method, path, status, seconds, client):
    """One line of the request log. path is the path alone: the query string never comes here (the sign-in
    callback's carries the authorization code and the state), nor anything from headers, cookies or bodies."""
    took = round(seconds * 1000, 1)
    fields = {"method": method, "path": path, "status": status, "duration_ms": took, "client": client}
    logging.getLogger(ACCESS).info("%s %s %d in %.1f ms from %s", method, path, status, took, client,
                                   extra={"fields": fields})
