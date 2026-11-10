"""Assessment serializers, split by audience.

``Admin*`` serializers may contain correctness keys (``is_correct``) and other
students' data; they are only used behind ``IsActiveAdmin``/``is_admin``.
``Student*`` serializers never contain ``is_correct`` or any other answer key,
at any nesting depth (D0 A12, D3). ``QuizListSerializer`` and
``CourseSerializer`` carry no keys and are shared.

Release behaviour is MCQ-only: student payloads include only MCQ questions and
authoring accepts only ``type="mcq"``. Legacy text/multi-answer questions stay
readable to admins.
"""
from collections.abc import Mapping

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import Course, Enrollment, Option, Question, Quiz, Submission
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


def _ordered(queryset):
    return queryset.order_by('pk')


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
# Admin serializers (may contain answer keys)
# ----------------------------------------------------------------------------

class AdminOptionSerializer(serializers.ModelSerializer):
    """Option including its correctness key. Admin only."""

    class Meta:
        model = Option
        fields = ['id', 'text', 'is_correct']
        read_only_fields = ['id']


class AdminQuestionSerializer(serializers.ModelSerializer):
    """Question with options and keys. Admin only."""
    options = serializers.SerializerMethodField()

    class Meta:
        model = Question
        fields = ['id', 'quiz', 'type', 'text', 'points', 'options']
        read_only_fields = fields

    @extend_schema_field(AdminOptionSerializer(many=True))
    def get_options(self, obj):
        return AdminOptionSerializer(_ordered(obj.option_set.all()), many=True).data


@extend_schema_field({'type': 'string', 'enum': [Question.Type.mcq.value]})
class ReleaseQuestionTypeField(serializers.ChoiceField):
    """Authoring accepts only the release MCQ type (A08)."""

    def __init__(self, **kwargs):
        kwargs.setdefault('default', Question.Type.mcq)
        super().__init__(choices=[(Question.Type.mcq.value, Question.Type.mcq.label)], **kwargs)


class AdminQuestionWriteSerializer(serializers.ModelSerializer):
    """Create/replace a question and its options. Admin only.

    Option count/uniqueness/single-correct rules and publication immutability
    are ASSESS-03 authoring rules and are not enforced here.
    """
    type = ReleaseQuestionTypeField()
    options = AdminOptionSerializer(many=True, required=False)

    class Meta:
        model = Question
        fields = ['id', 'quiz', 'type', 'text', 'points', 'options']
        read_only_fields = ['id']

    def create(self, validated_data):
        options_data = validated_data.pop('options', [])
        question = Question.objects.create(**validated_data)
        for option_data in options_data:
            Option.objects.create(question=question, **option_data)
        return question

    def update(self, instance, validated_data):
        options_data = validated_data.pop('options', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if options_data is not None:
            instance.option_set.all().delete()
            for option_data in options_data:
                Option.objects.create(question=instance, **option_data)
        return instance

    def to_representation(self, instance):
        return AdminQuestionSerializer(instance, context=self.context).data


class _QuizSummaryMixin(serializers.Serializer):
    course_title = serializers.CharField(source='related_course.title', read_only=True)
    event_title = serializers.CharField(source='related_event.title', read_only=True)
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
    'related_course', 'time_limit', 'is_active', 'questions',
    'course_title', 'event_title', 'total_points', 'question_count',
]


class AdminQuizSerializer(_QuizSummaryMixin, serializers.ModelSerializer):
    """Quiz with every question and its keys. Admin read/write."""
    questions = serializers.SerializerMethodField()

    class Meta:
        model = Quiz
        fields = QUIZ_FIELDS
        read_only_fields = ['id']

    @extend_schema_field(AdminQuestionSerializer(many=True))
    def get_questions(self, obj):
        return AdminQuestionSerializer(_ordered(obj.question_set.all()), many=True).data


class QuizListSerializer(serializers.ModelSerializer):
    """Lightweight quiz listing without questions. Safe for every role."""
    course_title = serializers.CharField(source='related_course.title', read_only=True)
    event_title = serializers.CharField(source='related_event.title', read_only=True)
    question_count = serializers.SerializerMethodField()

    class Meta:
        model = Quiz
        fields = [
            'id', 'title', 'type', 'description', 'related_event',
            'related_course', 'time_limit', 'is_active', 'course_title',
            'event_title', 'question_count'
        ]
        read_only_fields = fields

    def get_question_count(self, obj) -> int:
        """Get total number of questions in the quiz"""
        if is_admin_context(self.context):
            return obj.question_set.count()
        return obj.question_set.filter(type=Question.Type.mcq).count()


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
        fields = ['id', 'text']
        read_only_fields = fields


class StudentQuestionSerializer(serializers.ModelSerializer):
    options = serializers.SerializerMethodField()

    class Meta:
        model = Question
        fields = ['id', 'quiz', 'text', 'points', 'options']
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
    quiz = serializers.IntegerField(min_value=1)
    answers = AnswersField()

    def validate_answers(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError('Answers must be a JSON object.')
        return value
