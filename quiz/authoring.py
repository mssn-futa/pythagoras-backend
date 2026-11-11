"""Admin authoring services: draft questions/options (ASSESS-03, D3 A08/A11).

The HTTP layer validates request structure (``quiz.serializers``); these
functions apply the rules that need the quiz row lock and perform the nested
writes atomically:

* every write opens one ``transaction.atomic()`` and first calls
  ``assert_content_editable`` (quiz row ``FOR NO KEY UPDATE``), so it
  serializes with ``publish_quiz``/``close_quiz`` (``FOR UPDATE``) and with
  attempt start/finalize (``FOR SHARE``). A question write that loses a race
  with publication sees the published row and raises ``QuizImmutable``; one
  that wins is validated by the publication that runs after it.
* question ``order`` uniqueness within the quiz is checked under that lock
  (``AuthoringInvalid`` on ``order``); the database constraint is deferred.
* options are replaced as a set; persisted option IDs are kept by position
  (the n-th option by order keeps the n-th existing row), extra rows are
  created and surplus rows deleted.

Raises ``quiz.errors`` (``QuizImmutable``, ``AuthoringInvalid``) or
``Question.DoesNotExist``/``Quiz.DoesNotExist``. No HTTP imports.
"""
from django.db import transaction
from django.db.models import Max

from .errors import AuthoringInvalid
from .models import Option, Question, Quiz, assert_content_editable

# Authoring limit for question/option ``order`` (D3 engineering limit; far
# below the integer column so an appended order can never overflow it).
MAX_ORDER = 10000

QUESTION_FIELDS = ('type', 'text', 'points', 'order')
OPTION_FIELDS = ('text', 'is_correct', 'order')


def _check_question_order(quiz_id, order, exclude_pk=None):
    if order is None:
        return
    taken = Question.objects.filter(quiz_id=quiz_id, order=order).exclude(pk=exclude_pk)
    if taken.exists():
        raise AuthoringInvalid(fields={'order': [
            'Another question of this quiz already has this order.']})


def create_question(quiz_id, data):
    """Create an MCQ question with its options in a content-editable quiz.

    ``data``: validated ``type``/``text``/``points``/optional ``order`` and
    ``options`` (each ``text``/``is_correct``/``order``). An omitted order is
    appended after the quiz's last question.
    """
    data = dict(data)
    options = data.pop('options')
    with transaction.atomic():
        assert_content_editable(quiz_id)
        if not Quiz.objects.filter(pk=quiz_id).exists():
            raise Quiz.DoesNotExist()
        if data.get('order') is None:
            # Appended under the quiz lock, so the next order is stable.
            last = Question.objects.filter(quiz_id=quiz_id).aggregate(last=Max('order'))['last']
            data['order'] = (last or 0) + 1
            if data['order'] > MAX_ORDER:
                raise AuthoringInvalid(fields={'order': [
                    f'The quiz already uses question order {MAX_ORDER}; set an order '
                    f'between 1 and {MAX_ORDER} that is free.']})
        else:
            _check_question_order(quiz_id, data['order'])
        question = Question.objects.create(
            quiz_id=quiz_id, **{name: data[name] for name in QUESTION_FIELDS if name in data})
        Option.objects.bulk_create(
            Option(question=question, **{name: option[name] for name in OPTION_FIELDS})
            for option in options)
    return question


def _replace_options(question, options):
    existing = list(question.option_set.order_by('order', 'pk'))
    ordered = sorted(options, key=lambda option: option['order'])
    kept = existing[:len(ordered)]
    for row, option in zip(kept, ordered):
        for name in OPTION_FIELDS:
            setattr(row, name, option[name])
    if kept:
        Option.objects.bulk_update(kept, list(OPTION_FIELDS))
    surplus = [row.pk for row in existing[len(ordered):]]
    if surplus:
        Option.objects.filter(pk__in=surplus).delete()
    extra = ordered[len(kept):]
    if extra:
        Option.objects.bulk_create(
            Option(question=question, **{name: option[name] for name in OPTION_FIELDS})
            for option in extra)


def update_question(question_id, data):
    """Change a draft question's fields and, when ``options`` is given, its option set.

    Keys absent from ``data`` stay unchanged. The question never moves to another quiz.
    """
    data = dict(data)
    options = data.pop('options', None)
    with transaction.atomic():
        quiz_id = Question.objects.filter(pk=question_id).values_list('quiz_id', flat=True).first()
        if quiz_id is None:
            raise Question.DoesNotExist()
        assert_content_editable(quiz_id)
        question = Question.objects.select_for_update().get(pk=question_id)
        if question.type != Question.Type.mcq:
            raise AuthoringInvalid(fields={'type': [
                'Legacy non-MCQ questions cannot be edited or converted; '
                'create a new MCQ question instead.']})
        if 'order' in data:
            _check_question_order(quiz_id, data['order'], exclude_pk=question.pk)
        changed = [name for name in QUESTION_FIELDS
                   if name in data and getattr(question, name) != data[name]]
        for name in changed:
            setattr(question, name, data[name])
        if changed:
            question.save(update_fields=changed)
        if options is not None:
            _replace_options(question, options)
    return question


def delete_question(question_id):
    """Delete a draft question and its options (``QuizImmutable`` once content-locked)."""
    with transaction.atomic():
        quiz_id = Question.objects.filter(pk=question_id).values_list('quiz_id', flat=True).first()
        if quiz_id is None:
            raise Question.DoesNotExist()
        assert_content_editable(quiz_id)
        Question.objects.get(pk=question_id).delete()


def publication_fields(errors):
    """``publication_errors`` output as flat dotted field paths for the D0 envelope.

    ``{'questions': ['A quiz needs...', {'10': ['...']}]}`` becomes
    ``{'questions': ['A quiz needs...'], 'questions.10': ['Question 2: ...']}``
    (the prefix is the question's persisted order, what the admin sees); every
    value is a list of strings.
    """
    question_ids = [int(key) for messages in errors.values() for message in messages
                    if isinstance(message, dict) for key in message if str(key).isdigit()]
    orders = dict(Question.objects.filter(pk__in=question_ids).values_list('pk', 'order'))
    fields = {}
    for name, messages in errors.items():
        for message in messages:
            if isinstance(message, dict):
                for key, nested in message.items():
                    order = orders.get(int(key)) if str(key).isdigit() else None
                    prefix = f'Question {order}: ' if order is not None else ''
                    fields.setdefault(f'{name}.{key}', []).extend(
                        f'{prefix}{m}' for m in nested)
            else:
                fields.setdefault(name, []).append(str(message))
    return fields


def flatten_errors(detail, path=''):
    """DRF nested validation detail as ``{dotted.path: [messages]}``.

    A list of nested objects (``many=True``) is indexed from 0
    (``options.2.text``); a nested ``non_field_errors`` belongs to its parent path.
    """
    fields = {}

    def add(at, message):
        fields.setdefault(at or 'non_field_errors', []).append(str(message))

    def walk(value, at):
        if isinstance(value, dict):
            for key, nested in value.items():
                key = str(key)
                walk(nested, at if key == 'non_field_errors' and at else
                     (f'{at}.{key}' if at else key))
        elif isinstance(value, (list, tuple)):
            if value and all(isinstance(item, (dict, list, tuple)) for item in value):
                for index, item in enumerate(value):
                    walk(item, f'{at}.{index}' if at else str(index))
            else:
                for item in value:
                    if isinstance(item, (dict, list, tuple)):
                        walk(item, at)
                    else:
                        add(at, item)
        else:
            add(at, value)

    walk(detail, path)
    return fields
