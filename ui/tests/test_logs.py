"""The Grid UI's log (logs.py): the level and format settings, one line a record, the request log, and
gunicorn's own lines (gunicorn_conf.py)."""
import contextlib
import importlib
import json
import logging
import os
import re
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import pytest
from werkzeug.serving import make_server

from ui import app as grid_app, demo, logs, store

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TIME = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z"
# Everything a terminal or a log collector takes for the end of a line, or for an instruction.
BREAKS = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")


@pytest.fixture
def configured():
    """configured(level=..., style=...) sets GRID_UI_LOG_LEVEL and GRID_UI_LOG_FORMAT and sets the log up from
    them. Afterwards the settings and the log are what they were. It patches for itself: the monkeypatch fixture
    would put the settings back only after this one has finished."""
    with pytest.MonkeyPatch.context() as patch:
        def configure(level=None, style=None):
            for name, value in (("GRID_UI_LOG_LEVEL", level), ("GRID_UI_LOG_FORMAT", style)):
                if value is None:
                    patch.delenv(name, raising=False)
                else:
                    patch.setenv(name, value)
            return logs.configure()

        yield configure
    logs.configure()


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = str(tmp_path / "grid.db")
    with contextlib.closing(sqlite3.connect(path)) as con, con:
        demo.seed(con)
    monkeypatch.setattr(store, "DB_ENGINE", "sqlite")
    monkeypatch.setattr(store, "SQLITE_PATH", path)
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "")
    return grid_app.app.test_client()


def written(capsys):
    """The lines that reached stderr since the last look, split only where a line really ends."""
    err = capsys.readouterr().err
    assert err == "" or err.endswith("\n")
    return err[:-1].split("\n") if err else []


def ours():
    return [h for h in logging.getLogger().handlers if getattr(h, "grid_ui", False)]


# ---- the level and the format ----------------------------------------------------------

def test_info_lines_reach_the_log_with_nothing_set(configured, capsys):
    configured()
    logging.getLogger("ui.app").debug("recorder: no snapshot before the action: %s", "busy")
    logging.getLogger("ui.app").info("mfa secret %r added by %s", "intranet", "Admin")
    logging.getLogger("ui.store").warning("could not apply schema statement: %s", "denied")
    first, second = written(capsys)
    assert re.fullmatch(TIME + " INFO ui.app: mfa secret 'intranet' added by Admin", first)
    assert re.fullmatch(TIME + " WARNING ui.store: could not apply schema statement: denied", second)


@pytest.mark.parametrize("level, expected", [
    ("debug", ["DEBUG", "INFO", "WARNING", "ERROR"]),
    ("info", ["INFO", "WARNING", "ERROR"]),
    ("warning", ["WARNING", "ERROR"]),
    ("error", ["ERROR"]),
    (" Warning ", ["WARNING", "ERROR"]),
    ("", ["INFO", "WARNING", "ERROR"]),
])
def test_the_level_setting(configured, capsys, level, expected):
    configured(level=level)
    for name in ("debug", "info", "warning", "error"):
        getattr(logging.getLogger("ui.store"), name)("a line")
    assert [line.split()[1] for line in written(capsys) if line.endswith(" ui.store: a line")] == expected


def test_a_logger_with_a_level_of_its_own_is_held_to_the_level_too(configured, capsys):
    # werkzeug's and gunicorn's loggers are set to info, whatever the root logger's level is.
    own = logging.getLogger("ui.tests.a-level-of-its-own")
    own.setLevel(logging.INFO)
    try:
        configured(level="warning")
        own.info("Running on http://127.0.0.1:4444")
        own.warning("kept")
    finally:
        own.setLevel(logging.NOTSET)
    assert [line.split(": ", 1)[1] for line in written(capsys)] == ["kept"]


def test_a_wrong_level_is_warned_about_once_and_info_taken(configured, capsys):
    handler = configured(level="loud")
    assert handler.least == logging.INFO and logging.getLogger().level == logging.INFO
    logs.configure()  # a worker's import, after the master's
    logging.getLogger("ui.app").debug("not this")
    logging.getLogger("ui.app").info("but this")
    warning, line = written(capsys)
    assert re.fullmatch(TIME + " WARNING ui.logs: GRID_UI_LOG_LEVEL is debug, info, warning or error, not 'loud': "
                        "taking info", warning)
    assert line.endswith(" INFO ui.app: but this")


@pytest.mark.parametrize("style", ["json", " JSON "])
def test_the_format_setting(configured, capsys, style):
    configured(style=style)
    logging.getLogger("ui.app").info("mfa secret %d deleted by %s", 7, "Admin")
    (line,) = written(capsys)
    assert json.loads(line)["message"] == "mfa secret 7 deleted by Admin"


def test_a_wrong_format_is_warned_about_once_and_text_taken(configured, capsys):
    configured(style="xml")
    logs.configure()
    logging.getLogger("ui.app").info("in text")
    warning, line = written(capsys)
    assert re.fullmatch(TIME + " WARNING ui.logs: GRID_UI_LOG_FORMAT is text or json, not 'xml': taking text", warning)
    assert re.fullmatch(TIME + " INFO ui.app: in text", line)


def test_a_changed_setting_is_taken_up(configured, capsys):
    handler = configured(level="error")
    assert configured(level="debug", style="json") is handler and handler.least == logging.DEBUG
    logging.getLogger("ui.recorder").debug("recorder: snapshot failed: %s", "busy")
    (line,) = [line for line in written(capsys) if "snapshot failed: busy" in line]
    assert (json.loads(line)["level"], json.loads(line)["logger"]) == ("DEBUG", "ui.recorder")


# ---- what a line is --------------------------------------------------------------------

def test_a_json_line_has_the_same_keys_every_time(configured, capsys):
    configured(style="json")
    logging.getLogger("ui.app").info("%s signed in with single sign-on (subject %s)", "Zoë Ångström", "00u1")
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("ui.store").exception("couldn't record %s in the audit log", "mfa.added")
    plain, failed = (json.loads(line) for line in written(capsys))
    assert list(plain) == ["time", "level", "logger", "message"]
    assert re.fullmatch(TIME, plain["time"])
    assert (plain["level"], plain["logger"]) == ("INFO", "ui.app")
    assert plain["message"] == "Zoë Ångström signed in with single sign-on (subject 00u1)"
    assert list(failed) == ["time", "level", "logger", "message", "exception"]
    assert (failed["level"], failed["logger"]) == ("ERROR", "ui.store")
    assert failed["message"] == "couldn't record mfa.added in the audit log"
    assert failed["exception"].startswith("Traceback (most recent call last):\n")
    assert failed["exception"].endswith("\nValueError: boom")


def test_the_time_is_utc(configured, capsys):
    configured(style="json")
    record = logging.LogRecord("ui.app", logging.INFO, __file__, 1, "then", (), None)
    record.created = 1111111109.25  # 2005-03-18 01:58:29 UTC, one of RFC 6238's times
    logs.configure().handle(record)
    assert json.loads(written(capsys)[0])["time"] == "2005-03-18T01:58:29.250Z"


def test_a_text_line_holds_the_exception_too(configured, capsys):
    configured()
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("ui.store").exception("couldn't record %s in the audit log", "mfa.added")
    (line,) = written(capsys)
    assert re.match(TIME + r" ERROR ui.store: couldn't record mfa.added in the audit log\\nTraceback \(most recent "
                    r"call last\):\\n  File ", line)
    assert line.endswith("\\nValueError: boom")


NAMES = [
    "Mallory\n2026-01-01T00:00:00.000Z INFO ui.app: Admin signed in with single sign-on (subject 1)",
    "Mallory\r\nINFO", "a\rb", "a\x0bb\x0cc", "a\x1cb\x1db\x1ec", "a\x85b", "a\u2028b\u2029c", "a\x00b\x7fc",
    "a\x1b[2Kb\x1b]0;title\x07c", "a\ud800b",
]


@pytest.mark.parametrize("style", ["text", "json"])
@pytest.mark.parametrize("name", NAMES)
def test_a_record_is_one_line_whatever_its_values_hold(configured, capsys, style, name):
    configured(style=style)
    logging.getLogger("ui.app").info("%s signed in with single sign-on (subject %s)", name, name)
    logging.getLogger("ui.app").warning("saved sign-in " + name + ": no key")
    try:
        raise ValueError(name)
    except ValueError:
        logging.getLogger("ui.app").exception("refused")
    err = capsys.readouterr().err
    assert err.count("\n") == 3 and err.endswith("\n")
    assert not BREAKS.search(err.replace("\n", "")) and err.isascii()
    signed_in, warned, refused = err[:-1].split("\n")
    if style == "json":
        assert json.loads(signed_in)["message"] == "{0} signed in with single sign-on (subject {0})".format(name)
        assert json.loads(warned)["message"] == "saved sign-in " + name + ": no key"
        assert json.loads(refused)["exception"].endswith("ValueError: " + name)
    else:
        escaped = name.encode("unicode_escape").decode("ascii")
        assert signed_in.endswith(" INFO ui.app: {0} signed in with single sign-on (subject {0})".format(escaped))
        assert warned.endswith(" WARNING ui.app: saved sign-in " + escaped + ": no key")
        assert refused.endswith("ValueError: " + escaped)


def test_the_development_servers_colours_are_left_out(configured, capsys):
    configured()
    logging.getLogger("werkzeug").warning("\x1b[31m\x1b[1mWARNING: This is a development server.\x1b[0m")
    logging.getLogger("werkzeug").warning(" * Running on all addresses (0.0.0.0)\n * Running on http://127.0.0.1:4444")
    first, second = written(capsys)
    assert first.endswith(" WARNING werkzeug: WARNING: This is a development server.")
    assert second.endswith(" WARNING werkzeug:  * Running on all addresses (0.0.0.0)\\n * Running on "
                           "http://127.0.0.1:4444")


# ---- set up once -----------------------------------------------------------------------

def test_setting_the_log_up_again_adds_no_second_handler(configured, capsys):
    handler = configured()
    others = [h for h in logging.getLogger().handlers if h is not handler]
    assert ours() == [handler]
    assert logs.configure() is handler
    assert importlib.reload(logs).configure() is handler  # the module loaded again still knows its handler
    assert configured(style="json") is handler and configured() is handler
    assert ours() == [handler]
    assert [h for h in logging.getLogger().handlers if h is not handler] == others  # pytest's own, untouched
    logging.getLogger("ui.app").info("once")
    assert len(written(capsys)) == 1


def test_importing_the_app_twice_leaves_one_handler():
    code = ("import importlib, logging, ui.app\n"
            "importlib.reload(ui.app)\n"
            "handlers = logging.getLogger().handlers\n"
            "print(len(handlers), sum(getattr(h, 'grid_ui', False) for h in handlers))\n"
            "logging.getLogger('ui.app').info('a made-up line')\n")
    env = {name: value for name, value in os.environ.items() if not name.startswith("GRID_UI_LOG_")}
    done = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
    assert done.stdout.split() == ["1", "1"], done.stderr
    lines = [line for line in done.stderr.splitlines() if "a made-up line" in line]
    assert len(lines) == 1 and re.fullmatch(TIME + " INFO ui.app: a made-up line", lines[0])


def test_caplog_gets_the_records_as_before(configured, caplog):
    configured(level="error", style="json")
    with caplog.at_level(logging.INFO, logger="ui.app"):
        logging.getLogger("ui.app").info("mfa secret %r added by %s", "intranet", "Admin")
    assert [(r.name, r.levelname, r.getMessage()) for r in caplog.records] == [
        ("ui.app", "INFO", "mfa secret 'intranet' added by Admin")]
    assert "mfa secret 'intranet' added by Admin" in caplog.text


# ---- the request log -------------------------------------------------------------------

def requests_logged(capsys):
    return [json.loads(line) for line in written(capsys) if json.loads(line)["logger"] == "ui.access"]


def test_no_request_is_logged_unless_asked_for(client, configured, monkeypatch, capsys):
    configured(style="json")
    monkeypatch.setattr(grid_app, "ACCESS_LOG", False)
    assert client.get("/api/status?code=made-up-code", buffered=True).status_code == 200
    assert requests_logged(capsys) == []


def test_a_request_is_logged_without_its_query_string(client, configured, monkeypatch, capsys):
    configured(style="json")
    monkeypatch.setattr(grid_app, "ACCESS_LOG", True)
    client.set_cookie("grid-session", "made-up-cookie")
    r = client.get("/api/status?code=made-up-code&state=made-up-state", buffered=True, headers={
        "X-Grid-Token": "made-up-token", "Authorization": "Bearer made-up-bearer", "User-Agent": "made-up-agent"})
    assert r.status_code == 200
    r = client.get("/auth/callback?code=made-up-code&state=made-up-state", buffered=True)
    assert r.status_code == 404  # single sign-on isn't set up here
    r = client.post("/api/mfa", json={"name": "made-up-name", "secret": "made-up-seed"}, buffered=True)
    err = capsys.readouterr().err
    assert "made-up" not in err
    status, callback, mfa_added = (json.loads(line) for line in err.splitlines() if '"ui.access"' in line)
    assert list(status) == ["time", "level", "logger", "message", "method", "path", "status", "duration_ms", "client"]
    assert (status["level"], status["logger"]) == ("INFO", "ui.access")
    assert (status["method"], status["path"], status["status"], status["client"]) == (
        "GET", "/api/status", 200, "127.0.0.1")
    assert isinstance(status["duration_ms"], float) and 0 <= status["duration_ms"] < 60000
    assert re.fullmatch(r"GET /api/status 200 in \d+\.\d ms from 127\.0\.0\.1", status["message"])
    assert (callback["method"], callback["path"], callback["status"]) == ("GET", "/auth/callback", 404)
    assert (mfa_added["method"], mfa_added["path"], mfa_added["status"]) == ("POST", "/api/mfa", r.status_code)


def test_a_request_is_one_text_line(client, configured, monkeypatch, capsys):
    configured()
    monkeypatch.setattr(grid_app, "ACCESS_LOG", True)
    client.get("/api/status?code=made-up-code", buffered=True)
    (line,) = [line for line in written(capsys) if " ui.access: " in line]
    assert re.fullmatch(TIME + r" INFO ui.access: GET /api/status 200 in \d+\.\d ms from 127\.0\.0\.1", line)


def test_a_logged_path_is_percent_encoded(client, configured, monkeypatch, capsys):
    # The path arrives decoded: a "?" that was part of it must not read as the start of a query string, nor a
    # space as the end of the path.
    configured(style="json")
    monkeypatch.setattr(grid_app, "ACCESS_LOG", True)
    client.get("/nope%3Fcode=x/a%20b%0A%E2%80%A8?state=made-up-state", buffered=True)
    (line,) = requests_logged(capsys)
    assert (line["path"], line["status"]) == ("/nope%3Fcode%3Dx/a%20b%0A%E2%80%A8", 404)


def test_requests_are_logged_whatever_the_level(client, configured, monkeypatch, capsys):
    configured(level="error", style="json")
    monkeypatch.setattr(grid_app, "ACCESS_LOG", True)
    client.get("/api/status", buffered=True)
    assert [line["path"] for line in requests_logged(capsys)] == ["/api/status"]


def test_the_logged_address_is_the_one_the_proxies_were_asked_from(client, configured, monkeypatch, capsys):
    configured(style="json")
    monkeypatch.setattr(grid_app, "ACCESS_LOG", True)
    monkeypatch.setattr(grid_app, "PROXY_HOPS", 1)
    client.get("/api/status", buffered=True, headers={"X-Forwarded-For": "198.51.100.7, 203.0.113.9"})
    assert [line["client"] for line in requests_logged(capsys)] == ["203.0.113.9"]


@pytest.mark.parametrize("value, on", [(None, False), ("", False), ("0", False), ("1", True), (" 1 ", True)])
def test_the_request_log_setting(monkeypatch, caplog, value, on):
    if value is None:
        monkeypatch.delenv("GRID_UI_ACCESS_LOG", raising=False)
    else:
        monkeypatch.setenv("GRID_UI_ACCESS_LOG", value)
    with caplog.at_level(logging.WARNING, logger="ui.app"):
        assert grid_app._access_log() is on
    assert caplog.records == []


@pytest.mark.parametrize("value", ["yes", "true", "2", "on"])
def test_a_wrong_request_log_setting_is_warned_about_and_off(monkeypatch, caplog, value):
    monkeypatch.setenv("GRID_UI_ACCESS_LOG", value)
    with caplog.at_level(logging.WARNING, logger="ui.app"):
        assert grid_app._access_log() is False
    assert [r.getMessage() for r in caplog.records] == [
        "GRID_UI_ACCESS_LOG is 1 for a line in the log per request, or 0 for none, not {!r}: taking 0".format(value)]


def test_the_development_server_writes_no_request_line_of_its_own(configured, monkeypatch, capsys):
    """werkzeug's line per request has the query string in it: python -m ui.app serves without it, and with the
    request log when that is asked for."""
    configured()
    monkeypatch.setattr(grid_app, "ACCESS_LOG", True)
    server = make_server("127.0.0.1", 0, grid_app.app, request_handler=grid_app._DevServerRequests)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:{}/auth/callback?code=made-up-code&state=made-up-state".format(server.server_port)
    try:
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(url, timeout=10)
        refused.value.close()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    lines = written(capsys)
    assert not any("made-up" in line for line in lines)
    (line,) = [line for line in lines if "/auth/callback" in line]
    assert re.fullmatch(TIME + r" INFO ui.access: GET /auth/callback 404 in \d+\.\d ms from 127\.0\.0\.1", line)


def test_a_request_whose_client_hung_up_is_logged_too(configured, monkeypatch, capsys):
    """werkzeug's development server never closes the answer of a client that reset the connection, so the line
    can't wait for that."""
    configured()
    monkeypatch.setattr(grid_app, "ACCESS_LOG", True)
    server = make_server("127.0.0.1", 0, grid_app.app, request_handler=grid_app._DevServerRequests)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=10) as client:
            client.sendall(b"GET /nope?code=made-up-code HTTP/1.0\r\n\r\n")
            assert client.recv(1)  # the answer is on its way
            # Closed without lingering, with the rest unread: the server gets a reset.
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    lines = written(capsys)
    assert not any("made-up" in line for line in lines)
    (line,) = [line for line in lines if "/nope" in line]
    assert re.fullmatch(TIME + r" INFO ui.access: GET /nope 404 in \d+\.\d ms from 127\.0\.0\.1", line)


# ---- gunicorn's own lines (gunicorn_conf.py) -------------------------------------------

@pytest.fixture
def gunicorn_logger(configured):
    """gunicorn_logger(level=..., style=...): gunicorn's logger as its master makes it from ui/gunicorn_conf.py,
    under those settings. gunicorn's two loggers are what they were afterwards."""
    pytest.importorskip("gunicorn.glogging")  # not installed, or on Windows, where it can't be imported
    from gunicorn.config import Config

    from ui import gunicorn_conf

    loggers = [logging.getLogger(name) for name in ("gunicorn.error", "gunicorn.access")]
    before = [(logger, logger.level, logger.propagate, logger.handlers[:]) for logger in loggers]

    def make(**settings):
        configured(**settings)
        cfg = Config()
        cfg.set("logger_class", gunicorn_conf.logger_class)
        return cfg.logger_class(cfg)

    yield make
    for logger, level, propagate, handlers in before:
        logger.setLevel(level)
        logger.propagate = propagate
        logger.handlers[:] = handlers


@pytest.mark.parametrize("style", ["text", "json"])
def test_gunicorns_own_lines_are_in_the_format(gunicorn_logger, capsys, style):
    gunicorn = gunicorn_logger(style=style)
    assert len(ours()) == 1  # the master's logging is set up as well: it never imports ui.app
    gunicorn.info("Starting gunicorn %s", "26.2.0")
    logging.getLogger("ui.logs").info("and ours")
    first, second = written(capsys)
    if style == "json":
        first, second = json.loads(first), json.loads(second)
        assert list(first) == list(second) == ["time", "level", "logger", "message"]
        assert (first["level"], first["logger"]) == ("INFO", "gunicorn.error")
        assert (first["message"], second["message"]) == ("Starting gunicorn 26.2.0", "and ours")
    else:
        assert re.fullmatch(TIME + " INFO gunicorn.error: Starting gunicorn 26.2.0", first)
        assert re.fullmatch(TIME + " INFO ui.logs: and ours", second)


def test_the_level_raises_gunicorns_and_never_lowers_it(gunicorn_logger, capsys):
    gunicorn = gunicorn_logger(level="warning")
    gunicorn.info("Booting worker with pid: %s", 14)
    gunicorn.warning("Worker (pid:%s) was sent SIGKILL!", 14)
    assert [line.split(": ", 1)[1] for line in written(capsys)] == ["Worker (pid:14) was sent SIGKILL!"]
    # At debug gunicorn would write its whole configuration and a line per request: that stays --log-level's.
    gunicorn = gunicorn_logger(level="debug")
    gunicorn.debug("GET /api/status")
    gunicorn.info("Booting worker with pid: %s", 15)
    assert [line.split(": ", 1)[1] for line in written(capsys)] == ["Booting worker with pid: 15"]


def test_setting_gunicorns_logger_up_again_adds_no_second_handler(gunicorn_logger, capsys):
    gunicorn = gunicorn_logger()
    gunicorn.setup(gunicorn.cfg)  # as a reload (SIGHUP) does
    assert len(ours()) == 1
    gunicorn.info("Hang up: %s", "Master")
    assert len(written(capsys)) == 1


def test_gunicorn_logs_a_failed_request_without_its_query_string(gunicorn_logger, capsys):
    gunicorn = gunicorn_logger(style="json")
    try:
        raise OSError("gone")
    except OSError:  # as gunicorn's worker logs it
        gunicorn.exception("Error handling request %s %s", "GET", "/auth/callback?code=made-up-code&state=made-up")
    (line,) = written(capsys)
    assert "made-up" not in line
    line = json.loads(line)
    assert line["message"] == "Error handling request GET /auth/callback"
    assert line["exception"].endswith("OSError: gone")
