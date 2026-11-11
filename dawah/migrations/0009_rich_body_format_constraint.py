"""COMM-04: plain/rich body invariant.

Before COMM-04 the API accepted plain drafts only, so every existing row is
plain. The data step makes that explicit for any row created outside the API
(e.g. a shell) before the constraint is added: a row that is not a complete
rich row becomes plain with ``body_html`` null. ``body`` and every frozen
``snapshot_*`` field (queued text/rendering evidence) are left untouched.
"""
from django.db import migrations, models
from django.db.models import Q


def existing_rows_are_plain(apps, schema_editor):
    Broadcast = apps.get_model('dawah', 'Broadcast')
    complete_rich = Q(body_format='rich', body_html__isnull=False) & ~Q(body_html='')
    Broadcast.objects.exclude(complete_rich).exclude(body_format='plain', body_html__isnull=True).update(
        body_format='plain', body_html=None)


class Migration(migrations.Migration):

    dependencies = [
        ("dawah", "0008_delivery_attempt_evidence"),
    ]

    operations = [
        migrations.RunPython(existing_rows_are_plain, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="broadcast",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("body_format", "plain"), ("body_html__isnull", True)),
                    models.Q(
                        ("body_format", "rich"),
                        ("body_html__isnull", False),
                        models.Q(("body_html", ""), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="dawah_broadcast_body_format_html",
            ),
        ),
    ]
