"""Portable runtime settings and daily UTC operation logs. No account data."""
import configparser
import datetime as dt
import json
import logging
import os
from pathlib import Path
import threading

ROOT = Path(__file__).resolve().parent
_config = configparser.ConfigParser()
_config.read(ROOT / 'settings.ini')


def setting(name, default, minimum, maximum):
    raw = os.environ.get(name.upper(), _config.get('app', name, fallback=str(default)))
    try:
        value = int(raw)
        if not minimum <= value <= maximum:
            raise ValueError()
    except ValueError:
        raise ValueError(f'{name.upper()} must be an integer between {minimum} and {maximum}') from None
    return value


SYNC_INTERVAL = setting('sync_interval_minutes', 60, 5, 1440) * 60
SYNC_HOUR = setting('sync_hour', 6, 0, 23)
SYNC_LIMIT = setting('sync_limit', 100, 1, 500)
PORT = setting('port', 8082, 1, 65535)
RETENTION = setting('log_retention_days', 14, 1, 365)
LOG_DIR = ROOT / os.environ.get('LOG_DIR', _config.get('app', 'log_dir', fallback='../logs'))


class DailyHandler(logging.Handler):
    def emit(self, record):
        if not record.getMessage().startswith(('sync_operation ', 'sync_job_error ', 'Application started')):
            return
        try:
            now = dt.datetime.now(dt.timezone.utc)
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = LOG_DIR / f'{now:%Y-%m-%d}.log'
            with path.open('a', encoding='utf-8') as output:
                output.write(json.dumps({'timestamp': now.isoformat(), 'level': record.levelname,
                                         'event': record.getMessage()}, ensure_ascii=False) + '\n')
            if getattr(self, '_date', None) != now.date():
                self._date = now.date()
                for old in LOG_DIR.glob('????-??-??.log'):
                    try:
                        date = dt.date.fromisoformat(old.stem)
                    except ValueError:
                        continue
                    if (now.date() - date).days >= RETENTION:
                        old.unlink()
        except Exception:
            self.handleError(record)


state_lock = threading.Lock()
sync_state = {'douyin': {}, 'youtube': {}}


def sync_snapshot():
    with state_lock:
        return {name: dict(value) for name, value in sync_state.items()}


def sync_update(platform, **values):
    with state_lock:
        sync_state[platform].update(values)


def next_sync_time(now):
    """Keep the daily slot while bounding the wait after failed/finished runs."""
    daily = now.replace(hour=SYNC_HOUR, minute=0, second=0, microsecond=0)
    if daily <= now:
        daily += dt.timedelta(days=1)
    return min(daily, now + dt.timedelta(seconds=SYNC_INTERVAL))


class SyncCoordinator:
    """One bounded, process-local background job shared by manual/scheduled sync."""
    def __init__(self):
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state = {'running': False}

    def snapshot(self):
        with self._state_lock:
            return dict(self._state)

    def _claim(self, source):
        if not self._lock.acquire(blocking=False):
            return False
        with self._state_lock:
            self._state = {'running': True, 'source': source,
                           'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                           'completed_at': None, 'error': ''}
        for platform in ('douyin', 'youtube'):
            sync_update(platform, running=False, queued=True)
        return True

    def _execute(self, worker):
        try:
            worker()
        except Exception:
            logging.getLogger(__name__).error('sync_job_error background worker stopped unexpectedly')
            with self._state_lock:
                self._state['error'] = 'Sync stopped unexpectedly. Try again.'
        finally:
            for platform in ('douyin', 'youtube'):
                sync_update(platform, running=False, queued=False)
            with self._state_lock:
                self._state.update(running=False,
                                   completed_at=dt.datetime.now(dt.timezone.utc).isoformat())
            self._lock.release()

    def start(self, worker, source='manual'):
        if not self._claim(source):
            return False
        try:
            threading.Thread(target=self._execute, args=(worker,), daemon=True,
                             name='myfollows-sync').start()
        except Exception:
            for platform in ('douyin', 'youtube'):
                sync_update(platform, queued=False)
            with self._state_lock:
                self._state.update(running=False, error='Could not start sync. Try again.')
            self._lock.release()
            raise
        return True

    def run(self, worker, source):
        """Scheduler uses its existing thread and skips an already active job."""
        if not self._claim(source):
            return False
        self._execute(worker)
        return True


sync_jobs = SyncCoordinator()
