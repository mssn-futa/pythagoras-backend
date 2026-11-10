"""Assessment role and object-visibility helpers (D0 roles, D3 security).

Role checks use the shared ``accounts.permissions`` classes: admin authority is
an active account with ``role == 'admin'``; ``is_staff`` grants nothing here.
Object visibility is expressed as querysets so a hidden object is a 404 before
any serializer or mutation runs.

Legacy availability: until ASSESS-02 adds quiz status, a quiz is visible to a
student only while ``is_active`` is true. That rule is temporary.
"""
from accounts.permissions import IsActiveAdmin, IsActiveStudent, IsActiveUser
from rest_framework.permissions import SAFE_METHODS

from .models import Question, Quiz, Submission

__all__ = ['IsActiveAdmin', 'IsActiveStudent', 'IsActiveUser', 'is_admin',
           'admin_or_read_permissions', 'visible_quizzes', 'visible_questions',
           'visible_submissions']


def is_admin(user):
    """Same rule as ``IsActiveAdmin``, for choosing a serializer/queryset."""
    return bool(user and user.is_authenticated and user.is_active and user.role == 'admin')


def admin_or_read_permissions(request):
    """Any active user may read; only an active admin may write."""
    if request.method in SAFE_METHODS:
        return [IsActiveUser()]
    return [IsActiveAdmin()]


def visible_quizzes(user):
    queryset = Quiz.objects.all()
    if is_admin(user):
        return queryset
    return queryset.filter(is_active=True)


def visible_questions(user):
    queryset = Question.objects.select_related('quiz')
    if is_admin(user):
        return queryset
    # Release behaviour is MCQ-only; legacy text/multi questions stay admin-only.
    return queryset.filter(quiz__is_active=True, type=Question.Type.mcq)


def visible_submissions(user):
    """Admins see every submission; anyone else only their own."""
    queryset = Submission.objects.select_related('quiz', 'student')
    if is_admin(user):
        return queryset
    return queryset.filter(student=user)
