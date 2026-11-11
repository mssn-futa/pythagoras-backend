"""Arabic class, enrollment, roster and learning-module endpoints (D3, ASSESS-07).

Routes live under ``/quiz/classes/``; the same paths serve both roles and
choose the queryset and serializer by role before any object is read:

* admins (active, ``role=admin``) manage every Arabic class in any state,
  its modules (drafts included) and its roster;
* students see the published catalog, read a published/closed class (a draft
  is 404), manage their own enrollment, and read published modules only while
  they hold an active enrollment in the published class: a published class
  without one is 403 ``ENROLLMENT_REQUIRED``; a draft, closed, legacy or
  missing class, or a draft/foreign/missing module, is 404.

Legacy/general courses are never served here (404). Assessment eligibility is
unchanged: enrollment gates modules only.
"""
from django.db.models import Count, Prefetch, Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from pythagoras.pagination import StandardPagination
from pythagoras.utils import APIError, success_response
from quiz import learning
from quiz.errors import EnrollmentRequired
from quiz.learning_serializers import (
    AdminClassSerializer, AdminEnrollmentSerializer, AdminEnrollmentWriteSerializer,
    AdminModuleSerializer, ClassWriteSerializer, ModuleWriteSerializer, ReorderSerializer,
    StudentClassSerializer, StudentEnrollmentSerializer, StudentModuleSerializer,
    module_context,
)
from quiz.models import Course, Enrollment, LearningModule, ModuleResource
from quiz.permissions import IsActiveAdmin, IsActiveStudent, IsActiveUser, is_admin
from quiz.views import domain_errors, envelope, error, reject_body, role_specific, validated

# ---------------------------------------------------------------------------
# Schema examples (synthetic, with the real envelope)
# ---------------------------------------------------------------------------

EXAMPLE_CLASS = {
    'id': 2, 'code': 'ARB101', 'title': 'Arabic for beginners',
    'description': 'Weekly Arabic class', 'kind': 'arabic', 'status': 'published',
    'published_at': '2026-10-01T09:00:00Z', 'closed_at': None,
    'created_at': '2026-09-30T09:00:00Z', 'updated_at': '2026-10-01T09:00:00Z',
    'module_count': 3, 'enrollment_count': 25,
}
EXAMPLE_STUDENT_CLASS = {key: value for key, value in EXAMPLE_CLASS.items()
                         if key != 'enrollment_count'}
EXAMPLE_RESOURCE = {
    'id': 8, 'event_id': 3, 'title': 'Greetings audio', 'type': 'aud', 'status': 'published',
    'storage_status': 'ready', 'duration': 300,
    'media_url': 'https://media.example.test/greetings.mp3', 'playback': None,
    'content_type': 'audio/mpeg', 'size_bytes': 4800000, 'uploaded_at': '2026-09-29T09:00:00Z',
    'preview_url': None, 'published_at': '2026-09-29T10:00:00Z',
    'created_at': '2026-09-29T08:00:00Z', 'updated_at': '2026-09-29T10:00:00Z',
    'created_by': None, 'legacy_file_present': None,
    'event': {'id': 3, 'title': 'Arabic circle', 'speakers': 'Ustadh Musa',
              'category': 'special_lecture', 'start_time': '2026-09-28T17:00:00Z',
              'summary': 'Greetings and introductions'},
}
EXAMPLE_VIDEO = {
    **EXAMPLE_RESOURCE, 'id': 19, 'title': 'Greetings video', 'type': 'vid',
    'storage_status': 'not_applicable', 'duration': None, 'media_url': None,
    'content_type': None, 'size_bytes': None, 'uploaded_at': None,
    'playback': {'provider': 'youtube', 'video_id': 'abcdefghijk',
                 'watch_url': 'https://www.youtube.com/watch?v=abcdefghijk',
                 'embed_url': 'https://www.youtube-nocookie.com/embed/abcdefghijk'},
}
EXAMPLE_QUIZZES = [{'id': 45, 'title': 'Arabic week 1', 'status': 'published'}]
EXAMPLE_STUDENT_MODULE = {
    'id': 11, 'title': 'Arabic greetings', 'body': 'Lesson text', 'order': 1,
    'resources': [EXAMPLE_RESOURCE, EXAMPLE_VIDEO], 'quizzes': EXAMPLE_QUIZZES,
}
EXAMPLE_ADMIN_MODULE = {
    'id': 11, 'course_id': 2, 'title': 'Arabic greetings', 'body': 'Lesson text', 'order': 1,
    'status': 'draft', 'published_at': None, 'created_at': '2026-10-01T09:00:00Z',
    'updated_at': '2026-10-01T09:00:00Z', 'resource_ids': [8, 19, 23],
    'hidden_resource_ids': [23], 'resources': [EXAMPLE_RESOURCE, EXAMPLE_VIDEO],
    'quizzes': EXAMPLE_QUIZZES,
}
EXAMPLE_ENROLLMENT = {'id': 6, 'course_id': 2, 'student_id': 1, 'status': 'active',
                      'enrolled_at': '2026-10-02T08:00:00Z', 'withdrawn_at': None}
EXAMPLE_ROSTER_ROW = {**EXAMPLE_ENROLLMENT, 'student_name': 'Amina Yusuf',
                      'student_email': 'amina@example.test',
                      'updated_at': '2026-10-02T08:00:00Z'}
MODULE_REQUEST = OpenApiExample(
    'Module', request_only=True,
    value={'title': 'Arabic greetings', 'body': 'Lesson text', 'order': 1,
           'resource_ids': [8, 19]})
CLOSED_EXAMPLE = OpenApiExample(
    'Closed class', response_only=True, status_codes=['409'],
    value=error('CLASS_CLOSED', 'This class is closed and can no longer be changed.'))
ENROLLMENT_REQUIRED_EXAMPLE = OpenApiExample(
    'Not enrolled', response_only=True, status_codes=['403'],
    value=error('ENROLLMENT_REQUIRED',
                'An active enrollment in this class is required to read its modules.'))
HIDDEN_RESOURCE_EXAMPLE = OpenApiExample(
    'Unavailable resource', response_only=True, status_codes=['400'],
    value=error('VALIDATION_ERROR', 'Correct the highlighted fields.', {
        'resource_ids': ['Resource 23 is not published or not available.']}))
ERRORS = {400: OpenApiTypes.OBJECT, 403: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT,
          409: OpenApiTypes.OBJECT}
PAGE_PARAMS = [OpenApiParameter('page', int, description='1-based page number.'),
               OpenApiParameter('page_size', int, description='Default 20, maximum 100.')]


def paginated(request, view, queryset, serialize, message):
    paginator = StandardPagination()
    page = paginator.paginate_queryset(queryset, request, view=view)
    return success_response(paginator.get_paginated_response(serialize(page)).data, message)


def status_param(request, values):
    wanted = request.query_params.get('status')
    if wanted is not None and wanted not in values:
        raise serializers.ValidationError({'status': [f'Select one of: {", ".join(values)}.']})
    return wanted


def classes_for(user):
    admin = is_admin(user)
    queryset = learning.arabic_classes() if admin else learning.student_classes()
    if admin:
        return queryset.annotate(
            all_module_count=Count('modules', distinct=True),
            active_enrollment_count=Count('enrollment', distinct=True,
                                          filter=Q(enrollment__status=Enrollment.Status.ACTIVE)))
    return queryset.annotate(published_module_count=Count(
        'modules', distinct=True, filter=Q(modules__status=LearningModule.Status.PUBLISHED)))


def class_payload(user, course_id):
    course = get_object_or_404(classes_for(user), pk=course_id)
    serializer = AdminClassSerializer if is_admin(user) else StudentClassSerializer
    return serializer(course).data


def modules_with_attachments(queryset):
    return queryset.prefetch_related(Prefetch(
        'attachments', queryset=ModuleResource.objects.order_by('position')
        .select_related('resource__event')))


def module_payloads(request, course, modules, many):
    admin = is_admin(request.user)
    items = list(modules) if many else [modules]
    context = module_context(items, course, admin=admin, request=request)
    serializer = AdminModuleSerializer if admin else StudentModuleSerializer
    data = serializer(items, many=True, context=context).data
    return data if many else data[0]


def readable_module_class(request, class_id):
    """The class whose modules the requester may read, or 404/403 (see module doc)."""
    if is_admin(request.user):
        return get_object_or_404(learning.arabic_classes(), pk=class_id), None
    course = get_object_or_404(learning.student_classes(), pk=class_id)
    if course.status != Course.Status.PUBLISHED:
        raise Http404()  # closing a class hides its modules
    if not learning.has_active_enrollment(course, request.user):
        exc = EnrollmentRequired()
        raise APIError(exc.code, exc.message, status_code=exc.status_code)
    return course, LearningModule.Status.PUBLISHED


def admin_class(class_id):
    return get_object_or_404(learning.arabic_classes(), pk=class_id)


class ReadOrAdminView(APIView):
    """Active users read (role-filtered); only active admins write."""

    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsActiveUser()]
        return [IsActiveAdmin()]


# ---------------------------------------------------------------------------
# Classes
# ---------------------------------------------------------------------------

class ClassListView(ReadOrAdminView):

    @extend_schema(
        operation_id='quiz_classes_list',
        parameters=[OpenApiParameter('status', str, enum=Course.Status.values,
                                     description='Admin only: classes in this state.'),
                    *PAGE_PARAMS],
        responses={200: AdminClassSerializer(many=True)},
        examples=[OpenApiExample('Page', response_only=True, value=EXAMPLE_CLASS)],
        description='Paginated Arabic classes. Admins: every Arabic class in any state, newest '
                    'first, optionally filtered by `status`. Students: the published catalog '
                    '(no `enrollment_count`; `module_count` counts published modules), ordered '
                    'by `code` then `id`; `status` is ignored. Legacy/general academic courses '
                    'are never listed.')
    def get(self, request):
        queryset = classes_for(request.user)
        if is_admin(request.user):
            wanted = status_param(request, Course.Status.values)
            if wanted:
                queryset = queryset.filter(status=wanted)
            queryset, serializer = queryset.order_by('-pk'), AdminClassSerializer
        else:
            queryset = queryset.filter(status=Course.Status.PUBLISHED).order_by('code', 'pk')
            serializer = StudentClassSerializer
        return paginated(request, self, queryset,
                         lambda page: serializer(page, many=True).data, 'Classes retrieved.')

    @extend_schema(
        operation_id='quiz_classes_create',
        request=ClassWriteSerializer, responses={201: AdminClassSerializer, **ERRORS},
        examples=[OpenApiExample('Class', request_only=True,
                                 value={'code': 'ARB101', 'title': 'Arabic for beginners',
                                        'description': 'Weekly Arabic class'}),
                  OpenApiExample('Created', response_only=True, status_codes=['201'],
                                 value=envelope({**EXAMPLE_CLASS, 'status': 'draft',
                                                 'published_at': None, 'module_count': 0,
                                                 'enrollment_count': 0}, 'Class created.'))],
        description='Create a draft Arabic class (admin only): `code` (1-7 chars), `title` '
                    '(1-100), optional `description`. `kind` is always `arabic`; unknown keys '
                    'are rejected.')
    def post(self, request):
        data = validated(ClassWriteSerializer(data=request.data))
        with domain_errors():
            course = learning.create_class(data)
        return success_response(class_payload(request.user, course.pk), 'Class created.',
                                status=status.HTTP_201_CREATED)


class ClassDetailView(ReadOrAdminView):

    @extend_schema(
        operation_id='quiz_classes_retrieve',
        responses={200: role_specific('ClassDetail', AdminClassSerializer,
                                      StudentClassSerializer), 404: OpenApiTypes.OBJECT},
        examples=[OpenApiExample('Admin', response_only=True,
                                 value=envelope(EXAMPLE_CLASS, 'Class retrieved.')),
                  OpenApiExample('Student', response_only=True,
                                 value=envelope(EXAMPLE_STUDENT_CLASS, 'Class retrieved.'))],
        description='Admins: any Arabic class. Students: a published or closed class; a draft '
                    'is 404.')
    def get(self, request, class_id):
        return success_response(class_payload(request.user, class_id), 'Class retrieved.')

    @extend_schema(
        operation_id='quiz_classes_partial_update',
        request=ClassWriteSerializer, responses={200: AdminClassSerializer, **ERRORS},
        examples=[OpenApiExample('Edit', request_only=True,
                                 value={'title': 'Arabic for beginners (evening)'}),
                  CLOSED_EXAMPLE],
        description='Change `code`/`title`/`description` of a draft or published class (admin '
                    'only; partial). A closed class is read-only: 409 CLASS_CLOSED.')
    def patch(self, request, class_id):
        admin_class(class_id)
        data = validated(ClassWriteSerializer(data=request.data, partial=True))
        with domain_errors():
            learning.update_class(class_id, data)
        return success_response(class_payload(request.user, class_id), 'Class updated.')

    @extend_schema(
        operation_id='quiz_classes_destroy',
        responses={204: None, 404: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT},
        description='Delete a draft class and its modules (admin only). A published/closed '
                    'class is 409 INVALID_TRANSITION (close it instead); a class with any '
                    'enrollment or linked quiz is 409 RESOURCE_REFERENCED.')
    def delete(self, request, class_id):
        admin_class(class_id)
        with domain_errors():
            learning.delete_class(class_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ClassPublishView(APIView):
    permission_classes = [IsActiveAdmin]

    @extend_schema(
        operation_id='quiz_classes_publish', request=None,
        responses={200: AdminClassSerializer, **ERRORS},
        description='Publish a draft class (admin only): it joins the student catalog and '
                    'accepts enrollment. Publishing a published class is idempotent (200); a '
                    'closed class is 409 INVALID_TRANSITION. No request body.')
    def post(self, request, class_id):
        reject_body(request)
        admin_class(class_id)
        with domain_errors():
            _, changed = learning.publish_class(class_id)
        return success_response(class_payload(request.user, class_id),
                                'Class published.' if changed else 'Class already published.')


class ClassCloseView(APIView):
    permission_classes = [IsActiveAdmin]

    @extend_schema(
        operation_id='quiz_classes_close', request=None,
        responses={200: AdminClassSerializer, **ERRORS},
        description='Close a published class (admin only; terminal): no new enrollment, module '
                    'access is hidden from students, the class becomes read-only; enrollments, '
                    'quizzes and results stay retrievable. Closing a closed class is idempotent '
                    '(200); a draft is 409 INVALID_TRANSITION. No request body.')
    def post(self, request, class_id):
        reject_body(request)
        admin_class(class_id)
        with domain_errors():
            _, changed = learning.close_class(class_id)
        return success_response(class_payload(request.user, class_id),
                                'Class closed.' if changed else 'Class already closed.')


# ---------------------------------------------------------------------------
# Own enrollment (students)
# ---------------------------------------------------------------------------

class OwnEnrollmentView(APIView):
    permission_classes = [IsActiveStudent]

    def student_class(self, class_id):
        return get_object_or_404(learning.student_classes(), pk=class_id)

    @extend_schema(
        operation_id='quiz_classes_enrollment_retrieve',
        responses={200: StudentEnrollmentSerializer, 404: OpenApiTypes.OBJECT},
        examples=[OpenApiExample('Own enrollment', response_only=True,
                                 value=envelope(EXAMPLE_ENROLLMENT, 'Enrollment retrieved.'))],
        description='The student\'s own enrollment record (active or withdrawn) in a '
                    'published/closed class; 404 if never enrolled (or a draft class).')
    def get(self, request, class_id):
        course = self.student_class(class_id)
        enrollment = get_object_or_404(Enrollment, course=course, student=request.user)
        return success_response(StudentEnrollmentSerializer(enrollment).data,
                                'Enrollment retrieved.')

    @extend_schema(
        operation_id='quiz_classes_enrollment_create', request=None,
        responses={201: StudentEnrollmentSerializer, 200: StudentEnrollmentSerializer, **ERRORS},
        examples=[OpenApiExample('Enrolled', response_only=True, status_codes=['201'],
                                 value=envelope(EXAMPLE_ENROLLMENT, 'Enrolled.')),
                  OpenApiExample('Class closed', response_only=True, status_codes=['409'],
                                 value=error('CLASS_CLOSED', 'This class is closed and can no '
                                             'longer be changed.'))],
        description='Enroll in a published class (body `{}` or empty). Idempotent: 201 with a '
                    'new record; 200 with the same record when already active, or after '
                    'reactivating a withdrawn record in place (same `id`). A closed class is '
                    '409 CLASS_CLOSED (an already-active record is still returned 200); a draft '
                    'class is 404.')
    def post(self, request, class_id):
        reject_body(request)
        self.student_class(class_id)
        with domain_errors():
            enrollment, created, _ = learning.enroll(class_id, request.user)
        return success_response(StudentEnrollmentSerializer(enrollment).data, 'Enrolled.',
                                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    @extend_schema(
        operation_id='quiz_classes_enrollment_destroy', request=None,
        responses={200: StudentEnrollmentSerializer, **ERRORS},
        examples=[OpenApiExample('Withdrawn', response_only=True, value=envelope(
            {**EXAMPLE_ENROLLMENT, 'status': 'withdrawn',
             'withdrawn_at': '2026-10-09T08:00:00Z'}, 'Withdrawn.'))],
        description='Withdraw (idempotent, 200 with the record; the record is kept for '
                    're-enrollment). 404 if never enrolled. Withdrawing from a closed class is '
                    '409 CLASS_CLOSED (a withdrawn record is still returned 200). Attempts '
                    'already started keep their own expiry.')
    def delete(self, request, class_id):
        self.student_class(class_id)
        with domain_errors():
            try:
                enrollment, _ = learning.withdraw(class_id, request.user.pk)
            except Enrollment.DoesNotExist:
                raise Http404() from None
        return success_response(StudentEnrollmentSerializer(enrollment).data, 'Withdrawn.')


# ---------------------------------------------------------------------------
# Roster (admins)
# ---------------------------------------------------------------------------

class RosterView(APIView):
    permission_classes = [IsActiveAdmin]

    @extend_schema(
        operation_id='quiz_classes_enrollments_list',
        parameters=[OpenApiParameter('status', str, description='active or withdrawn.'),
                    OpenApiParameter('search', str, description='Case-insensitive match on the '
                                     'student\'s first name, last name, full name or email.'),
                    *PAGE_PARAMS],
        responses={200: AdminEnrollmentSerializer(many=True)},
        examples=[OpenApiExample('Page', response_only=True, value=EXAMPLE_ROSTER_ROW)],
        description='Paginated roster of an Arabic class (admin only), in record `id` order.')
    def get(self, request, class_id):
        course = admin_class(class_id)
        queryset = Enrollment.objects.filter(course=course).select_related('student')
        wanted = status_param(request, Enrollment.Status.values)
        if wanted:
            queryset = queryset.filter(status=wanted)
        search = (request.query_params.get('search') or '').strip()
        if search:
            queryset = queryset.filter(learning_search(search))
        return paginated(request, self, queryset.order_by('pk'),
                         lambda page: AdminEnrollmentSerializer(page, many=True).data,
                         'Roster retrieved.')

    @extend_schema(
        operation_id='quiz_classes_enrollments_create',
        request=AdminEnrollmentWriteSerializer,
        responses={201: AdminEnrollmentSerializer, 200: AdminEnrollmentSerializer, **ERRORS},
        examples=[OpenApiExample('Enroll', request_only=True,
                                 value={'student_id': 1, 'status': 'active'}),
                  OpenApiExample('Withdraw', request_only=True,
                                 value={'student_id': 1, 'status': 'withdrawn'}),
                  OpenApiExample('Row', response_only=True,
                                 value=envelope(EXAMPLE_ROSTER_ROW, 'Enrolled.')),
                  CLOSED_EXAMPLE],
        description='Enroll, withdraw or reinstate one student (admin only). Idempotent per '
                    '(class, student): 201 when a record is created, else 200 with the same '
                    'record (re-enrollment reactivates it). `student_id` must be a student '
                    'account (active to enroll); withdrawing a never-enrolled student is a 400 '
                    'field error. Enrolling needs a published class (draft: 409 '
                    'CLASS_NOT_PUBLISHED); a closed class is read-only (409 CLASS_CLOSED).')
    def post(self, request, class_id):
        admin_class(class_id)
        data = validated(AdminEnrollmentWriteSerializer(data=request.data))
        with domain_errors():
            enrollment, created, _ = learning.admin_set_enrollment(
                class_id, data['student_id'], data['status'])
        enrollment = Enrollment.objects.select_related('student').get(pk=enrollment.pk)
        message = 'Enrolled.' if enrollment.status == Enrollment.Status.ACTIVE else 'Withdrawn.'
        return success_response(AdminEnrollmentSerializer(enrollment).data, message,
                                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


def learning_search(term):
    query = (Q(student__first_name__icontains=term) | Q(student__last_name__icontains=term)
             | Q(student__email__icontains=term))
    first, _, last = term.partition(' ')
    if last.strip():
        query |= (Q(student__first_name__icontains=first)
                  & Q(student__last_name__icontains=last.strip()))
    return query


# ---------------------------------------------------------------------------
# Modules
# ---------------------------------------------------------------------------

MODULE_RULES = (
    'Body: `title` (1-250), `body` (plain text), `order` (1-10000, unique in the class; '
    'omitted on create = after the last module) and `resource_ids` (up to 50 distinct dawah '
    'resource IDs, kept in the given order). Resource IDs newly attached must be visible '
    '(published, published event, ready asset or valid YouTube reference); IDs already '
    'attached stay accepted even if now hidden (they appear in `hidden_resource_ids`). '
    'A closed class is read-only (409 CLASS_CLOSED).'
)


class ModuleListView(ReadOrAdminView):

    @extend_schema(
        operation_id='quiz_classes_modules_list',
        parameters=[OpenApiParameter('status', str,
                                     description='Admin only: draft or published.'), *PAGE_PARAMS],
        responses={200: AdminModuleSerializer(many=True), 403: OpenApiTypes.OBJECT,
                   404: OpenApiTypes.OBJECT},
        examples=[OpenApiExample('Student page', response_only=True,
                                 value=EXAMPLE_STUDENT_MODULE),
                  ENROLLMENT_REQUIRED_EXAMPLE],
        description='Paginated modules in (`order`, `id`) order. Admins: every module (drafts '
                    'included; optional `status`) with `status`, `resource_ids` and '
                    '`hidden_resource_ids`. Students: published modules of a published class '
                    'they are actively enrolled in, as `{id, title, body, order, resources, '
                    'quizzes}`; no active enrollment is 403 ENROLLMENT_REQUIRED; a draft or '
                    'closed class is 404. `resources` are full public resource payloads '
                    '(`GET /dawah/resources/{id}/`) of currently visible attachments only.')
    def get(self, request, class_id):
        course, only = readable_module_class(request, class_id)
        queryset = learning.class_modules(course)
        if only is not None:
            queryset = queryset.filter(status=only)
        else:
            wanted = status_param(request, LearningModule.Status.values)
            if wanted:
                queryset = queryset.filter(status=wanted)
        queryset = modules_with_attachments(queryset.order_by('order', 'pk'))
        return paginated(request, self, queryset,
                         lambda page: module_payloads(request, course, page, many=True),
                         'Modules retrieved.')

    @extend_schema(
        operation_id='quiz_classes_modules_create',
        request=ModuleWriteSerializer, responses={201: AdminModuleSerializer, **ERRORS},
        examples=[MODULE_REQUEST,
                  OpenApiExample('Created', response_only=True, status_codes=['201'],
                                 value=envelope({**EXAMPLE_ADMIN_MODULE, 'resource_ids': [8, 19],
                                                 'hidden_resource_ids': []},
                                                'Module created.')),
                  HIDDEN_RESOURCE_EXAMPLE, CLOSED_EXAMPLE],
        description='Create a draft module (admin only). ' + MODULE_RULES)
    def post(self, request, class_id):
        admin_class(class_id)
        data = validated(ModuleWriteSerializer(data=request.data))
        with domain_errors():
            module = learning.create_module(class_id, data)
        return success_response(module_detail(request, module), 'Module created.',
                                status=status.HTTP_201_CREATED)


def module_detail(request, module):
    module = modules_with_attachments(LearningModule.objects.select_related('course')).get(
        pk=module.pk)
    return module_payloads(request, module.course, module, many=False)


class ModuleReorderView(APIView):
    permission_classes = [IsActiveAdmin]

    @extend_schema(
        operation_id='quiz_classes_modules_reorder',
        request=ReorderSerializer, responses={200: AdminModuleSerializer(many=True), **ERRORS},
        examples=[OpenApiExample('Reorder', request_only=True,
                                 value={'module_ids': [12, 11, 13]})],
        description='Atomically renumber the class\'s modules 1..n in the given order (admin '
                    'only). `module_ids` must list every module of the class exactly once; '
                    'otherwise 400 on `module_ids` and nothing changes. Returns the ordered '
                    'module list (not paginated). Order is unique per class (deferred DB '
                    'constraint). A closed class is 409 CLASS_CLOSED.')
    def post(self, request, class_id):
        admin_class(class_id)
        data = validated(ReorderSerializer(data=request.data))
        with domain_errors():
            learning.reorder_modules(class_id, data['module_ids'])
        course = admin_class(class_id)
        modules = modules_with_attachments(learning.class_modules(course).order_by('order', 'pk'))
        return success_response(module_payloads(request, course, modules, many=True),
                                'Module order saved.')


class ModuleDetailView(ReadOrAdminView):

    def readable(self, request, class_id, module_id):
        course, only = readable_module_class(request, class_id)
        queryset = learning.class_modules(course)
        if only is not None:
            queryset = queryset.filter(status=only)
        module = get_object_or_404(modules_with_attachments(queryset), pk=module_id)
        return course, module

    @extend_schema(
        operation_id='quiz_classes_modules_retrieve',
        responses={200: role_specific('ModuleDetail', AdminModuleSerializer,
                                      StudentModuleSerializer),
                   403: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
        examples=[OpenApiExample('Student', response_only=True,
                                 value=envelope(EXAMPLE_STUDENT_MODULE, 'Module retrieved.')),
                  OpenApiExample('Admin', response_only=True,
                                 value=envelope(EXAMPLE_ADMIN_MODULE, 'Module retrieved.')),
                  ENROLLMENT_REQUIRED_EXAMPLE],
        description='Admins: any module of the class (draft preview). Students: a published '
                    'module of a published class with an active enrollment (else 403 '
                    'ENROLLMENT_REQUIRED); a draft/foreign/missing module or a draft/closed '
                    'class is 404.')
    def get(self, request, class_id, module_id):
        course, module = self.readable(request, class_id, module_id)
        return success_response(module_payloads(request, course, module, many=False),
                                'Module retrieved.')

    @extend_schema(
        operation_id='quiz_classes_modules_partial_update',
        request=ModuleWriteSerializer, responses={200: AdminModuleSerializer, **ERRORS},
        examples=[OpenApiExample('Move', request_only=True, value={'order': 3}),
                  OpenApiExample('Replace attachments', request_only=True,
                                 value={'resource_ids': [19, 8]}),
                  HIDDEN_RESOURCE_EXAMPLE, CLOSED_EXAMPLE],
        description='Partial module edit in any module state (admin only); a sent '
                    '`resource_ids` replaces the attachment set. ' + MODULE_RULES)
    def patch(self, request, class_id, module_id):
        course = admin_class(class_id)
        module = get_object_or_404(learning.class_modules(course), pk=module_id)
        data = validated(ModuleWriteSerializer(data=request.data, partial=True))
        with domain_errors():
            module = learning.update_module(class_id, module.pk, data)
        return success_response(module_detail(request, module), 'Module updated.')

    @extend_schema(
        operation_id='quiz_classes_modules_destroy',
        responses={204: None, 404: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT},
        description='Delete a draft module and its attachments (admin only); the attached '
                    'resources themselves are untouched. A published module is 409 '
                    'UNPUBLISH_REQUIRED; a closed class 409 CLASS_CLOSED.')
    def delete(self, request, class_id, module_id):
        course = admin_class(class_id)
        module = get_object_or_404(learning.class_modules(course), pk=module_id)
        with domain_errors():
            learning.delete_module(class_id, module.pk)
        return Response(status=status.HTTP_204_NO_CONTENT)


class _ModuleStatusView(APIView):
    permission_classes = [IsActiveAdmin]
    target = None
    message = ''

    def post(self, request, class_id, module_id):
        reject_body(request)
        course = admin_class(class_id)
        module = get_object_or_404(learning.class_modules(course), pk=module_id)
        with domain_errors():
            module, _ = learning.set_module_status(class_id, module.pk, self.target)
        return success_response(module_detail(request, module), self.message)


class ModulePublishView(_ModuleStatusView):
    target = LearningModule.Status.PUBLISHED
    message = 'Module published.'

    @extend_schema(
        operation_id='quiz_classes_modules_publish', request=None,
        responses={200: AdminModuleSerializer, **ERRORS},
        description='Publish a module (admin only; idempotent). Students read it once the '
                    'class is published too. A closed class is 409 CLASS_CLOSED. No body.')
    def post(self, request, class_id, module_id):
        return super().post(request, class_id, module_id)


class ModuleUnpublishView(_ModuleStatusView):
    target = LearningModule.Status.DRAFT
    message = 'Module unpublished.'

    @extend_schema(
        operation_id='quiz_classes_modules_unpublish', request=None,
        responses={200: AdminModuleSerializer, **ERRORS},
        description='Return a module to draft (admin only; idempotent): hidden from students '
                    'immediately. A closed class is 409 CLASS_CLOSED. No body.')
    def post(self, request, class_id, module_id):
        return super().post(request, class_id, module_id)
