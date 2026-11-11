"""ASSESS-07 schema: Arabic classes, enrollment status/history, learning modules.

Existing data is preserved, never reinterpreted:

* every existing Course becomes ``kind = 'general'`` (legacy/general academic)
  with ``status = 'draft'`` and NULL created/updated/published/closed times;
  the class API only ever surfaces ``kind = 'arabic'`` rows, so no legacy
  course is presented as a Phase 1 Arabic class. Code/title/department/level/
  semester are kept as stored (the last three become optional for new rows).
* every existing Enrollment keeps its (course, student) pair and uniqueness,
  becomes ``status = 'active'`` with NULL enrolled/withdrawn/created/updated
  times (unknown, not invented). Memberships lost by the 0002 M2M -> Enrollment
  transition are not recreated.
* Enrollment course/student foreign keys become PROTECT (Django-level; no
  schema change), so deleting a class or user never drops roster history.

New tables: ``quiz_learningmodule`` (ordered per class, deferred unique order)
and ``quiz_moduleresource`` (ordered attachments of dawah resources, PROTECT).

Rollback drops the new columns/tables (the module, attachment and enrollment
status data created after this migration is lost; the original course and
enrollment rows survive). Created/updated columns are added as plain nullable
columns first so existing rows are not stamped with the migration time.
"""
import django.db.models.constraints
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def nullable_timestamps(model_name):
    return [
        migrations.AddField(model_name=model_name, name='created_at',
                            field=models.DateTimeField(null=True)),
        migrations.AddField(model_name=model_name, name='updated_at',
                            field=models.DateTimeField(null=True)),
        migrations.AlterField(model_name=model_name, name='created_at',
                              field=models.DateTimeField(auto_now_add=True, null=True)),
        migrations.AlterField(model_name=model_name, name='updated_at',
                              field=models.DateTimeField(auto_now=True, null=True)),
    ]


class Migration(migrations.Migration):

    dependencies = [
        # Only the EventResource table is needed (it exists since dawah.0001); a later
        # dependency would make dawah migration rollbacks unapply this migration.
        ('dawah', '0001_initial'),
        ('quiz', '0007_assessment_history_triggers'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # -- Course: Arabic class fields --------------------------------------
        migrations.AddField(
            model_name='course', name='kind',
            field=models.CharField(choices=[('arabic', 'Arabic class'),
                                            ('general', 'General academic (legacy)')],
                                   default='general', max_length=7)),
        migrations.AddField(
            model_name='course', name='description',
            field=models.TextField(blank=True, default='')),
        migrations.AddField(
            model_name='course', name='status',
            field=models.CharField(choices=[('draft', 'Draft'), ('published', 'Published'),
                                            ('closed', 'Closed')],
                                   default='draft', max_length=9)),
        migrations.AddField(
            model_name='course', name='published_at',
            field=models.DateTimeField(blank=True, editable=False, null=True)),
        migrations.AddField(
            model_name='course', name='closed_at',
            field=models.DateTimeField(blank=True, editable=False, null=True)),
        *nullable_timestamps('course'),
        migrations.AlterField(
            model_name='course', name='department',
            field=models.CharField(blank=True, default='', max_length=100)),
        migrations.AlterField(
            model_name='course', name='level',
            field=models.CharField(blank=True, choices=[
                ('100', '100 Level'), ('200', '200 Level'), ('300', '300 Level'),
                ('400', '400 Level'), ('500', '500 Level')], default='', max_length=3)),
        migrations.AlterField(
            model_name='course', name='semester',
            field=models.CharField(blank=True, default='', max_length=10)),
        # -- Enrollment: status and history -----------------------------------
        migrations.AlterField(
            model_name='enrollment', name='course',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                    to='quiz.course')),
        migrations.AlterField(
            model_name='enrollment', name='student',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                    to=settings.AUTH_USER_MODEL)),
        migrations.AddField(
            model_name='enrollment', name='status',
            field=models.CharField(choices=[('active', 'Active'), ('withdrawn', 'Withdrawn')],
                                   default='active', max_length=9)),
        migrations.AddField(
            model_name='enrollment', name='enrolled_at',
            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(
            model_name='enrollment', name='withdrawn_at',
            field=models.DateTimeField(blank=True, null=True)),
        *nullable_timestamps('enrollment'),
        migrations.AddConstraint(
            model_name='course',
            constraint=models.CheckConstraint(condition=models.Q(kind__in=['arabic', 'general']),
                                              name='quiz_course_kind_valid')),
        migrations.AddConstraint(
            model_name='course',
            constraint=models.CheckConstraint(
                condition=models.Q(status__in=['draft', 'published', 'closed']),
                name='quiz_course_status_valid')),
        migrations.AddConstraint(
            model_name='course',
            constraint=models.CheckConstraint(
                condition=models.Q(status='draft') | models.Q(published_at__isnull=False),
                name='quiz_course_released_has_published_at')),
        migrations.AddConstraint(
            model_name='course',
            constraint=models.CheckConstraint(
                condition=(models.Q(status='closed', closed_at__isnull=False)
                           | (~models.Q(status='closed') & models.Q(closed_at__isnull=True))),
                name='quiz_course_closed_at_matches_status')),
        migrations.AddConstraint(
            model_name='enrollment',
            constraint=models.CheckConstraint(
                condition=models.Q(status__in=['active', 'withdrawn']),
                name='quiz_enrollment_status_valid')),
        migrations.AddConstraint(
            model_name='enrollment',
            constraint=models.CheckConstraint(
                condition=(models.Q(status='withdrawn', withdrawn_at__isnull=False)
                           | models.Q(status='active', withdrawn_at__isnull=True)),
                name='quiz_enrollment_withdrawn_at_matches_status')),
        # -- Learning modules and attachments ---------------------------------
        migrations.CreateModel(
            name='LearningModule',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False,
                                           verbose_name='ID')),
                ('title', models.CharField(max_length=250)),
                ('body', models.TextField(blank=True, default='')),
                ('order', models.PositiveIntegerField()),
                ('status', models.CharField(choices=[('draft', 'Draft'),
                                                     ('published', 'Published')],
                                            default='draft', max_length=9)),
                ('published_at', models.DateTimeField(blank=True, editable=False, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('course', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                             related_name='modules', to='quiz.course')),
            ],
        ),
        migrations.CreateModel(
            name='ModuleResource',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False,
                                           verbose_name='ID')),
                ('position', models.PositiveIntegerField()),
                ('module', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                             related_name='attachments',
                                             to='quiz.learningmodule')),
                ('resource', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                               related_name='+', to='dawah.eventresource')),
            ],
        ),
        migrations.AddField(
            model_name='learningmodule', name='resources',
            field=models.ManyToManyField(blank=True, related_name='learning_modules',
                                         through='quiz.ModuleResource',
                                         to='dawah.eventresource')),
        migrations.AddConstraint(
            model_name='moduleresource',
            constraint=models.UniqueConstraint(fields=('module', 'resource'),
                                               name='quiz_module_resource_unique')),
        migrations.AddConstraint(
            model_name='moduleresource',
            constraint=models.UniqueConstraint(
                deferrable=django.db.models.constraints.Deferrable['DEFERRED'],
                fields=('module', 'position'), name='quiz_module_resource_position_unique')),
        migrations.AddConstraint(
            model_name='learningmodule',
            constraint=models.UniqueConstraint(
                deferrable=django.db.models.constraints.Deferrable['DEFERRED'],
                fields=('course', 'order'), name='quiz_module_order_unique')),
        migrations.AddConstraint(
            model_name='learningmodule',
            constraint=models.CheckConstraint(condition=models.Q(order__gte=1),
                                              name='quiz_module_order_positive')),
        migrations.AddConstraint(
            model_name='learningmodule',
            constraint=models.CheckConstraint(
                condition=models.Q(status__in=['draft', 'published']),
                name='quiz_module_status_valid')),
    ]
