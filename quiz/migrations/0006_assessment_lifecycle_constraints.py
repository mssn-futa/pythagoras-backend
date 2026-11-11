"""ASSESS-02 constraints: NOT NULL order, auto timestamps, DB invariants.

Rollback: the last operation's reverse refuses to unapply while attempts or
release (non-legacy) submissions exist, because 0004's reverse would drop
attempt, denominator and snapshot history. Narrowing ``score`` back to a small
integer also fails (by design) if any stored score exceeds 32767.
"""
import django.db.models.expressions
from django.db import migrations, models


class HistoryRollbackError(RuntimeError):
    pass


def refuse_rollback_with_release_history(apps, schema_editor):
    db = schema_editor.connection.alias
    Attempt = apps.get_model('quiz', 'Attempt')
    Submission = apps.get_model('quiz', 'Submission')
    attempts = Attempt.objects.using(db).count()
    release = Submission.objects.using(db).filter(is_legacy=False).count()
    if attempts or release:
        raise HistoryRollbackError(
            f'Refusing to unapply quiz.0006: {attempts} attempts and {release} release '
            'submissions would lose their attempt/denominator/snapshot history. Restore a '
            'backup taken before the upgrade instead.')


class Migration(migrations.Migration):

    dependencies = [
        ('quiz', '0005_assessment_legacy_data'),
    ]

    operations = [
        migrations.AlterField(
            model_name='question',
            name='order',
            field=models.PositiveIntegerField(),
        ),
        migrations.AlterField(
            model_name='option',
            name='order',
            field=models.PositiveIntegerField(),
        ),
        migrations.AlterField(
            model_name='quiz',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, null=True),
        ),
        migrations.AlterField(
            model_name='quiz',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, null=True),
        ),
        migrations.AddConstraint(
            model_name='quiz',
            constraint=models.CheckConstraint(
                condition=models.Q(status__in=['draft', 'published', 'closed']),
                name='quiz_status_valid'),
        ),
        migrations.AddConstraint(
            model_name='quiz',
            constraint=models.CheckConstraint(
                condition=models.Q(models.Q(is_active=True, status='published'),
                                   models.Q(models.Q(status='published', _negated=True),
                                            is_active=False),
                                   _connector='OR'),
                name='quiz_is_active_matches_status'),
        ),
        migrations.AddConstraint(
            model_name='quiz',
            constraint=models.CheckConstraint(
                condition=models.Q(('status', 'draft'),
                                   models.Q(deadline__isnull=False, published_at__isnull=False,
                                            time_limit__gte=1, time_limit__lte=180),
                                   _connector='OR'),
                name='quiz_released_timing_complete'),
        ),
        migrations.AddConstraint(
            model_name='quiz',
            constraint=models.CheckConstraint(
                condition=models.Q(models.Q(closed_at__isnull=False, status='closed'),
                                   models.Q(models.Q(status='closed', _negated=True),
                                            closed_at__isnull=True),
                                   _connector='OR'),
                name='quiz_closed_at_matches_status'),
        ),
        migrations.AddConstraint(
            model_name='question',
            constraint=models.UniqueConstraint(
                deferrable=models.Deferrable['DEFERRED'], fields=('quiz', 'order'),
                name='quiz_question_order_unique'),
        ),
        migrations.AddConstraint(
            model_name='question',
            constraint=models.CheckConstraint(condition=models.Q(order__gte=1),
                                              name='quiz_question_order_positive'),
        ),
        migrations.AddConstraint(
            model_name='option',
            constraint=models.UniqueConstraint(
                deferrable=models.Deferrable['DEFERRED'], fields=('question', 'order'),
                name='quiz_option_order_unique'),
        ),
        migrations.AddConstraint(
            model_name='option',
            constraint=models.CheckConstraint(condition=models.Q(order__gte=1),
                                              name='quiz_option_order_positive'),
        ),
        migrations.AddConstraint(
            model_name='attempt',
            constraint=models.UniqueConstraint(fields=('quiz', 'student'),
                                               name='quiz_attempt_one_per_student'),
        ),
        migrations.AddConstraint(
            model_name='attempt',
            constraint=models.CheckConstraint(
                condition=models.Q(status__in=['in_progress', 'submitted', 'expired']),
                name='quiz_attempt_status_valid'),
        ),
        migrations.AddConstraint(
            model_name='attempt',
            constraint=models.CheckConstraint(
                condition=models.Q(expires_at__gt=django.db.models.expressions.F('started_at')),
                name='quiz_attempt_expires_after_start'),
        ),
        migrations.AddConstraint(
            model_name='attempt',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(finalized_at__gte=django.db.models.expressions.F('started_at'),
                             finalized_at__isnull=False,
                             finalized_at__lt=django.db.models.expressions.F('expires_at'),
                             status='submitted'),
                    models.Q(models.Q(status='submitted', _negated=True),
                             finalized_at__isnull=True),
                    _connector='OR'),
                name='quiz_attempt_finalized_before_expiry'),
        ),
        migrations.AddConstraint(
            model_name='submission',
            constraint=models.UniqueConstraint(fields=('quiz', 'student'),
                                               name='quiz_submission_one_per_student'),
        ),
        migrations.AddConstraint(
            model_name='submission',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(attempt__isnull=True, is_legacy=True),
                    models.Q(attempt__isnull=False, graded_at__isnull=False,
                             grading_snapshot__isnull=False, is_legacy=False,
                             total_points__isnull=False),
                    _connector='OR'),
                name='quiz_submission_release_shape'),
        ),
        migrations.AddConstraint(
            model_name='submission',
            constraint=models.CheckConstraint(
                condition=models.Q(('total_points__isnull', True),
                                   ('score__lte', django.db.models.expressions.F('total_points')),
                                   _connector='OR'),
                name='quiz_submission_score_within_total'),
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_rollback_with_release_history,
                             elidable=False),
    ]
