from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.db import transaction

from rest_framework import status, permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import PermissionDenied
from drf_spectacular.utils import extend_schema
from accounts.permissions import IsAdmin, IsAdminOrReadOnly
from quiz.models import Course, Question, Quiz, Submission
from .serializers import (
                CourseSerializer, QuestionSerializer, QuestionCreateSerializer, 
                QuizSerializer, QuizListSerializer, SubmissionCreateSerializer, 
                SubmissionDetailSerializer, SubmissionSerializer
)
from quiz.services import QuizService

class CourseView(APIView):
    
    permission_classes = [IsAdminOrReadOnly]

    @extend_schema(
        responses={200: CourseSerializer(many=True)},
        description="Retrieve a list of all courses."
    )
    def get(self, request):
        queryset = Course.objects.all()
        serializer = CourseSerializer(queryset, many=True)

        return Response(
            {
                "success": True,
                "data": serializer.data,
                "message": "Courses Retrieved Successfully"
            },
            status=status.HTTP_200_OK
        )

    @extend_schema(
        request=CourseSerializer,
        responses={201: CourseSerializer},
        description="Create a new Course."
    )
    def post(self, request):
        serializer = CourseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {
                "success": True,
                "data": serializer.data,
                "message": "Course Created Successfully"
            },
            status=status.HTTP_201_CREATED
        )


class QuizList(APIView):
    
    permission_classes = [IsAdminOrReadOnly]

    @extend_schema(
        responses={200: QuizListSerializer(many=True)},
        description="Retrieve a list of all quizzes."
    )
    def get(self, request):
        queryset = Quiz.objects.all()
        serializer = QuizListSerializer(queryset, many=True)

        return Response(
            {
                'success': True,
                'data': serializer.data,
                'message': "Quizzes Retrieved Successfully"
            }
        )

    @extend_schema(
        request=QuizSerializer,
        responses={201: QuizSerializer},
        description="Create a new Quiz."
    )
    def post(self, request):
        serializer = QuizSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(
            {
                'success': True,
                'data': serializer.data,
                'message': "Quiz Created Successfully"
            },
            status=status.HTTP_201_CREATED
        )

class QuizDetail(APIView):

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        responses ={200: QuizSerializer},
        description='Retrieve a Specific Quiz by ID.'
    )
    def get(self, request, quiz_id):
        quiz = get_object_or_404(Quiz, pk=quiz_id)
        serializer = QuizSerializer(quiz)
        return Response(
            {
                "success": True,
                "data": serializer.data,
                "message": "Quiz Retrieved Successfully"
            },
            status=status.HTTP_200_OK
        )

    @extend_schema(
        request = QuizSerializer,
        responses = {200: QuizSerializer},
        description='Update an existing Quiz.'
    )
    def put(self, request, quiz_id):
        quiz = get_object_or_404(Quiz, pk=quiz_id)
        serializer = QuizSerializer(quiz, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {
                "success": True,
                "data": serializer.data,
                "message": "Quiz Updated Successfully"
            },
            status=status.HTTP_200_OK
        )
    
    @extend_schema(
        responses = {204: None},
        description="Delete a Quiz"
    )
    def delete(self, request, quiz_id):
        permission_classes = [IsAdmin]
        quiz = get_object_or_404(Quiz, pk=quiz_id)
        quiz.delete()
        return Response(
            {
                "success": True,
                "data": {},
                "message": "Quiz Deleted Successfully"
            },
            status=status.HTTP_204_NO_CONTENT
        )


class QuestionList(APIView):

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        responses = {200: QuestionSerializer(many=True)}
    )
    def get(self, request):
        queryset = Question.objects.all()
        
        return Response(
            {
                "success": True,
                "data": serializer.data,
                "message": "Questions Retrieved Successfully"
            },
            status=status.HTTP_200_OK
        )

    @extend_schema(
        request=QuestionCreateSerializer,
        responses={201: QuestionSerializer},
        description="Create a new Question."
    )
    def post(self, request):
        serializer = QuestionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(
            {
                'success': True,
                'data': serializer.data,
                'message': "Question Created Successfully"
            },
            status=status.HTTP_201_CREATED
        )

class QuestionDetail(APIView):

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        responses ={200: QuestionSerializer},
        description='Retrieve a Specific Question by ID.'
    )
    def get(self, request, question_id):
        question = get_object_or_404(Question, pk=question_id)
        serializer = QuestionSerializer(question)
        return Response(
            {
                "success": True,
                "data": serializer.data,
                "message": "Question Retrieved Successfully"
            },
            status=status.HTTP_200_OK
        )

    @extend_schema(
        request = QuestionCreateSerializer,
        responses = {200: QuestionSerializer},
        description='Update an existing Quiz.'
    )
    def put(self, request, question_id):
        question = get_object_or_404(Question, pk=question_id)
        serializer = QuestionCreateSerializer(question, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {
                "success": True,
                "data": serializer.data,
                "message": "Question Updated Successfully"
            },
            status=status.HTTP_200_OK
        )
    
    @extend_schema(
        responses = {204: None},
        description="Delete a Question"
    )
    def delete(self, request, question_id):
        # only admin can delete
        if not request.user.is_staff:
            raise PermissionDenied("Only admins can delete questions.")
        question = get_object_or_404(Question, pk=question_id)
        question.delete()
        return Response(
            {
                "success": True,
                "data": {},
                "message": "Question Deleted Successfully"
            },
            status=status.HTTP_204_NO_CONTENT
        )


class QuizSubmissionView(APIView):

    permission_classes = [IsAdmin]

    @extend_schema(
        summary="Get all submissions for a Quiz (Admin Only)",
        description="Retrieves a list of all student submissions for a specific quiz ID.",
        responses = {200: SubmissionSerializer(many=True)}
    )
    def get(self, request, quiz_id):
        quiz = get_object_or_404(Quiz, pk=quiz_id)

        submission = Submission.objects.filter(quiz=quiz)
        serializer = SubmissionSerializer(submission, many=True)
        return Response(
            {
                "success": True,
                "data": serializer.data,
                "message": f"Found {submission.count()} submissions for {quiz.title}"
            },
            status= status.HTTP_200_OK
        )


class UserSubmissionView(APIView):

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="Get my submission history",
        description = "Retrieve all the Courses Submisiion for a specific student",
        responses = {200: SubmissionSerializer(many=True)}
    )
    def get(self, request):
        my_submission = Submission.objects.filter(student=request.user)
        serializer = SubmissionSerializer(my_submission, many=True)
        return Response(
            {
                "success": True,
                "data": serializer.data,
                "message": "Your submission history retrieved successfully"
            },
            status= status.HTTP_200_OK
        )
    
    @extend_schema(
        summary="Submit a Quiz",
        description="Create a new submission. Answers must be valid JSON.",
        request= SubmissionCreateSerializer,
        responses = {201: SubmissionCreateSerializer}
    )
    def post(self, request):
        serializer = SubmissionCreateSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)

        # create + grade atomically
        with transaction.atomic():
            instance = serializer.save(student=request.user)
            score = QuizService.grade_submission(instance)
            instance.score = score
            instance.graded_at = timezone.now()
            instance.save()

        out = SubmissionDetailSerializer(instance, context={'request': request})
        return Response(
            {
                "success": True,
                "data": out.data,
                "message": "Quiz submitted and graded successfully!"
            },
            status=status.HTTP_201_CREATED
        )


class SubmissionDetailView(APIView):

    permission_classes = [permissions.IsAuthenticated]

    def get_object(self, pk, user):
        submission = get_object_or_404(Submission, pk=pk)

        if not user.is_staff and submission.student != user:
            raise PermissionDenied("You are not allowed to view other student's submission.")

        return submission

    @extend_schema(
        summary="View Submission Details",
        description="Retrieve full details of a submission (Answers + Score). Students see only their own; Admin see any.",
        responses = {200: SubmissionDetailSerializer}
    )
    def get(self, request, quiz_id):
        submission = self.get_object(pk=quiz_id, user=request.user)
        serializer = SubmissionDetailSerializer(submission, context={'request': request})
        return Response(
            {
                "success": True,
                "data": serializer.data,
                "message": "Student's Submission for a quiz retrieved successfully"
            },
            status=status.HTTP_200_OK
        )

    @extend_schema(
        summary = "Grade a Submission (Admin only)",
        description = "Update the score and feedback. Only accessible by Admins.",
        request = SubmissionDetailSerializer,
        responses = {200: SubmissionDetailSerializer},
    )
    def patch(self, request, pk):
        if not request.user.is_staff:
            return Response(
                {
                    "success": False,
                    "message": "Permission denied. Only Admin can grade submissions."
                },
                status=status.HTTP_403_FORBIDDEN
            )
        submission = get_object_or_404(Submission, pk=pk)
        serializer = SubmissionDetailSerializer(submission, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {
                "success": True,
                "data": serializer.data,
                "message": "Submission graded successfully."
            },
            status=status.HTTP_200_OK
        )
