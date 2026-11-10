"""ASSESS-02 legacy data mapping (deterministic, reversible).

* Every existing quiz stays ``draft`` pending validation (D3); its old
  ``is_active`` value is copied to ``legacy_is_active`` and ``is_active``
  becomes false (derived from status). Students therefore stop seeing legacy
  quizzes until an admin publishes them through validation.
* Question ``order`` = 1..n per quiz by ascending ID; option ``order`` =
  1..n per question by ascending ID (the order every API already used).

Reverse: a quiz still in draft with a recorded ``legacy_is_active`` gets that
value back; any other quiz gets ``is_active = (status == 'published')``.
``legacy_is_active`` and orders are cleared (0004's reverse drops them).
"""
from django.db import migrations


def _number(rows, group_attr):
    counters = {}
    changed = []
    for row in rows:
        key = getattr(row, group_attr)
        counters[key] = counters.get(key, 0) + 1
        if row.order != counters[key]:
            row.order = counters[key]
            changed.append(row)
    return changed


def forward(apps, schema_editor):
    db = schema_editor.connection.alias
    Quiz = apps.get_model('quiz', 'Quiz')
    Question = apps.get_model('quiz', 'Question')
    Option = apps.get_model('quiz', 'Option')
    for quiz in Quiz.objects.using(db).order_by('pk').only('pk', 'is_active'):
        Quiz.objects.using(db).filter(pk=quiz.pk).update(
            legacy_is_active=quiz.is_active, is_active=False, status='draft')
    questions = list(Question.objects.using(db).order_by('quiz_id', 'pk').only('pk', 'quiz_id', 'order'))
    Question.objects.using(db).bulk_update(_number(questions, 'quiz_id'), ['order'], batch_size=500)
    options = list(Option.objects.using(db).order_by('question_id', 'pk').only('pk', 'question_id', 'order'))
    Option.objects.using(db).bulk_update(_number(options, 'question_id'), ['order'], batch_size=500)


def backward(apps, schema_editor):
    db = schema_editor.connection.alias
    Quiz = apps.get_model('quiz', 'Quiz')
    Question = apps.get_model('quiz', 'Question')
    Option = apps.get_model('quiz', 'Option')
    for quiz in Quiz.objects.using(db).order_by('pk').only('pk', 'status', 'legacy_is_active'):
        if quiz.status == 'draft' and quiz.legacy_is_active is not None:
            restored = quiz.legacy_is_active  # never released since the upgrade
        else:
            restored = quiz.status == 'published'
        Quiz.objects.using(db).filter(pk=quiz.pk).update(is_active=restored, legacy_is_active=None)
    Question.objects.using(db).update(order=None)
    Option.objects.using(db).update(order=None)


class Migration(migrations.Migration):

    dependencies = [
        ('quiz', '0004_assessment_lifecycle_schema'),
    ]

    operations = [
        migrations.RunPython(forward, backward, elidable=False),
    ]
