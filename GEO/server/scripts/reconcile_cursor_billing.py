"""Annotate proven historical provider failures; preserve browser/resume facts."""
import argparse
import datetime as dt
import json
from pathlib import Path
import re
import shutil
import tempfile
import os


def invoice_error(path):
    with path.open(encoding='utf-8', errors='replace') as stream:
        return invoice_stream_error(stream)


def invoice_stream_error(stream):
    for line in stream:
        try:
            event = json.loads(line)
        except ValueError:
            if not line.lstrip().startswith('{') and 'unpaid invoice' in line.lower():
                return True
            continue
        if isinstance(event, dict) and (event.get('type') == 'error' or event.get('type') == 'result' and event.get('is_error')):
            if 'unpaid invoice' in json.dumps(event, ensure_ascii=False).lower():
                return True
    return False


def reconcile(data, backup=None):
    changes = []
    for log in sorted((data / 'cursor-logs').glob('*.log')):
        if not re.fullmatch('[0-9a-f]{24}', log.stem) or not invoice_error(log):
            continue
        path = data / 'cursor-calls' / (log.stem + '.json')
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if record.get('status') == 'running' or record.get('process_error_code') == 'cli_billing_required':
            continue
        changes.append({'call_id': log.stem, 'prior_status': record.get('status'), 'verified': record.get('verified'), 'resume_ok': record.get('resume_ok')})
        if backup is None:
            continue
        backup.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Refuse to overwrite the original evidence on a second migration.
        original = backup / path.name
        if original.exists():
            raise RuntimeError('original backup already exists: ' + log.stem)
        shutil.copy2(path, original)
        record.update(status='failed', ok=False, error_code='cli_billing_required', process_status='failed',
                      process_error_code='cli_billing_required', cli_assisted=False,
                      recovery_origin='external' if record.get('verified') is True else 'unavailable',
                      build_id='historical-billing-audit', schema_version=2)
        fd, name = tempfile.mkstemp(prefix='.billing-audit-', dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(record, stream, ensure_ascii=False)
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
    return {'at': dt.datetime.now().astimezone().isoformat(), 'applied': backup is not None, 'count': len(changes), 'changes': changes}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'data')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup-dir', type=Path)
    args = parser.parse_args()
    if args.apply and args.backup_dir is None:
        parser.error('--apply requires --backup-dir')
    report = reconcile(args.data_dir, args.backup_dir if args.apply else None)
    if args.apply:
        (args.backup_dir / 'manifest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'applied': report['applied'], 'count': report['count']}, ensure_ascii=False))
