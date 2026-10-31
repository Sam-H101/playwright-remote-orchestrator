import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402

ENDPOINT = "ws://hub:4500/connect?gateway_token=s3cret&node=runner-a"

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


def hub_answer(status, reason, message):
    # How pytest prints the Playwright error for a refused handshake (Playwright 1.64).
    return """
E           playwright._impl._errors.Error: BrowserType.connect: WebSocket error: ws://hub:4500/connect {0} {1}
E           {2}
E
E           Call log:
E             - <ws connecting> ws://hub:4500/connect
E             - <ws unexpected response> ws://hub:4500/connect {0} {1}
E           {2}
""".format(status, reason, message) + HUB_FAILURE_OUTPUT


class FakeDb:
    def __init__(self, fail_init=False, fail_reclaim=False):
        self.fail_init = fail_init
        self.fail_reclaim = fail_reclaim
        self.calls = []

    def initial_db_start(self):
        self.calls.append("init")
        if self.fail_init:
            raise RuntimeError("db down")

    def reclaim_stale_claims(self):
        self.calls.append("reclaim")
        if self.fail_reclaim:
            raise RuntimeError("db down")
        return 0


def fake_check(monkeypatch, result):
    seen = {}

    def run_check(testfile, env):
        seen["env"] = env
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(main, "run_check", run_check)
    return seen


@pytest.fixture
def logs(caplog):
    with caplog.at_level(logging.DEBUG, logger="checklog"):
        yield caplog


def test_markers_are_module_constants():
    assert main.CONNECT_FAILURE_MARKER == "connect_Failure"
    assert main.is_connect_failure(CONNECT_FAILURE_OUTPUT)
    assert not main.is_connect_failure(SITE_DOWN_OUTPUT)
    assert main.HUB_FAILURE_MARKER == "hub_Failure"
    assert main.is_hub_failure(HUB_FAILURE_OUTPUT)
    assert not main.is_connect_failure(HUB_FAILURE_OUTPUT)


def test_check_env_sets_only_the_endpoint():
    env = main.check_env(ENDPOINT, {"PATH": "x"})
    assert env == {"PATH": "x", "PW_WS_ENDPOINT": ENDPOINT}


def test_check_env_does_not_mutate_os_environ():
    main.check_env(ENDPOINT)
    assert os.environ.get("PW_WS_ENDPOINT") != ENDPOINT


@pytest.mark.parametrize("endpoint, shown", [
    (ENDPOINT, "ws://hub:4500/connect?node=runner-a"),
    ("ws://hub:4500/connect?gateway_token=s3cr%26t", "ws://hub:4500/connect"),
    ("wss://grid.example.com/connect", "wss://grid.example.com/connect"),
])
def test_redact_hides_only_the_token(endpoint, shown):
    assert main.redact(endpoint) == shown


@pytest.mark.parametrize("status, reason, message, expected", [
    (401, "Unauthorized", "missing or bad gateway token",
     "the hub answered 401: missing or bad gateway token (check GATEWAY_CLIENT_TOKEN"),
    (401, "Unauthorized", "gateway token is revoked", "gateway token is revoked (check GATEWAY_CLIENT_TOKEN"),
    (404, "Not Found", "no node or static host named zz is known to the hub", "(check GATEWAY_NODE: "),
    (400, "Bad Request", "node must be 1-128 characters of A-Z a-z 0-9 . _ -", "(check GATEWAY_NODE)"),
    (410, "Gone", "the /connect/<node>/<slot> endpoints were removed: connect to /connect", "PW_WS_ENDPOINT"),
    (428, "Precondition Required", "Playwright version mismatch: the client is 1.62, the nodes offering "
     "chromium run 1.64.", "Upgrading Playwright"),
    (503, "Service Unavailable", "no free chromium slot within 60s: 4 matching slots, all busy",
     "within 60s: 4 matching slots, all busy (every slot that fits stayed busy or failing for the hub's "
     "GATEWAY_QUEUE_TIMEOUT_S"),
    (503, "Service Unavailable", "the hub's queue is full (200 clients waiting for a slot); try again later",
     "GATEWAY_QUEUE_MAX"),
    (503, "Service Unavailable", "no node offers firefox (offered: chromium)", "BROWSERS"),
    (503, "Service Unavailable", "node runner-a does not offer webkit (offered: chromium)", "BROWSERS"),
    (503, "Service Unavailable", "the hub cannot reach its database to pick a slot right now; try again later",
     "(the hub can't reach MySQL)"),
    (503, "Service Unavailable", "hub is shutting down", "the hub answered 503: hub is shutting down"),
    (502, "Bad Gateway", "node runner-a slot 2: run-server is not running (tried 3 slots)",
     "no slot the hub tried could open a browser"),
    (504, "Gateway Timeout", "node runner-a slot 2 did not answer within 15s", "did not answer in time"),
    # GATEWAY_QUEUE_TIMEOUT_S=0 leaves out "within"; ?node= adds "on node"
    (503, "Service Unavailable", "no free firefox slot on node runner-a: 2 matching slots, all busy",
     "GATEWAY_QUEUE_TIMEOUT_S"),
    (503, "Service Unavailable", "the hub cannot check gateway tokens right now (database unavailable); try "
     "again later", "(the hub can't reach MySQL)"),
])
def test_explain_names_the_hubs_answer_and_what_to_do(status, reason, message, expected):
    text = main.explain(hub_answer(status, reason, message))
    assert text.startswith("the hub answered {}: {}".format(status, message))
    assert expected in text


def test_explain_unknown_status_keeps_the_hubs_message():
    assert main.explain(hub_answer(418, "I'm a Teapot", "short and stout")) == "the hub answered 418: short and stout"


def test_explain_unreachable_hub_and_timeout():
    unreachable = "E   playwright._impl._errors.Error: BrowserType.connect: WebSocket error: connect ECONNREFUSED 10.0.0.5:4500"
    assert main.explain(unreachable) == ("the hub could not be reached (ECONNREFUSED); check GATEWAY_URL and "
                                         "that the hub is running")
    dns = "browserType.connect: WebSocket error: getaddrinfo ENOTFOUND hub"
    assert "(ENOTFOUND)" in main.explain(dns)
    timeout = "E   playwright._impl._errors.TimeoutError: BrowserType.connect: Timeout 180000ms exceeded."
    assert main.explain(timeout) == "no browser within the check's 180 s connect timeout"


def test_explain_ignores_site_failures():
    assert main.explain(SITE_DOWN_OUTPUT) is None
    # a WebSocket error from the page under test is not the connect
    assert main.explain("E   Error: WebSocket error: wss://site/ws 502 Bad Gateway\nE   nope") is None


def test_success_is_logged_with_its_duration(monkeypatch, logs):
    seen = fake_check(monkeypatch, (0, "1 passed"))
    assert main.datest("/app/example.py", ENDPOINT) == 0
    assert seen["env"]["PW_WS_ENDPOINT"] == ENDPOINT
    assert "running example.py through ws://hub:4500/connect?node=runner-a" in logs.text
    assert "example.py passed in " in logs.text
    assert "s3cret" not in logs.text


def test_site_failure_is_logged_as_a_failed_check(monkeypatch, logs):
    fake_check(monkeypatch, (1, SITE_DOWN_OUTPUT))
    assert main.datest("example.py", ENDPOINT) == 1
    assert "example.py failed in " in logs.text and "with exit code 1" in logs.text
    assert "got no browser" not in logs.text


def test_hub_refusal_is_explained(monkeypatch, logs):
    fake_check(monkeypatch, (1, hub_answer(503, "Service Unavailable",
                                           "no free chromium slot within 60s: 4 matching slots, all busy")))
    assert main.datest("example.py", ENDPOINT) == 1
    error = [r.getMessage() for r in logs.records if r.name == "checklog" and r.levelname == "ERROR"]
    assert len(error) == 1
    assert error[0].startswith("example.py got no browser after ")
    assert "the site was not checked: the hub answered 503: no free chromium slot within 60s" in error[0]


@pytest.mark.parametrize("output", [CONNECT_FAILURE_OUTPUT, HUB_FAILURE_OUTPUT])
def test_marker_without_a_readable_error_still_says_no_browser(monkeypatch, logs, output):
    fake_check(monkeypatch, (1, output))
    assert main.datest("example.py", ENDPOINT) == 1
    assert "got no browser" in logs.text and "the connect failed, see error_info.log" in logs.text


def test_check_error_propagates(monkeypatch):
    fake_check(monkeypatch, OSError("no pytest"))
    with pytest.raises(OSError):
        main.datest("example.py", ENDPOINT)


class StopLoop(BaseException):
    pass


def test_each_run_frees_stale_claims_and_seeds_once(monkeypatch):
    fake_check(monkeypatch, (0, "ok"))
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise StopLoop()
    db = FakeDb()
    with pytest.raises(StopLoop):
        main.run_test("example.py", ENDPOINT, 100, db=db, sleep=sleep)
    assert db.calls == ["init", "reclaim", "reclaim", "reclaim"]


def test_database_problems_do_not_stop_the_check(monkeypatch, logs):
    seen = fake_check(monkeypatch, (0, "ok"))
    db = FakeDb(fail_init=True)
    assert main.run_test("example.py", ENDPOINT, 0, db=db, sleep=None) == 0
    assert seen["env"]["PW_WS_ENDPOINT"] == ENDPOINT
    assert db.calls == ["init"]  # no sweep without the tables
    assert "retrying next run" in logs.text
    db = FakeDb(fail_reclaim=True)
    assert main.run_test("example.py", ENDPOINT, 0, db=db, sleep=None) == 0
    assert db.calls == ["init", "reclaim"]


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
        main.run_test("example.py", ENDPOINT, 100, db=db, sleep=sleep)
    assert sleeps == [100, 100, 100]
    # the schema step is retried until it works
    assert db.calls.count("init") == 3


def _next(results):
    result = next(results)
    if isinstance(result, Exception):
        raise result
    return result


def test_single_run_returns_exit_code(monkeypatch):
    fake_check(monkeypatch, (1, SITE_DOWN_OUTPUT))
    assert main.run_test("example.py", ENDPOINT, 0, db=FakeDb(), sleep=None) == 1


def test_sigterm_handler_raises_system_exit():
    with pytest.raises(SystemExit) as exit:
        main.exit_on_sigterm(main.signal.SIGTERM, None)
    assert exit.value.code == 128 + main.signal.SIGTERM


def run_main(monkeypatch, tmp_path, env, yaml_text="hosts: []\n"):
    """ main.main with the check and the database faked; returns (exit code, the check's env, check_info.log) """
    config_path = tmp_path / "database.yaml"
    config_path.write_text(yaml_text)
    monkeypatch.setenv("ORCHESTRATOR_CONFIG", str(config_path))
    for name in ("GATEWAY_URL", "GATEWAY_CLIENT_TOKEN", "GATEWAY_NODE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    seen = fake_check(monkeypatch, (0, "1 passed"))
    monkeypatch.setattr(main.database, "database", lambda cfg=None: FakeDb())
    installed = []
    monkeypatch.setattr(main.signal, "signal", lambda *args: installed.append(args))
    handlers = list(main.checklog.handlers), list(main.errorlog.handlers)
    try:
        code = main.main(["--fil", "example.py", "--fol", str(tmp_path)])
    finally:
        for logger, before in zip((main.checklog, main.errorlog), handlers):
            for handler in logger.handlers[len(before):]:
                handler.close()
            logger.handlers[:] = before
    assert installed == [(main.signal.SIGTERM, main.exit_on_sigterm)]
    return code, seen.get("env"), (tmp_path / "logs" / "check_info.log").read_text()


def test_main_passes_the_hubs_connect_url(monkeypatch, tmp_path):
    code, env, log = run_main(monkeypatch, tmp_path, {"GATEWAY_URL": "ws://10.0.0.5:4500/",
                                                      "GATEWAY_CLIENT_TOKEN": "pwgc_s3cr&t"})
    assert code == 0
    assert env["PW_WS_ENDPOINT"] == "ws://10.0.0.5:4500/connect?gateway_token=pwgc_s3cr%26t"
    assert "running example.py through ws://10.0.0.5:4500/connect\n" in log
    assert "s3cr" not in log


def test_main_adds_the_configured_node(monkeypatch, tmp_path):
    code, env, _ = run_main(monkeypatch, tmp_path, {"GATEWAY_CLIENT_TOKEN": "t", "GATEWAY_NODE": "Runner-A"})
    assert env["PW_WS_ENDPOINT"] == "ws://hub:4500/connect?gateway_token=t&node=Runner-A"


def test_main_reads_the_gateway_section(monkeypatch, tmp_path):
    code, env, _ = run_main(monkeypatch, tmp_path, {}, "gateway:\n  url: wss://grid.lab\n  node: win-01\n")
    assert env["PW_WS_ENDPOINT"] == "wss://grid.lab/connect?node=win-01"


def test_main_warns_without_a_client_token(monkeypatch, tmp_path):
    code, env, log = run_main(monkeypatch, tmp_path, {})
    assert env["PW_WS_ENDPOINT"] == "ws://hub:4500/connect"
    assert "GATEWAY_CLIENT_TOKEN is not set" in log


def test_main_refuses_a_bad_node_name(monkeypatch, tmp_path):
    code, env, log = run_main(monkeypatch, tmp_path, {"GATEWAY_NODE": "runner a/3"})
    assert code == 1 and env is None
    assert "check not run: GATEWAY_NODE must be a node name" in log


def test_main_refuses_an_old_slot_endpoint_as_gateway_url(monkeypatch, tmp_path):
    code, env, log = run_main(monkeypatch, tmp_path, {"GATEWAY_URL": "ws://192.168.112.184:4500/connect/runner-7f3a9c/3",
                                                      "GATEWAY_CLIENT_TOKEN": "s3cret"})
    assert code == 1 and env is None
    assert "check not run: GATEWAY_URL must be the hub's address" in log and "set GATEWAY_NODE" in log
    assert "s3cret" not in log


def test_main_exit_code_follows_the_check(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "run_test", lambda testfile, endpoint, interval, db: 2)
    code, _, _ = run_main(monkeypatch, tmp_path, {})
    assert code == 1


def test_first_run_only_seeds(monkeypatch, tmp_path):
    config_path = tmp_path / "database.yaml"
    config_path.write_text("hosts: []\n")
    monkeypatch.setenv("ORCHESTRATOR_CONFIG", str(config_path))
    made = []
    monkeypatch.setattr(main.database, "database", lambda cfg=None: made.append(FakeDb()) or made[-1])
    monkeypatch.setattr(main.signal, "signal", lambda *args: None)
    monkeypatch.setattr(main, "run_check", lambda *args: pytest.fail("no check on --fr"))
    assert main.main(["--fil", "example.py", "--fol", str(tmp_path), "--fr", "true"]) == 0
    assert made[0].calls == ["init"]


def test_cli_flags_still_parse():
    args = main.parse_args(["--fil", "example.py", "--fol", "/app/", "-i", "100", "--fr", "true"])
    assert (args.testfile, args.check_folder, args.interval, args.first_run) == ("example.py", "/app/", 100, "true")


def test_run_check_decodes_output(tmp_path):
    check = tmp_path / "test_marker.py"
    check.write_text("def test_x():\n    raise Exception('connect_Failure')\n")
    code, output = main.run_check(str(check), dict(os.environ))
    assert code == 1
    assert isinstance(output, str) and main.is_connect_failure(output)
