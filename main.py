import os
import re
import signal
import subprocess
import sys
import time
import logging
from logging.handlers import RotatingFileHandler
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from argparse import ArgumentParser

from src import config, database

# Every check connects to the hub's /connect (PW_WS_ENDPOINT). The hub picks a free slot for the browser
# the check launches, holds the check while every such slot is busy, fails over to another slot and counts
# slot failures. So this only runs the check, and says why one that got no browser didn't.
# Checks raise Exception(HUB_FAILURE_MARKER) when the hub refused the connect or could not be reached,
# and Exception(CONNECT_FAILURE_MARKER) when it failed otherwise; either way the site wasn't checked.
CONNECT_FAILURE_MARKER = "connect_Failure"
HUB_FAILURE_MARKER = "hub_Failure"
CHECK_TIMEOUT = int(os.environ.get("CHECK_TIMEOUT_S", "1800"))

# Playwright reports a refused handshake as "BrowserType.connect: WebSocket error: <url without its query>
# <status> <reason>", with the hub's own message on the next line (which pytest starts with "E").
HUB_ANSWER = re.compile(r"browserType\.connect: WebSocket error: \S+ (\d{3}) [^\n]*\n(?:E +)?([^\n]*)", re.I)
UNREACHABLE = re.compile(r"browserType\.connect: WebSocket error: [^\n]*?\b(ECONNREFUSED|ENOTFOUND|EAI_AGAIN|"
                         r"EHOSTUNREACH|ENETUNREACH|ETIMEDOUT|ECONNRESET)\b", re.I)
CONNECT_TIMEOUT = re.compile(r"browserType\.connect: Timeout (\d+)ms exceeded", re.I)
# What to do about the hub's answer: (status, pattern in its message or None, hint). The first match wins.
HINTS = [
    (400, re.compile(r"^node "), "check GATEWAY_NODE"),
    (401, None, "check GATEWAY_CLIENT_TOKEN: it is missing, wrong, revoked or expired"),
    (404, None, "check GATEWAY_NODE: the hub knows no node or static host of that name"),
    (410, None, "the check builds a /connect/<node>/<slot> address; make it connect to PW_WS_ENDPOINT"),
    (428, None, "the check's Playwright differs from the nodes' (see Upgrading Playwright in the README)"),
    (503, re.compile(r"^no free "), "every slot that fits stayed busy or failing for the hub's "
                                    "GATEWAY_QUEUE_TIMEOUT_S; add slots or spread the checks out"),
    (503, re.compile(r"queue is full"), "more checks are waiting than the hub's GATEWAY_QUEUE_MAX"),
    (503, re.compile(r"does not offer|no node offers"), "install the browser on a node and add it to "
                                                        "that node's BROWSERS (--browsers, -Browsers)"),
    (503, re.compile(r"database"), "the hub can't reach MySQL"),
    (502, None, "no slot the hub tried could open a browser; see the hub's log and the Grid UI"),
    (504, None, "the slots the hub tried did not answer in time; see the hub's log and the Grid UI"),
]

checklog = logging.getLogger('checklog')
checklog.setLevel(logging.DEBUG)
errorlog = logging.getLogger('errorlog')
errorlog.setLevel(logging.DEBUG)
checklog_formatter = logging.Formatter('[%(asctime)s] %(levelname)-8s %(message)s')
errorlog_formatter = logging.Formatter('%(message)s')


def is_connect_failure(output):
    return CONNECT_FAILURE_MARKER in output


def is_hub_failure(output):
    return HUB_FAILURE_MARKER in output


def explain(output):
    """ why the check's connect failed, from the Playwright error in its output, or None """
    answer = HUB_ANSWER.search(output)
    if answer:
        status, message = int(answer.group(1)), answer.group(2).strip()
        text = "the hub answered {}{}".format(status, ": " + message if message else "")
        for code, pattern, hint in HINTS:
            if code == status and (pattern is None or pattern.search(message)):
                return "{} ({})".format(text, hint)
        return text
    unreachable = UNREACHABLE.search(output)
    if unreachable:
        return "the hub could not be reached ({}); check GATEWAY_URL and that the hub is running".format(
            unreachable.group(1).upper())
    timeout = CONNECT_TIMEOUT.search(output)
    if timeout:
        return "no browser within the check's {} s connect timeout".format(int(timeout.group(1)) // 1000)
    return None


def redact(endpoint):
    """ the endpoint without its client token """
    parts = urlsplit(endpoint)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "gateway_token"]
    return urlunsplit(parts._replace(query=urlencode(query, quote_via=quote, safe="")))


def check_env(endpoint, base=None):
    env = dict(os.environ if base is None else base)
    env["PW_WS_ENDPOINT"] = endpoint
    return env


def run_check(testfile, env):
    try:
        proc = subprocess.run([sys.executable, '-m', 'pytest', testfile], env=env, capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=CHECK_TIMEOUT)
        return proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as e:
        output = ''.join(s.decode('utf-8', 'replace') if isinstance(s, bytes) else s
                         for s in (e.stdout, e.stderr) if s)
        return -1, output + '\ncheck timed out after {} seconds'.format(CHECK_TIMEOUT)


def record_result(testfile, return_code, output, duration):
    name = os.path.basename(testfile)
    if return_code == 0:
        checklog.info('%s passed in %.1fs', name, duration)
        checklog.info(output)
        return
    reason = explain(output)
    if reason or is_hub_failure(output) or is_connect_failure(output):
        checklog.error('%s got no browser after %.1fs, so the site was not checked: %s', name, duration,
                       reason or 'the connect failed, see error_info.log')
    else:
        checklog.error('%s failed in %.1fs with exit code %s', name, duration, return_code)
    errorlog.error(output)


def datest(testfile, endpoint):
    checklog.info('running %s through %s', os.path.basename(testfile), redact(endpoint))
    start = time.monotonic()
    return_code, output = run_check(testfile, check_env(endpoint))
    record_result(testfile, return_code, output, time.monotonic() - start)
    return return_code


def prepare_db(db, initialised):
    """ create the tables and sync hosts: once, then free stale claims. Returns whether the tables are
    done. The hub routes the checks, so a database problem here is logged and the check runs anyway. """
    try:
        if not initialised:
            db.initial_db_start()
            initialised = True
        db.reclaim_stale_claims()
    except Exception:
        checklog.exception('could not create or seed the tables or free stale claims, retrying next run')
    return initialised


def run_test(testfile, endpoint, interval=None, db=None, sleep=time.sleep):
    db = db or database.database()
    initialised = False
    while True:
        try:
            initialised = prepare_db(db, initialised)
            return_code = datest(testfile, endpoint)
            if not interval:
                return return_code
        except Exception:
            if not interval:
                raise
            checklog.exception('check run failed')
        checklog.info('Sleeping for {0} seconds.'.format(str(interval)))
        sleep(interval)


def setup_logging(log_dir):
    os.makedirs(log_dir, exist_ok=True)

    check_stream_handler = logging.StreamHandler()
    check_stream_handler.setLevel(logging.DEBUG)
    check_stream_handler.setFormatter(checklog_formatter)
    checklog.addHandler(check_stream_handler)

    check_log_handler = RotatingFileHandler(os.path.join(log_dir, 'check_info.log'), maxBytes=10 << 20, backupCount=5)
    check_log_handler.setLevel(logging.DEBUG)
    check_log_handler.setFormatter(checklog_formatter)
    checklog.addHandler(check_log_handler)

    error_log_handler = RotatingFileHandler(os.path.join(log_dir, 'error_info.log'), maxBytes=10 << 20, backupCount=5)
    error_log_handler.setLevel(logging.DEBUG)
    error_log_handler.setFormatter(errorlog_formatter)
    errorlog.addHandler(error_log_handler)


def parse_args(argv=None):
    parser = ArgumentParser()
    parser.add_argument("--fil",
                        dest="testfile",
                        metavar="FILE",
                        required=True,
                        help="test file to run")
    parser.add_argument("-i",
                        dest="interval",
                        metavar="INTERVAL",
                        default=0,
                        type=int,
                        required=False,
                        help="run test file repeatedly at interval in seconds")
    parser.add_argument("--fol",
                        dest="check_folder",
                        metavar="check_folder",
                        required=True)
    parser.add_argument("--fr",
                        dest="first_run",
                        metavar="first_run",
                        required=False)
    return parser.parse_args(argv)


def exit_on_sigterm(signum, frame):
    # SystemExit unwinds the stack: subprocess.run kills the running check, its browser connection closes
    # and the hub frees the slot. Without a handler, python as PID 1 in a container ignores SIGTERM and
    # is SIGKILLed.
    raise SystemExit(128 + signum)


def main(argv=None):
    args = parse_args(argv)
    signal.signal(signal.SIGTERM, exit_on_sigterm)
    cfg = config.config()

    if args.first_run:
        database.database(cfg).initial_db_start()
        return 0

    setup_logging(os.path.join(args.check_folder, 'logs'))
    checklog.debug({'args': vars(args)})

    gateway = cfg.get_gateway_config()
    try:
        endpoint = config.client_endpoint(**gateway)
    except ValueError as e:
        checklog.error('check not run: %s', e)
        return 1
    if not gateway["client_token"]:
        checklog.warning('GATEWAY_CLIENT_TOKEN is not set: the hub refuses the check unless it accepts '
                         'clients without a token')

    testfile = os.path.join(args.check_folder, args.testfile)
    return 0 if run_test(testfile, endpoint, args.interval, db=database.database(cfg)) == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
