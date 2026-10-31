#!/usr/bin/env python3
"""Pause one pool account, open its real profile, and save a manual login locally."""
import argparse
import datetime
import fcntl
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
SITES = {
    'doubao': ('https://www.doubao.com/chat/', ['doubao.com']),
    'deepseek': ('https://chat.deepseek.com/', ['deepseek.com']),
    'kimi': ('https://www.kimi.com/', ['kimi.com', 'moonshot.cn']),
    'yuanbao': ('https://yuanbao.tencent.com/chat', ['yuanbao.tencent.com']),
    'qianwen': ('https://www.qianwen.com/', ['qianwen.com', 'aliyun.com']),
    'yiyan': ('https://yiyan.baidu.com/', ['baidu.com']),
}


def service_environment():
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():
            continue
        try:
            if (p / 'comm').read_text(encoding='utf-8').strip() != 'geo-server':
                continue
            if str(ROOT) not in os.readlink(p / 'exe'):
                continue
            wanted = {b'GEO_ADMIN_USER', b'GEO_ADMIN_PASSWORD', b'DISPLAY', b'XAUTHORITY'}
            return {k.decode('utf-8'): v.decode('utf-8') for field in (p / 'environ').read_bytes().split(b'\0')
                    for k, sep, v in [field.partition(b'=')] if sep and k in wanted}
        except OSError:
            continue
    raise RuntimeError('GEO service is not running')


def admin_call(session, base, method, path, payload=None):
    response = session.request(method, base + path, json=payload, timeout=60)
    if response.status_code == 401 and path != '/api/admin/login':
        # An operator can leave the chooser or browser open across a service
        # restart. Refresh only an explicitly rejected request: a network error
        # or another status must never repeat a possibly completed operation.
        configured = service_environment()
        admin_call(session, base, 'POST', '/api/admin/login', {
            'user': configured.get('GEO_ADMIN_USER', 'admin'),
            'password': configured['GEO_ADMIN_PASSWORD'],
        })
        response = session.request(method, base + path, json=payload, timeout=60)
    if not response.ok:
        try:
            detail = response.json().get('detail', 'Admin operation failed')
        except ValueError:
            detail = 'Admin operation failed'
        raise RuntimeError(str(detail))
    response.raise_for_status()
    return response.json()


def merge_snapshot(old, state, domains):
    def owned(host):
        host = host.lstrip('.').split(':')[0]
        return any(host == d or host.endswith('.' + d) for d in domains)
    cookies = {(c['domain'], c['path'], c['name']): c for c in old.get('cookies', []) if not owned(c['domain'])}
    for c in state.get('cookies', []):
        if not owned(c['domain']):
            continue
        snap = {k: c[k] for k in ['name', 'value', 'domain', 'path', 'expires', 'secure']}
        snap.update(http_only=c.get('httpOnly', False), same_site=c.get('sameSite', ''), session=c.get('expires', -1) <= 0)
        cookies[(c['domain'], c['path'], c['name'])] = snap
    storage = {origin: pairs for origin, pairs in old.get('local_storage', {}).items()
               if not owned(origin.split('://')[-1].split('/')[0])}
    for origin in state.get('origins', []):
        if owned(origin['origin'].split('://')[-1].split('/')[0]):
            storage[origin['origin']] = [[item['name'], item['value']] for item in origin.get('localStorage', [])]
    return {'saved_at': datetime.datetime.now().astimezone().isoformat(), 'cookies': list(cookies.values()), 'local_storage': storage}


def write_private(path, value):
    fd, name = tempfile.mkstemp(prefix='.manual-login-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account')
    parser.add_argument('--site', choices=SITES, default='doubao')
    parser.add_argument('--self-check', action='store_true')
    args = parser.parse_args()
    configured = service_environment()
    session = requests.Session()
    session.trust_env = False
    base = 'http://127.0.0.1:8080'

    def call(method, path, payload=None):
        return admin_call(session, base, method, path, payload)

    call('POST', '/api/admin/login', {'user': configured.get('GEO_ADMIN_USER', 'admin'), 'password': configured['GEO_ADMIN_PASSWORD']})
    accounts = [a for a in call('GET', '/api/admin/accounts')['accounts'] if a.get('enabled')]
    if args.self_check:
        print(json.dumps({'admin_api': 'ok', 'enabled_accounts': len(accounts), 'display': configured.get('DISPLAY'), 'chrome_available': Path('/usr/bin/google-chrome').exists()}))
        return
    if args.account:
        account = next(a for a in accounts if a['id'] == args.account)
    else:
        print('Select an account. This pauses ALL automated sites for that account during manual use.')
        for i, a in enumerate(accounts, 1):
            print(f"{i}. {a['id']}  phone: ****{str(a.get('phone', ''))[-4:]}")
        choice = int(input('Account number (Ctrl+C to cancel): '))
        if not 1 <= choice <= len(accounts):
            raise ValueError('Invalid account number')
        account = accounts[choice - 1]
    aid = account['id']
    path = '/api/admin/accounts/' + aid
    lock = Path('/run/geo-manual-login.lock').open('a', encoding='utf-8')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # The API rejects disabling an account with an active job; never kill it.
    call('PATCH', path, {'enabled': False})
    print(f'{aid}: automation paused. Complete login/verification in the Chrome window.')
    print('Keep Chrome OPEN. Return here and press Enter to save and resume.')
    profile = ROOT / '.chrome-cn-ai-profiles' / aid
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    port_file = profile / 'DevToolsActivePort'
    port_file.unlink(missing_ok=True)
    env = dict(os.environ)
    # The operator desktop is separate from the automation display, so a
    # customer job cannot take keyboard focus while a person is logging in.
    if not env.get('DISPLAY'):
        env.update({k: v for k, v in configured.items() if k in ['DISPLAY', 'XAUTHORITY']})
    process = None
    try:
        log_path = profile / 'manual-browser.log'
        with log_path.open('a', encoding='utf-8') as log:
            log_path.chmod(0o600)
            process = subprocess.Popen(['/usr/bin/google-chrome', '--no-sandbox', '--disable-dev-shm-usage',
                                        '--no-first-run', '--no-default-browser-check', '--start-maximized',
                                        '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
                                        '--user-data-dir=' + str(profile), '--new-window', SITES[args.site][0]],
                                       env=env, stdout=log, stderr=log)
            deadline = time.monotonic() + 30
            while not port_file.exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('Manual Chrome did not start')
                time.sleep(.2)
            input('After login/verification is complete, press Enter here to SAVE: ')
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                port = port_file.read_text(encoding='utf-8').splitlines()[0]
                browser = pw.chromium.connect_over_cdp('http://127.0.0.1:' + port)
                state = browser.contexts[0].storage_state()
                store = profile / '.session-store.json'
                old = json.loads(store.read_text(encoding='utf-8')) if store.exists() else {}
                if store.exists():
                    backup = profile / ('.session-store.before-manual-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '.json')
                    write_private(backup, old)
                updated = merge_snapshot(old, state, SITES[args.site][1])
                write_private(store, updated)
                print('Session saved locally (0600); cookie values are never printed.')
                try:
                    browser.new_browser_cdp_session().send('Browser.close')
                except Exception:
                    pass
            process.wait(timeout=20)
    finally:
        if process is None or process.poll() is not None:
            call('PATCH', path, {'enabled': True})
            checked = call('POST', path + '/sites/' + args.site + '/check', {})
            print(json.dumps({'account': aid, 'automation_resumed': True, 'site': args.site, 'login_status': checked.get('status')}, ensure_ascii=False))
        else:
            print(f'{aid} remains PAUSED because its manual Chrome is still open. Finish/save before resuming automation.')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nManual login cancelled.')
    except Exception as error:
        print('Manual login stopped:', str(error))
        if os.isatty(0):
            input('Press Enter to close this terminal. Reopen the desktop login menu to retry.')
        raise SystemExit(1)
