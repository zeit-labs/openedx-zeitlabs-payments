"""Mock enrollments api for testing.

Provides a minimal ``add_enrollment`` that mimics the real edx-platform contract: it calls
``CourseEnrollment.enroll`` to create the enrollment row. This lets the ``override_add_enrollment``
wrap be exercised end-to-end in the test suite.
"""

from common.djangoapps.course_modes.models import CourseMode
from common.djangoapps.student.models import CourseEnrollment


def add_enrollment(username, course_id, mode=None, is_active=True, enrollment_attributes=None,
                   enterprise_uuid=None, force_enrollment=False, include_expired=False):
    """Mock add_enrollment — mirrors openedx.core.djangoapps.enrollments.api.add_enrollment."""
    from django.contrib.auth import get_user_model
    User = get_user_model()
    user = User.objects.get(username=username)
    if mode is None:
        mode = CourseMode.AUDIT
    CourseEnrollment.enroll(user, course_id, mode=mode)
    return {
        'mode': mode,
        'is_active': is_active,
        'user': username,
        'course_details': {'course_id': str(course_id)},
    }
