"""Arabic class, enrollment and module serializers (ASSESS-07), split by audience.

Write serializers validate structure only and reject unknown keys (400);
``quiz.learning`` applies the rules that need locks. Read serializers:

* class: ``Admin`` adds ``enrollment_count``; students get ``module_count`` of
  published modules only.
* module: admins get every attachment ID (``resource_ids``), the attached but
  currently hidden ones (``hidden_resource_ids``), ``status`` and timestamps;
  students get ``{id, title, body, order, resources, quizzes}``. In both,
  ``resources`` holds only currently visible resources, each the full public
  resource payload of anonymous ``GET /dawah/resources/{id}/`` (through the
  ``quiz.resources`` adapter, so no private preview URL or creator is included). ``quizzes`` is ``[{id, title, status}]``: never questions or keys.
* enrollment: admin roster rows add the student's name/email.

Module serializers read ``context['visible_ids']`` and ``context['quizzes']``
prepared once per request by ``module_context``.
"""
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .authoring import MAX_ORDER
from .learning import MAX_RESOURCES_PER_MODULE, class_quizzes
from .models import Course, Enrollment, LearningModule
from .resources import PublicResourceSerializer, public_resource_payloads, visible_resource_ids
from .serializers import RejectUnknownFieldsMixin, StrictCharField, StrictIntegerField

MAX_DESCRIPTION_LENGTH = 5000
MAX_BODY_LENGTH = 50000


class NonBlankCharField(StrictCharField):
    """JSON string with visible content (stored trimmed)."""

    def __init__(self, **kwargs):
        kwargs.setdefault('allow_blank', False)
        super().__init__(**kwargs)


# ----------------------------------------------------------------------------
# Classes
# ----------------------------------------------------------------------------

class ClassWriteSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    """``{"code", "title", "description"}``. ``kind`` is always ``arabic`` and
    ``status`` changes only through publish/close, so neither is accepted."""
    code = NonBlankCharField(max_length=7)
    title = NonBlankCharField(max_length=100)
    description = StrictCharField(required=False, allow_blank=True,
                                  max_length=MAX_DESCRIPTION_LENGTH)


CLASS_FIELDS = ['id', 'code', 'title', 'description', 'kind', 'status', 'published_at',
                'closed_at', 'created_at', 'updated_at', 'module_count']


class StudentClassSerializer(serializers.ModelSerializer):
    """A published/closed Arabic class as a student sees it."""
    module_count = serializers.SerializerMethodField()

    class Meta:
        model = Course
        fields = CLASS_FIELDS
        read_only_fields = fields

    def get_module_count(self, obj) -> int:
        if hasattr(obj, 'published_module_count'):
            return obj.published_module_count
        return obj.modules.filter(status=LearningModule.Status.PUBLISHED).count()


class AdminClassSerializer(serializers.ModelSerializer):
    """Admin class payload: every module (any state) and active enrollments counted."""
    module_count = serializers.SerializerMethodField()
    enrollment_count = serializers.SerializerMethodField()

    class Meta:
        model = Course
        fields = [*CLASS_FIELDS, 'enrollment_count']
        read_only_fields = fields

    def get_module_count(self, obj) -> int:
        if hasattr(obj, 'all_module_count'):
            return obj.all_module_count
        return obj.modules.count()

    @extend_schema_field(serializers.IntegerField(help_text='Active enrollments.'))
    def get_enrollment_count(self, obj):
        if hasattr(obj, 'active_enrollment_count'):
            return obj.active_enrollment_count
        return obj.enrollment_set.filter(status=Enrollment.Status.ACTIVE).count()


# ----------------------------------------------------------------------------
# Modules
# ----------------------------------------------------------------------------

class ModuleWriteSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    """``{"title", "body", "order", "resource_ids"}`` (D3 module request).

    Create: ``title`` required; ``body`` defaults to ``""``; ``order`` (1-10000,
    unique in the class) defaults to after the last module; ``resource_ids``
    defaults to ``[]``. PATCH is partial (``{"order": 3}`` alone is valid); a
    sent ``resource_ids`` replaces the attachment set in the given order.
    """
    title = NonBlankCharField(max_length=250)
    body = StrictCharField(required=False, allow_blank=True, trim_whitespace=False,
                           max_length=MAX_BODY_LENGTH)
    order = StrictIntegerField(required=False, min_value=1, max_value=MAX_ORDER)
    resource_ids = serializers.ListField(
        child=StrictIntegerField(min_value=1), required=False, allow_empty=True,
        max_length=MAX_RESOURCES_PER_MODULE)

    def validate_resource_ids(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError('Each resource may be attached only once.')
        return value


class ReorderSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    """``{"module_ids": [...every module ID of the class, in the new order]}``."""
    module_ids = serializers.ListField(child=StrictIntegerField(min_value=1),
                                       allow_empty=True)


def module_context(modules, course, *, admin, request=None):
    """Per-request data shared by every module payload of one class.

    One query for the visible subset of every attached resource and one for the
    class's quizzes, so a page of modules costs a constant number of queries
    (attachments are prefetched by the view).
    """
    attached = {attachment.resource_id for module in modules
                for attachment in module.attachments.all()}
    visible = visible_resource_ids(attached)
    return {'visible_ids': visible, 'quizzes': class_quizzes(course.pk, admin=admin),
            'request': request}


QUIZ_REF_SCHEMA = {
    'type': 'array',
    'items': {'type': 'object', 'properties': {
        'id': {'type': 'integer'}, 'title': {'type': 'string'},
        'status': {'type': 'string', 'enum': ['draft', 'published', 'closed']}},
        'required': ['id', 'title', 'status']},
    'description': 'Quizzes linked to this class (`related_course`). Students see '
                   'published/closed quizzes only. Metadata only: no questions or keys.',
}


class _ModuleReadMixin(serializers.Serializer):
    # Plain strings (not schema enums) so these state names do not collide with
    # other ``status`` enums in the OpenAPI components.
    status = serializers.CharField(read_only=True, help_text='Module state: draft or published.')
    resources = serializers.SerializerMethodField()
    quizzes = serializers.SerializerMethodField()

    def _attachments(self, obj):
        return sorted(obj.attachments.all(), key=lambda item: item.position)

    @extend_schema_field(PublicResourceSerializer(many=True))
    def get_resources(self, obj):
        visible = self.context['visible_ids']
        resources = [item.resource for item in self._attachments(obj)
                     if item.resource_id in visible]
        # Public (anonymous) shape for every audience: no preview URL or creator.
        return public_resource_payloads(resources)

    @extend_schema_field(QUIZ_REF_SCHEMA)
    def get_quizzes(self, obj):
        return self.context['quizzes']


class StudentModuleSerializer(_ModuleReadMixin, serializers.ModelSerializer):
    class Meta:
        model = LearningModule
        fields = ['id', 'title', 'body', 'order', 'resources', 'quizzes']
        read_only_fields = fields


class AdminModuleSerializer(_ModuleReadMixin, serializers.ModelSerializer):
    course_id = serializers.IntegerField(read_only=True)
    resource_ids = serializers.SerializerMethodField()
    hidden_resource_ids = serializers.SerializerMethodField()

    class Meta:
        model = LearningModule
        fields = ['id', 'course_id', 'title', 'body', 'order', 'status', 'published_at',
                  'created_at', 'updated_at', 'resource_ids', 'hidden_resource_ids',
                  'resources', 'quizzes']
        read_only_fields = fields

    @extend_schema_field(serializers.ListField(
        child=serializers.IntegerField(), help_text='Every attached resource ID, in order.'))
    def get_resource_ids(self, obj):
        return [item.resource_id for item in self._attachments(obj)]

    @extend_schema_field(serializers.ListField(
        child=serializers.IntegerField(),
        help_text='Attached but not currently visible (unpublished, hidden parent or no '
                  'deliverable asset); offered for detaching. Never shown to students.'))
    def get_hidden_resource_ids(self, obj):
        visible = self.context['visible_ids']
        return [item.resource_id for item in self._attachments(obj)
                if item.resource_id not in visible]


# ----------------------------------------------------------------------------
# Enrollment
# ----------------------------------------------------------------------------

ENROLLMENT_FIELDS = ['id', 'course_id', 'student_id', 'status', 'enrolled_at', 'withdrawn_at']


class StudentEnrollmentSerializer(serializers.ModelSerializer):
    """The student's own record. ``enrolled_at`` is the latest (re)activation;
    both timestamps are null for records that predate ASSESS-07."""
    course_id = serializers.IntegerField(read_only=True)
    student_id = serializers.IntegerField(read_only=True)
    status = serializers.CharField(read_only=True, help_text='Enrollment state: active or '
                                                             'withdrawn.')

    class Meta:
        model = Enrollment
        fields = ENROLLMENT_FIELDS
        read_only_fields = fields


class AdminEnrollmentSerializer(StudentEnrollmentSerializer):
    """Roster row (admin only)."""
    student_name = serializers.SerializerMethodField()
    student_email = serializers.EmailField(source='student.email', read_only=True)

    class Meta(StudentEnrollmentSerializer.Meta):
        fields = [*ENROLLMENT_FIELDS, 'student_name', 'student_email', 'updated_at']
        read_only_fields = fields

    def get_student_name(self, obj) -> str:
        return f'{obj.student.first_name} {obj.student.last_name}'.strip()


class AdminEnrollmentWriteSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    """``{"student_id": 7, "status": "active" | "withdrawn"}`` (status defaults to active)."""
    student_id = StrictIntegerField(min_value=1)
    status = StrictCharField(required=False, default=Enrollment.Status.ACTIVE,
                             help_text='active (enroll/reinstate, default) or withdrawn.')

    def validate_status(self, value):
        if value not in Enrollment.Status.values:
            raise serializers.ValidationError('Select active or withdrawn.')
        return value
