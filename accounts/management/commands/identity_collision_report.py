"""Preflight for the identity normalization/constraint migrations.

Reports colliding normalized email/matric groups by user ID only and exits
nonzero when any exist. Read-only: it never merges, deletes or edits users.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from accounts.identity import find_identity_collisions, format_collisions, has_collisions


class Command(BaseCommand):
    help = ('Report user IDs whose email (trimmed, lowercased) or matric number '
            '(trimmed, uppercased, blank=NULL) would collide. Exits 1 on collisions.')

    def add_arguments(self, parser):
        parser.add_argument('--database', default='default')

    def handle(self, *args, database, **options):
        # Raw read of the base columns works before or after the constraint
        # migrations and is independent of current model code.
        with connections[database].cursor() as cursor:
            cursor.execute('SELECT id, email, matric_number FROM accounts_customuser')
            report = find_identity_collisions(cursor.fetchall())
        if has_collisions(report):
            for line in format_collisions(report):
                self.stdout.write(line)
            raise CommandError(
                'Identity collisions found. Resolve them (with lead approval) before '
                'applying accounts migrations 0003/0004; no users were changed.')
        self.stdout.write(self.style.SUCCESS('No identity collisions found.'))
