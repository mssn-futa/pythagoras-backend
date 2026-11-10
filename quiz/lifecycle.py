"""Assessment lifecycle, attempt timing and finalization primitives (D3, A09-A11).

These are the service-layer building blocks for ASSESS-03 (authoring,
publication, closing) and ASSESS-04 (attempt start/submit/results). They raise
``quiz.errors`` domain errors and never import HTTP code.

Clock and arrival: every lifecycle timestamp comes from ``server_now()``,
PostgreSQL ``transaction_timestamp()``: the start of the transaction the
service runs in (the request transaction when a view wraps the call in
``atomic()``, otherwise the service's own transaction, whose first statement
reads it before any lock is requested). That instant is the request's
"arrival": expiry and availability are judged on it, so time spent waiting
for locks never turns an on-time submission into ``ATTEMPT_EXPIRED``. All
application containers share this one clock. Callers may pass ``now=`` only
in tests.

Locks (lock order: quiz row, then attempt row):

* publish/close: quiz ``FOR UPDATE`` (exclusive).
* start/finalize: quiz ``FOR SHARE`` (students proceed in parallel, but not
  while the quiz publishes/closes or its content changes) and, for finalize,
  the attempt row ``FOR UPDATE OF`` the attempt only. One attempt per student
  is decided by the unique constraint (a racing insert waits, then reuses).
* question/option writes: quiz ``FOR NO KEY UPDATE`` (conflicts with both).

Every function that writes opens its own ``transaction.atomic()``.
"""
import json
from datetime import timedelta

from django.db import IntegrityError, connection, transaction
from django.db.models import Prefetch
from django.db.models.functions import Now
from django.utils import timezone

from accounts.models import CustomUser

from .errors import (
    AttemptExpired, InvalidAnswers, InvalidTransition, PublicationInvalid, QuizImmutable,
    QuizReferenced, QuizUnavailable, SubmissionConflict,
)
from .models import (
    MAX_STORED_POINTS, MAX_TIME_LIMIT, MIN_TIME_LIMIT, Attempt, Option, Question, Quiz,
    Submission,
)
from .services import QuizService

OPTIONS_PER_QUESTION = 4


# ---------------------------------------------------------------------------
# Clock, expiry and availability
# ---------------------------------------------------------------------------

def server_now():
    """Arrival time: the current transaction's start on the database clock.

    ``transaction_timestamp()`` is constant within a transaction, so every
    timestamp a service writes in one transaction is the same instant.
    """
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute('SELECT transaction_timestamp()')
            return cursor.fetchone()[0]
    return timezone.now()


def compute_expires_at(started_at, time_limit_minutes, deadline):
    """A10: ``min(started_at + time_limit, deadline)``.

    Requires a positive time limit and a deadline after ``started_at`` (a quiz
    is only startable while ``deadline > now``).
    """
    if not time_limit_minutes or time_limit_minutes < 1:
        raise ValueError('A positive time limit is required.')
    if deadline is None or deadline <= started_at:
        raise ValueError('The deadline must be after the attempt start.')
    return min(started_at + timedelta(minutes=time_limit_minutes), deadline)


def is_expired(attempt, now):
    """True at/after expiry: the first finalization must be strictly before it."""
    return now >= attempt.expires_at


def effective_attempt_status(attempt, now):
    """Status to report: an in-progress attempt past expiry reads as ``expired``."""
    if attempt.status == Attempt.Status.IN_PROGRESS and is_expired(attempt, now):
        return Attempt.Status.EXPIRED
    return attempt.status


def quiz_is_available(quiz, now):
    """Active availability (D3): published and deadline > now."""
    return (quiz.status == Quiz.Status.PUBLISHED and quiz.deadline is not None
            and quiz.deadline > now)


def available_quizzes(queryset=None):
    """Quizzes a student may start now (DB clock), as a queryset filter."""
    queryset = Quiz.objects.all() if queryset is None else queryset
    return queryset.filter(status=Quiz.Status.PUBLISHED, deadline__gt=Now())


def ordered_questions(quiz, *, mcq_only=True):
    """Questions in persisted order with options prefetched in persisted order."""
    queryset = quiz.question_set.all()
    if mcq_only:
        queryset = queryset.filter(type=Question.Type.mcq)
    return queryset.order_by('order', 'pk').prefetch_related(
        Prefetch('option_set', queryset=Option.objects.order_by('order', 'pk')))


# ---------------------------------------------------------------------------
# Authoring guards and publication
# ---------------------------------------------------------------------------

def assert_quiz_draft(quiz):
    """Raise ``QuizImmutable`` unless ``quiz`` is a draft (question/option edits)."""
    if quiz.status != Quiz.Status.DRAFT:
        raise QuizImmutable()


def assert_quiz_changes_allowed(quiz, changed_fields):
    """Raise ``QuizImmutable`` if a non-draft quiz would change a locked field.

    ``changed_fields`` are model field names. Title/description corrections
    are always allowed; status/is_active/published_at/closed_at are never
    written directly (use ``publish_quiz``/``close_quiz``).
    """
    lifecycle = {'status', 'is_active', 'published_at', 'closed_at', 'legacy_is_active'}
    direct = sorted(set(changed_fields) & lifecycle)
    if direct:
        raise InvalidTransition('Quiz status changes only through publish/close.',
                                fields={name: ['Use the publish or close action.']
                                        for name in direct})
    if quiz.status == Quiz.Status.DRAFT:
        return
    locked = sorted(set(changed_fields) - set(Quiz.METADATA_FIELDS))
    if locked:
        raise QuizImmutable(
            'Only the title and description of a published or closed quiz can change.',
            fields={name: ['This field cannot change after publication.'] for name in locked})


def normalized_text(text):
    """Distinct-option normalization (lead decision): trim, collapse internal
    whitespace, casefold. Authoring and publication use this one rule."""
    return ' '.join((text or '').split()).casefold()


_normalized_text = normalized_text


def publication_errors(quiz, now):
    """Field errors that block publishing ``quiz`` at ``now`` ({} when valid)."""
    errors = {}
    if quiz.time_limit is None or not MIN_TIME_LIMIT <= quiz.time_limit <= MAX_TIME_LIMIT:
        errors['time_limit'] = [f'Time limit must be {MIN_TIME_LIMIT}-{MAX_TIME_LIMIT} minutes.']
    if quiz.deadline is None:
        errors['deadline'] = ['A deadline is required to publish.']
    elif quiz.deadline <= now:
        errors['deadline'] = ['The deadline must be in the future.']
    questions = list(ordered_questions(quiz, mcq_only=False))
    if not questions:
        errors['questions'] = ['A quiz needs at least one question to publish.']
    question_errors = {}
    total = 0
    for question in questions:
        problems = []
        if question.type != Question.Type.mcq:
            problems.append('Only multiple-choice questions can be published.')
        if question.points < 1:
            problems.append('Points must be a positive integer.')
        options = list(question.option_set.all())
        if len(options) != OPTIONS_PER_QUESTION:
            problems.append(f'Exactly {OPTIONS_PER_QUESTION} options are required.')
        texts = [_normalized_text(option.text) for option in options]
        if any(not text for text in texts):
            problems.append('Option text cannot be empty.')
        elif len(set(texts)) != len(texts):
            problems.append('Options must be distinct.')
        if sum(1 for option in options if option.is_correct) != 1:
            problems.append('Exactly one option must be correct.')
        if not _normalized_text(question.text):
            problems.append('Question text cannot be empty.')
        if problems:
            question_errors[str(question.pk)] = problems
        total += question.points
    if question_errors:
        errors.setdefault('questions', []).append(question_errors)
    if total > MAX_STORED_POINTS:
        errors['total_points'] = [f'Total points cannot exceed {MAX_STORED_POINTS}.']
    return errors


def _lock_quiz(quiz_id):
    quiz = Quiz.objects.select_for_update().filter(pk=quiz_id).first()
    if quiz is None:
        raise Quiz.DoesNotExist()
    return quiz


def publish_quiz(quiz_id, *, now=None):
    """draft -> published after validating the whole graph; returns ``(quiz, changed)``.

    Re-publishing a published quiz is a no-op (``changed=False``); a closed
    quiz raises ``InvalidTransition``; validation failures raise
    ``PublicationInvalid`` with ``fields``. Raises ``Quiz.DoesNotExist``.
    """
    with transaction.atomic():
        now = now or server_now()
        quiz = _lock_quiz(quiz_id)
        if quiz.status == Quiz.Status.PUBLISHED:
            return quiz, False
        if quiz.status != Quiz.Status.DRAFT:
            raise InvalidTransition('A closed quiz cannot be published again.')
        errors = publication_errors(quiz, now)
        if errors:
            raise PublicationInvalid(fields=errors)
        quiz.status = Quiz.Status.PUBLISHED
        quiz.published_at = now
        quiz._lifecycle_transition = (Quiz.Status.DRAFT, Quiz.Status.PUBLISHED)
        quiz.save(update_fields=['status', 'published_at', 'updated_at'])
    return quiz, True


def close_quiz(quiz_id, *, now=None):
    """published -> closed, idempotent for closed; returns ``(quiz, changed)``.

    Blocks new attempts; existing attempts keep their original expiry.
    A draft raises ``InvalidTransition``. Raises ``Quiz.DoesNotExist``.
    """
    with transaction.atomic():
        now = now or server_now()
        quiz = _lock_quiz(quiz_id)
        if quiz.status == Quiz.Status.CLOSED:
            return quiz, False
        if quiz.status != Quiz.Status.PUBLISHED:
            raise InvalidTransition('Only a published quiz can be closed.')
        quiz.status = Quiz.Status.CLOSED
        quiz.closed_at = now
        quiz._lifecycle_transition = (Quiz.Status.PUBLISHED, Quiz.Status.CLOSED)
        quiz.save(update_fields=['status', 'closed_at', 'updated_at'])
    return quiz, True


def quiz_has_history(quiz):
    return (Attempt.objects.filter(quiz=quiz).exists()
            or Submission.objects.filter(quiz=quiz).exists())


def delete_draft_quiz(quiz_id):
    """Delete a draft with no attempts/submissions (``QuizReferenced``/``QuizImmutable``)."""
    with transaction.atomic():
        quiz = _lock_quiz(quiz_id)
        if quiz_has_history(quiz):
            raise QuizReferenced()
        if quiz.status != Quiz.Status.DRAFT:
            raise QuizImmutable('Only a draft quiz can be deleted; close it instead.')
        quiz.delete()


# ---------------------------------------------------------------------------
# Attempts and finalization
# ---------------------------------------------------------------------------

def is_eligible_student(user):
    """Approved policy: any active student account (not enrollment)."""
    return bool(user is not None and user.is_active and getattr(user, 'role', None) == CustomUser.Role.STUDENT)


def share_lock_quiz(quiz_id):
    """Load a quiz holding ``FOR SHARE`` on its row (start/finalize).

    Shared locks do not block each other, so all students start and submit in
    parallel; they do conflict with publish/close (``FOR UPDATE``) and content
    writes (``FOR NO KEY UPDATE``). Raises ``Quiz.DoesNotExist``.
    """
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute('SELECT id FROM quiz_quiz WHERE id = %s FOR SHARE', [quiz_id])
            if cursor.fetchone() is None:
                raise Quiz.DoesNotExist()
    return Quiz.objects.get(pk=quiz_id)


def lock_attempt(**lookup):
    """Lock one attempt row (only the attempt, not the joined student row)."""
    return (Attempt.objects.select_for_update(of=('self',)).select_related('student')
            .get(**lookup))


def create_attempt(quiz, student, now):
    """Insert the attempt or, if a concurrent start won the unique race, return
    that one locked; ``(attempt, created)``. Caller holds the quiz share lock."""
    attempt = Attempt(quiz=quiz, student=student, status=Attempt.Status.IN_PROGRESS,
                      started_at=now,
                      expires_at=compute_expires_at(now, quiz.time_limit, quiz.deadline))
    try:
        with transaction.atomic():
            attempt.save(force_insert=True)
    except IntegrityError:
        # The competing insert has committed (it held the unique index entry).
        return lock_attempt(quiz=quiz, student=student), False
    return attempt, True


def start_attempt(quiz_id, student, *, now=None):
    """Start, or return unchanged, the student's single attempt; ``(attempt, created)``.

    An existing attempt is returned first, even after closing/deadline/expiry
    (its retrieval path never disappears). Otherwise: a student with a legacy
    submission for the quiz has used their attempt (``SubmissionConflict``);
    the quiz must be available at arrival (``QuizUnavailable``). Concurrent
    starts by one student yield one row (unique constraint). Raises
    ``Quiz.DoesNotExist``/``QuizUnavailable``.
    """
    if not is_eligible_student(student):
        raise QuizUnavailable('Only active student accounts can take assessments.')
    with transaction.atomic():
        now = now or server_now()
        quiz = share_lock_quiz(quiz_id)
        existing = Attempt.objects.filter(quiz=quiz, student=student).first()
        if existing is not None:
            return existing, False
        if Submission.objects.filter(quiz=quiz, student=student).exists():
            raise SubmissionConflict('You already have a submission for this quiz.')
        if not quiz_is_available(quiz, now):
            raise QuizUnavailable()
        return create_attempt(quiz, student, now)


def mark_expired(attempt_id, *, now=None):
    """Persist ``expired`` for an in-progress attempt past expiry; returns the attempt.

    Never creates a grade. Safe to call repeatedly or for any attempt.
    """
    with transaction.atomic():
        now = now or server_now()
        attempt = lock_attempt(pk=attempt_id)
        if attempt.status == Attempt.Status.IN_PROGRESS and is_expired(attempt, now):
            attempt.status = Attempt.Status.EXPIRED
            attempt.save(update_fields=['status'])
    return attempt


def _canonical(value):
    # Strict comparison: JSON true/1/1.0 are different answers.
    return json.dumps(value, sort_keys=True)


def replay_or_conflict(submission, answers):
    """Return ``submission`` for an identical answer map, else ``SubmissionConflict``."""
    if _canonical(submission.answers) == _canonical(answers):
        return submission
    raise SubmissionConflict()


def finalize_locked(quiz, attempt, answers, now):
    """Grade and persist a submission for a locked in-progress, unexpired attempt.

    Caller holds the quiz and attempt locks inside ``transaction.atomic`` and
    has already handled replay and expiry. Validates answers
    (``InvalidAnswers``), then inserts the submission and marks the attempt
    submitted at one instant ``now``.
    """
    normalized = QuizService.validate_answers(quiz, answers)
    result = QuizService.grade(quiz, normalized)
    if result.total_points > MAX_STORED_POINTS:
        raise InvalidAnswers({'quiz': ['This quiz cannot be graded until its points are '
                                       'reduced.']}, field=None)
    submission = Submission(
        quiz=quiz, student=attempt.student, attempt=attempt, answers=normalized,
        score=result.score, total_points=result.total_points,
        grading_snapshot=result.snapshot, is_legacy=False, submitted_at=now, graded_at=now)
    submission.save(force_insert=True)
    attempt.status = Attempt.Status.SUBMITTED
    attempt.finalized_at = now
    attempt.save(update_fields=['status', 'finalized_at'])
    return submission


def finalize_attempt(attempt_id, student, answers, *, now=None):
    """Submit answers for the student's attempt; returns ``(submission, created)``.

    Order (D3): ownership (``Attempt.DoesNotExist`` for a foreign/missing
    attempt), then replay of an existing finalization (identical -> returned,
    even after expiry/closing; changed -> ``SubmissionConflict``), then expiry
    at arrival (``AttemptExpired``; the attempt is persisted as expired, no
    grade), then answer validation (``InvalidAnswers``) and atomic grading.
    Closing the quiz does not block an unexpired attempt.
    """
    expired = False
    with transaction.atomic():
        now = now or server_now()  # arrival, before any lock wait
        quiz_id = Attempt.objects.filter(pk=attempt_id, student=student).values_list(
            'quiz_id', flat=True).first()
        if quiz_id is None:
            raise Attempt.DoesNotExist()
        quiz = share_lock_quiz(quiz_id)
        attempt = lock_attempt(pk=attempt_id, student=student)
        existing = Submission.objects.filter(attempt=attempt).first()
        if existing is not None:
            return replay_or_conflict(existing, answers), False
        if attempt.status != Attempt.Status.IN_PROGRESS or is_expired(attempt, now):
            if attempt.status == Attempt.Status.IN_PROGRESS:
                attempt.status = Attempt.Status.EXPIRED
                attempt.save(update_fields=['status'])
            expired = True
        else:
            return finalize_locked(quiz, attempt, answers, now), True
    if expired:
        raise AttemptExpired()
