from django.urls import path

from .views import (
    CurrentUserView,
    LogoutView,
    StudentDetailView,
    StudentListView,
    TokenRefreshView,
    UserLoginView,
    UserRegistrationView,
)

app_name = "accounts"

urlpatterns = [
    path("register/", UserRegistrationView.as_view(), name="register"),
    path("login/", UserLoginView.as_view(), name="login"),
    path("refresh/", TokenRefreshView.as_view(), name="refresh"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("students/", StudentListView.as_view(), name="student-list"),
    path("students/<int:pk>/", StudentDetailView.as_view(), name="student-detail"),
    path("me/", CurrentUserView.as_view(), name="me"),
]
