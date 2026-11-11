from django.urls import path

from . import learning_views, views

# Canonical routes end with "/" (D0); the historical no-slash detail routes are
# kept as aliases of the same views, so they share permissions and serializers.
# Aliases are left out of the OpenAPI schema to avoid duplicate operations.
urlpatterns = [
    path('courses/', views.CourseView.as_view(), name='courses'),
    path('question/', views.QuestionList.as_view(), name='questions-list'),
    path('question/<int:question_id>/', views.QuestionDetail.as_view(), name='questions-detail'),
    path('question/<int:question_id>', views.QuestionDetail.as_view(schema=None), name='questions-detail-legacy'),
    path('quizzes/', views.QuizList.as_view(), name='quizzes-list'),
    path('quizzes/<int:quiz_id>/', views.QuizDetail.as_view(), name='quiz-detail'),
    path('quizzes/<int:quiz_id>', views.QuizDetail.as_view(schema=None), name='quiz-detail-legacy'),
    path('quizzes/<int:quiz_id>/questions/', views.QuizQuestionList.as_view(), name='quiz-question-list'),
    path('quizzes/<int:quiz_id>/publish/', views.QuizPublishView.as_view(), name='quiz-publish'),
    path('quizzes/<int:quiz_id>/close/', views.QuizCloseView.as_view(), name='quiz-close'),
    path('quizzes/<int:quiz_id>/attempts/', views.QuizAttemptStartView.as_view(), name='quiz-attempt-start'),
    path('attempts/<int:attempt_id>/', views.AttemptDetailView.as_view(), name='attempt-detail'),
    path('quizzes/<int:quiz_id>/submissions/', views.QuizSubmissionView.as_view(), name='quiz-submission-list'),
    path('submissions/', views.UserSubmissionView.as_view(), name='user-submission-list'),
    path('submissions/<int:pk>/', views.SubmissionDetailView.as_view(), name='submission-detail'),
    path('submissions/<int:pk>', views.SubmissionDetailView.as_view(schema=None), name='submission-detail-legacy'),
    # ASSESS-07 Arabic classes, enrollment, roster and modules (canonical slash routes only).
    path('classes/', learning_views.ClassListView.as_view(), name='class-list'),
    path('classes/<int:class_id>/', learning_views.ClassDetailView.as_view(), name='class-detail'),
    path('classes/<int:class_id>/publish/', learning_views.ClassPublishView.as_view(), name='class-publish'),
    path('classes/<int:class_id>/close/', learning_views.ClassCloseView.as_view(), name='class-close'),
    path('classes/<int:class_id>/enrollment/', learning_views.OwnEnrollmentView.as_view(),
         name='class-own-enrollment'),
    path('classes/<int:class_id>/enrollments/', learning_views.RosterView.as_view(), name='class-roster'),
    path('classes/<int:class_id>/modules/', learning_views.ModuleListView.as_view(), name='class-module-list'),
    path('classes/<int:class_id>/modules/reorder/', learning_views.ModuleReorderView.as_view(),
         name='class-module-reorder'),
    path('classes/<int:class_id>/modules/<int:module_id>/', learning_views.ModuleDetailView.as_view(),
         name='class-module-detail'),
    path('classes/<int:class_id>/modules/<int:module_id>/publish/', learning_views.ModulePublishView.as_view(),
         name='class-module-publish'),
    path('classes/<int:class_id>/modules/<int:module_id>/unpublish/',
         learning_views.ModuleUnpublishView.as_view(), name='class-module-unpublish'),
]
