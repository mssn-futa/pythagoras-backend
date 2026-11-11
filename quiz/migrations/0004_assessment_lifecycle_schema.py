"""ASSESS-02 schema: quiz lifecycle/timing, persisted order, attempts, snapshots.

The first operation is a preflight: duplicate submissions for one
student/quiz block this migration (listing IDs only) before anything changes,
because 0006 adds the one-submission constraint. Nothing is deleted or merged;
run ``python manage.py assessment_preflight`` and resolve with lead approval.

Existing rows: quizzes get status ``draft`` (0005 records the old is_active),
created/updated times stay NULL (unknown, not invented), existing submissions
get ``is_legacy = true`` with no attempt, denominator or snapshot, and keep
their score/answers/timestamps. Event/course/quiz/student foreign keys that
used to cascade history become PROTECT.
"""
import django.core.validators
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

from quiz.preflight import find_duplicate_submissions, format_duplicates


class DuplicateSubmissionError(RuntimeError):
    pass


def block_duplicate_submissions(apps, schema_editor):
    duplicates = find_duplicate_submissions(schema_editor.connection)
    if duplicates:
        raise DuplicateSubmissionError(
            'Duplicate submissions block this migration; no records were changed. '
            + '; '.join(format_duplicates(duplicates))
            + '. Run `python manage.py assessment_preflight` and resolve with lead approval.')


class Migration(migrations.Migration):

    dependencies = [
        ('dawah', '0001_initial'),
        ('quiz', '0003_alter_quiz_related_course_alter_quiz_related_event_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(block_duplicate_submissions, migrations.RunPython.noop,
                             elidable=False),
        # -- Quiz lifecycle, timing and metadata ------------------------------
        migrations.AddField(
            model_name='quiz',
            name='status',
            field=models.CharField(
                choices=[('draft', 'Draft'), ('published', 'Published'), ('closed', 'Closed')],
                default='draft', max_length=9),
        ),
        migrations.AddField(
            model_name='quiz',
            name='deadline',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='quiz',
            name='published_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='quiz',
            name='closed_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        # Plain nullable columns here so existing rows stay NULL; 0006 switches
        # them to auto_now_add/auto_now (adding an auto_now column directly
        # would stamp every legacy row with the migration time).
        migrations.AddField(
            model_name='quiz',
            name='created_at',
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name='quiz',
            name='updated_at',
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name='quiz',
            name='legacy_is_active',
            field=models.BooleanField(blank=True, editable=False, null=True),
        ),
        migrations.AlterField(
            model_name='quiz',
            name='type',
            field=models.CharField(default='arabic', max_length=250),
        ),
        migrations.AlterField(
            model_name='quiz',
            name='is_active',
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.AlterField(
            model_name='quiz',
            name='time_limit',
            field=models.PositiveSmallIntegerField(
                help_text='Time limit in minutes', null=True,
                validators=[django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(180)]),
        ),
        migrations.AlterField(
            model_name='quiz',
            name='related_event',
            field=models.ForeignKey(blank=True, null=True,
                                    on_delete=django.db.models.deletion.PROTECT,
                                    to='dawah.event'),
        ),
        migrations.AlterField(
            model_name='quiz',
            name='related_course',
            field=models.ForeignKey(blank=True, null=True,
                                    on_delete=django.db.models.deletion.PROTECT,
                                    to='quiz.course'),
        ),
        # -- Persisted order (backfilled by 0005, NOT NULL in 0006) ----------
        migrations.AddField(
            model_name='question',
            name='order',
            field=models.PositiveIntegerField(null=True),
        ),
        migrations.AddField(
            model_name='option',
            name='order',
            field=models.PositiveIntegerField(null=True),
        ),
        # -- Attempts ----------------------------------------------------------
        migrations.CreateModel(
            name='Attempt',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False,
                                           verbose_name='ID')),
                ('status', models.CharField(
                    choices=[('in_progress', 'In progress'), ('submitted', 'Submitted'),
                             ('expired', 'Expired')],
                    default='in_progress', max_length=11)),
                ('started_at', models.DateTimeField()),
                ('expires_at', models.DateTimeField()),
                ('finalized_at', models.DateTimeField(blank=True, null=True)),
                ('quiz', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                           related_name='attempts', to='quiz.quiz')),
                ('student', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                              related_name='quiz_attempts',
                                              to=settings.AUTH_USER_MODEL)),
            ],
        ),
        # -- Submission linkage, denominator, snapshot ------------------------
        migrations.AddField(
            model_name='submission',
            name='attempt',
            field=models.OneToOneField(blank=True, null=True,
                                       on_delete=django.db.models.deletion.PROTECT,
                                       related_name='submission', to='quiz.attempt'),
        ),
        migrations.AddField(
            model_name='submission',
            name='total_points',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='submission',
            name='grading_snapshot',
            field=models.JSONField(blank=True, null=True),
        ),
        # Every row present now is legacy; later inserts default to release rows.
        migrations.AddField(
            model_name='submission',
            name='is_legacy',
            field=models.BooleanField(default=True),
        ),
        migrations.AlterField(
            model_name='submission',
            name='is_legacy',
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name='submission',
            name='score',
            field=models.PositiveIntegerField(),
        ),
        migrations.AlterField(
            model_name='submission',
            name='submitted_at',
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AlterField(
            model_name='submission',
            name='quiz',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                    to='quiz.quiz'),
        ),
        migrations.AlterField(
            model_name='submission',
            name='student',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                    to=settings.AUTH_USER_MODEL),
        ),
    ]
