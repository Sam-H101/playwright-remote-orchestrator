import os
import signal
import subprocess
import sys
import time
import logging
from logging.handlers import RotatingFileHandler

from argparse import ArgumentParser

from src import database

# Checks raise Exception(CONNECT_FAILURE_MARKER) when they cannot reach the Playwright server.
# Only those failures count against the slot; any other failure is the site under test.
# failCount holds consecutive connect failures: any run that reached the server resets it.
CONNECT_FAILURE_MARKER = "connect_Failure"
# Checks raise Exception(HUB_FAILURE_MARKER) when the hub itself refused them (bad token) or
# could not be reached. That is not the slot's fault, so its failCount is left alone.
HUB_FAILURE_MARKER = "hub_Failure"
NO_SLOT_MAX_WAIT = 60
# A single run (no -i) gives up after waiting this long for a free slot.
NO_SLOT_TIMEOUT = int(os.environ.get("NO_SLOT_TIMEOUT_S", "300"))
CHECK_TIMEOUT = int(os.environ.get("CHECK_TIMEOUT_S", "1800"))

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


def redact(endpoint):
    return endpoint.split("?", 1)[0]


def check_env(slot, base=None):
    env = dict(os.environ if base is None else base)
    env.update(PW_WS_ENDPOINT=slot.endpoint, host=str(slot[2]), port=str(slot[0]))
    return env


def acquire_slot(db, sleep=time.sleep, max_wait=NO_SLOT_MAX_WAIT, timeout=None):
    """ claim a free slot, waiting with backoff; give up after `timeout` seconds if it is set """
    delay, waited = 1, 0
    while True:
        try:
            return db.get_a_port()
        except database.NoFreeSlot as e:
            if timeout is not None:
                if waited >= timeout:
                    raise database.NoFreeSlot('{}, gave up after {} seconds'.format(e, waited)) from e
                delay = min(delay, timeout - waited)
            checklog.warning('%s, retrying in %s seconds', e, delay)
            sleep(delay)
            waited += delay
            delay = min(delay * 2, max_wait)


def run_check(testfile, env):
    try:
        proc = subprocess.run([sys.executable, '-m', 'pytest', testfile], env=env, capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=CHECK_TIMEOUT)
        return proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as e:
        output = ''.join(s.decode('utf-8', 'replace') if isinstance(s, bytes) else s
                         for s in (e.stdout, e.stderr) if s)
        return -1, output + '\ncheck timed out after {} seconds'.format(CHECK_TIMEOUT)


def record_result(db, slot, return_code, output):
    if return_code == 0:
        checklog.info(output)
    elif is_hub_failure(output):
        checklog.error('the hub refused the check or could not be reached; check GATEWAY_URL and '
                       'GATEWAY_CLIENT_TOKEN. Not counted against %s slot %s', slot[2], slot[0])
        errorlog.error(output)
        return
    elif is_connect_failure(output):
        checklog.error('could not connect to %s slot %s, failure counted against the slot', slot[2], slot[0])
        errorlog.error(output)
        return db.increment_failure(slot)
    else:
        checklog.error('check failed with exit code %s', return_code)
        errorlog.error(output)
    # The check reached the Playwright server, so the slot works.
    db.reset_failure(slot)


def datest(db, testfile, slot_timeout=None):
    slot = acquire_slot(db, timeout=slot_timeout)
    try:
        checklog.info('running %s on %s slot %s (%s)', testfile, slot[2], slot[0], redact(slot.endpoint))
        return_code, output = run_check(testfile, check_env(slot))
        try:
            record_result(db, slot, return_code, output)
        except Exception:
            checklog.exception('could not update the failure count of %s slot %s', slot[2], slot[0])
        return return_code
    finally:
        # An error here must not replace the check's result (or its exception).
        try:
            db.release_port(slot)
        except Exception:
            checklog.exception('could not release %s slot %s; it is freed after %s seconds',
                               slot[2], slot[0], database.STALE_CLAIM_AFTER)


def run_test(testfile, interval=None, db=None, sleep=time.sleep):
    db = db or database.database()
    initialised = False
    while True:
        try:
            if not initialised:
                # The tables may already exist, so a failure here shouldn't stop the check.
                try:
                    db.initial_db_start()
                    initialised = True
                except Exception:
                    checklog.exception('could not create or seed the tables, retrying next run')
            return_code = datest(db, testfile, slot_timeout=None if interval else NO_SLOT_TIMEOUT)
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
    # SystemExit unwinds the stack: subprocess.run kills the running check and datest frees its slot.
    # Without a handler, python as PID 1 in a container ignores SIGTERM and is SIGKILLed.
    raise SystemExit(128 + signum)


def main(argv=None):
    args = parse_args(argv)
    signal.signal(signal.SIGTERM, exit_on_sigterm)

    if args.first_run:
        database.database().initial_db_start()
        return 0

    setup_logging(os.path.join(args.check_folder, 'logs'))
    checklog.debug({'args': vars(args)})

    testfile = os.path.join(args.check_folder, args.testfile)
    try:
        return 0 if run_test(testfile, args.interval) == 0 else 1
    except database.NoFreeSlot as e:
        checklog.error('check not run: %s', e)
        return 1


if __name__ == '__main__':
    sys.exit(main())
