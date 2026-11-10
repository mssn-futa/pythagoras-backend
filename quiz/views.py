"""Legacy assessment endpoints with shared role and object permissions.

Every handler resolves permissions through ``get_permissions`` (never by
assigning ``permission_classes`` inside a handler), and every object lookup goes
through a role-aware queryset from ``quiz.permissions`` before any serializer
or mutation runs. Student responses use only ``Student*`` serializers.

ASSESS-02: student availability is ``published`` with a future deadline.
Quiz/question writes are draft authoring only; published content returns 409
``QUIZ_IMMUTABLE`` (title/description corrections excepted). Temporary until
ASSESS-04: ``POST /quiz/submissions/`` grades an MCQ answer map directly,
creating and finalizing the student's single attempt in one step; one
submission per student and quiz, and an identical replay returns the saved
result.
"""
from contextlib import contextmanager

from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import PolymorphicProxySerializer, extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from pythagoras.utils import APIError, success_response
from quiz.models import Course, Question, Quiz, Submission
from quiz.permissions import (
    IsActiveAdmin, IsActiveStudent, IsActiveUser, admin_or_read_permissions, is_admin,
    visible_questions, visible_quizzes, visible_submissions,
)
from quiz.serializers import (
    AdminQuestionSerializer, AdminQuestionWriteSerializer, AdminQuizSerializer,
    AdminSubmissionDetailSerializer, AdminSubmissionSerializer, CourseSerializer,
    QuizListSerializer, StudentQuestionSerializer, StudentQuizSerializer,
    StudentSubmissionSerializer, SubmissionCreateSerializer,
)
from quiz import lifecycle
from quiz.errors import AssessmentError, PublicationInvalid, QuizImmutable, is_immutable_db_error
from quiz.services import InvalidAnswers, QuizService


@contextmanager
def domain_errors():
    """Translate ``quiz.errors`` domain errors into the shared D0 error envelope."""
    try:
        yield
    except PublicationInvalid as exc:
        raise serializers.ValidationError(exc.fields or {'non_field_errors': [exc.message]})
    except AssessmentError as exc:
        raise APIError(exc.code, exc.message, fields=exc.fields)
    except InvalidAnswers as exc:
        raise serializers.ValidationError(exc.as_fields())
    except ProtectedError:
        raise APIError('RESOURCE_REFERENCED',
                       'This record is referenced by assessment history and cannot be deleted.')
    except IntegrityError as exc:
        # A quiz.0007 trigger refusal (a path the Python guards did not cover).
        if is_immutable_db_error(exc):
            raise APIError(QuizImmutable.code, QuizImmutable.default_message)
        raise


def role_specific(name, admin, student):
    return PolymorphicProxySerializer(
        component_name=name, serializers=[admin, student], resource_type_field_name=None)


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

    @extend_schema(
        responses={200: QuizListSerializer(many=True)},
        description="Admins list every quiz; students list published quizzes whose "
                    "deadline has not passed."
    )
    def get(self, request):
        queryset = visible_quizzes(request.user).select_related(
            'related_course', 'related_event').order_by('pk')
        serializer = QuizListSerializer(queryset, many=True, context={'request': request})
        return success_response(serializer.data, "Quizzes Retrieved Successfully")

    @extend_schema(
        request=AdminQuizSerializer,
        responses={201: AdminQuizSerializer},
        description="Create a new Quiz (admin only)."
    )
    def post(self, request):
        serializer = AdminQuizSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with domain_errors():
            serializer.save()
        return success_response(serializer.data, "Quiz Created Successfully",
                                status=status.HTTP_201_CREATED)


class QuizDetail(AdminOrReadView):

    def get_quiz(self, quiz_id):
        return get_object_or_404(visible_quizzes(self.request.user), pk=quiz_id)

    @extend_schema(
        responses={200: role_specific('QuizDetail', AdminQuizSerializer, StudentQuizSerializer)},
        description='Retrieve a quiz. Admins receive answer keys; students receive an available '
                    '(published, before deadline) quiz with MCQ questions and options only, '
                    'never keys.'
    )
    def get(self, request, quiz_id):
        quiz = self.get_quiz(quiz_id)
        serializer_class = AdminQuizSerializer if is_admin(request.user) else StudentQuizSerializer
        return success_response(serializer_class(quiz).data, "Quiz Retrieved Successfully")

    @extend_schema(
        request=AdminQuizSerializer,
        responses={200: AdminQuizSerializer},
        description='Update a quiz (admin only). A draft accepts every authoring field; a '
                    'published or closed quiz only title/description (other changes are 409 '
                    'QUIZ_IMMUTABLE). status/is_active change only through publish/close.'
    )
    def put(self, request, quiz_id):
        with transaction.atomic(), domain_errors():
            quiz = get_object_or_404(visible_quizzes(request.user).select_for_update(),
                                     pk=quiz_id)
            serializer = AdminQuizSerializer(quiz, data=request.data)
            serializer.is_valid(raise_exception=True)
            serializer.save()
        return success_response(serializer.data, "Quiz Updated Successfully")

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


class QuestionList(APIView):
    """Admin authoring/list path. Students read questions through quiz detail."""

    permission_classes = [IsActiveAdmin]

    @extend_schema(
        responses={200: AdminQuestionSerializer(many=True)},
        description="List every question with answer keys (admin only)."
    )
    def get(self, request):
        queryset = Question.objects.order_by('pk')
        serializer = AdminQuestionSerializer(queryset, many=True)
        return success_response(serializer.data, "Questions Retrieved Successfully")

    @extend_schema(
        request=AdminQuestionWriteSerializer,
        responses={201: AdminQuestionSerializer},
        description="Create an MCQ question with options in a draft quiz (admin only). "
                    "A published/closed quiz returns 409 QUIZ_IMMUTABLE."
    )
    def post(self, request):
        serializer = AdminQuestionWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic(), domain_errors():
            serializer.save()
        return success_response(serializer.data, "Question Created Successfully",
                                status=status.HTTP_201_CREATED)


class QuestionDetail(AdminOrReadView):

    def get_question(self, question_id):
        return get_object_or_404(visible_questions(self.request.user), pk=question_id)

    @extend_schema(
        responses={200: role_specific('QuestionDetail', AdminQuestionSerializer,
                                      StudentQuestionSerializer)},
        description='Retrieve a question. Admins receive answer keys; students receive an '
                    'MCQ question of an active quiz without keys.'
    )
    def get(self, request, question_id):
        question = self.get_question(question_id)
        serializer_class = (AdminQuestionSerializer if is_admin(request.user)
                            else StudentQuestionSerializer)
        return success_response(serializer_class(question).data, "Question Retrieved Successfully")

    @extend_schema(
        request=AdminQuestionWriteSerializer,
        responses={200: AdminQuestionSerializer},
        description='Replace a draft question and its options (admin only). A question of a '
                    'published/closed quiz returns 409 QUIZ_IMMUTABLE.'
    )
    def put(self, request, question_id):
        question = self.get_question(question_id)
        serializer = AdminQuestionWriteSerializer(question, data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic(), domain_errors():
            serializer.save()
        return success_response(serializer.data, "Question Updated Successfully")

    @extend_schema(
        responses={204: None},
        description="Delete a draft question (admin only); 409 QUIZ_IMMUTABLE once published."
    )
    def delete(self, request, question_id):
        question = self.get_question(question_id)
        with transaction.atomic(), domain_errors():
            question.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


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
