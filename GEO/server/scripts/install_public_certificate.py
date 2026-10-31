#!/usr/bin/env python3
"""Certbot deploy hook: validate and atomically install GEO's public IP pair.

The running server reloads changed certificate files on TLS handshakes.
No business-service restart or session interruption is needed for renewals.
"""
import ipaddress
import os
import shutil
import ssl
import subprocess
from pathlib import Path


def install(lineage, destination, public_ip):
    ipaddress.ip_address(public_ip)
    certificate, private_key = lineage / 'fullchain.pem', lineage / 'privkey.pem'
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(certificate), str(private_key))
    details = ssl._ssl._test_decode_cert(str(certificate))
    ssl.match_hostname(details, public_ip)
    # Only a publicly trusted and currently valid pair may replace the fallback.
    result = subprocess.run(['openssl', 'verify', '-CApath', '/etc/ssl/certs',
                             '-untrusted', str(certificate), str(certificate)],
                            capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError('Certificate chain verification failed; current certificate kept')
    subprocess.run(['openssl', 'x509', '-in', str(certificate), '-checkend', '3600', '-noout'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    destination.mkdir(parents=True, exist_ok=True)
    for name, source, mode in [('key.pem', private_key, 0o600), ('cert.pem', certificate, 0o644)]:
        temporary = destination / ('.' + name + '.renewing')
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
            with os.fdopen(descriptor, 'wb') as target, source.open('rb') as original:
                shutil.copyfileobj(original, target)
                target.flush()
                os.fsync(target.fileno())
            temporary.chmod(mode)
            os.replace(temporary, destination / name)
        finally:
            temporary.unlink(missing_ok=True)
    print('Public HTTPS certificate installed; running server reloads it without restart.')


if __name__ == '__main__':
    lineage = Path(os.environ.get('RENEWED_LINEAGE', '/etc/letsencrypt/live/geo-public-ip'))
    destination = Path(os.environ.get('GEO_CERT_DESTINATION', '/root/workspace/GUIAgent/GEO/server/certs'))
    public_ip = os.environ.get('GEO_CERT_PUBLIC_IP', '127.0.0.1')
    install(lineage, destination, public_ip)
