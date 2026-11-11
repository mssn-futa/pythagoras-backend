"""Arabic class, enrollment and learning-module services (D3, ASSESS-07).

The HTTP layer (``quiz.learning_views``) validates request structure; these
functions apply the rules that need row locks and perform the writes
atomically. They raise ``quiz.errors`` domain errors (or ``DoesNotExist``)
and never import HTTP code.

Scope: only ``Course.kind == 'arabic'`` rows are classes. Legacy/general
courses are invisible here (``Course.DoesNotExist``), so the release
endpoints never read or change them.

Lifecycle: class draft -> published -> closed (terminal); module draft <->
published. A closed class is read-only (``ClassClosed``): no class edits,
module writes or roster changes. Enrollment needs a published class.

Locks (lock order: class row, then enrollment/module rows, then resources):

* class lifecycle, class edits and every module write: class ``FOR UPDATE``,
  so module order checks and the atomic reorder are serialized per class and
  never interleave with publish/close/delete.
* enrollment writes: class ``FOR SHARE`` (students enroll in parallel, but not
  while the class closes), then the enrollment row ``FOR UPDATE``. One record
  per (class, student) is decided by the unique constraint: a racing insert
  waits for the winner, then reuses its row.
* newly attached resources: ``FOR KEY SHARE`` on their rows while validating,
  so a concurrent resource deletion waits and then meets the PROTECT link.

Timestamps come from ``quiz.lifecycle.server_now`` (database clock).
"""
from django.db import IntegrityError, connection, transaction
from django.db.models import Max, ProtectedError

from accounts.models import CustomUser
from .authoring import MAX_ORDER
from .errors import (
    AuthoringInvalid, ClassClosed, ClassNotPublished, ClassReferenced, InvalidTransition,
    UnpublishRequired,
)
from .lifecycle import server_now
from .models import Course, Enrollment, LearningModule, ModuleResource, Quiz
from .resources import visible_resource_ids

MAX_RESOURCES_PER_MODULE = 50
CLASS_FIELDS = ('code', 'title', 'description')
MODULE_FIELDS = ('title', 'body', 'order')


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------

def arabic_classes():
    return Course.objects.filter(kind=Course.Kind.ARABIC)


def is_student_visible_class(course):
    return course.kind == Course.Kind.ARABIC and course.status in (
        Course.Status.PUBLISHED, Course.Status.CLOSED)


def student_classes():
    """Classes a student may read: published or closed Arabic classes (drafts are 404).

    The catalog lists only published ones; a closed class stays readable so its
    enrollment state and history remain retrievable.
    """
    return arabic_classes().filter(status__in=[Course.Status.PUBLISHED, Course.Status.CLOSED])


def has_active_enrollment(course, student):
    return Enrollment.objects.filter(course=course, student=student,
                                     status=Enrollment.Status.ACTIVE).exists()


def class_quizzes(course_id, *, admin):
    """Quizzes linked to the class (``Quiz.related_course``), as ``{id,title,status}``.

    Students see published/closed ones only (never drafts); the quiz's own
    availability and eligibility rules still apply when they follow the link.
    """
    queryset = Quiz.objects.filter(related_course_id=course_id)
    if not admin:
        queryset = queryset.exclude(status=Quiz.Status.DRAFT)
    return [{'id': pk, 'title': title, 'status': status}
            for pk, title, status in queryset.order_by('pk').values_list('pk', 'title', 'status')]


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------

def lock_class(class_id):
    """The Arabic class row ``FOR UPDATE``. Raises ``Course.DoesNotExist``."""
    return arabic_classes().select_for_update().get(pk=class_id)


def share_lock_class(class_id):
    """The Arabic class row ``FOR SHARE`` (enrollment writes run in parallel)."""
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM quiz_course WHERE id = %s AND kind = 'arabic' "
                           'FOR SHARE', [class_id])
            if cursor.fetchone() is None:
                raise Course.DoesNotExist()
    return arabic_classes().get(pk=class_id)


def _assert_open(course):
    if course.status == Course.Status.CLOSED:
        raise ClassClosed()


# ---------------------------------------------------------------------------
# Classes
# ---------------------------------------------------------------------------

def create_class(data):
    with transaction.atomic():
        return Course.objects.create(kind=Course.Kind.ARABIC, status=Course.Status.DRAFT,
                                     **{name: data[name] for name in CLASS_FIELDS if name in data})


def update_class(class_id, data):
    with transaction.atomic():
        course = lock_class(class_id)
        changed = [name for name in CLASS_FIELDS
                   if name in data and getattr(course, name) != data[name]]
        if changed:
            _assert_open(course)
            for name in changed:
                setattr(course, name, data[name])
            course.save(update_fields=[*changed, 'updated_at'])
        return course


def publish_class(class_id):
    """draft -> published; ``(course, changed)``. Publishing again is a no-op."""
    with transaction.atomic():
        course = lock_class(class_id)
        if course.status == Course.Status.PUBLISHED:
            return course, False
        if course.status == Course.Status.CLOSED:
            raise InvalidTransition('A closed class cannot be published again.')
        course.status = Course.Status.PUBLISHED
        course.published_at = server_now()
        course.save(update_fields=['status', 'published_at', 'updated_at'])
        return course, True


def close_class(class_id):
    """published -> closed; ``(course, changed)``. Closing again is a no-op."""
    with transaction.atomic():
        course = lock_class(class_id)
        if course.status == Course.Status.CLOSED:
            return course, False
        if course.status == Course.Status.DRAFT:
            raise InvalidTransition('Only a published class can be closed; delete a draft '
                                    'instead.')
        course.status = Course.Status.CLOSED
        course.closed_at = server_now()
        course.save(update_fields=['status', 'closed_at', 'updated_at'])
        return course, True


def delete_class(class_id):
    """Delete a draft class (and its modules) that no enrollment or quiz references."""
    with transaction.atomic():
        course = lock_class(class_id)
        if course.status != Course.Status.DRAFT:
            raise InvalidTransition('Only a draft class can be deleted; close it instead.')
        if (Enrollment.objects.filter(course=course).exists()
                or Quiz.objects.filter(related_course=course).exists()):
            raise ClassReferenced()
        try:
            with transaction.atomic():
                course.delete()
        except ProtectedError:
            raise ClassReferenced() from None


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------

def _locked_enrollment(course, student_id):
    return (Enrollment.objects.select_for_update(of=('self',))
            .filter(course=course, student_id=student_id).first())


def enroll(class_id, student):
    """Enroll or reactivate; ``(enrollment, created, changed)``.

    Idempotent per (class, student): an active record is returned unchanged and
    a withdrawn one is reactivated in place (same ID). Needs a published class
    (closed: ``ClassClosed``; draft: ``ClassNotPublished``).
    """
    with transaction.atomic():
        course = share_lock_class(class_id)
        enrollment = _locked_enrollment(course, student.pk)
        if enrollment is not None and enrollment.status == Enrollment.Status.ACTIVE:
            return enrollment, False, False
        _assert_open(course)
        if course.status != Course.Status.PUBLISHED:
            raise ClassNotPublished()
        now = server_now()
        if enrollment is None:
            enrollment = Enrollment(course=course, student=student,
                                    status=Enrollment.Status.ACTIVE, enrolled_at=now)
            try:
                with transaction.atomic():
                    enrollment.save(force_insert=True)
                return enrollment, True, True
            except IntegrityError:
                # A concurrent enroll committed the row first: reuse it.
                enrollment = _locked_enrollment(course, student.pk)
                if enrollment.status == Enrollment.Status.ACTIVE:
                    return enrollment, False, False
        enrollment.status = Enrollment.Status.ACTIVE
        enrollment.enrolled_at = now
        enrollment.withdrawn_at = None
        enrollment.save(update_fields=['status', 'enrolled_at', 'withdrawn_at', 'updated_at'])
        return enrollment, False, True


def withdraw(class_id, student_id):
    """Withdraw; ``(enrollment, changed)``. Idempotent; the record is kept.

    Raises ``Enrollment.DoesNotExist`` when the student was never enrolled and
    ``ClassClosed`` for an active enrollment in a closed (read-only) class.
    """
    with transaction.atomic():
        course = share_lock_class(class_id)
        enrollment = _locked_enrollment(course, student_id)
        if enrollment is None:
            raise Enrollment.DoesNotExist()
        if enrollment.status == Enrollment.Status.WITHDRAWN:
            return enrollment, False
        _assert_open(course)
        enrollment.status = Enrollment.Status.WITHDRAWN
        enrollment.withdrawn_at = server_now()
        enrollment.save(update_fields=['status', 'withdrawn_at', 'updated_at'])
        return enrollment, True


def admin_set_enrollment(class_id, student_id, status):
    """Admin roster management: enroll, withdraw or reinstate one student.

    ``(enrollment, created, changed)``. The student must be a student account;
    enrolling/reinstating needs an active account. Withdrawing a student who was
    never enrolled is a field error (no record is invented).
    """
    student = CustomUser.objects.filter(pk=student_id, role=CustomUser.Role.STUDENT).first()
    if student is None:
        arabic_classes().get(pk=class_id)  # 404 for a missing class first
        raise AuthoringInvalid(fields={'student_id': ['Select a student account.']})
    if status == Enrollment.Status.ACTIVE:
        if not student.is_active:
            arabic_classes().get(pk=class_id)
            raise AuthoringInvalid(fields={'student_id': [
                'This student account is deactivated and cannot be enrolled.']})
        return enroll(class_id, student)
    try:
        enrollment, changed = withdraw(class_id, student.pk)
    except Enrollment.DoesNotExist:
        raise AuthoringInvalid(fields={'student_id': [
            'This student is not enrolled in this class.']}) from None
    return enrollment, False, changed


# ---------------------------------------------------------------------------
# Modules
# ---------------------------------------------------------------------------

def class_modules(course):
    return LearningModule.objects.filter(course=course)


def _check_order(course, order, exclude_pk=None):
    if class_modules(course).filter(order=order).exclude(pk=exclude_pk).exists():
        raise AuthoringInvalid(fields={'order': [
            'Another module of this class already has this order.']})


def _next_order(course):
    last = class_modules(course).aggregate(last=Max('order'))['last'] or 0
    if last >= MAX_ORDER:
        raise AuthoringInvalid(fields={'order': [
            f'Choose an order between 1 and {MAX_ORDER}.']})
    return last + 1


def _key_share_resources(resource_ids):
    if resource_ids and connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute('SELECT id FROM dawah_eventresource WHERE id = ANY(%s) '
                           'ORDER BY id FOR KEY SHARE', [list(resource_ids)])


def _set_resources(module, resource_ids, current_ids):
    """Replace the attachment set with ``resource_ids`` in that order.

    Lead decision (after ASSESS-08 review): IDs newly added must be visible
    (published, visible parent, deliverable asset/reference: D2
    ``quiz.resources.visible_resource_ids``); IDs already attached are accepted even
    if now hidden, so unrelated edits never fail.
    """
    new_ids = [pk for pk in resource_ids if pk not in current_ids]
    _key_share_resources(new_ids)
    visible = visible_resource_ids(new_ids)
    unavailable = [pk for pk in new_ids if pk not in visible]
    if unavailable:
        raise AuthoringInvalid(fields={'resource_ids': [
            f'Resource {pk} is not published or not available.' for pk in unavailable]})
    ModuleResource.objects.filter(module=module).delete()
    ModuleResource.objects.bulk_create([
        ModuleResource(module=module, resource_id=pk, position=index)
        for index, pk in enumerate(resource_ids, start=1)])


def _attached_ids(module):
    return list(ModuleResource.objects.filter(module=module).order_by('position')
                .values_list('resource_id', flat=True))


def create_module(class_id, data):
    with transaction.atomic():
        course = lock_class(class_id)
        _assert_open(course)
        order = data.get('order')
        if order is None:
            order = _next_order(course)
        else:
            _check_order(course, order)
        module = LearningModule.objects.create(
            course=course, title=data['title'], body=data.get('body', ''), order=order)
        _set_resources(module, data.get('resource_ids', []), set())
        return module


def _locked_module(course, module_id):
    return LearningModule.objects.select_for_update().get(course=course, pk=module_id)


def update_module(class_id, module_id, data):
    """Partial edit (any module state). ``resource_ids``, when sent, replaces the set."""
    with transaction.atomic():
        course = lock_class(class_id)
        module = _locked_module(course, module_id)
        changed = [name for name in MODULE_FIELDS
                   if name in data and getattr(module, name) != data[name]]
        current = _attached_ids(module)
        resources_changed = 'resource_ids' in data and list(data['resource_ids']) != current
        if not changed and not resources_changed:
            return module
        _assert_open(course)
        if 'order' in changed:
            _check_order(course, data['order'], exclude_pk=module.pk)
        for name in changed:
            setattr(module, name, data[name])
        module.save(update_fields=[*changed, 'updated_at'])
        if resources_changed:
            _set_resources(module, data['resource_ids'], set(current))
            if not changed:
                module.save(update_fields=['updated_at'])
        return module


def delete_module(class_id, module_id):
    with transaction.atomic():
        course = lock_class(class_id)
        module = _locked_module(course, module_id)
        _assert_open(course)
        if module.status == LearningModule.Status.PUBLISHED:
            raise UnpublishRequired()
        module.delete()


def set_module_status(class_id, module_id, status):
    """Publish/unpublish; ``(module, changed)``. Repeating a transition is a no-op."""
    with transaction.atomic():
        course = lock_class(class_id)
        module = _locked_module(course, module_id)
        if module.status == status:
            return module, False
        _assert_open(course)
        module.status = status
        fields = ['status', 'updated_at']
        if status == LearningModule.Status.PUBLISHED:
            module.published_at = server_now()
            fields.append('published_at')
        module.save(update_fields=fields)
        return module, True


def reorder_modules(class_id, module_ids):
    """Atomically renumber every module of the class 1..n in ``module_ids`` order.

    ``module_ids`` must list each of the class's modules exactly once
    (``AuthoringInvalid`` on ``module_ids`` otherwise, nothing changes). The
    deferred unique (class, order) constraint is checked at commit.
    """
    with transaction.atomic():
        course = lock_class(class_id)
        _assert_open(course)
        modules = {module.pk: module for module in
                   class_modules(course).select_for_update().order_by('pk')}
        if len(module_ids) != len(set(module_ids)):
            raise AuthoringInvalid(fields={'module_ids': ['Each module may appear only once.']})
        if set(module_ids) != set(modules):
            missing = sorted(set(modules) - set(module_ids))
            foreign = sorted(set(module_ids) - set(modules))
            messages = []
            if missing:
                messages.append('Missing modules of this class: '
                                + ', '.join(map(str, missing)) + '.')
            if foreign:
                messages.append('Not modules of this class: '
                                + ', '.join(map(str, foreign)) + '.')
            raise AuthoringInvalid(fields={'module_ids': messages})
        now = server_now()
        changed = []
        for index, pk in enumerate(module_ids, start=1):
            module = modules[pk]
            if module.order != index:
                module.order = index
                module.updated_at = now
                changed.append(module)
        if changed:
            LearningModule.objects.bulk_update(changed, ['order', 'updated_at'])
        return [modules[pk] for pk in module_ids]
