"""Process recorded Da'wah media cleanup (D2 orphan/compensation records).

Expires unfinished upload intents past their expiry, then deletes recorded
orphan objects that no resource references. Safe to run repeatedly (e.g. from
the scheduled job runner). Prints counts only - never object keys or URLs.
"""
from django.core.management.base import BaseCommand

from dawah import uploads
from dawah.storage import StorageError


class Command(BaseCommand):
    help = 'Expire stale upload intents and delete recorded orphan media objects.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=500, help='Maximum cleanup records to process.')

    def handle(self, *args, limit, **options):
        intents = uploads.expire_stale_intents()
        try:
            counts = uploads.process_cleanup(limit=max(1, limit))
        except StorageError as exc:
            self.stderr.write(f'Storage unavailable ({exc.code}); cleanup records were kept.')
            counts = {'done': 0, 'skipped': 0, 'failed': 0}
        self.stdout.write(
            'intents expired={expired} abandoned={abandoned}; cleanup done={done} skipped={skipped} '
            'failed={failed}'.format(**intents, **counts))
