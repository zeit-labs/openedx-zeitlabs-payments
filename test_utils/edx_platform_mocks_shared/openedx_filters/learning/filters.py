"""Mock for openedx_filters.learning.filters."""

from openedx_filters import OpenEdxFilterException, OpenEdxPublicFilter


class CourseEnrollmentStarted(OpenEdxPublicFilter):
    """Mock for CourseEnrollmentStarted filter."""

    filter_type = 'org.openedx.learning.course.enrollment.started.v1'

    class PreventEnrollment(OpenEdxFilterException):
        """Raised to prevent enrollment."""
