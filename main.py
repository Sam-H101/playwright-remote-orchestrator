import os
import time
import logging
from logging.handlers import RotatingFileHandler

from argparse import ArgumentParser

from src import database
from subprocess import Popen, PIPE
checklog = logging.getLogger('checklog')
checklog.setLevel(logging.DEBUG)
errorlog = logging.getLogger('errorlog')
errorlog.setLevel(logging.DEBUG)
checklog_formatter = logging.Formatter('[%(asctime)s] %(levelname)-8s %(message)s')
errorlog_formatter = logging.Formatter('%(message)s')


def datest(testfile):
    print("getting a port")
    db = database.database()
    db.initial_db_start()
    port = db.get_a_port()

    os.environ["port"] = "{}".format(port[0])
    os.environ["host"] = "{}".format(port[2])
    print("running test on port: ", port)
    proc = Popen(['pytest', testfile], stdout=PIPE, stderr=PIPE)

    z_stdout, z_stderr = proc.communicate()
    return_code = proc.returncode
    print(z_stdout, z_stderr)
    if return_code == 0:
        db.release_port(port)
        checklog.info(z_stdout)
    else:
        db.increment_failure(port)
        db.release_port(port)
        checklog.info(z_stderr)

def run_test(testfile, interval=None):
    if interval:
        while True:
            datest(testfile)
            checklog.info('Sleeping for {0} seconds.'.format(str(interval)))
            time.sleep(interval)
    else:
        datest(testfile)


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


args = parser.parse_args()

if args.first_run:
    db = database.database()
    db.initial_db_start()
    exit(0)


checklog.debug({'args': vars(args)})

check_stream_handler = logging.StreamHandler()
check_stream_handler.setLevel(logging.DEBUG)
check_stream_handler.setFormatter(checklog_formatter)
checklog.addHandler(check_stream_handler)

check_log_handler = RotatingFileHandler('{}/check_info.log'.format(args.check_folder + 'logs'))
check_log_handler.setLevel(logging.DEBUG)
check_log_handler.setFormatter(checklog_formatter)
checklog.addHandler(check_log_handler)

error_log_handler = RotatingFileHandler('{}/error_info.log'.format(args.check_folder + 'logs'))
error_log_handler.setLevel(logging.DEBUG)
error_log_handler.setFormatter(errorlog_formatter)
errorlog.addHandler(error_log_handler)


testfile = args.check_folder + args.testfile
print("running test on ", testfile)
run_test(testfile, args.interval)
