from django.urls import path, include
from . import views


urlpatterns = [
    path('courses/', views.CourseView.as_view(), name='courses'),
    path('question/', views.QuestionList.as_view(), name='questions-list'),
    path('question/<int:question_id>', views.QuestionDetail.as_view(), name='questions-detail'),
    path('quizzes/', views.QuizList.as_view(), name='quizzes-list'),
    path('quizzes/<int:quiz_id>', views.QuizDetail.as_view(), name='quiz-detail'),
    path('quizzes/<int:quiz_id>/submissions/', views.QuizSubmissionView.as_view(), name='quiz-submission-list'),
    path('submissions/', views.UserSubmissionView.as_view(), name='user-submission-list'),
    path('submissions/<int:pk>', views.SubmissionDetailView.as_view(), name='submission-detail'),
]











