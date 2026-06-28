"""Open edX filter pipeline steps for zeitlabs_payments."""

import logging
from typing import TYPE_CHECKING, Any

from django.db.models import Q

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    # These imports are only available at runtime inside edx-platform (production/CI).
    from opaque_keys.edx.keys import CourseKey


def _payments_enabled() -> bool:
    """Return True if zeitlabs-payments is active on this instance."""
    from django.conf import settings  # pylint: disable=import-outside-toplevel
    return bool(getattr(settings, 'IS_ZEITLABS_PAYMENTS_ENABLED', False))


def _has_paid_cart_for_course(user: Any, course_key: str) -> bool:
    """
    Check whether the user has a paid cart containing a CartItem for the given course.

    A paid cart has status ``Cart.Status.PAID``.  Having such a cart means the
    user went through the checkout→payment→fulfillment flow and is eligible
    for enrollment.
    """
    from zeitlabs_payments.models import Cart, CartItem  # pylint: disable=import-outside-toplevel

    return CartItem.objects.filter(
        cart__user=user,
        cart__status=Cart.Status.PAID,
        catalogue_item__item_ref_id=str(course_key),
    ).exists()


def _course_has_paid_mode(course_key: str) -> bool:
    """
    Return True if the course has at least one paid CourseMode.

    Paid modes are those that are not ``audit``.
    """
    from common.djangoapps.course_modes.models import CourseMode  # pylint: disable=import-outside-toplevel

    return CourseMode.objects.filter(
        Q(course_id=course_key),
        ~Q(mode_slug=CourseMode.AUDIT),
    ).exists()


def block_unpaid_course_enrollment(user: Any, course_key: 'CourseKey', mode: str) -> None:
    """
    Open edX pipeline step for ``CourseEnrollmentStarted``.

    Prevents enrollment in a paid course unless the user has a completed
    payment (Cart with status PAID) for that course.

    This closes the security gap where the Learning MFE's "Enroll"
    button calls ``CourseEnrollment.enroll()`` directly — bypassing the
    checkout flow that the Mako-based about page uses.

    *Free* courses (no paid CourseMode) are never blocked.
    *Staff/superusers* are never blocked (admins may enroll users
    directly).
    """
    from openedx_filters.learning.filters import CourseEnrollmentStarted  # pylint: disable=import-outside-toplevel

    # Short-circuit if payments are disabled for this instance — the
    # database tables may not even exist.
    if not _payments_enabled():
        return

    # Never block staff/superuser enrollments (admin panel, etc.).
    if getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False):
        return

    course_key_str = str(course_key)

    if not _course_has_paid_mode(course_key_str):
        # Free course — nothing to gate.
        return

    if _has_paid_cart_for_course(user, course_key_str):
        # Legitimate post-payment fulfillment enrollment.
        return

    logger.warning(
        'Blocked unpaid enrollment for user %s in paid course %s (mode=%s)',
        user.id,
        course_key_str,
        mode,
    )

    raise CourseEnrollmentStarted.PreventEnrollment(
        f'Payment required for course {course_key_str}. '
        f'User {user.id} has no paid cart.'
    )
