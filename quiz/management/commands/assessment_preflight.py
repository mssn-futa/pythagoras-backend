"""Preflight for the ASSESS-02 quiz migrations (read-only).

Exits nonzero when duplicate submissions per student/quiz exist; those block
``quiz.0004``. Never deletes, merges or edits records.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from quiz.preflight import find_duplicate_submissions, format_duplicates, legacy_notes


class Command(BaseCommand):
    help = ('Report submission IDs that duplicate a student/quiz pair (blocking) and how '
            'legacy quizzes/questions/submissions are carried forward. Exits 1 on duplicates.')

    def add_arguments(self, parser):
        parser.add_argument('--database', default='default')

    def handle(self, *args, database, **options):
        connection = connections[database]
        if 'quiz_submission' not in connection.introspection.table_names():
            self.stdout.write(self.style.SUCCESS('No quiz tables yet; nothing to check.'))
            return
        for note in legacy_notes(connection):
            self.stdout.write(f'note: {note}')
        duplicates = find_duplicate_submissions(connection)
        if duplicates:
            for line in format_duplicates(duplicates):
                self.stdout.write(line)
            raise CommandError(
                'Duplicate submissions block the quiz migrations. Resolve them with lead '
                'approval before applying quiz.0004; no records were changed.')
        self.stdout.write(self.style.SUCCESS('No duplicate submissions found.'))
