"""Open edX filter pipeline steps for zeitlabs_payments."""

import logging
from typing import TYPE_CHECKING, Any

from common.djangoapps.course_modes.models import CourseMode
from openedx_filters.learning.filters import CourseEnrollmentStarted

from zeitlabs_payments.helpers import get_settings
from zeitlabs_payments.models import Cart, CartItem, CatalogueItem

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from opaque_keys.edx.keys import CourseKey


def _payments_enabled() -> bool:
    """Return True if openedx-zeitlabs-payments is active on this instance."""
    return get_settings().is_payments_enabled


def _has_paid_cart_for_course(user: Any, course_key: str) -> bool:
    """
    Check whether the user has a paid cart containing a CartItem.

    Returns True if the user's cart has status ``Cart.Status.PAID`` and
    its catalogue item directly references the given course via its
    ``item_ref_id``. Having such a cart means the user went through the
    checkout→payment→fulfillment flow and is eligible for enrollment
    into that exact course.
    """
    return CartItem.objects.filter(
        cart__user=user,
        cart__status=Cart.Status.PAID,
        catalogue_item__item_ref_id=str(course_key),
    ).exists()


def _has_paid_cart_via_bundle(user: Any, course_key: str) -> bool:
    """
    Check whether the user has a paid cart that grants access via a bundle.

    Returns True if the user has a paid cart containing a
    ``PROGRAM_BUNDLE`` CartItem whose bundle links to the given course
    via ``BundleCourseItem``. When a learner purchases a program/diploma,
    the cart's catalogue item is the bundle (its ``item_ref_id`` is the
    program UUID, not any individual course key). Access to each
    constituent course is granted by the ``BundleCourseItem`` rows
    linking the bundle to those courses, so we must follow that join to
    recognize "the user paid for this course as part of a bundle".
    """
    return CartItem.objects.filter(
        cart__user=user,
        cart__status=Cart.Status.PAID,
        catalogue_item__type=CatalogueItem.ItemType.PROGRAM_BUNDLE,
        catalogue_item__bundle_courses__course_item__item_ref_id=str(course_key),
    ).exists()


def _course_has_paid_mode(course_key: str) -> bool:
    """
    Return True if the course has at least one paid CourseMode.

    Paid modes are those that are not ``audit``.
    """
    return CourseMode.objects.filter(course_id=course_key).exists()


class BlockUnpaidCourseEnrollment:
    """
    Open edX pipeline step for ``CourseEnrollmentStarted``.

    Prevents enrollment in a paid course unless the user has a completed
    payment (Cart with status PAID) for that course — either by purchasing
    the course directly or by purchasing a program bundle that includes
    the course.

    This closes the security gap where the Learning MFE's "Enroll"
    button calls ``CourseEnrollment.enroll()`` directly — bypassing the
    checkout flow that the Mako-based about page uses.

    *Free* courses (no paid CourseMode) are never blocked.
    *Staff/superusers* are never blocked (admins may enroll users
    directly).

    The Open edX filter framework instantiates this class with filter
    metadata (``filter_type``, ``running_pipeline``) and then calls
    ``run_filter()`` with the enrollment arguments.
    """

    def __init__(self, filter_type: str, running_pipeline: list, **extra_config: Any) -> None:
        """Store filter metadata passed by the framework."""
        self.filter_type = filter_type
        self.running_pipeline = running_pipeline
        self.extra_config = extra_config

    def run_filter(
        self, user: Any, course_key: 'CourseKey', mode: str
    ) -> dict[str, Any]:
        """Check payment and allow or block enrollment."""
        if not _payments_enabled():
            return {}

        if getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False):
            return {}

        course_key_str = str(course_key)

        if not _course_has_paid_mode(course_key_str):
            return {}

        if _has_paid_cart_for_course(user, course_key_str):
            return {}

        if _has_paid_cart_via_bundle(user, course_key_str):
            return {}

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
