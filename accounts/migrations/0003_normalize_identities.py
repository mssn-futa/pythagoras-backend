"""Normalize stored identities before installing case-insensitive constraints.

Refuses to run when normalization would make two accounts collide. It never
merges, deletes or re-roles users; resolve collisions with lead approval after
`python manage.py identity_collision_report`, then migrate again.
"""
from django.db import migrations

from accounts.identity import (
    find_identity_collisions, format_collisions, has_collisions,
    normalize_department, normalize_email, normalize_matric,
)


class IdentityCollisionError(RuntimeError):
    pass


def normalize_identities(apps, schema_editor):
    User = apps.get_model('accounts', 'CustomUser')
    rows = User.objects.using(schema_editor.connection.alias).values_list('id', 'email', 'matric_number')
    report = find_identity_collisions(rows)
    if has_collisions(report):
        raise IdentityCollisionError(
            'Normalized identity collisions block this migration; no users were changed. '
            + '; '.join(format_collisions(report))
            + '. Run `python manage.py identity_collision_report` and resolve with lead approval.'
        )
    users = User.objects.using(schema_editor.connection.alias).only(
        'id', 'email', 'matric_number', 'department')
    for user in users.iterator():
        values = {
            'email': normalize_email(user.email),
            'matric_number': normalize_matric(user.matric_number),
            'department': normalize_department(user.department),
        }
        changed = {k: v for k, v in values.items() if getattr(user, k) != v}
        if changed:
            User.objects.using(schema_editor.connection.alias).filter(pk=user.pk).update(**changed)


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_customuser_role'),
    ]

    operations = [
        migrations.RunPython(normalize_identities, migrations.RunPython.noop),
    ]
