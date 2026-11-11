"""Broadcast outbox worker (D4/D5). See docs/communication-outbox.md "Worker".

    python manage.py dawah_outbox --once                  # Cron-started bounded pass (default 240 s)
    python manage.py dawah_outbox                         # continuous local/operator loop
    python manage.py dawah_outbox --status                # backlog JSON, sends nothing

SIGTERM/SIGINT stop new claims, let the in-flight send finish (bounded by
RESEND_TIMEOUT_SECONDS), release unused claims and exit 0. Output is one JSON
summary line of ids-free counts; never addresses, subjects or bodies.
"""
import json
import logging
import signal
import sys
import threading

from django.core.management.base import BaseCommand, CommandError

from dawah import outbox


def _positive(kind):
    def parse(value):
        try:
            number = kind(value)
        except ValueError:
            raise CommandError(f'{value!r} is not a valid number.')
        if number <= 0:
            raise CommandError('Value must be positive.')
        return number
    return parse


class Command(BaseCommand):
    help = ('Drain the broadcast outbox through the shared Resend transport. --once runs one bounded pass '
            '(scheduler tick, lease recovery, bounded drain, release unused claims) for Cron-started '
            'containers; without it the worker polls until SIGTERM/SIGINT.')

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument('--once', action='store_true',
                          help='One bounded pass, then exit (production Cron mode).')
        mode.add_argument('--status', action='store_true',
                          help='Print backlog counts/ages as JSON and exit; sends nothing.')
        parser.add_argument('--budget-seconds', type=_positive(float), default=240.0,
                            help='Wall budget for --once (default 240). Dispatch stops when the remaining '
                                 'budget cannot cover one bounded send plus the shutdown margin.')
        parser.add_argument('--max-sends', type=_positive(int), default=None,
                            help='Optional cap on provider handoffs in this run.')
        parser.add_argument('--batch-size', type=_positive(int), default=outbox.OutboxConfig.batch_size,
                            help='Rows claimed per batch (default 20).')
        parser.add_argument('--poll-seconds', type=_positive(float), default=outbox.OutboxConfig.poll_seconds,
                            help='Idle poll interval in continuous mode (default 5).')

    def handle(self, *args, once, status, budget_seconds, max_sends, batch_size, poll_seconds, **options):
        if status:
            self.stdout.write(json.dumps(outbox.backlog(), sort_keys=True))
            return
        self._ensure_log_handler(options.get('verbosity', 1))
        stop = threading.Event()
        previous = self._install_signal_handlers(stop)
        try:
            worker = outbox.OutboxWorker(
                outbox.OutboxConfig(batch_size=batch_size, poll_seconds=poll_seconds), stop_event=stop)
            if once:
                summary = worker.run_once(budget_seconds=budget_seconds, max_sends=max_sends)
            else:
                summary = worker.run_forever(max_sends=max_sends)
        finally:
            self._restore_signal_handlers(previous)
        self.stdout.write(json.dumps(summary.as_dict(), sort_keys=True))

    @staticmethod
    def _install_signal_handlers(stop):
        if threading.current_thread() is not threading.main_thread():
            return {}

        def handler(signum, frame):
            stop.set()

        previous = {}
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, handler)
        return previous

    @staticmethod
    def _restore_signal_handlers(previous):
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    @staticmethod
    def _ensure_log_handler(verbosity):
        """Worker events go to stderr unless logging is configured elsewhere."""
        log = logging.getLogger('dawah.outbox')
        configured = [h for h in log.handlers if not isinstance(h, logging.NullHandler)]
        if verbosity < 1 or configured or logging.getLogger().handlers:
            return
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
        log.addHandler(handler)
        log.setLevel(logging.INFO if verbosity < 2 else logging.DEBUG)
