"""Assessment and Arabic class models (D3 domain model).

Lifecycle invariants live in four layers:

* Database constraints (PostgreSQL): valid states, published timing, derived
  ``is_active``, one attempt and one submission per student/quiz, one
  submission per attempt, finalization strictly before expiry, legacy/new
  submission shape, score <= denominator, unique persisted ordering.
* Database triggers (``quiz.0007``, SQLSTATE ``23Q01``/``23Q02``, raised as
  ``IntegrityError``): question/option content of a published/closed quiz or
  of any quiz with attempts/submissions cannot be inserted, updated or
  deleted; a non-draft quiz's frozen columns and status (except
  published -> closed) cannot change; attempt identity/timing and finalized
  attempts cannot change; submissions are insert-only, must match their
  attempt's quiz/student, and new ``is_legacy`` rows are refused; attempts are
  never deleted. These hold for every path, including bulk ORM calls and raw
  SQL.
* Model and queryset guards: the same rules as clear Python errors
  (``quiz.errors``) before SQL runs, including ``bulk_create``/``bulk_update``/
  ``update``/``delete``. Quiz status changes only through
  ``quiz.lifecycle.publish_quiz``/``close_quiz`` (an internal token).
* ``quiz.lifecycle`` services: publication validation, locking and
  server-clock timing (A10). ASSESS-03/04 build on those, not on raw writes.

Foreign keys that would cascade history (quiz -> event/course, attempts and
submissions -> quiz/student/attempt, enrollments -> course/student, module
attachments -> resource) use ``PROTECT``.
"""
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models import F, Q
from django.utils import timezone

from accounts.models import CustomUser
from dawah.models import Event, EventResource

from .errors import InvalidTransition, QuizImmutable, SubmissionImmutable

MIN_TIME_LIMIT = 1
MAX_TIME_LIMIT = 180  # minutes, D3 engineering limit
# Submission.score / total_points are PostgreSQL ``integer`` (PositiveIntegerField).
MAX_STORED_POINTS = 2147483647

CONTENT_LOCKED_MESSAGE = ('Questions and options cannot be added, changed, reordered or removed '
                          'once a quiz is published or has attempts or submissions; author a new '
                          'quiz instead.')


class Course(models.Model):
    """A course record; ``kind="arabic"`` rows are the Phase 1 Arabic classes (D3).

    Rows that existed before ASSESS-07 are ``general`` (legacy/general
    academic): they are kept unchanged and never surfaced by the class API.
    Arabic class lifecycle: draft -> published -> closed (terminal), changed
    only by ``quiz.learning.publish_class``/``close_class``.
    """

    class Kind(models.TextChoices):
        ARABIC = 'arabic', 'Arabic class'
        GENERAL = 'general', 'General academic (legacy)'

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        PUBLISHED = 'published', 'Published'
        CLOSED = 'closed', 'Closed'

    code = models.CharField(max_length=7)
    title = models.CharField(max_length=100)
    # Department/level/semester describe general academic courses; an Arabic
    # class leaves them blank (no proficiency/department gate is invented).
    department = models.CharField(max_length=100, blank=True, default='')
    level = models.CharField(max_length=3, choices=CustomUser.LEVEL_CHOICES, blank=True,
                             default='')
    semester = models.CharField(max_length=10, blank=True, default='')

    kind = models.CharField(max_length=7, choices=Kind, default=Kind.GENERAL)
    description = models.TextField(blank=True, default='')
    status = models.CharField(max_length=9, choices=Status, default=Status.DRAFT)
    published_at = models.DateTimeField(null=True, blank=True, editable=False)
    closed_at = models.DateTimeField(null=True, blank=True, editable=False)
    # Null for rows that existed before ASSESS-07 (their times are unknown).
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    updated_at = models.DateTimeField(auto_now=True, null=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(kind__in=['arabic', 'general']),
                                   name='quiz_course_kind_valid'),
            models.CheckConstraint(condition=Q(status__in=['draft', 'published', 'closed']),
                                   name='quiz_course_status_valid'),
            models.CheckConstraint(
                condition=Q(status='draft') | Q(published_at__isnull=False),
                name='quiz_course_released_has_published_at'),
            models.CheckConstraint(
                condition=(Q(status='closed', closed_at__isnull=False)
                           | (~Q(status='closed') & Q(closed_at__isnull=True))),
                name='quiz_course_closed_at_matches_status'),
        ]

    def __str__(self):
        return self.title


class Enrollment(models.Model):
    """One record per (course, student); withdrawal and re-enrollment change its
    status, never create a second row (D3). Rows from before ASSESS-07 are
    ``active`` with unknown (null) timestamps."""

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        WITHDRAWN = 'withdrawn', 'Withdrawn'

    # PROTECT: deleting a class or a user never silently drops roster history.
    course = models.ForeignKey(to=Course, on_delete=models.PROTECT)
    student = models.ForeignKey(to=settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    status = models.CharField(max_length=9, choices=Status, default=Status.ACTIVE)
    # Most recent (re)activation; null for legacy rows.
    enrolled_at = models.DateTimeField(null=True, blank=True)
    # Most recent withdrawal; set exactly while withdrawn.
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    updated_at = models.DateTimeField(auto_now=True, null=True)

    class Meta:
        unique_together = ["course", "student"]
        constraints = [
            models.CheckConstraint(condition=Q(status__in=['active', 'withdrawn']),
                                   name='quiz_enrollment_status_valid'),
            models.CheckConstraint(
                condition=(Q(status='withdrawn', withdrawn_at__isnull=False)
                           | Q(status='active', withdrawn_at__isnull=True)),
                name='quiz_enrollment_withdrawn_at_matches_status'),
        ]

    def __str__(self):
        return f"{self.student.email} -> {self.course.title}"


class LearningModule(models.Model):
    """An ordered digital learning module of an Arabic class (D3).

    Plain-text ``body`` plus attached published Da'wah resources (by ID, via
    ``ModuleResource``). ``order`` is unique per class (deferred constraint, so
    an atomic reorder can permute it). Students read a module only while it and
    its class are published and they hold an active enrollment.
    """

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        PUBLISHED = 'published', 'Published'

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='modules')
    title = models.CharField(max_length=250)
    body = models.TextField(blank=True, default='')
    order = models.PositiveIntegerField()
    status = models.CharField(max_length=9, choices=Status, default=Status.DRAFT)
    published_at = models.DateTimeField(null=True, blank=True, editable=False)
    resources = models.ManyToManyField(EventResource, through='ModuleResource',
                                       related_name='learning_modules', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['course', 'order'], name='quiz_module_order_unique',
                                    deferrable=models.Deferrable.DEFERRED),
            models.CheckConstraint(condition=Q(order__gte=1), name='quiz_module_order_positive'),
            models.CheckConstraint(condition=Q(status__in=['draft', 'published']),
                                   name='quiz_module_status_valid'),
        ]

    def __str__(self):
        return self.title


class ModuleResource(models.Model):
    """Attachment of a Da'wah resource to a module, in ``position`` order.

    ``resource`` is PROTECT: an attached resource (or its event) cannot be
    deleted; detach it or unpublish it instead. Deleting a module removes its
    attachments only.
    """

    module = models.ForeignKey(LearningModule, on_delete=models.CASCADE,
                               related_name='attachments')
    resource = models.ForeignKey(EventResource, on_delete=models.PROTECT, related_name='+')
    position = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['module', 'resource'],
                                    name='quiz_module_resource_unique'),
            models.UniqueConstraint(fields=['module', 'position'],
                                    name='quiz_module_resource_position_unique',
                                    deferrable=models.Deferrable.DEFERRED),
        ]


def quiz_content_locked(quiz_id):
    """True when a quiz's questions/options are frozen (D3 + lead decision M4).

    Locked once the quiz is not a draft, or as soon as it has any attempt or
    submission (legacy included), even while it is a draft.
    """
    status = Quiz.objects.filter(pk=quiz_id).values_list('status', flat=True).first()
    if status is None:
        return False
    return (status != Quiz.Status.DRAFT
            or Attempt.objects.filter(quiz_id=quiz_id).exists()
            or Submission.objects.filter(quiz_id=quiz_id).exists())


def assert_content_editable(*quiz_ids):
    """Raise ``QuizImmutable`` unless every quiz's content may change.

    Opens its own transaction and takes ``FOR NO KEY UPDATE`` on each quiz row
    (in ID order), which conflicts with publish/close (``FOR UPDATE``) and with
    attempt start/finalize (``FOR SHARE``), so content cannot change while a
    quiz publishes or an attempt starts. Callers that then write must stay in
    the same transaction (the guards below do).
    """
    with transaction.atomic():
        for quiz_id in sorted({qid for qid in quiz_ids if qid is not None}):
            Quiz.objects.select_for_update(no_key=True).filter(pk=quiz_id).values_list(
                'pk', flat=True).first()
            if quiz_content_locked(quiz_id):
                raise QuizImmutable(CONTENT_LOCKED_MESSAGE)


class QuizQuerySet(models.QuerySet):
    """Bulk quiz updates cannot change lifecycle state or frozen published fields."""

    LIFECYCLE_KEYS = {'status', 'is_active', 'published_at', 'closed_at', 'legacy_is_active'}

    def update(self, **kwargs):
        direct = sorted(self.LIFECYCLE_KEYS & set(kwargs))
        if direct:
            raise InvalidTransition('Quiz status changes only through publish/close.',
                                    fields={name: ['Use the publish or close action.']
                                            for name in direct})
        locked = sorted({name.removesuffix('_id') for name in kwargs}
                        & set(Quiz.LOCKED_FIELDS))
        if locked and self.exclude(status=Quiz.Status.DRAFT).exists():
            raise QuizImmutable(
                'Only the title and description of a published or closed quiz can change.',
                fields={name: ['This field cannot change after publication.'] for name in locked})
        return super().update(**kwargs)


class Quiz(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        PUBLISHED = 'published', 'Published'
        CLOSED = 'closed', 'Closed'

    # D3: draft -> published -> closed; closed is terminal. Only these two
    # transitions exist, and only quiz.lifecycle may perform them.
    TRANSITIONS = {(Status.DRAFT, Status.PUBLISHED), (Status.PUBLISHED, Status.CLOSED)}
    # Fields that may change after publication (D3 metadata corrections).
    METADATA_FIELDS = ('title', 'description')
    # Grading/timing/association fields frozen once the quiz leaves draft.
    LOCKED_FIELDS = ('type', 'time_limit', 'deadline', 'related_event', 'related_course')
    # Written only by publish_quiz/close_quiz.
    LIFECYCLE_FIELDS = ('status', 'published_at', 'closed_at')

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

    objects = QuizQuerySet.as_manager()

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
        # Single-use token set by quiz.lifecycle right before a transition save.
        token = self.__dict__.pop('_lifecycle_transition', None)
        self.is_active = self.status == self.Status.PUBLISHED
        update_fields = kwargs.get('update_fields')
        if update_fields is not None and 'status' in update_fields:
            kwargs['update_fields'] = set(update_fields) | {'is_active'}
        self._check_lifecycle(update_fields, token)
        super().save(*args, **kwargs)

    def _attname(self, name):
        return self._meta.get_field(name).attname

    def _check_lifecycle(self, update_fields, token):
        attnames = [self._attname(name) for name in (*self.LOCKED_FIELDS, *self.LIFECYCLE_FIELDS)]
        current = None
        if self.pk is not None and not self._state.adding:
            current = Quiz.objects.filter(pk=self.pk).values(*attnames).first()
        if current is None:
            if (self.status, self.published_at, self.closed_at) != (self.Status.DRAFT, None, None):
                raise InvalidTransition('A quiz is created as a draft and published through '
                                        'the publication service.')
            return

        def written(name):
            return update_fields is None or name in update_fields

        lifecycle_changed = [name for name in self.LIFECYCLE_FIELDS
                             if written(name) and getattr(self, name) != current[name]]
        if lifecycle_changed:
            transition = (current['status'], self.status)
            if transition not in self.TRANSITIONS or token != transition:
                raise InvalidTransition(
                    'Quiz status, published_at and closed_at change only through the '
                    'publish/close actions.',
                    fields={name: ['Use the publish or close action.']
                            for name in lifecycle_changed})
        if current['status'] == self.Status.DRAFT:
            return
        changed = [name for name in self.LOCKED_FIELDS
                   if (written(name) or written(self._attname(name)))
                   and getattr(self, self._attname(name)) != current[self._attname(name)]]
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


class ContentQuerySet(models.QuerySet):
    """Bulk question/option writes obey the same content lock as ``save``/``delete``.

    ``quiz_path`` is the lookup from the model to its quiz ID.
    """

    quiz_path = 'quiz_id'

    def _quiz_ids(self):
        return set(self.values_list(self.quiz_path, flat=True))

    def _object_quiz_ids(self, objs):
        raise NotImplementedError

    def update(self, **kwargs):
        with transaction.atomic():
            ids = self._quiz_ids() | self._target_quiz_ids(kwargs)
            assert_content_editable(*ids)
            return super().update(**kwargs)

    def _target_quiz_ids(self, kwargs):
        return set()

    def delete(self):
        with transaction.atomic():
            assert_content_editable(*self._quiz_ids())
            return super().delete()

    def bulk_create(self, objs, *args, **kwargs):
        objs = list(objs)
        with transaction.atomic():
            assert_content_editable(*self._object_quiz_ids(objs))
            return super().bulk_create(objs, *args, **kwargs)

    def bulk_update(self, objs, fields, *args, **kwargs):
        objs = list(objs)
        with transaction.atomic():
            ids = self._object_quiz_ids(objs) | self.filter(
                pk__in=[obj.pk for obj in objs])._quiz_ids()
            assert_content_editable(*ids)
            return super().bulk_update(objs, fields, *args, **kwargs)


class QuestionQuerySet(ContentQuerySet):
    quiz_path = 'quiz_id'

    def _object_quiz_ids(self, objs):
        return {obj.quiz_id for obj in objs}

    def _target_quiz_ids(self, kwargs):
        target = kwargs.get('quiz', kwargs.get('quiz_id'))
        return {getattr(target, 'pk', target)} if target is not None else set()


class OptionQuerySet(ContentQuerySet):
    quiz_path = 'question__quiz_id'

    def _object_quiz_ids(self, objs):
        return set(Question.objects.filter(pk__in={obj.question_id for obj in objs})
                   .values_list('quiz_id', flat=True))

    def _target_quiz_ids(self, kwargs):
        target = kwargs.get('question', kwargs.get('question_id'))
        if target is None:
            return set()
        return set(Question.objects.filter(pk=getattr(target, 'pk', target))
                   .values_list('quiz_id', flat=True))


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

    objects = QuestionQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['quiz', 'order'], name='quiz_question_order_unique',
                                    deferrable=models.Deferrable.DEFERRED),
            models.CheckConstraint(condition=Q(order__gte=1), name='quiz_question_order_positive'),
        ]

    def __str__(self):
        return self.text

    def save(self, *args, **kwargs):
        with transaction.atomic():
            previous_quiz = None
            if self.pk is not None and not self._state.adding:
                previous_quiz = Question.objects.filter(pk=self.pk).values_list(
                    'quiz_id', flat=True).first()
            assert_content_editable(self.quiz_id, previous_quiz)
            if self.order is None:
                last = Question.objects.filter(quiz_id=self.quiz_id).aggregate(
                    last=models.Max('order'))['last']
                self.order = (last or 0) + 1
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            assert_content_editable(self.quiz_id)
            return super().delete(*args, **kwargs)


class Option(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE)

    text = models.CharField(max_length=250)
    is_correct = models.BooleanField(default=False)
    # 1-based position within the question; display order is (order, id).
    order = models.PositiveIntegerField()

    objects = OptionQuerySet.as_manager()

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
        with transaction.atomic():
            assert_content_editable(*self._quiz_ids())
            if self.order is None:
                last = Option.objects.filter(question_id=self.question_id).aggregate(
                    last=models.Max('order'))['last']
                self.order = (last or 0) + 1
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            assert_content_editable(*self._quiz_ids())
            return super().delete(*args, **kwargs)


class AttemptQuerySet(models.QuerySet):
    """Attempts are history: no bulk update or deletion (use quiz.lifecycle)."""

    def update(self, **kwargs):
        raise QuizImmutable('Attempts change only through quiz.lifecycle.')

    def delete(self):
        raise SubmissionImmutable('Assessment attempts are never deleted.')


class SubmissionQuerySet(models.QuerySet):

    def update(self, **kwargs):
        raise SubmissionImmutable()

    def delete(self):
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

    objects = AttemptQuerySet.as_manager()

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

    Release rows (``is_legacy=False``) always have an attempt of the same
    quiz/student, ``total_points``, ``graded_at`` and a ``grading_snapshot``
    (see ``quiz.lifecycle``). Rows that existed before ASSESS-02 are
    ``is_legacy=True`` with no attempt and null denominator/snapshot: their
    grades are preserved, not reconstructed. No new legacy rows can be created.
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
        if self.is_legacy:
            raise SubmissionImmutable('Legacy submissions are migrated, never created.')
        if self.attempt_id is not None:
            owner = Attempt.objects.filter(pk=self.attempt_id).values_list(
                'quiz_id', 'student_id').first()
            if owner != (self.quiz_id, self.student_id):
                raise ValueError('A submission must belong to its attempt\'s quiz and student.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise SubmissionImmutable()
