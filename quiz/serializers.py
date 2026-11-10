"""Assessment serializers, split by audience.

``Admin*`` serializers may contain correctness keys (``is_correct``) and other
students' data; they are only used behind ``IsActiveAdmin``/``is_admin``.
``Student*`` serializers never contain ``is_correct`` or any other answer key,
at any nesting depth (D0 A12, D3). ``QuizListSerializer`` and
``CourseSerializer`` carry no keys and are shared.

Release behaviour is MCQ-only: student payloads include only MCQ questions and
authoring accepts only ``type="mcq"``. Legacy text/multi-answer questions stay
readable to admins.

Questions/options are listed in persisted ``order`` (ID tie-break). Quiz
``status``/``is_active``/``published_at``/``closed_at`` are read-only here and
change only through the publish/close actions. Write serializers
(``AdminQuizWriteSerializer``, ``AdminQuestionWriteSerializer``) reject
unknown keys and only validate structure; ``quiz.authoring``/``quiz.lifecycle``
apply the rules that need the quiz lock (409 ``QUIZ_IMMUTABLE``).
"""
from collections.abc import Mapping

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from dawah.models import Event

from .lifecycle import OPTIONS_PER_QUESTION, assert_quiz_changes_allowed, normalized_text
from .models import (
    MAX_TIME_LIMIT, MIN_TIME_LIMIT, Attempt, Course, Enrollment, Option, Question, Quiz,
    Submission, quiz_content_locked,
)
from .permissions import is_admin


class RejectUnknownFieldsMixin:
    """Reject request keys the serializer does not accept, e.g. ``student``/``score``."""

    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            writable = {name for name, field in self.fields.items() if not field.read_only}
            unknown = sorted(str(key) for key in data if key not in writable)
            if unknown:
                raise serializers.ValidationError(
                    {key: ['This field cannot be set or changed here.'] for key in unknown})
        return super().to_internal_value(data)


# PostgreSQL limits of Question.points (smallint) and the order columns (integer).
MAX_QUESTION_POINTS = 32767
MAX_ORDER = 2147483647


def _ordered(queryset):
    """Persisted order with ID tie-break (deterministic for every audience)."""
    return queryset.order_by('order', 'pk')


class CourseSerializer(serializers.ModelSerializer):
    """Serializer for Course model"""

    class Meta:
        model = Course
        fields = ['id', 'code', 'title', 'department', 'level', 'semester']


class EnrollmentSerializer(serializers.ModelSerializer):
    """Serializer for Enrollment model (not routed in this release; see ASSESS-07)."""
    course_details = CourseSerializer(source='course', read_only=True)
    student_email = serializers.EmailField(source='student.email', read_only=True)

    class Meta:
        model = Enrollment
        fields = ['id', 'course', 'student', 'course_details', 'student_email']
        read_only_fields = ['id']

    def validate(self, data):
        """Validate unique_together constraint"""
        if Enrollment.objects.filter(
            course=data.get('course'),
            student=data.get('student')
        ).exists():
            raise serializers.ValidationError(
                "This student is already enrolled in this course."
            )
        return data


# ----------------------------------------------------------------------------
# Strict JSON input fields
# ----------------------------------------------------------------------------

class StrictIntegerField(serializers.IntegerField):
    """JSON integer only: rejects strings ("1"), floats (1.0) and booleans."""

    def to_internal_value(self, data):
        if type(data) is not int:
            self.fail('invalid')
        return super().to_internal_value(data)


class StrictBooleanField(serializers.BooleanField):
    """JSON ``true``/``false`` only (no "true", 1 or "yes")."""

    def to_internal_value(self, data):
        if type(data) is not bool:
            self.fail('invalid', input=data)
        return data


@extend_schema_field({'type': 'integer', 'nullable': True})
class StrictPrimaryKeyRelatedField(serializers.PrimaryKeyRelatedField):
    """Integer primary key only (a boolean would otherwise match pk=1)."""

    def to_internal_value(self, data):
        if type(data) is not int:
            self.fail('incorrect_type', data_type=type(data).__name__)
        return super().to_internal_value(data)


# ----------------------------------------------------------------------------
# Admin serializers (may contain answer keys)
# ----------------------------------------------------------------------------

class AdminOptionSerializer(serializers.ModelSerializer):
    """Option including its correctness key. Admin only (read)."""

    class Meta:
        model = Option
        fields = ['id', 'text', 'is_correct', 'order']
        read_only_fields = fields


class AdminQuestionSerializer(serializers.ModelSerializer):
    """Question with options and keys. Admin only (read)."""
    options = serializers.SerializerMethodField()

    class Meta:
        model = Question
        fields = ['id', 'quiz', 'type', 'text', 'points', 'order', 'options']
        read_only_fields = fields

    @extend_schema_field(AdminOptionSerializer(many=True))
    def get_options(self, obj):
        return AdminOptionSerializer(_ordered(obj.option_set.all()), many=True).data


@extend_schema_field({'type': 'string', 'enum': [Question.Type.mcq.value]})
class ReleaseQuestionTypeField(serializers.ChoiceField):
    """Authoring accepts only the release MCQ type (A08).

    Optional and without a field default: a create defaults to ``mcq`` in
    ``AdminQuestionWriteSerializer.validate``; an update never fills it in.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault('required', False)
        super().__init__(choices=[(Question.Type.mcq.value, Question.Type.mcq.label)], **kwargs)


class AuthoringOptionSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    """One option of the D3 question body. Option IDs are never accepted."""
    text = serializers.CharField(max_length=250)
    is_correct = StrictBooleanField(required=False, default=False)
    order = StrictIntegerField(required=False, min_value=1, max_value=MAX_ORDER)


class AdminQuestionWriteSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    """D3 question body: ``{"type","text","points","order","options":[...]}``. Admin only.

    Validation only; ``quiz.authoring`` performs the locked nested write.
    Create requires ``options``; an update replaces the option set only when
    ``options`` is sent. Options: exactly four, non-empty, distinct after
    trim + whitespace collapse + casefold, exactly one ``is_correct``; option
    ``order`` is optional (missing ones fill the free positions) and unique.
    ``points`` is a positive integer (default 1); question ``order`` is
    optional (appended) and unique in the quiz (checked under the quiz lock).

    ``context['quiz_field']``: ``'required'`` (legacy ``POST /quiz/question/``
    names its quiz), ``'same'`` (updates: accepted only when unchanged; a
    question never moves) or absent (the quiz comes from the URL; ``quiz`` is
    an unknown key).
    """
    quiz = StrictPrimaryKeyRelatedField(queryset=Quiz.objects.all(), required=False)
    type = ReleaseQuestionTypeField()
    text = serializers.CharField()
    points = StrictIntegerField(required=False, min_value=1, max_value=MAX_QUESTION_POINTS)
    order = StrictIntegerField(required=False, min_value=1, max_value=MAX_ORDER)
    options = AuthoringOptionSerializer(many=True, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        mode = self.context.get('quiz_field')
        if mode is None:
            self.fields.pop('quiz')
        elif mode == 'required':
            self.fields['quiz'].required = True
        if self.instance is not None and self.context.get('patch'):
            # PATCH: every top-level key is optional, but nested options stay
            # fully validated (DRF ``partial`` would also skip their required keys).
            for field in self.fields.values():
                field.required = False

    def validate_options(self, options):
        problems = []
        if len(options) != OPTIONS_PER_QUESTION:
            problems.append(f'Exactly {OPTIONS_PER_QUESTION} options are required.')
        if sum(1 for option in options if option['is_correct']) != 1:
            problems.append('Exactly one option must be correct.')
        texts = [normalized_text(option['text']) for option in options]
        if any(not text for text in texts):
            problems.append('Option text cannot be empty.')
        elif len(set(texts)) != len(texts):
            problems.append('Options must be distinct (ignoring case and extra spaces).')
        orders = [option['order'] for option in options if 'order' in option]
        if len(orders) != len(set(orders)):
            problems.append('Option orders must be unique.')
        if problems:
            raise serializers.ValidationError(problems)
        taken = set(orders)
        free = (n for n in range(1, len(options) + len(orders) + 2) if n not in taken)
        return [option if 'order' in option else {**option, 'order': next(free)}
                for option in options]

    def validate_text(self, value):
        if not normalized_text(value):
            raise serializers.ValidationError('Question text cannot be empty.')
        return value

    def validate(self, data):
        if self.instance is None:
            data.setdefault('type', Question.Type.mcq)
            data.setdefault('points', 1)
            if 'options' not in self.initial_data:
                raise serializers.ValidationError({'options': ['This field is required.']})
        else:
            if self.instance.type != Question.Type.mcq:
                # Legacy text/multi questions are never converted: their options
                # hold free-text answer keys that would become student-visible MCQ
                # options. They stay admin-only; author a new MCQ question instead.
                raise serializers.ValidationError({'type': [
                    'Legacy non-MCQ questions cannot be edited or converted; '
                    'create a new MCQ question instead.']})
            if 'quiz' in data and data.pop('quiz').pk != self.instance.quiz_id:
                raise serializers.ValidationError({'quiz': [
                    'A question cannot move to another quiz.']})
        return data


class _QuizSummaryMixin(serializers.Serializer):
    course_title = serializers.CharField(source='related_course.title', read_only=True,
                                         allow_null=True)
    event_title = serializers.CharField(source='related_event.title', read_only=True,
                                        allow_null=True)
    total_points = serializers.SerializerMethodField()
    question_count = serializers.SerializerMethodField()

    def audience_questions(self, obj):
        return obj.question_set.all()

    def get_total_points(self, obj) -> int:
        return sum(question.points for question in self.audience_questions(obj))

    def get_question_count(self, obj) -> int:
        return self.audience_questions(obj).count()


QUIZ_FIELDS = [
    'id', 'title', 'type', 'description', 'related_event',
    'related_course', 'time_limit', 'deadline', 'status', 'is_active', 'published_at',
    'closed_at', 'created_at', 'updated_at', 'questions',
    'course_title', 'event_title', 'total_points', 'question_count',
]
QUIZ_LIFECYCLE_FIELDS = ['status', 'is_active', 'published_at', 'closed_at']


class AdminQuizSerializer(_QuizSummaryMixin, serializers.ModelSerializer):
    """Admin quiz detail: every question (legacy included) with options and keys.

    ``content_editable`` is false once questions/options are locked (not a
    draft, or any attempt/submission exists); title/description stay
    correctable regardless (``AdminQuizWriteSerializer``).
    """
    questions = serializers.SerializerMethodField()
    content_editable = serializers.SerializerMethodField()

    class Meta:
        model = Quiz
        fields = [*QUIZ_FIELDS, 'content_editable']
        read_only_fields = fields

    @extend_schema_field(AdminQuestionSerializer(many=True))
    def get_questions(self, obj):
        return AdminQuestionSerializer(_ordered(obj.question_set.all()), many=True).data

    def get_content_editable(self, obj) -> bool:
        return not quiz_content_locked(obj.pk)


class AdminQuizWriteSerializer(RejectUnknownFieldsMixin, serializers.ModelSerializer):
    """Draft create/edit and published/closed metadata corrections. Admin only.

    Writable: ``title``, ``description``, ``time_limit`` (1-180 or null),
    ``deadline`` (ISO-8601 or null; future-ness is checked at publish),
    ``related_event``/``related_course`` (integer pk or null) and legacy
    ``type``. A published/closed quiz accepts only title/description changes;
    any other changed value is 409 ``QUIZ_IMMUTABLE`` with ``fields``
    (``quiz.lifecycle.assert_quiz_changes_allowed``). ``status``/``is_active``
    are lifecycle state: echoing the current value is tolerated, a different
    value is a 400 field error. Every other key is rejected (400).
    Responses use ``AdminQuizSerializer``.
    """
    ECHO_FIELDS = ('status', 'is_active')

    title = serializers.CharField(max_length=250)
    description = serializers.CharField(max_length=250, required=False, allow_blank=True)
    type = serializers.CharField(max_length=250, required=False)
    time_limit = StrictIntegerField(required=False, allow_null=True, min_value=MIN_TIME_LIMIT,
                                    max_value=MAX_TIME_LIMIT)
    deadline = serializers.DateTimeField(required=False, allow_null=True)
    related_event = StrictPrimaryKeyRelatedField(queryset=Event.objects.all(), required=False,
                                                 allow_null=True)
    related_course = StrictPrimaryKeyRelatedField(queryset=Course.objects.all(), required=False,
                                                  allow_null=True)

    class Meta:
        model = Quiz
        fields = ['title', 'description', 'type', 'time_limit', 'deadline', 'related_event',
                  'related_course']

    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            current = {'status': Quiz.Status.DRAFT, 'is_active': False}
            if self.instance is not None:
                current = {'status': self.instance.status, 'is_active': self.instance.is_active}
            errors = {name: ['Quiz status changes only through the publish/close actions.']
                      for name in self.ECHO_FIELDS if name in data and data[name] != current[name]}
            if errors:
                raise serializers.ValidationError(errors)
            data = {key: value for key, value in data.items() if key not in self.ECHO_FIELDS}
        return super().to_internal_value(data)

    def create(self, validated_data):
        validated_data.setdefault('description', '')
        return super().create(validated_data)

    def update(self, instance, validated_data):
        changed = [name for name, value in validated_data.items()
                   if getattr(instance, name) != value]
        assert_quiz_changes_allowed(instance, changed)
        for name in changed:
            setattr(instance, name, validated_data[name])
        if changed:
            instance.save(update_fields=[*changed, 'updated_at'])
        return instance

    def to_representation(self, instance):
        return AdminQuizSerializer(instance, context=self.context).data


class QuizListSerializer(serializers.ModelSerializer):
    """Paginated quiz list item without questions. Safe for every role.

    ``question_count``/``total_points`` cover every question for admins and
    only release MCQ questions for students (what a student can answer);
    views annotate them (``quiz.views.annotate_summary``).
    """
    course_title = serializers.CharField(source='related_course.title', read_only=True,
                                         allow_null=True)
    event_title = serializers.CharField(source='related_event.title', read_only=True,
                                        allow_null=True)
    question_count = serializers.SerializerMethodField()
    total_points = serializers.SerializerMethodField()

    class Meta:
        model = Quiz
        fields = [
            'id', 'title', 'type', 'description', 'related_event',
            'related_course', 'time_limit', 'deadline', 'status', 'is_active',
            'published_at', 'closed_at', 'created_at', 'updated_at',
            'course_title', 'event_title', 'question_count', 'total_points',
        ]
        read_only_fields = fields

    def _questions(self, obj):
        questions = obj.question_set.all()
        if not is_admin_context(self.context):
            questions = questions.filter(type=Question.Type.mcq)
        return questions

    def get_question_count(self, obj) -> int:
        if hasattr(obj, 'summary_question_count'):
            return obj.summary_question_count
        return self._questions(obj).count()

    def get_total_points(self, obj) -> int:
        if hasattr(obj, 'summary_total_points'):
            return obj.summary_total_points
        return sum(self._questions(obj).values_list('points', flat=True))


def is_admin_context(context):
    request = context.get('request')
    return bool(request is not None and is_admin(request.user))


class AdminSubmissionSerializer(serializers.ModelSerializer):
    """Result row for admins, including the student's identity. Admin only."""
    quiz_title = serializers.CharField(source='quiz.title', read_only=True)
    student_email = serializers.EmailField(source='student.email', read_only=True)
    student_name = serializers.SerializerMethodField()
    is_graded = serializers.SerializerMethodField()

    class Meta:
        model = Submission
        fields = [
            'id', 'quiz', 'quiz_title', 'student', 'student_email', 'student_name',
            'answers', 'score', 'submitted_at', 'graded_at', 'is_graded',
        ]
        read_only_fields = fields

    def get_student_name(self, obj) -> str:
        return f'{obj.student.first_name} {obj.student.last_name}'.strip()

    def get_is_graded(self, obj) -> bool:
        return obj.graded_at is not None


class AdminSubmissionDetailSerializer(AdminSubmissionSerializer):
    """Admin result with the nested quiz (including keys) for review."""
    quiz = AdminQuizSerializer(read_only=True)


# ----------------------------------------------------------------------------
# Student-safe serializers (never contain answer keys)
# ----------------------------------------------------------------------------

class StudentOptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Option
        fields = ['id', 'text', 'order']
        read_only_fields = fields


class StudentQuestionSerializer(serializers.ModelSerializer):
    options = serializers.SerializerMethodField()

    class Meta:
        model = Question
        fields = ['id', 'quiz', 'text', 'points', 'order', 'options']
        read_only_fields = fields

    @extend_schema_field(StudentOptionSerializer(many=True))
    def get_options(self, obj):
        return StudentOptionSerializer(_ordered(obj.option_set.all()), many=True).data


class StudentQuizSerializer(_QuizSummaryMixin, serializers.ModelSerializer):
    """Quiz as a student sees it: MCQ questions and options without keys."""
    questions = serializers.SerializerMethodField()

    class Meta:
        model = Quiz
        fields = QUIZ_FIELDS
        read_only_fields = fields

    def audience_questions(self, obj):
        return obj.question_set.filter(type=Question.Type.mcq)

    @extend_schema_field(StudentQuestionSerializer(many=True))
    def get_questions(self, obj):
        return StudentQuestionSerializer(_ordered(self.audience_questions(obj)), many=True).data


class StudentSubmissionSerializer(serializers.ModelSerializer):
    """A student's own result: selected option IDs and score, no keys."""
    quiz_title = serializers.CharField(source='quiz.title', read_only=True)
    is_graded = serializers.SerializerMethodField()

    class Meta:
        model = Submission
        fields = ['id', 'quiz', 'quiz_title', 'answers', 'score',
                  'submitted_at', 'graded_at', 'is_graded']
        read_only_fields = fields

    def get_is_graded(self, obj) -> bool:
        return obj.graded_at is not None


class StudentAttemptSerializer(serializers.ModelSerializer):
    """Frozen student attempt contract for ASSESS-04/06 (not routed in ASSESS-02).

    ``status`` is the effective status at ``server_time`` (an in-progress
    attempt past expiry reads ``expired``); ``server_time`` is
    ``context['now']`` or the database clock; ``submission_id`` is null until
    the attempt is finalized. Questions are delivered by the ASSESS-04 view.
    No answer keys.
    """
    quiz_id = serializers.IntegerField(read_only=True)
    quiz_title = serializers.CharField(source='quiz.title', read_only=True)
    status = serializers.SerializerMethodField()
    server_time = serializers.SerializerMethodField()
    submission_id = serializers.SerializerMethodField()

    class Meta:
        model = Attempt
        fields = ['id', 'quiz_id', 'quiz_title', 'status', 'started_at', 'expires_at',
                  'server_time', 'submission_id']
        read_only_fields = fields

    def _now(self):
        if 'now' not in self.context:
            from .lifecycle import server_now
            self.context['now'] = server_now()
        return self.context['now']

    def get_status(self, obj) -> str:
        from .lifecycle import effective_attempt_status
        return effective_attempt_status(obj, self._now())

    @extend_schema_field(serializers.DateTimeField())
    def get_server_time(self, obj):
        return serializers.DateTimeField().to_representation(self._now())

    def get_submission_id(self, obj) -> int | None:
        return Submission.objects.filter(attempt=obj).values_list('pk', flat=True).first()


@extend_schema_field({
    'type': 'object',
    'description': 'Keys are decimal question-ID strings of the quiz; values are the '
                   'integer ID of the selected option of that question.',
    'additionalProperties': {'type': 'integer'},
})
class AnswersField(serializers.JSONField):
    pass


class SubmissionCreateSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    """Legacy submission input. Student and score are server-owned (D0, D3).

    Only the structure is checked here; quiz availability and answer membership
    are checked by ``QuizService.submit`` under the quiz row lock. Temporary
    until the ASSESS-04 attempt contract (``attempt_id``) replaces it.
    """
    quiz = StrictIntegerField(min_value=1)
    answers = AnswersField()

    def validate_answers(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError('Answers must be a JSON object.')
        return value
