"""Generate a bounded, local HTML report from daily UTC sync logs."""
import argparse
import datetime as dt
import html
import json
from pathlib import Path


def aggregate(root, days=7, today=None):
    today = today or dt.datetime.now(dt.timezone.utc).date()
    rows = []
    malformed = 0
    for offset in range(days - 1, -1, -1):
        date = today - dt.timedelta(days=offset)
        path = root / f'{date}.log'
        operations = []
        if path.exists():
            # Bound IO to 8 MiB per day; report truncation as incomplete coverage.
            with path.open('rb') as stream:
                raw = stream.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                malformed += 1
                raw = raw[:8 * 1024 * 1024].rsplit(b'\n', 1)[0]
            for line in raw.splitlines():
                try:
                    event = json.loads(line)['event']
                    if not event.startswith('sync_operation '):
                        continue
                    op = json.loads(event[len('sync_operation '):])
                    if not isinstance(op['ok'], bool) or op['duration_seconds'] < 0 or op['fetched'] < 0:
                        raise ValueError()
                    operations.append(op)
                except (ValueError, KeyError, TypeError):
                    malformed += 1
        for platform in ('douyin', 'youtube'):
            selected = [op for op in operations if op.get('platform') == platform]
            count = len(selected)
            rows.append({'date': str(date), 'platform': platform, 'count': count,
                         'failures': sum(not op['ok'] for op in selected),
                         'seconds': sum(op['duration_seconds'] for op in selected),
                         'fetched': sum(op['fetched'] for op in selected), 'file_present': path.exists()})
    return rows, malformed


def render(rows, malformed):
    body = []
    for row in rows:
        count = row['count']
        failure = f"{row['failures'] / count:.0%}" if count else 'unavailable'
        duration = f"{row['seconds'] / count:.1f}" if count else 'unavailable'
        body.append(f"<tr><td>{html.escape(row['date'])}</td><td>{row['platform']}</td>"
                    f"<td><meter min='0' max='{max(1, max(r['count'] for r in rows))}' value='{count}'></meter> {count}</td>"
                    f"<td>{failure}</td><td>{duration}</td><td>{row['fetched']}</td>"
                    f"<td>{'present' if row['file_present'] else 'missing'}</td></tr>")
    return ("<!doctype html><meta charset='utf-8'><title>Sync history</title>"
            "<style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:16px}"
            "td,th{padding:10px;text-align:left}meter{width:100px}</style>"
            "<h1>Sync history · UTC daily buckets</h1>"
            f"<p>Generated {dt.datetime.now(dt.timezone.utc).isoformat()}. Skipped malformed records / truncated files: {malformed}.</p>"
            "<p>Scheduled, startup and background manual operations; legacy blocking API calls and individual retries excluded. "
            "A partial sync counts as failed. Missing records mean unavailable coverage. "
            "Fetched items include existing videos, not unique new items.</p>"
            "<table><tr><th>Date (UTC)</th><th>Platform</th><th>Runs</th><th>Failure rate</th>"
            "<th>Mean duration (s)</th><th>Items fetched</th><th>Log coverage</th></tr>" + ''.join(body) + '</table>')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--logs', type=Path, default=Path(__file__).resolve().parents[1] / 'logs')
    parser.add_argument('--days', type=int, choices=range(1, 32), default=7)
    parser.add_argument('--output', type=Path, default=Path('/tmp/myfollows-sync-report.html'))
    args = parser.parse_args()
    args.output.write_text(render(*aggregate(args.logs, args.days)), encoding='utf-8')
    print(args.output)
