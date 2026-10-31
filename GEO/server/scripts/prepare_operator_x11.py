#!/usr/bin/env python3
"""Create a private X11 authorization file for the operator desktop."""
import os
import secrets
import subprocess
from pathlib import Path

path = Path('/run/geo-operator/Xauthority')
path.touch(mode=0o600, exist_ok=True)
path.chmod(0o600)
subprocess.run(['/usr/bin/xauth', '-f', str(path)],
               input='add :0 . ' + secrets.token_hex(16) + '\n', text=True, check=True,
               stdout=subprocess.DEVNULL)
