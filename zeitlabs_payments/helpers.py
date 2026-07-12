"""Utility functions for the Payfort payment gateway."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urljoin

import qrcode
import qrcode.image.svg
from common.djangoapps.course_modes.models import CourseMode
from common.djangoapps.student.models import (
    AlreadyEnrolledError,
    CourseEnrollment,
    CourseFullError,
    EnrollmentClosedError,
)
from crum import get_current_request
from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils.safestring import mark_safe
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
from openedx.core.djangoapps.site_configuration import helpers as configuration_helpers

from zeitlabs_payments.exceptions import DuplicateCartError, GatewayError
from zeitlabs_payments.models import AuditLog, BundleCourseItem, Cart, CartItem, CatalogueItem, Invoice

logger = logging.getLogger(__name__)


VALID_PATTERNS = {
    'order_description': r"[^A-Za-z0-9 '/\._\-#:$]",
    'customer_name': r"[^A-Za-z _\\/\-\.']",
}
MAX_ORDER_DESCRIPTION_LENGTH_DEFAULT = 150


@dataclass
class ZeitLabsPluginSettings:
    """Dataclass to hold ZeitLabs Payments plugin settings."""

    @staticmethod
    def get_by_root_key(key: str, default: Any = None) -> Any:
        """Retrieve a setting by root key with a default value."""
        return configuration_helpers.get_value(
            key,
            getattr(settings, key, default),
        )

    @staticmethod
    def get_by_zeitlabs_key(key: str, default: Any) -> Any:
        """Retrieve a setting by key with a default value."""
        site_settings = ZeitLabsPluginSettings.get_by_root_key('ZEITLABS_PAYMENTS_SETTINGS') or {}
        return site_settings.get(key, default)

    invoice_prefix: str = field(
        default_factory=lambda: ZeitLabsPluginSettings.get_by_zeitlabs_key('invoice_prefix', '')
    )
    organization: str = field(
        default_factory=lambda: ZeitLabsPluginSettings.get_by_zeitlabs_key('organization', '')
    )
    customer_number: str = field(
        default_factory=lambda: ZeitLabsPluginSettings.get_by_zeitlabs_key('customer_number', '')
    )
    is_payments_enabled: bool = field(
        default_factory=lambda: ZeitLabsPluginSettings.get_by_root_key('IS_ZEITLABS_PAYMENTS_ENABLED', False)
    )
    valid_currency: str = field(
        default_factory=lambda: ZeitLabsPluginSettings.get_by_zeitlabs_key('valid_currency', '!!!')
    )
    support_url: str = field(
        default_factory=lambda: ZeitLabsPluginSettings.get_by_zeitlabs_key('support_url', '')
    )
    support_email: str = field(
        default_factory=lambda: ZeitLabsPluginSettings.get_by_zeitlabs_key('support_email', '')
    )
    logo_url: str = field(
        default_factory=lambda: ZeitLabsPluginSettings.get_by_zeitlabs_key('logo_url', '')
    )
    invoice_logo_url: str = field(
        default_factory=lambda: ZeitLabsPluginSettings.get_by_zeitlabs_key('invoice_logo_url', '')
    )
    root_url: str = field(
        default_factory=lambda: ZeitLabsPluginSettings.get_by_root_key(
            'LMS_ROOT_URL',
            ZeitLabsPluginSettings.get_by_root_key(
                'ECOMMERCE_PUBLIC_URL_ROOT',
                'ZeitLabs Payments: neither LMS_ROOT_URL nor ECOMMERCE_PUBLIC_URL_ROOT is set!'
            )
        )
    )


def get_settings() -> ZeitLabsPluginSettings:
    """Retrieve ZeitLabs Payments plugin settings."""
    return ZeitLabsPluginSettings()


def verify_param(param: Any, param_name: str, required_type: Any) -> None:
    """
    Verify a parameter type.

    :param param: The parameter to verify.
    :param param_name: The name of the parameter to be used in the exception message.
    :param required_type: The required type of the parameter.
    :raises GatewayError: If the parameter is None or not of the required type.
    """
    if param is None or not isinstance(param, required_type):
        raise GatewayError(
            f'verify_param failed: {param_name} is required and must be '
            f'({required_type.__name__}), but got ({type(param).__name__})'
        )


def get_currency(cart: Cart) -> str:
    """
    Return the currency for the given cart.

    :param cart: The shopping cart instance.
    :type cart: Cart
    :raises Exception: If any item in the cart has a currency other than the valid currency.
    :return: The valid currency code.
    :rtype: str
    """
    valid_currency = get_settings().valid_currency
    for item in cart.items.all():
        if item.catalogue_item.currency and item.catalogue_item.currency != valid_currency:
            raise Exception(f'Currency not supported: {item.catalogue_item.currency}')
    return valid_currency


def get_language(request: Optional[Any]) -> str:
    """
    Return the language code extracted from the request.

    :param request: The request object, expected to have LANGUAGE_CODE attribute.
    :type request: Optional[Any]
    :return: The language code, either 'en' or 'ar', defaults to 'en'.
    :rtype: str
    """
    if request is None or not hasattr(request, 'LANGUAGE_CODE') or not request.LANGUAGE_CODE:
        return 'en'
    result = request.LANGUAGE_CODE.split('-')[0].lower()
    return result if result in ('en', 'ar') else 'en'


def sanitize_text(
    text_to_sanitize: str,
    valid_pattern: str,
    max_length: Optional[int] = None,
    replacement: str = '_',
) -> str:
    """
    Sanitize the input text by replacing invalid characters matching the valid pattern.

    :param text_to_sanitize: The text to sanitize.
    :type text_to_sanitize: str
    :param valid_pattern: The regex pattern of valid characters.
    :type valid_pattern: str
    :param max_length: Maximum length of sanitized text. If None or <=0, no limit.
    :type max_length: Optional[int]
    :param replacement: The replacement character for invalid characters.
    :type replacement: str
    :return: The sanitized text, possibly truncated with ellipsis if too long.
    :rtype: str
    """
    if not valid_pattern:
        return ''

    sanitized = re.sub(valid_pattern, replacement, text_to_sanitize)

    if max_length is None or max_length <= 0:
        return sanitized

    if len(sanitized) > max_length and r'\.' in valid_pattern:
        return sanitized[: max_length - 3] + '...'

    return sanitized[:max_length]


def relative_url_to_absolute_url(
    relative_url: str, request: Any
) -> Optional[str]:
    """
    Convert a relative URL to an absolute URL using request's scheme and site domain.

    :param relative_url: The relative URL to convert.
    :type relative_url: str
    :param request: Django HttpRequest object, expected to have 'scheme' and 'site.domain' attributes.
    :type request: Optional[HttpRequest]
    :return: The absolute URL if conversion is possible, otherwise None.
    :rtype: Optional[str]
    """
    if request and hasattr(request, 'scheme') and hasattr(request, 'site') and request.site:
        return str(urljoin(f'{request.scheme}://{request.site.domain}', relative_url))
    return None


def get_course_id(item: CartItem) -> Optional[str]:
    """Return the course ID."""
    verify_param(item, 'item', CartItem)
    if item.catalogue_item.type == CatalogueItem.ItemType.PAID_COURSE:
        try:
            course = CourseOverview.objects.get(id=item.catalogue_item.item_ref_id)
        except Exception as exc:
            raise GatewayError(
                f'Unable to get course from catalogue item of type "{CatalogueItem.ItemType.PAID_COURSE}" '
                f'and ref_id: "{item.catalogue_item.item_ref_id}".'
            ) from exc
        return str(course.id)
    if item.catalogue_item.type == CatalogueItem.ItemType.PROGRAM_BUNDLE:
        return item.catalogue_item.item_ref_id
    raise GatewayError(f'Catalogue Item type: "{item.catalogue_item.type}" not supported.')


def get_order_description(cart: Cart, max_length: int = None) -> str:
    """
    Return the order description for the given cart.

    :param cart: The cart.
    :return: The order description.
    """
    def _get_product_description(item: CartItem) -> str:
        """Return the product description."""
        result = get_course_id(item)
        return result or '-'

    verify_param(cart, 'cart', Cart)
    description = ''
    items = list(cart.items.all())
    max_index = len(items) - 1

    for index, item in enumerate(items):
        description += f"{_get_product_description(item).replace(';', '_') or '-'}"
        if index < max_index:
            description += ' // '

    return sanitize_text(
        description,
        VALID_PATTERNS['order_description'],
        max_length=max_length or MAX_ORDER_DESCRIPTION_LENGTH_DEFAULT,
    )


def get_customer_name(cart: Cart) -> str:
    """
    Return the customer name for the given cart.

    :param cart: The cart.
    :return: The customer name.
    """
    verify_param(cart, 'cart', Cart)

    return sanitize_text(
        cart.user.get_full_name() or 'Name not set',
        VALID_PATTERNS['customer_name'],
        max_length=50,
    )


def get_merchant_reference(site_id: int, cart: Cart) -> str:
    """
    Return the merchant reference for the given cart.

    :param site_id: The site ID.
    :param cart: The cart.
    :return: The merchant reference.
    """
    verify_param(site_id, 'site_id', int)
    verify_param(cart, 'cart', Cart)

    return f'{site_id}-{cart.id}'


def get_first_course_for_cart(cart: Cart) -> Optional[dict]:
    """
    Return the first course that a learner should open after a successful payment.

    Used by the post-payment confirmation page so that learners who buy a
    program (diploma) — which contains multiple courses — are not stranded on
    the success screen. The function resolves to the same first course regardless
    of whether the cart contains a single paid course or a program bundle.

    Resolution rules:
    - For a cart with a single ``program_bundle`` item: return the first
      course linked to that bundle, ordered by ``BundleCourseItem.id`` (i.e.
      the order the admin linked them in).
    - For a cart with a single ``paid_course`` item: return that course.
    - For a cart with no items, multiple items, or any other catalogue type:
      return ``None`` (the caller should fall back to a generic CTA).

    :param cart: The cart whose contents should be inspected.
    :return: A dict with ``course_id``, ``course_name`` and ``is_program``
        keys, or ``None`` if no first course can be determined.
    """
    verify_param(cart, 'cart', Cart)

    items = list(cart.items.all())
    if len(items) != 1:
        return None

    item = items[0]
    catalogue_item = item.catalogue_item

    if catalogue_item.type == CatalogueItem.ItemType.PAID_COURSE:
        return {
            'course_id': catalogue_item.item_ref_id,
            'course_name': catalogue_item.title,
            'is_program': False,
        }

    if catalogue_item.type == CatalogueItem.ItemType.PROGRAM_BUNDLE:
        first_link = (
            BundleCourseItem.objects
            .filter(bundle=catalogue_item)
            .select_related('course_item')
            .order_by('id')
            .first()
        )
        if not first_link:
            logger.warning(
                f'Program bundle "{catalogue_item.sku}" has no linked courses; '
                'cannot determine first course for post-payment navigation.'
            )
            return None
        return {
            'course_id': first_link.course_item.item_ref_id,
            'course_name': first_link.course_item.title,
            'is_program': True,
        }

    return None


def get_first_course_url(course_id: str) -> str:
    """
    Return the URL the learner should be sent to open the course after payment.

    Prefers the Open edX Learning Microfrontend (MFE) course-home page so
    the learner lands on the modern course experience. The URL is built
    from the platform's ``LEARNING_MICROFRONTEND_URL`` setting and points
    at the course-home tab:

        ``{LEARNING_MICROFRONTEND_URL}/course/{course_key}/home``

    When ``LEARNING_MICROFRONTEND_URL`` is not configured (e.g. in some
    test setups) the function falls back to the legacy LMS course page
    at ``/courses/{course_key}/course/`` so the CTA still works.

    :param course_id: The Open edX course run key.
    :return: Absolute URL the learner should be redirected to.
    """
    mfe_base = getattr(settings, 'LEARNING_MICROFRONTEND_URL', None)
    if mfe_base:
        base = mfe_base.rstrip('/')
        return f'{base}/course/{course_id}/home'
    return f'/courses/{course_id}/course/'


def get_invoice_item_navigation(invoice_item: Any) -> Optional[dict]:
    """
    Return the post-payment navigation target for an invoice line item.

    Mirrors :func:`get_first_course_for_cart` but operates on an
    :class:`InvoiceItem`. Used by the invoice template to render a
    CTA next to each line.

    Returns a dict with ``url`` and ``is_program`` keys when a meaningful
    navigation target exists, otherwise ``None``. The template is
    responsible for picking the right button copy from the
    ``is_program`` flag, which keeps the strings translatable via
    Django's ``{% trans %}`` block.

    - ``paid_course``     → the course's page
    - ``program_bundle``  → first linked course
    - any other type     → ``None`` (no CTA)
    """
    catalogue_item = invoice_item.cart_item.catalogue_item

    if catalogue_item.type == CatalogueItem.ItemType.PAID_COURSE:
        return {
            'url': get_first_course_url(catalogue_item.item_ref_id),
            'is_program': False,
        }

    if catalogue_item.type == CatalogueItem.ItemType.PROGRAM_BUNDLE:
        first_course = get_first_course_for_cart(invoice_item.cart_item.cart)
        if first_course is None:
            return None
        return {
            'url': get_first_course_url(first_course['course_id']),
            'is_program': True,
        }

    return None


def check_user_enroll_conditions(user: get_user_model, course_mode: CourseMode) -> None:
    """
    Check whether a user can enroll in the given course mode.

    This function validates:
    - If enrollment for the course is closed.
    - If the course has reached its maximum capacity.
    - If the user is already enrolled.

    Raises an appropriate exception if any condition is not met.

    :param user: The user attempting to enroll.
    :param course_mode: The course mode of the course to check.
    :raises EnrollmentClosedError: If enrollment for the course is closed.
    :raises CourseFullError: If the course has reached its maximum allowed enrollments.
    :raises AlreadyEnrolledError: If the user is already enrolled in the course.
    :return: None
    """
    if CourseEnrollment.is_enrollment_closed(user, course_mode.course):
        logger.warning(
            'User %s failed to enroll in course %s because enrollment is closed.',
            user.username,
            str(course_mode.course.id),
        )
        raise EnrollmentClosedError('Enrollment is closed.')

    if CourseEnrollment.objects.is_course_full(course_mode.course):
        logger.warning(
            'Course %s has reached its maximum enrollment of %d learners. User %s failed to enroll.',
            str(course_mode.course.id),
            course_mode.course.max_student_enrollments_allowed,
            user.username,
        )
        raise CourseFullError('Course is Full.')
    if CourseEnrollment.is_enrolled(user, course_mode.course.id):
        logger.warning(
            'User %s attempted to enroll in %s, but they were already enrolled',
            user.username,
            str(course_mode.course.id)
        )
        raise AlreadyEnrolledError('User is already enrolled in the course.')


def generate_invoice_number(request: Any) -> str:
    """
    Generate a new unique invoice number with the given prefix.

    :param prefix: The prefix string to prepend to the invoice number (e.g., 'DEV-').
    :type prefix: str
    :returns: A unique invoice number string with the given prefix (e.g., 'DEV-100002').
    :rtype: str
    """
    prefix = get_settings().invoice_prefix
    last_invoice = Invoice.objects.filter(invoice_number__startswith=prefix).order_by('-invoice_number').first()
    if last_invoice and last_invoice.invoice_number:
        try:
            last_number = int(last_invoice.invoice_number.replace(prefix, '').replace('-', ''))
            new_number = last_number + 1
        except ValueError:
            new_number = 100001
    else:
        new_number = 100001
    return f'{prefix}-{new_number}'


def cancel_old_pending_carts(user: get_user_model) -> None:
    """
    Cancel all open carts (in 'PENDING' state) for the given user, and logs each cancellation for auditing.

    :param user: User whose carts need to be cancelled.
    """
    pending_carts = list(
        Cart.objects.filter(user=user, status=Cart.Status.PENDING)
    )

    if not pending_carts:
        logger.debug(f'No pending carts to cancel for user {user}.')
        return

    cart_ids = [cart.id for cart in pending_carts]
    updated_count = Cart.objects.filter(id__in=cart_ids).update(status=Cart.Status.CANCELLED)
    logger.debug(f'Cancelled {updated_count} pending cart(s) for user {user}.')

    for cart in pending_carts:
        cart.refresh_from_db(fields=['status'])
        AuditLog.log(
            action=AuditLog.AuditActions.CART_STATUS_UPDATED,
            cart=cart,
            context={
                'old_status': Cart.Status.PENDING,
                'new_status': Cart.Status.CANCELLED,
            }
        )


def check_duplicate_cart_with_item(
    user: get_user_model,
    course_mode: CourseMode,
    status: str = None,
    item_type: str = None
) -> None:
    """
    Check if there is existing cart.

    Raise DuplicateCartError if user already has a cart (in given status)
    containing an item with the same course.
    """
    if not status:
        status = Cart.Status.PROCESSING

    filters = {
        'user': user,
        'status': status,
        'items__catalogue_item__item_ref_id': str(course_mode.course.id),
    }
    if item_type:
        filters['items__catalogue_item__type'] = item_type

    duplicate_cart = Cart.objects.filter(**filters).first()

    if duplicate_cart:
        raise DuplicateCartError(
            f'Duplicate cart found ID: {duplicate_cart.id}, state: {status}.'
        )


def generate_invoice_qr_code(invoice_number: str) -> str:
    """
    Generate a QR code in SVG format for the checkout receipt page.

    The function creates a checkout receipt URL that includes the given order number
    as a query parameter. The QR code is generated for this URL and returned as an SVG image.

    :params order_number (int): The order number to include in the checkout receipt URL.
    :returns: str: A QR code in SVG format, rendered as a string.
    """
    receipt_url = reverse(
        'zeitlabs_payments:invoice',
        args=[invoice_number]
    )
    request = get_current_request()
    url = request.build_absolute_uri(receipt_url)

    qr = qrcode.QRCode(image_factory=qrcode.image.svg.SvgPathImage)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image()

    return mark_safe(img.to_string(encoding='unicode'))
