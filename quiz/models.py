"""Assessment and Arabic class models (D3 domain model).

Lifecycle invariants live in three layers:

* Database constraints (PostgreSQL): valid states, published timing, derived
  ``is_active``, one attempt and one submission per student/quiz, one
  submission per attempt, finalization strictly before expiry, legacy/new
  submission shape, score <= denominator, unique persisted ordering.
* Model guards (``save``/``delete``): status transitions, immutability of
  published content and of finalized attempts/submissions. Bulk
  ``QuerySet.update`` on Quiz/Question/Option bypasses model guards, so release
  code goes through ``quiz.lifecycle``; Submission/Attempt querysets refuse
  bulk update/delete outright.
* ``quiz.lifecycle`` services: publication validation, locking order and
  server-clock timing (A10). ASSESS-03/04 build on those, not on raw writes.

Foreign keys that would cascade history (quiz -> event/course, attempts and
submissions -> quiz/student/attempt) use ``PROTECT``.
"""
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import connection, models
from django.db.models import F, Q
from django.utils import timezone

from accounts.models import CustomUser
from dawah.models import Event

from .errors import InvalidTransition, QuizImmutable, SubmissionImmutable

MIN_TIME_LIMIT = 1
MAX_TIME_LIMIT = 180  # minutes, D3 engineering limit
# Submission.score / total_points are PostgreSQL ``integer`` (PositiveIntegerField).
MAX_STORED_POINTS = 2147483647


class Course(models.Model):
    code = models.CharField(max_length=7)
    title = models.CharField(max_length=100)
    department = models.CharField(max_length=100)
    level = models.CharField(max_length=3, choices=CustomUser.LEVEL_CHOICES)
    semester = models.CharField(max_length=10)

    def __str__(self):
        return self.title


class Enrollment(models.Model):
    course = models.ForeignKey(to=Course, on_delete=models.CASCADE)
    student = models.ForeignKey(to=settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    class Meta:
        unique_together = ["course", "student"]

    def __str__(self):
        return f"{self.student.email} -> {self.course.title}"


def _quiz_status_for_write(quiz_id):
    """Current DB status of a quiz, row-locked when inside a transaction.

    The ``FOR NO KEY UPDATE`` lock serializes child writes with publish/close
    (``FOR UPDATE``), so a question cannot slip into a quiz while it publishes.
    """
    queryset = Quiz.objects.filter(pk=quiz_id)
    if connection.in_atomic_block:
        queryset = queryset.select_for_update(no_key=True)
    return queryset.values_list('status', flat=True).first()


def _assert_draft_quiz(*quiz_ids):
    for quiz_id in {qid for qid in quiz_ids if qid is not None}:
        status = _quiz_status_for_write(quiz_id)
        if status is not None and status != Quiz.Status.DRAFT:
            raise QuizImmutable('Questions and options of a published or closed quiz '
                                'cannot be added, changed or removed.')


class Quiz(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        PUBLISHED = 'published', 'Published'
        CLOSED = 'closed', 'Closed'

    # D3: draft -> published -> closed; closed is terminal. Same-state saves are
    # no-ops (e.g. metadata corrections).
    TRANSITIONS = {
        Status.DRAFT: {Status.DRAFT, Status.PUBLISHED},
        Status.PUBLISHED: {Status.PUBLISHED, Status.CLOSED},
        Status.CLOSED: {Status.CLOSED},
    }
    # Fields that may change after publication (D3 metadata corrections).
    METADATA_FIELDS = ('title', 'description')
    # Grading/timing/association fields frozen once the quiz leaves draft.
    LOCKED_FIELDS = ('type', 'time_limit', 'deadline', 'related_event', 'related_course')

    title = models.CharField(max_length=250)
    # Legacy metadata; new release drafts default to "arabic". No eligibility rule uses it.
    type = models.CharField(max_length=250, default='arabic')
    description = models.CharField(max_length=250)

    related_event = models.ForeignKey(Event, null=True, blank=True, on_delete=models.PROTECT)
    related_course = models.ForeignKey(Course, null=True, blank=True, on_delete=models.PROTECT)

    time_limit = models.PositiveSmallIntegerField(
        null=True, help_text="Time limit in minutes",
        validators=[MinValueValidator(MIN_TIME_LIMIT), MaxValueValidator(MAX_TIME_LIMIT)])
    deadline = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=9, choices=Status, default=Status.DRAFT)
    # Derived from status (published <=> True) on every save and by a DB check.
    # Kept for compatibility; never an independent writable switch.
    is_active = models.BooleanField(default=False, editable=False)
    published_at = models.DateTimeField(null=True, blank=True, editable=False)
    closed_at = models.DateTimeField(null=True, blank=True, editable=False)
    # Null for rows that existed before ASSESS-02 (their times are unknown).
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    updated_at = models.DateTimeField(auto_now=True, null=True)
    # The pre-ASSESS-02 is_active value of legacy rows (null for newer quizzes):
    # audit trail of the draft migration and the source of its reversal.
    legacy_is_active = models.BooleanField(null=True, blank=True, editable=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=['draft', 'published', 'closed']),
                name='quiz_status_valid'),
            models.CheckConstraint(
                condition=(Q(status='published', is_active=True)
                           | (~Q(status='published') & Q(is_active=False))),
                name='quiz_is_active_matches_status'),
            models.CheckConstraint(
                condition=(Q(status='draft')
                           | Q(time_limit__gte=MIN_TIME_LIMIT, time_limit__lte=MAX_TIME_LIMIT,
                               deadline__isnull=False, published_at__isnull=False)),
                name='quiz_released_timing_complete'),
            models.CheckConstraint(
                condition=(Q(status='closed', closed_at__isnull=False)
                           | (~Q(status='closed') & Q(closed_at__isnull=True))),
                name='quiz_closed_at_matches_status'),
        ]

    def __str__(self):
        return self.title

    @property
    def is_draft(self):
        return self.status == self.Status.DRAFT

    def save(self, *args, **kwargs):
        self.is_active = self.status == self.Status.PUBLISHED
        update_fields = kwargs.get('update_fields')
        if update_fields is not None and 'status' in update_fields:
            kwargs['update_fields'] = set(update_fields) | {'is_active'}
        self._check_lifecycle(update_fields)
        super().save(*args, **kwargs)

    def _check_lifecycle(self, update_fields):
        attnames = {name: self._meta.get_field(name).attname for name in self.LOCKED_FIELDS}
        current = None
        if self.pk is not None and not self._state.adding:
            current = Quiz.objects.filter(pk=self.pk).values('status', *attnames.values()).first()
        if current is None:
            if self.status != self.Status.DRAFT:
                raise InvalidTransition('A quiz is created as a draft and published through '
                                        'the publication service.')
            return
        if self.status not in self.TRANSITIONS[current['status']]:
            raise InvalidTransition(f'A {current["status"]} quiz cannot become {self.status}.')
        if current['status'] == self.Status.DRAFT:
            return
        changed = [name for name, attname in attnames.items()
                   if (update_fields is None or name in update_fields
                       or attname in update_fields)
                   and getattr(self, attname) != current[attname]]
        if changed:
            raise QuizImmutable(
                'Only the title and description of a published or closed quiz can change.',
                fields={name: ['This field cannot change after publication.']
                        for name in changed})

    def delete(self, *args, **kwargs):
        status = Quiz.objects.filter(pk=self.pk).values_list('status', flat=True).first()
        if status is not None and status != self.Status.DRAFT:
            raise QuizImmutable('Only a draft quiz can be deleted; close it instead.')
        # Attempts/submissions PROTECT the quiz (ProtectedError).
        return super().delete(*args, **kwargs)


class Question(models.Model):
    class Type(models.TextChoices):
        mcq = "mcq", "MULTIPLE_CHOICE"
        multi = "multi", "MULTIPLE_ANSWER"
        text = "text", "TEXT"

    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE)
    type = models.CharField(max_length=5, choices=Type)
    text = models.TextField()
    points = models.PositiveSmallIntegerField(default=1)
    # 1-based position within the quiz; display/grading order is (order, id).
    order = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['quiz', 'order'], name='quiz_question_order_unique',
                                    deferrable=models.Deferrable.DEFERRED),
            models.CheckConstraint(condition=Q(order__gte=1), name='quiz_question_order_positive'),
        ]

    def __str__(self):
        return self.text

    def save(self, *args, **kwargs):
        previous_quiz = None
        if self.pk is not None and not self._state.adding:
            previous_quiz = Question.objects.filter(pk=self.pk).values_list(
                'quiz_id', flat=True).first()
        _assert_draft_quiz(self.quiz_id, previous_quiz)
        if self.order is None:
            last = Question.objects.filter(quiz_id=self.quiz_id).aggregate(
                last=models.Max('order'))['last']
            self.order = (last or 0) + 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        _assert_draft_quiz(self.quiz_id)
        return super().delete(*args, **kwargs)


class Option(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE)

    text = models.CharField(max_length=250)
    is_correct = models.BooleanField(default=False)
    # 1-based position within the question; display order is (order, id).
    order = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['question', 'order'], name='quiz_option_order_unique',
                                    deferrable=models.Deferrable.DEFERRED),
            models.CheckConstraint(condition=Q(order__gte=1), name='quiz_option_order_positive'),
        ]

    def __str__(self):
        return self.text

    def _quiz_ids(self):
        ids = [Question.objects.filter(pk=self.question_id).values_list('quiz_id', flat=True)
               .first()]
        if self.pk is not None and not self._state.adding:
            ids.append(Option.objects.filter(pk=self.pk).values_list(
                'question__quiz_id', flat=True).first())
        return ids

    def save(self, *args, **kwargs):
        _assert_draft_quiz(*self._quiz_ids())
        if self.order is None:
            last = Option.objects.filter(question_id=self.question_id).aggregate(
                last=models.Max('order'))['last']
            self.order = (last or 0) + 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        _assert_draft_quiz(*self._quiz_ids())
        return super().delete(*args, **kwargs)


class HistoryQuerySet(models.QuerySet):
    """Attempts and submissions are history: no bulk deletion."""

    def delete(self):
        raise SubmissionImmutable('Assessment attempts and submissions are never deleted.')


class SubmissionQuerySet(HistoryQuerySet):

    def update(self, **kwargs):
        raise SubmissionImmutable()


class Attempt(models.Model):
    """A student's single timed attempt at a quiz (A09/A10).

    Server-owned ``started_at``; ``expires_at = min(started_at + time_limit,
    deadline)`` fixed at start and never extended (closing does not shorten it).
    """

    class Status(models.TextChoices):
        IN_PROGRESS = 'in_progress', 'In progress'
        SUBMITTED = 'submitted', 'Submitted'
        EXPIRED = 'expired', 'Expired'

    TRANSITIONS = {
        Status.IN_PROGRESS: {Status.IN_PROGRESS, Status.SUBMITTED, Status.EXPIRED},
        Status.SUBMITTED: {Status.SUBMITTED},
        Status.EXPIRED: {Status.EXPIRED},
    }
    FROZEN_FIELDS = ('quiz_id', 'student_id', 'started_at', 'expires_at')

    quiz = models.ForeignKey(Quiz, on_delete=models.PROTECT, related_name='attempts')
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                related_name='quiz_attempts')
    status = models.CharField(max_length=11, choices=Status, default=Status.IN_PROGRESS)
    started_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    # Set exactly when the attempt is submitted; null while in progress or expired.
    finalized_at = models.DateTimeField(null=True, blank=True)

    objects = HistoryQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['quiz', 'student'], name='quiz_attempt_one_per_student'),
            models.CheckConstraint(
                condition=Q(status__in=['in_progress', 'submitted', 'expired']),
                name='quiz_attempt_status_valid'),
            models.CheckConstraint(condition=Q(expires_at__gt=F('started_at')),
                                   name='quiz_attempt_expires_after_start'),
            models.CheckConstraint(
                condition=(Q(status='submitted', finalized_at__isnull=False,
                             finalized_at__gte=F('started_at'), finalized_at__lt=F('expires_at'))
                           | (~Q(status='submitted') & Q(finalized_at__isnull=True))),
                name='quiz_attempt_finalized_before_expiry'),
        ]

    def __str__(self):
        return f'Attempt {self.pk} ({self.status})'

    def save(self, *args, **kwargs):
        if self.pk is not None and not self._state.adding:
            current = Attempt.objects.filter(pk=self.pk).values(
                'status', 'finalized_at', *self.FROZEN_FIELDS).first()
            if current is not None:
                if any(getattr(self, name) != current[name] for name in self.FROZEN_FIELDS):
                    raise QuizImmutable("An attempt's quiz, student and timing are fixed at start.")
                if current['status'] != self.Status.IN_PROGRESS and (
                        self.status != current['status']
                        or self.finalized_at != current['finalized_at']):
                    raise QuizImmutable('A submitted or expired attempt cannot change.')
                if self.status not in self.TRANSITIONS[current['status']]:
                    raise InvalidTransition()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise SubmissionImmutable('Assessment attempts are never deleted.')


class Submission(models.Model):
    """A finalized, graded result. Insert-only: never updated or deleted.

    Release rows (``is_legacy=False``) always have an attempt, ``total_points``,
    ``graded_at`` and a ``grading_snapshot`` (see ``quiz.lifecycle``). Rows
    that existed before ASSESS-02 are ``is_legacy=True`` with no attempt and
    null denominator/snapshot: their grades are preserved, not reconstructed.
    """

    quiz = models.ForeignKey(Quiz, on_delete=models.PROTECT)
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    attempt = models.OneToOneField(Attempt, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name='submission')

    answers = models.JSONField()
    score = models.PositiveIntegerField()
    total_points = models.PositiveIntegerField(null=True, blank=True)
    grading_snapshot = models.JSONField(null=True, blank=True)
    is_legacy = models.BooleanField(default=False)
    # Server-set by quiz.lifecycle (same instant as graded_at and the attempt's
    # finalized_at); the default only covers non-service inserts.
    submitted_at = models.DateTimeField(default=timezone.now)
    graded_at = models.DateTimeField(blank=True, null=True)

    objects = SubmissionQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['quiz', 'student'],
                                    name='quiz_submission_one_per_student'),
            models.CheckConstraint(
                condition=(Q(is_legacy=True, attempt__isnull=True)
                           | Q(is_legacy=False, attempt__isnull=False, total_points__isnull=False,
                               grading_snapshot__isnull=False, graded_at__isnull=False)),
                name='quiz_submission_release_shape'),
            models.CheckConstraint(
                condition=Q(total_points__isnull=True) | Q(score__lte=F('total_points')),
                name='quiz_submission_score_within_total'),
        ]

    def save(self, *args, **kwargs):
        if self.pk is not None and not self._state.adding:
            raise SubmissionImmutable()
        if self.attempt_id is not None:
            owner = Attempt.objects.filter(pk=self.attempt_id).values_list(
                'quiz_id', 'student_id').first()
            if owner != (self.quiz_id, self.student_id):
                raise ValueError('A submission must belong to its attempt\'s quiz and student.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise SubmissionImmutable()
