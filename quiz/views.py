"""Assessment endpoints with shared role and object permissions.

Every handler resolves permissions through ``get_permissions`` (never by
assigning ``permission_classes`` inside a handler), and every object lookup goes
through a role-aware queryset from ``quiz.permissions`` before any serializer
or mutation runs. Student responses use only ``Student*`` serializers.

ASSESS-03 admin authoring (D3 "Backend Design"): paginated quiz lists with a
``status`` filter, draft create/edit, published/closed title/description
corrections, draft deletion, question create/edit/delete with options while
the quiz is content-editable, and the publish/close actions. Validation errors
are 400 ``VALIDATION_ERROR`` with flat dotted field paths; lifecycle conflicts
are 409 (``QUIZ_IMMUTABLE``, ``INVALID_TRANSITION``, ``RESOURCE_REFERENCED``).
Legacy aliases (no-slash detail routes, ``PUT``, ``POST /quiz/question/``) run
the same serializers and services, so they cannot bypass a rule.

Student availability is ``published`` with a future deadline.

ASSESS-04 student participation (docs/assessment-results.md): start or resume
the single timed attempt (``POST /quiz/quizzes/{id}/attempts/``), read it with
its key-free questions and server time (``GET /quiz/attempts/{id}/``), finalize
it once (``POST /quiz/submissions/`` with ``attempt_id``; identical replay 200,
changed 409 ``SUBMISSION_CONFLICT``, first arrival at/after expiry 409
``ATTEMPT_EXPIRED``), and read own results/history; admins read flat paginated
result rows per quiz and any result. The ASSESS-01 direct submission
(``{"quiz", "answers"}``) is retired (400), so nothing bypasses the attempt.
"""
from collections.abc import Mapping
from contextlib import contextmanager

from django.db import IntegrityError, transaction
from django.db.models import Count, ProtectedError, Q, Sum
from django.db.models.functions import Coalesce
from django.http import Http404
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample, OpenApiParameter, PolymorphicProxySerializer, extend_schema,
)
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from pythagoras.pagination import StandardPagination
from pythagoras.utils import APIError, success_response
from quiz import authoring, lifecycle
from quiz.errors import (
    AssessmentError, AuthoringInvalid, PublicationInvalid, QuizImmutable, is_immutable_db_error,
)
from quiz.models import Attempt, Course, Question, Quiz, Submission
from quiz.permissions import (
    IsActiveAdmin, IsActiveStudent, IsActiveUser, admin_or_read_permissions, is_admin,
    visible_questions, visible_quizzes, visible_submissions,
)
from quiz.serializers import (
    AdminQuestionSerializer, AdminQuestionWriteSerializer, AdminQuizSerializer,
    AdminQuizWriteSerializer, AdminSubmissionDetailSerializer, AdminSubmissionSerializer,
    CourseSerializer, QuizListSerializer, StudentAttemptDetailSerializer,
    StudentQuestionSerializer, StudentQuizSerializer, StudentSubmissionSerializer,
    SubmissionCreateSerializer,
)
from quiz.services import InvalidAnswers
from quiz.throttles import AttemptStartRateThrottle, SubmissionRateThrottle


@contextmanager
def domain_errors():
    """Translate ``quiz.errors`` domain errors into the shared D0 error envelope."""
    try:
        yield
    except PublicationInvalid as exc:
        fields = authoring.publication_fields(exc.fields or {})
        raise serializers.ValidationError(fields or {'non_field_errors': [exc.message]})
    except AuthoringInvalid as exc:
        raise serializers.ValidationError(exc.fields or {'non_field_errors': [exc.message]})
    except AssessmentError as exc:
        raise APIError(exc.code, exc.message, fields=exc.fields)
    except InvalidAnswers as exc:
        raise serializers.ValidationError(exc.as_fields())
    except (Quiz.DoesNotExist, Question.DoesNotExist, Attempt.DoesNotExist):
        raise Http404()
    except ProtectedError:
        raise APIError('RESOURCE_REFERENCED',
                       'This record is referenced by assessment history and cannot be deleted.')
    except IntegrityError as exc:
        # A quiz.0007 trigger refusal (a path the Python guards did not cover).
        if is_immutable_db_error(exc):
            raise APIError(QuizImmutable.code, QuizImmutable.default_message)
        raise


def validated(serializer):
    """``validated_data``, or a 400 whose ``fields`` are flat dotted paths."""
    if not serializer.is_valid():
        raise serializers.ValidationError(authoring.flatten_errors(serializer.errors))
    return serializer.validated_data


def reject_body(request):
    """Lifecycle actions take no body: any key is a 400 field error."""
    data = request.data
    if isinstance(data, Mapping):
        if data:
            raise serializers.ValidationError(
                {str(key): ['This action takes no request body.'] for key in data})
    elif data:
        raise serializers.ValidationError({'non_field_errors': ['This action takes no request body.']})


def role_specific(name, admin, student):
    return PolymorphicProxySerializer(
        component_name=name, serializers=[admin, student], resource_type_field_name=None)


def envelope(data, message):
    return {'success': True, 'data': data, 'message': message}


def error(code, message, fields=None):
    data = {'code': code}
    if fields is not None:
        data['fields'] = fields
    return {'success': False, 'data': data, 'message': message}


def annotate_summary(queryset, admin):
    """``question_count``/``total_points`` per quiz in one query (students: MCQ only)."""
    only = None if admin else Q(question__type=Question.Type.mcq)
    return queryset.annotate(
        summary_question_count=Count('question', filter=only),
        summary_total_points=Coalesce(Sum('question__points', filter=only), 0))


# Schema examples: synthetic, with the real envelope (D0).
EXAMPLE_OPTIONS = [
    {'id': 101, 'text': 'A', 'is_correct': False, 'order': 1},
    {'id': 102, 'text': 'B', 'is_correct': True, 'order': 2},
    {'id': 103, 'text': 'C', 'is_correct': False, 'order': 3},
    {'id': 104, 'text': 'D', 'is_correct': False, 'order': 4},
]
EXAMPLE_QUESTION = {'id': 10, 'quiz': 45, 'type': 'mcq', 'text': 'Question', 'points': 2,
                    'order': 1, 'options': EXAMPLE_OPTIONS}
EXAMPLE_QUIZ = {
    'id': 45, 'title': 'Arabic week 1', 'type': 'arabic', 'description': 'Weekly assessment',
    'related_event': None, 'related_course': None, 'time_limit': 30,
    'deadline': '2026-10-12T20:00:00Z', 'status': 'draft', 'is_active': False,
    'published_at': None, 'closed_at': None, 'created_at': '2026-10-01T09:00:00Z',
    'updated_at': '2026-10-01T09:00:00Z', 'questions': [EXAMPLE_QUESTION],
    'course_title': None, 'event_title': None, 'total_points': 2, 'question_count': 1,
    'content_editable': True,
}
EXAMPLE_LIST_ITEM = {key: value for key, value in EXAMPLE_QUIZ.items()
                     if key not in ('questions', 'content_editable')}
QUIZ_REQUEST = OpenApiExample(
    'Draft', request_only=True,
    value={'title': 'Arabic week 1', 'description': 'Weekly assessment', 'time_limit': 30,
           'deadline': '2026-10-12T20:00:00Z', 'related_event': None})
QUESTION_REQUEST = OpenApiExample(
    'MCQ question', request_only=True,
    value={'type': 'mcq', 'text': 'Question', 'points': 2, 'order': 1, 'options': [
        {'text': 'A', 'order': 1, 'is_correct': False},
        {'text': 'B', 'order': 2, 'is_correct': True},
        {'text': 'C', 'order': 3, 'is_correct': False},
        {'text': 'D', 'order': 4, 'is_correct': False}]})
IMMUTABLE_EXAMPLE = OpenApiExample(
    'Locked content', response_only=True, status_codes=['409'],
    value=error('QUIZ_IMMUTABLE', 'Only the title and description of a published or closed '
                'quiz can change.', {'time_limit': ['This field cannot change after publication.']}))
QUESTION_INVALID_EXAMPLE = OpenApiExample(
    'Invalid options', response_only=True, status_codes=['400'],
    value=error('VALIDATION_ERROR', 'Correct the highlighted fields.', {
        'options': ['Exactly one option must be correct.'],
        'options.2.text': ['This field may not be blank.']}))
PUBLISH_INVALID_EXAMPLE = OpenApiExample(
    'Not publishable', response_only=True, status_codes=['400'],
    value=error('VALIDATION_ERROR', 'Correct the highlighted fields.', {
        'deadline': ['The deadline must be in the future.'],
        'questions.10': ['Question 1: Exactly 4 options are required.']}))
TRANSITION_EXAMPLE = OpenApiExample(
    'Not a draft', response_only=True, status_codes=['409'],
    value=error('INVALID_TRANSITION', 'Only a draft quiz can be published.'))


# Declared so the documented error examples attach; the shared schema class
# replaces their body schema with the D0 error envelope.
ERROR_RESPONSES = {400: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT}


class AdminOrReadView(APIView):
    """Active users read (filtered by role); only active admins write."""

    def get_permissions(self):
        return admin_or_read_permissions(self.request)


class CourseView(AdminOrReadView):

    @extend_schema(
        responses={200: CourseSerializer(many=True)},
        description="Retrieve a list of all courses."
    )
    def get(self, request):
        serializer = CourseSerializer(Course.objects.order_by('pk'), many=True)
        return success_response(serializer.data, "Courses Retrieved Successfully")

    @extend_schema(
        request=CourseSerializer,
        responses={201: CourseSerializer},
        description="Create a new Course (admin only)."
    )
    def post(self, request):
        serializer = CourseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return success_response(serializer.data, "Course Created Successfully",
                                status=status.HTTP_201_CREATED)


class QuizList(AdminOrReadView):
    pagination_class = StandardPagination

    @extend_schema(
        parameters=[OpenApiParameter(
            'status', str, enum=[choice.value for choice in Quiz.Status],
            description='Only quizzes in this state. Students only ever see available '
                        '(published, before deadline) quizzes.')],
        responses={200: QuizListSerializer(many=True)},
        # drf-spectacular wraps a list example item in the page and the envelope.
        examples=[OpenApiExample('Page', response_only=True, value=EXAMPLE_LIST_ITEM)],
        description="Paginated quizzes (`page`, `page_size` default 20 max 100). Admins see every "
                    "quiz, newest first (`-id`), optionally filtered by `status`; students see "
                    "available quizzes (published, deadline in the future) in `id` order. "
                    "`question_count`/`total_points` count MCQ questions only for students."
    )
    def get(self, request):
        admin = is_admin(request.user)
        queryset = visible_quizzes(request.user).select_related('related_course', 'related_event')
        wanted = request.query_params.get('status')
        if wanted is not None:
            if wanted not in Quiz.Status.values:
                raise serializers.ValidationError({'status': [
                    f'Select one of: {", ".join(Quiz.Status.values)}.']})
            queryset = queryset.filter(status=wanted)
        queryset = annotate_summary(queryset, admin).order_by('-pk' if admin else 'pk')
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request, view=self)
        data = QuizListSerializer(page, many=True, context={'request': request}).data
        return success_response(paginator.get_paginated_response(data).data, 'Quizzes retrieved.')

    @extend_schema(
        request=AdminQuizWriteSerializer,
        responses={201: AdminQuizSerializer},
        examples=[QUIZ_REQUEST, OpenApiExample('Created', response_only=True, status_codes=['201'],
                                               value=envelope({**EXAMPLE_QUIZ, 'questions': [],
                                                               'total_points': 0,
                                                               'question_count': 0},
                                                              'Draft quiz created.'))],
        description="Create a draft quiz (admin only). `time_limit`/`deadline` may be null while "
                    "drafting; publication requires both. Unknown keys are rejected."
    )
    def post(self, request):
        serializer = AdminQuizWriteSerializer(data=request.data)
        validated(serializer)
        with transaction.atomic(), domain_errors():
            serializer.save()
        return success_response(serializer.data, 'Draft quiz created.',
                                status=status.HTTP_201_CREATED)


class QuizDetail(AdminOrReadView):

    def get_quiz(self, quiz_id):
        return get_object_or_404(visible_quizzes(self.request.user), pk=quiz_id)

    @extend_schema(
        responses={200: role_specific('QuizDetail', AdminQuizSerializer, StudentQuizSerializer)},
        examples=[OpenApiExample('Admin detail', response_only=True,
                                 value=envelope(EXAMPLE_QUIZ, 'Quiz retrieved.'))],
        description='Retrieve a quiz. Admins receive every question with options, persisted IDs '
                    'and `is_correct`; students receive an available (published, before '
                    'deadline) quiz with MCQ questions and options only, never keys.'
    )
    def get(self, request, quiz_id):
        quiz = self.get_quiz(quiz_id)
        serializer_class = AdminQuizSerializer if is_admin(request.user) else StudentQuizSerializer
        return success_response(serializer_class(quiz).data, 'Quiz retrieved.')

    def write(self, request, quiz_id, partial):
        with transaction.atomic(), domain_errors():
            quiz = get_object_or_404(Quiz.objects.select_for_update(), pk=quiz_id)
            serializer = AdminQuizWriteSerializer(quiz, data=request.data, partial=partial)
            validated(serializer)
            serializer.save()
        return success_response(serializer.data, 'Quiz updated.')

    @extend_schema(
        request=AdminQuizWriteSerializer,
        responses={200: AdminQuizSerializer, **ERROR_RESPONSES},
        examples=[OpenApiExample('Metadata correction', request_only=True,
                                 value={'title': 'Arabic week 1 (corrected)'}),
                  IMMUTABLE_EXAMPLE],
        description='Change a quiz (admin only); omitted keys stay unchanged. A draft accepts '
                    'every authoring field. A published or closed quiz accepts title/description '
                    'corrections only: any other changed value is 409 QUIZ_IMMUTABLE with '
                    '`fields`. Corrections never rewrite stored result snapshots. status/'
                    'is_active change only through publish/close.'
    )
    def patch(self, request, quiz_id):
        return self.write(request, quiz_id, partial=True)

    @extend_schema(
        request=AdminQuizWriteSerializer,
        responses={200: AdminQuizSerializer},
        description='Legacy full update (admin only): same rules as PATCH, with `title` required.'
    )
    def put(self, request, quiz_id):
        return self.write(request, quiz_id, partial=False)

    @extend_schema(
        responses={204: None},
        description="Delete a draft quiz without attempts or submissions (admin only). A quiz "
                    "with attempts/submissions returns 409 RESOURCE_REFERENCED so results are "
                    "never cascaded; a published/closed quiz returns 409 QUIZ_IMMUTABLE."
    )
    def delete(self, request, quiz_id):
        quiz = self.get_quiz(quiz_id)
        with domain_errors():
            lifecycle.delete_draft_quiz(quiz.pk)
        return Response(status=status.HTTP_204_NO_CONTENT)


QUESTION_RULES = (
    'Body: `type` ("mcq", default), `text`, `points` (positive integer, default 1), `order` '
    '(optional, 1-10000, unique in the quiz; omitted = appended) and `options`: exactly four '
    '`{text, order, is_correct}` objects (no IDs) with non-empty texts distinct after trim + '
    'whitespace collapse + casefold, exactly one `is_correct: true`, unique option orders '
    '(omitted ones fill free positions). Only while the quiz is content-editable (a draft '
    'without attempts/submissions); otherwise 409 QUIZ_IMMUTABLE. Field errors use dotted '
    'paths such as `options.2.text`.'
)


def create_question_response(quiz_id, data):
    with domain_errors():
        question = authoring.create_question(quiz_id, data)
    return success_response(AdminQuestionSerializer(question).data, 'Question created.',
                            status=status.HTTP_201_CREATED)


class QuizQuestionList(APIView):
    """Canonical question authoring path: the quiz comes from the URL."""

    permission_classes = [IsActiveAdmin]

    @extend_schema(
        request=AdminQuestionWriteSerializer,
        responses={201: AdminQuestionSerializer, **ERROR_RESPONSES},
        examples=[QUESTION_REQUEST,
                  OpenApiExample('Created', response_only=True, status_codes=['201'],
                                 value=envelope(EXAMPLE_QUESTION, 'Question created.')),
                  QUESTION_INVALID_EXAMPLE,
                  OpenApiExample('Locked quiz', response_only=True, status_codes=['409'],
                                 value=error('QUIZ_IMMUTABLE', 'Questions and options cannot be '
                                             'added, changed, reordered or removed once a quiz '
                                             'is published or has attempts or submissions; '
                                             'author a new quiz instead.'))],
        description='Add an MCQ question with its options to a quiz (admin only). '
                    + QUESTION_RULES,
    )
    def post(self, request, quiz_id):
        quiz = get_object_or_404(Quiz, pk=quiz_id)
        data = validated(AdminQuestionWriteSerializer(data=request.data))
        return create_question_response(quiz.pk, data)


class QuestionList(APIView):
    """Legacy admin question list/create path. Students read questions through quiz detail."""

    permission_classes = [IsActiveAdmin]
    pagination_class = StandardPagination

    @extend_schema(
        responses={200: AdminQuestionSerializer(many=True)},
        description="Paginated questions of every quiz with answer keys (admin only), in "
                    "(quiz, order, id) order."
    )
    def get(self, request):
        queryset = Question.objects.order_by('quiz_id', 'order', 'pk')
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request, view=self)
        data = AdminQuestionSerializer(page, many=True).data
        return success_response(paginator.get_paginated_response(data).data,
                                'Questions retrieved.')

    @extend_schema(
        request=AdminQuestionWriteSerializer,
        responses={201: AdminQuestionSerializer},
        description="Legacy alias of `POST /quiz/quizzes/{id}/questions/` with the quiz named "
                    "by an integer `quiz` key (admin only). " + QUESTION_RULES,
    )
    def post(self, request):
        data = validated(AdminQuestionWriteSerializer(data=request.data,
                                                      context={'quiz_field': 'required'}))
        quiz = data.pop('quiz')
        return create_question_response(quiz.pk, data)


class QuestionDetail(AdminOrReadView):

    def get_question(self, question_id):
        return get_object_or_404(visible_questions(self.request.user), pk=question_id)

    @extend_schema(
        responses={200: role_specific('QuestionDetail', AdminQuestionSerializer,
                                      StudentQuestionSerializer)},
        description='Retrieve a question. Admins receive answer keys; students receive an '
                    'MCQ question of an available quiz without keys.'
    )
    def get(self, request, question_id):
        question = self.get_question(question_id)
        serializer_class = (AdminQuestionSerializer if is_admin(request.user)
                            else StudentQuestionSerializer)
        return success_response(serializer_class(question).data, 'Question retrieved.')

    def write(self, request, question_id, patch):
        question = self.get_question(question_id)
        data = validated(AdminQuestionWriteSerializer(
            question, data=request.data, context={'quiz_field': 'same', 'patch': patch}))
        with domain_errors():
            question = authoring.update_question(question.pk, data)
        return success_response(AdminQuestionSerializer(question).data, 'Question updated.')

    @extend_schema(
        request=AdminQuestionWriteSerializer,
        responses={200: AdminQuestionSerializer, **ERROR_RESPONSES},
        examples=[QUESTION_REQUEST, QUESTION_INVALID_EXAMPLE],
        description='Change a draft question (admin only); omitted keys stay unchanged. Sent '
                    '`options` replace the whole set (persisted option IDs are kept by position). '
                    'Legacy non-MCQ questions cannot be edited (400 `type`). ' + QUESTION_RULES,
    )
    def patch(self, request, question_id):
        return self.write(request, question_id, patch=True)

    @extend_schema(
        request=AdminQuestionWriteSerializer,
        responses={200: AdminQuestionSerializer},
        description='Legacy update (admin only): same rules as PATCH, with `text` required. '
                    'A `quiz` key must name the question\'s own quiz.'
    )
    def put(self, request, question_id):
        return self.write(request, question_id, patch=False)

    @extend_schema(
        responses={204: None},
        description="Delete a question of a content-editable draft (admin only); otherwise 409 "
                    "QUIZ_IMMUTABLE."
    )
    def delete(self, request, question_id):
        question = self.get_question(question_id)
        with domain_errors():
            authoring.delete_question(question.pk)
        return Response(status=status.HTTP_204_NO_CONTENT)


class QuizPublishView(APIView):

    permission_classes = [IsActiveAdmin]

    @extend_schema(
        request=None,
        responses={200: AdminQuizSerializer, **ERROR_RESPONSES},
        examples=[OpenApiExample('Published', response_only=True, value=envelope(
                      {**EXAMPLE_QUIZ, 'status': 'published', 'is_active': True,
                       'published_at': '2026-10-02T08:00:00Z', 'content_editable': False},
                      'Quiz published.')),
                  PUBLISH_INVALID_EXAMPLE, TRANSITION_EXAMPLE],
        description='Validate the whole quiz graph and publish a draft (admin only), atomically '
                    'under the quiz row lock. Requirements: time_limit 1-180, a future deadline, '
                    'at least one question, only MCQ questions with positive points, exactly '
                    'four distinct non-empty options and exactly one correct option each, and '
                    'total points within storage limits. Failures are 400 VALIDATION_ERROR with '
                    'field paths (`time_limit`, `deadline`, `questions`, `questions.<id>`, '
                    '`total_points`). A published or closed quiz is 409 INVALID_TRANSITION. '
                    'No request body.'
    )
    def post(self, request, quiz_id):
        reject_body(request)
        quiz = get_object_or_404(Quiz, pk=quiz_id)
        with domain_errors():
            quiz, changed = lifecycle.publish_quiz(quiz.pk)
        if not changed:
            raise APIError('INVALID_TRANSITION', 'This quiz is already published.')
        return success_response(AdminQuizSerializer(quiz).data, 'Quiz published.')


class QuizCloseView(APIView):

    permission_classes = [IsActiveAdmin]

    @extend_schema(
        request=None,
        responses={200: AdminQuizSerializer, **ERROR_RESPONSES},
        examples=[OpenApiExample('Closed', response_only=True, value=envelope(
                      {**EXAMPLE_QUIZ, 'status': 'closed', 'published_at': '2026-10-02T08:00:00Z',
                       'closed_at': '2026-10-05T08:00:00Z', 'content_editable': False},
                      'Quiz closed.')),
                  OpenApiExample('Draft', response_only=True, status_codes=['409'],
                                 value=error('INVALID_TRANSITION',
                                             'Only a published quiz can be closed.'))],
        description='Close a published quiz (admin only): no new attempts; attempts already '
                    'started keep their original expiry and may still be submitted. Closing a '
                    'closed quiz is idempotent (200, unchanged `closed_at`); a draft is 409 '
                    'INVALID_TRANSITION. Closed is terminal. No request body.'
    )
    def post(self, request, quiz_id):
        reject_body(request)
        quiz = get_object_or_404(Quiz, pk=quiz_id)
        with domain_errors():
            quiz, changed = lifecycle.close_quiz(quiz.pk)
        return success_response(AdminQuizSerializer(quiz).data,
                                'Quiz closed.' if changed else 'Quiz already closed.')


# ----------------------------------------------------------------------------
# ASSESS-04: attempts, submissions and results
# ----------------------------------------------------------------------------

EXAMPLE_ATTEMPT = {
    'id': 123, 'quiz_id': 45, 'quiz_title': 'Arabic week 1', 'status': 'in_progress',
    'started_at': '2026-10-12T10:00:00Z', 'expires_at': '2026-10-12T10:30:00Z',
    'server_time': '2026-10-12T10:00:00Z', 'submission_id': None,
    'questions': [{'id': 10, 'text': 'Question', 'points': 2, 'order': 1, 'options': [
        {'id': 101, 'text': 'A', 'order': 1}, {'id': 102, 'text': 'B', 'order': 2},
        {'id': 103, 'text': 'C', 'order': 3}, {'id': 104, 'text': 'D', 'order': 4}]}],
}
EXAMPLE_RESULT = {
    'id': 9, 'quiz_id': 45, 'quiz_title': 'Arabic week 1', 'attempt_id': 123,
    'status': 'submitted', 'score': 2, 'total_points': 2,
    'submitted_at': '2026-10-12T10:20:00Z', 'graded_at': '2026-10-12T10:20:00Z',
}
EXAMPLE_ADMIN_ROW = {**EXAMPLE_RESULT, 'student_id': 7, 'student_name': 'Amina Yusuf',
                     'student_email': 'amina@example.test', 'is_legacy': False}
EXAMPLE_ADMIN_DETAIL = {
    **EXAMPLE_ADMIN_ROW, 'answers': {'10': 102},
    'grading_snapshot': {'version': 1, 'quiz_id': 45, 'quiz_title': 'Arabic week 1', 'score': 2,
                         'total_points': 2, 'questions': [
                             {'question_id': 10, 'order': 1, 'points': 2,
                              'selected_option_id': 102, 'awarded_points': 2}]},
    'started_at': '2026-10-12T10:00:00Z', 'expires_at': '2026-10-12T10:30:00Z',
}
SUBMIT_REQUEST = OpenApiExample('Submit', request_only=True,
                                value={'attempt_id': 123, 'answers': {'10': 102}})
NOT_FOUND_EXAMPLE = OpenApiExample(
    'Not found', response_only=True, status_codes=['404'],
    value=error('NOT_FOUND', 'The requested resource was not found.'))


def positive_int_param(request, name):
    """Optional positive-integer query filter (400 field error otherwise)."""
    raw = request.query_params.get(name)
    if raw is None:
        return None
    if not (raw.isascii() and raw.isdigit()) or int(raw) < 1:
        raise serializers.ValidationError({name: ['A positive integer ID is required.']})
    return int(raw)


def attempt_response(attempt, message, status_code=status.HTTP_200_OK):
    attempt = Attempt.objects.select_related('quiz').get(pk=attempt.pk)
    data = StudentAttemptDetailSerializer(attempt, context={'now': lifecycle.server_now()}).data
    return success_response(data, message, status=status_code)


class QuizAttemptStartView(APIView):
    """Start, or resume, the student's single timed attempt (A09/A10)."""

    permission_classes = [IsActiveStudent]
    throttle_classes = [AttemptStartRateThrottle]

    @extend_schema(
        summary='Start or resume my attempt (student only)',
        request=None,
        responses={201: StudentAttemptDetailSerializer, 200: StudentAttemptDetailSerializer,
                   **ERROR_RESPONSES, 404: OpenApiTypes.OBJECT},
        examples=[
            OpenApiExample('Started', response_only=True, status_codes=['201'],
                           value=envelope(EXAMPLE_ATTEMPT, 'Attempt started.')),
            OpenApiExample('Existing attempt', response_only=True, status_codes=['200'],
                           value=envelope({**EXAMPLE_ATTEMPT,
                                           'server_time': '2026-10-12T10:05:00Z'},
                                          'Attempt retrieved.')),
            OpenApiExample('Not available', response_only=True, status_codes=['409'],
                           value=error('QUIZ_UNAVAILABLE', 'This quiz is not available.')),
            NOT_FOUND_EXAMPLE,
        ],
        description=(
            'Idempotent start (A09). The first call creates the attempt at the server instant '
            '(201): `expires_at = min(started_at + time_limit, deadline)` (A10). Any later call '
            'returns the same attempt unchanged (200), also after the quiz closed or its '
            'deadline/expiry passed, so a retry never creates or re-times an attempt. Only a '
            'new attempt needs an available quiz (published, deadline in the future): a '
            'closed or past-deadline quiz is 409 QUIZ_UNAVAILABLE, a draft or missing quiz '
            '404 (checked first, so drafts look missing to every student); a legacy '
            'submission for the quiz is 409 SUBMISSION_CONFLICT. No request '
            'body. Throttled at 20/min per student (429).'),
    )
    def post(self, request, quiz_id):
        reject_body(request)
        with domain_errors():
            # A draft raises Quiz.DoesNotExist (404) before any other check.
            attempt, created = lifecycle.start_attempt(quiz_id, request.user)
        if created:
            return attempt_response(attempt, 'Attempt started.', status.HTTP_201_CREATED)
        return attempt_response(attempt, 'Attempt retrieved.')


class AttemptDetailView(APIView):
    """The student's own attempt with its key-free questions and server time."""

    permission_classes = [IsActiveStudent]

    @extend_schema(
        summary='Get my attempt (student only)',
        responses={200: StudentAttemptDetailSerializer, 404: OpenApiTypes.OBJECT},
        examples=[OpenApiExample('Attempt', response_only=True,
                                 value=envelope(EXAMPLE_ATTEMPT, 'Attempt retrieved.')),
                  OpenApiExample('Submitted', response_only=True, value=envelope(
                      {**EXAMPLE_ATTEMPT, 'status': 'submitted',
                       'server_time': '2026-10-12T10:21:00Z', 'submission_id': 9},
                      'Attempt retrieved.')),
                  NOT_FOUND_EXAMPLE],
        description=(
            'Own attempts only (another student\'s attempt is 404). `status` is effective at '
            '`server_time` (the database clock): an unfinalized attempt at/after `expires_at` '
            'reads `expired`. `started_at`/`expires_at` never change; `submission_id` is null '
            'until finalized. Questions are MCQ only, in persisted order, with option IDs and '
            'text and never a correctness key. Clients anchor their countdown to '
            '`server_time`/`expires_at`.'),
    )
    def get(self, request, attempt_id):
        attempt = get_object_or_404(Attempt.objects.filter(student=request.user), pk=attempt_id)
        return attempt_response(attempt, 'Attempt retrieved.')


class QuizSubmissionView(APIView):
    """Admin results of one quiz: flat, paginated rows."""

    permission_classes = [IsActiveAdmin]
    pagination_class = StandardPagination

    @extend_schema(
        summary='Results of a quiz (admin only)',
        responses={200: AdminSubmissionSerializer(many=True), 404: OpenApiTypes.OBJECT},
        examples=[OpenApiExample('Page', response_only=True, value=EXAMPLE_ADMIN_ROW)],
        description=(
            'Paginated (`page`, `page_size` default 20 max 100) finalized results of one quiz, '
            'newest first (`-submitted_at`, `-id`). Flat rows: result fields plus `student_id`, '
            '`student_name`, `student_email` and `is_legacy`. Legacy rows (before attempts) '
            'have null `attempt_id`/`total_points`. Expired attempts have no result row.'),
    )
    def get(self, request, quiz_id):
        quiz = get_object_or_404(Quiz, pk=quiz_id)
        queryset = Submission.objects.filter(quiz=quiz).select_related(
            'quiz', 'student').order_by('-submitted_at', '-pk')
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request, view=self)
        data = AdminSubmissionSerializer(page, many=True).data
        return success_response(paginator.get_paginated_response(data).data,
                                'Results retrieved.')


class UserSubmissionView(APIView):
    pagination_class = StandardPagination

    def get_permissions(self):
        if self.request.method == 'POST':
            return [IsActiveStudent()]
        return [IsActiveUser()]

    def get_throttles(self):
        if self.request.method == 'POST':
            return [SubmissionRateThrottle()]
        return []

    @extend_schema(
        summary='My results history',
        parameters=[
            OpenApiParameter('attempt', int, description='Only the result of this attempt.'),
            OpenApiParameter('quiz', int, description='Only results of this quiz.'),
        ],
        responses={200: StudentSubmissionSerializer(many=True)},
        examples=[OpenApiExample('Page', response_only=True, value=EXAMPLE_RESULT)],
        description=(
            'The authenticated user\'s own finalized results, newest first (`-submitted_at`, '
            '`-id`), paginated (`page`, `page_size` default 20 max 100), optionally filtered by '
            '`attempt` and/or `quiz` (positive integer IDs, else 400). History stays available '
            'after closing/deadline. No answers, snapshots or keys. Admins have no results '
            'of their own (use `/quiz/quizzes/{id}/submissions/`).'),
    )
    def get(self, request):
        queryset = Submission.objects.filter(student=request.user).select_related('quiz')
        attempt_id = positive_int_param(request, 'attempt')
        quiz_id = positive_int_param(request, 'quiz')
        if attempt_id is not None:
            queryset = queryset.filter(attempt_id=attempt_id)
        if quiz_id is not None:
            queryset = queryset.filter(quiz_id=quiz_id)
        queryset = queryset.order_by('-submitted_at', '-pk')
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request, view=self)
        data = StudentSubmissionSerializer(page, many=True).data
        return success_response(paginator.get_paginated_response(data).data,
                                'Results retrieved.')

    @extend_schema(
        summary='Submit my attempt (student only)',
        request=SubmissionCreateSerializer,
        responses={201: StudentSubmissionSerializer, 200: StudentSubmissionSerializer,
                   **ERROR_RESPONSES, 404: OpenApiTypes.OBJECT},
        examples=[
            SUBMIT_REQUEST,
            OpenApiExample('Graded', response_only=True, status_codes=['201'],
                           value=envelope(EXAMPLE_RESULT, 'Result saved.')),
            OpenApiExample('Replay', response_only=True, status_codes=['200'],
                           value=envelope(EXAMPLE_RESULT, 'Result already saved.')),
            OpenApiExample('Invalid answers', response_only=True, status_codes=['400'],
                           value=error('VALIDATION_ERROR', 'Correct the highlighted fields.',
                                       {'answers': {'10': ['Not an option of this question.']}})),
            OpenApiExample('Expired', response_only=True, status_codes=['409'],
                           value=error('ATTEMPT_EXPIRED', 'This attempt has expired.')),
            OpenApiExample('Conflict', response_only=True, status_codes=['409'],
                           value=error('SUBMISSION_CONFLICT', 'Different answers were already '
                                       'submitted for this attempt.')),
            NOT_FOUND_EXAMPLE,
        ],
        description=(
            'Finalize the student\'s own attempt once, graded on the server. Body: '
            '`attempt_id` and `answers` mapping decimal question-ID strings of the quiz to '
            'the integer ID of one option of that question; unanswered questions score zero. '
            'Every other key (`student`, `score`, `quiz`, ...) is 400. Order: 400 structure; '
            '404 a missing or another student\'s attempt; an existing result is returned for '
            'an identical answer map (200, also after expiry/closing) and different answers '
            'are 409 SUBMISSION_CONFLICT (never overwritten); a first submission arriving '
            'at/after `expires_at` is 409 ATTEMPT_EXPIRED (no grade); invalid answers are '
            '400 with `fields.answers.<question id>`; then grading by persisted question '
            'points and correct option, the immutable snapshot and the attempt status are '
            'saved atomically (201). Closing the quiz does not block an unexpired attempt. '
            'Throttled at 60/min per student (429).'),
    )
    def post(self, request):
        data = validated(SubmissionCreateSerializer(data=request.data))
        with domain_errors():
            submission, created = lifecycle.finalize_attempt(
                data['attempt_id'], request.user, data['answers'])
        submission = Submission.objects.select_related('quiz').get(pk=submission.pk)
        payload = StudentSubmissionSerializer(submission).data
        if not created:
            return success_response(payload, 'Result already saved.')
        return success_response(payload, 'Result saved.', status=status.HTTP_201_CREATED)


class SubmissionDetailView(APIView):

    permission_classes = [IsActiveUser]

    @extend_schema(
        summary='Get a result',
        description='Students see only their own result (another student\'s is 404) without '
                    'answers or keys. Admins see any result with the student\'s identity, the '
                    'stored answers, the grading snapshot and the attempt timing. Results are '
                    'immutable: there is no grade mutation route.',
        responses={200: role_specific('SubmissionDetail', AdminSubmissionDetailSerializer,
                                      StudentSubmissionSerializer), 404: OpenApiTypes.OBJECT},
        examples=[OpenApiExample('Student result', response_only=True,
                                 value=envelope(EXAMPLE_RESULT, 'Result retrieved.')),
                  OpenApiExample('Admin result', response_only=True,
                                 value=envelope(EXAMPLE_ADMIN_DETAIL, 'Result retrieved.')),
                  NOT_FOUND_EXAMPLE],
    )
    def get(self, request, pk):
        queryset = visible_submissions(request.user).select_related('attempt')
        submission = get_object_or_404(queryset, pk=pk)
        serializer_class = (AdminSubmissionDetailSerializer if is_admin(request.user)
                            else StudentSubmissionSerializer)
        return success_response(serializer_class(submission).data, 'Result retrieved.')
