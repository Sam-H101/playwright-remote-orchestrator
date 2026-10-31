import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from src import database  # noqa: E402

SLOT = database.Slot(2, 7, "HOST1", "ws://hub:4500/connect/HOST1/2?gateway_token=t", True)

CONNECT_FAILURE_OUTPUT = """
>           raise Exception(HUB_FAILURE if is_hub_error(ws, ex) else CONNECT_FAILURE) from ex
E           Exception: connect_Failure
FAILED example.py::test_docusign - Exception: connect_Failure
"""
HUB_FAILURE_OUTPUT = """
>           raise Exception(HUB_FAILURE if is_hub_error(ws, ex) else CONNECT_FAILURE) from ex
E           Exception: hub_Failure
FAILED example.py::test_docusign - Exception: hub_Failure
"""
SITE_DOWN_OUTPUT = """
E           Exception: NA2 Failed
FAILED example.py::test_docusign - Exception: NA2 Failed
"""


class FakeDb:
    def __init__(self, free_after=0, fail_init=False, fail_release=None, fail_count=None):
        self.free_after = free_after
        self.fail_init = fail_init
        self.fail_release = fail_release
        self.fail_count = fail_count
        self.calls = []

    def initial_db_start(self):
        self.calls.append("init")
        if self.fail_init:
            raise RuntimeError("db down")

    def get_a_port(self):
        self.calls.append("get")
        if self.free_after:
            self.free_after -= 1
            raise database.NoFreeSlot("no free slots available")
        return SLOT

    def increment_failure(self, slot):
        self.calls.append(("fail", slot.id))
        if self.fail_count:
            raise self.fail_count

    def reset_failure(self, slot):
        self.calls.append(("reset", slot.id))
        if self.fail_count:
            raise self.fail_count

    def release_port(self, slot):
        self.calls.append(("release", slot.id))
        if self.fail_release:
            raise self.fail_release


def fake_check(monkeypatch, result):
    seen = {}

    def run_check(testfile, env):
        seen["env"] = env
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(main, "run_check", run_check)
    return seen


def test_marker_is_module_constant():
    assert main.CONNECT_FAILURE_MARKER == "connect_Failure"
    assert main.is_connect_failure(CONNECT_FAILURE_OUTPUT)
    assert not main.is_connect_failure(SITE_DOWN_OUTPUT)
    assert main.HUB_FAILURE_MARKER == "hub_Failure"
    assert main.is_hub_failure(HUB_FAILURE_OUTPUT)
    assert not main.is_connect_failure(HUB_FAILURE_OUTPUT)


def test_check_env():
    env = main.check_env(SLOT, {"PATH": "x"})
    assert env == {"PATH": "x", "PW_WS_ENDPOINT": SLOT.endpoint, "host": "HOST1", "port": "2"}


def test_check_env_does_not_mutate_os_environ():
    main.check_env(SLOT)
    assert os.environ.get("PW_WS_ENDPOINT") != SLOT.endpoint


def test_redact_hides_token():
    assert main.redact(SLOT.endpoint) == "ws://hub:4500/connect/HOST1/2"


def test_success_resets_failures_and_releases(monkeypatch):
    seen = fake_check(monkeypatch, (0, "1 passed"))
    db = FakeDb()
    assert main.datest(db, "example.py") == 0
    assert db.calls == ["get", ("reset", 7), ("release", 7)]
    assert seen["env"]["PW_WS_ENDPOINT"] == SLOT.endpoint


def test_connect_failure_counts_against_slot(monkeypatch):
    fake_check(monkeypatch, (1, CONNECT_FAILURE_OUTPUT))
    db = FakeDb()
    assert main.datest(db, "example.py") == 1
    assert db.calls == ["get", ("fail", 7), ("release", 7)]


def test_site_down_does_not_count_against_slot(monkeypatch):
    fake_check(monkeypatch, (1, SITE_DOWN_OUTPUT))
    db = FakeDb()
    assert main.datest(db, "example.py") == 1
    # it reached the Playwright server, so the slot's consecutive connect failures end here
    assert db.calls == ["get", ("reset", 7), ("release", 7)]


def test_hub_failure_leaves_the_slot_count_alone(monkeypatch):
    fake_check(monkeypatch, (1, HUB_FAILURE_OUTPUT))
    db = FakeDb()
    assert main.datest(db, "example.py") == 1
    assert db.calls == ["get", ("release", 7)]


def test_release_error_keeps_the_check_result(monkeypatch):
    fake_check(monkeypatch, (0, "1 passed"))
    db = FakeDb(fail_release=database.pymysql.err.OperationalError(2003, "Can't connect to MySQL server"))
    assert main.datest(db, "example.py") == 0
    assert db.calls[-1] == ("release", 7)


def test_release_error_does_not_hide_the_check_error(monkeypatch):
    fake_check(monkeypatch, OSError("no pytest"))
    db = FakeDb(fail_release=database.pymysql.err.OperationalError(2003, "down"))
    with pytest.raises(OSError):
        main.datest(db, "example.py")


@pytest.mark.parametrize("result", [(0, "1 passed"), (1, CONNECT_FAILURE_OUTPUT)])
def test_failure_count_error_keeps_the_check_result(monkeypatch, result):
    fake_check(monkeypatch, result)
    db = FakeDb(fail_count=database.pymysql.err.OperationalError(2003, "down"))
    assert main.datest(db, "example.py") == result[0]
    assert db.calls[-1] == ("release", 7)


def test_slot_released_when_check_raises(monkeypatch):
    fake_check(monkeypatch, OSError("no pytest"))
    db = FakeDb()
    with pytest.raises(OSError):
        main.datest(db, "example.py")
    assert db.calls[-1] == ("release", 7)


def test_waits_for_a_free_slot_with_capped_backoff():
    sleeps = []
    db = FakeDb(free_after=8)
    assert main.acquire_slot(db, sleep=sleeps.append) is SLOT
    assert sleeps == [1, 2, 4, 8, 16, 32, 60, 60]


def test_gives_up_waiting_for_a_slot_after_the_timeout():
    sleeps = []
    db = FakeDb(free_after=100)
    with pytest.raises(database.NoFreeSlot, match="gave up after 100 seconds"):
        main.acquire_slot(db, sleep=sleeps.append, timeout=100)
    assert sleeps == [1, 2, 4, 8, 16, 32, 37]


def test_single_run_gives_up_when_no_slot_is_free(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "NO_SLOT_TIMEOUT", 0)
    monkeypatch.setattr(main.database, "database", lambda: FakeDb(free_after=100))
    installed = []
    monkeypatch.setattr(main.signal, "signal", lambda *args: installed.append(args))
    handlers = list(main.checklog.handlers), list(main.errorlog.handlers)
    try:
        assert main.main(["--fil", "example.py", "--fol", str(tmp_path)]) == 1
    finally:
        for logger, before in zip((main.checklog, main.errorlog), handlers):
            for handler in logger.handlers[len(before):]:
                handler.close()
            logger.handlers[:] = before
    assert installed == [(main.signal.SIGTERM, main.exit_on_sigterm)]
    assert "gave up" in (tmp_path / "logs" / "check_info.log").read_text()


def test_interval_mode_waits_for_a_slot_without_a_limit(monkeypatch):
    monkeypatch.setattr(main, "NO_SLOT_TIMEOUT", 0)
    seen = []
    monkeypatch.setattr(main, "datest", lambda db, testfile, slot_timeout: seen.append(slot_timeout) or 0)

    def stop(seconds):
        raise StopLoop()
    with pytest.raises(StopLoop):
        main.run_test("example.py", 100, db=FakeDb(), sleep=stop)
    assert main.run_test("example.py", 0, db=FakeDb(), sleep=None) == 0
    assert seen == [None, 0]


class StopLoop(BaseException):
    pass


def test_interval_loop_survives_errors(monkeypatch):
    results = iter([OSError("boom"), (0, "ok"), (1, SITE_DOWN_OUTPUT)])
    monkeypatch.setattr(main, "run_check", lambda testfile, env: _next(results))
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise StopLoop()
    db = FakeDb(fail_init=True)
    with pytest.raises(StopLoop):
        main.run_test("example.py", 100, db=db, sleep=sleep)
    assert sleeps == [100, 100, 100]
    assert db.calls.count(("release", 7)) == 3
    # the schema step is retried until it works
    assert db.calls.count("init") == 3


def _next(results):
    result = next(results)
    if isinstance(result, Exception):
        raise result
    return result


def test_sigterm_handler_raises_system_exit():
    with pytest.raises(SystemExit) as exit:
        main.exit_on_sigterm(main.signal.SIGTERM, None)
    assert exit.value.code == 128 + main.signal.SIGTERM


def test_sigterm_mid_check_still_releases_the_slot(monkeypatch):
    def run_check(testfile, env):
        main.exit_on_sigterm(main.signal.SIGTERM, None)
    monkeypatch.setattr(main, "run_check", run_check)
    db = FakeDb()
    with pytest.raises(SystemExit):
        main.run_test("example.py", 100, db=db, sleep=None)
    assert db.calls[-1] == ("release", 7)


def test_single_run_returns_exit_code(monkeypatch):
    fake_check(monkeypatch, (1, SITE_DOWN_OUTPUT))
    assert main.run_test("example.py", 0, db=FakeDb(), sleep=None) == 1


def test_cli_flags_still_parse():
    args = main.parse_args(["--fil", "example.py", "--fol", "/app/", "-i", "100", "--fr", "true"])
    assert (args.testfile, args.check_folder, args.interval, args.first_run) == ("example.py", "/app/", 100, "true")


def test_run_check_decodes_output(tmp_path):
    check = tmp_path / "test_marker.py"
    check.write_text("def test_x():\n    raise Exception('connect_Failure')\n")
    code, output = main.run_check(str(check), dict(os.environ))
    assert code == 1
    assert isinstance(output, str) and main.is_connect_failure(output)
