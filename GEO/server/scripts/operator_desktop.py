#!/usr/bin/env python3
"""Run the isolated human login desktop, leaving automation's display alone."""
import os
import signal
import subprocess
import time

children = []


def stop(*_):
    for child in children:
        if child.poll() is None:
            child.terminate()
    raise SystemExit(0)


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
for command in [
    ['/usr/bin/openbox', '--sm-disable', '--config-file', '/etc/geo-operator/rc.xml'],
    ['/usr/local/awesun/awesun'],
]:
    children.append(subprocess.Popen(command))
while True:
    if children[0].poll() is not None:
        stop()
    time.sleep(2)
