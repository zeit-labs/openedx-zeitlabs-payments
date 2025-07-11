"""edx-platform models mocks for testing purposes."""

from django.contrib.auth import get_user_model
from django.db import models
from opaque_keys.edx.django.models import CourseKeyField


class CourseOverview(models.Model):
    """Mock"""
    id = CourseKeyField(db_index=True, primary_key=True, max_length=255)  # pylint: disable=invalid-name
    org = models.CharField(max_length=255, db_collation='NOCASE')
    catalog_visibility = models.TextField(null=True)
    start = models.DateTimeField(null=True)
    end = models.DateTimeField(null=True)
    display_name = models.TextField(null=True)
    enrollment_start = models.DateTimeField(null=True)
    enrollment_end = models.DateTimeField(null=True)
    self_paced = models.BooleanField(default=False)
    course_image_url = models.TextField()
    visible_to_staff_only = models.BooleanField(default=False)
    effort = models.TextField(null=True)

    class Meta:
        app_label = 'fake_models'
        db_table = 'course_overviews_courseoverview'


class CourseMode(models.Model):
    """Mock"""
    course = models.ForeignKey(
        CourseOverview,
        db_constraint=False,
        db_index=True,
        related_name='modes',
        on_delete=models.DO_NOTHING,
    )
    mode_slug = models.CharField(max_length=100, verbose_name=('Mode'))
    mode_display_name = models.CharField(max_length=255, verbose_name=('Display Name'))
    min_price = models.IntegerField(default=0, verbose_name=('Price'))
    currency = models.CharField(default='usd', max_length=8)
    _expiration_datetime = models.DateTimeField(
        default=None, null=True, blank=True,
        verbose_name=('Upgrade Deadline'),
        db_column='expiration_datetime',
    )
    expiration_datetime_is_explicit = models.BooleanField(
        default=False,
        verbose_name=('Lock upgrade deadline date'),
    )
    sku = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        verbose_name='SKU',
    )

    HONOR = 'honor'
    VERIFIED = 'verified'
    AUDIT = 'audit'
    NO_ID_PROFESSIONAL_MODE = 'no-id-professional'

    class Meta:
        app_label = 'fake_models'
        db_table = 'course_modes_coursemode'
        unique_together = ('course', 'mode_slug', 'currency')


class CourseEnrollment(models.Model):
    """Mock"""
    user = models.ForeignKey(get_user_model(), on_delete=models.CASCADE)
    course = models.ForeignKey(CourseOverview, on_delete=models.CASCADE)
    is_active = models.BooleanField()
    created = models.DateTimeField(auto_now_add=True)
    mode = models.CharField(default=CourseMode.AUDIT, max_length=100)

    class Meta:
        app_label = 'fake_models'
        db_table = 'student_courseenrollment'

    @classmethod
    def enroll(cls, user, course_key, mode=None, check_access=False, can_upgrade=False, enterprise_uuid=None):
        cls.objects.get_or_create(
            user=user,
            course_id=course_key,
            defaults={
                'mode': mode or CourseMode.AUDIT,
                'is_active': True
            }
        )

    @classmethod
    def is_enrollment_closed(cls, user, course):
        """Mock"""
        return

    @classmethod
    def is_enrolled(cls, user, course_key):
        """Mock"""
        return cls.objects.filter(user=user, course__id=course_key, is_active=True).exists()

    @classmethod
    def is_course_full(cls, course):
        """Mock"""
        return False
