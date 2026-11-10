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

Student availability is ``published`` with a future deadline. Temporary until
ASSESS-04: ``POST /quiz/submissions/`` grades an MCQ answer map directly,
creating and finalizing the student's single attempt in one step.
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
from quiz.models import Course, Question, Quiz, Submission
from quiz.permissions import (
    IsActiveAdmin, IsActiveStudent, IsActiveUser, admin_or_read_permissions, is_admin,
    visible_questions, visible_quizzes, visible_submissions,
)
from quiz.serializers import (
    AdminQuestionSerializer, AdminQuestionWriteSerializer, AdminQuizSerializer,
    AdminQuizWriteSerializer, AdminSubmissionDetailSerializer, AdminSubmissionSerializer,
    CourseSerializer, QuizListSerializer, StudentQuestionSerializer, StudentQuizSerializer,
    StudentSubmissionSerializer, SubmissionCreateSerializer,
)
from quiz.services import InvalidAnswers, QuizService


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
    except (Quiz.DoesNotExist, Question.DoesNotExist):
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
        'questions.10': ['Exactly 4 options are required.']}))
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
    '(optional, unique in the quiz; omitted = appended) and `options`: exactly four '
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


class QuizSubmissionView(APIView):

    permission_classes = [IsActiveAdmin]

    @extend_schema(
        summary="Get all submissions for a Quiz (Admin Only)",
        description="Retrieves a list of all student submissions for a specific quiz ID.",
        responses={200: AdminSubmissionSerializer(many=True)}
    )
    def get(self, request, quiz_id):
        quiz = get_object_or_404(Quiz, pk=quiz_id)
        submissions = Submission.objects.filter(quiz=quiz).select_related(
            'quiz', 'student').order_by('pk')
        serializer = AdminSubmissionSerializer(submissions, many=True)
        return success_response(
            serializer.data, f"Found {submissions.count()} submissions for {quiz.title}")


class UserSubmissionView(APIView):

    def get_permissions(self):
        if self.request.method == 'POST':
            return [IsActiveStudent()]
        return [IsActiveUser()]

    @extend_schema(
        summary="Get my submission history",
        description="The authenticated user's own submissions, without answer keys.",
        responses={200: StudentSubmissionSerializer(many=True)}
    )
    def get(self, request):
        submissions = Submission.objects.filter(student=request.user).select_related(
            'quiz').order_by('pk')
        serializer = StudentSubmissionSerializer(submissions, many=True)
        return success_response(serializer.data, "Your submission history retrieved successfully")

    @extend_schema(
        summary="Submit a Quiz (student only)",
        description=(
            "Temporary direct submission of an available quiz (published, before its "
            "deadline), graded on the server; it creates and finalizes the student's single "
            "attempt. `answers` maps "
            "decimal question-ID strings to the selected option ID. The student and score "
            "come from the server; `student`, `score` and other keys are rejected. One "
            "submission per quiz: an identical replay returns 200 with the saved result, "
            "different answers return 409 SUBMISSION_CONFLICT."
        ),
        request=SubmissionCreateSerializer,
        responses={201: StudentSubmissionSerializer, 200: StudentSubmissionSerializer},
    )
    def post(self, request):
        serializer = SubmissionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            submission, created = QuizService.submit(
                quiz_id=serializer.validated_data['quiz'],
                student=request.user,
                answers=serializer.validated_data['answers'],
            )
        except InvalidAnswers as exc:
            raise serializers.ValidationError(exc.as_fields())
        except AssessmentError as exc:
            # SUBMISSION_CONFLICT, or ATTEMPT_EXPIRED for an existing expired attempt.
            message = exc.message
            if exc.code == 'SUBMISSION_CONFLICT':
                message = 'You have already submitted different answers for this quiz.'
            raise APIError(exc.code, message)
        data = StudentSubmissionSerializer(submission).data
        if not created:
            return success_response(data, "Submission already saved.")
        return success_response(data, "Quiz submitted and graded successfully!",
                                status=status.HTTP_201_CREATED)


class SubmissionDetailView(APIView):

    permission_classes = [IsActiveUser]

    @extend_schema(
        summary="View Submission Details",
        description="Students see only their own submission, without answer keys (another "
                    "student's submission is 404). Admins see any, with the quiz and keys.",
        responses={200: role_specific('SubmissionDetail', AdminSubmissionDetailSerializer,
                                      StudentSubmissionSerializer)},
    )
    def get(self, request, pk):
        submission = get_object_or_404(visible_submissions(request.user), pk=pk)
        serializer_class = (AdminSubmissionDetailSerializer if is_admin(request.user)
                            else StudentSubmissionSerializer)
        return success_response(serializer_class(submission).data,
                                "Student's Submission for a quiz retrieved successfully")
