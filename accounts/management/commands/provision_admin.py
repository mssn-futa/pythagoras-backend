"""Explicit, idempotent product-administrator provisioning for existing accounts."""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.identity import normalize_email


class Command(BaseCommand):
    help = ('Grant role=admin to existing accounts. Never creates, deletes, or '
            'reactivates users. --staff also grants Django admin (is_staff).')

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument('--email', help='Existing account email (case-insensitive).')
        target.add_argument('--user-id', type=int, help='Existing account ID.')
        target.add_argument('--existing-superusers', action='store_true',
                            help='Give role=admin to every active superuser lacking it.')
        parser.add_argument('--staff', action='store_true',
                            help='Also set is_staff=True (Django admin access).')

    def handle(self, *args, email=None, user_id=None, existing_superusers=False, staff=False, **options):
        User = get_user_model()
        with transaction.atomic():
            if existing_superusers:
                users = list(User.objects.select_for_update()
                             .filter(is_superuser=True, is_active=True).order_by('pk'))
            else:
                lookup = {'pk': user_id} if user_id is not None else {'email': normalize_email(email)}
                try:
                    users = [User.objects.select_for_update().get(**lookup)]
                except User.DoesNotExist:
                    raise CommandError('No matching account exists; nothing was changed.')
                if not users[0].is_active:
                    raise CommandError(
                        f'User {users[0].pk} is inactive; reactivate it deliberately first. '
                        'Nothing was changed.')
            for user in users:
                fields = []
                if user.role != User.Role.ADMIN:
                    user.role = User.Role.ADMIN
                    fields.append('role')
                if staff and not user.is_staff:
                    user.is_staff = True
                    fields.append('is_staff')
                if fields:
                    user.save(update_fields=fields)
                    self.stdout.write(f'User {user.pk}: updated {", ".join(fields)}.')
                else:
                    self.stdout.write(f'User {user.pk}: already provisioned; no change.')
        if not users:
            self.stdout.write('No matching accounts; no change.')
