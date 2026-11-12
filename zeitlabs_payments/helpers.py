"""Utility functions for the Payfort payment gateway."""

from __future__ import annotations

import logging
import re
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
from django.contrib.auth import get_user_model
from django.db.models import Q, QuerySet
from django.utils.safestring import mark_safe
from django.utils.timezone import now
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview

from zeitlabs_payments.exceptions import (
    CartStatusError,
    ConfigurationError,
    CurrencyMismatchError,
    DuplicateCartError,
    GatewayError,
    InFlightPaymentError,
)
from zeitlabs_payments.models import AuditLog, Cart, CartItem, CatalogueItem, InvoiceNumberSequence
from zeitlabs_payments.settings_service import get_settings
from zeitlabs_payments.validators import canonical_currency, is_supported_currency, is_valid_invoice_prefix

logger = logging.getLogger(__name__)


VALID_PATTERNS = {
    'order_description': r"[^A-Za-z0-9 '/\._\-#:$]",
    'customer_name': r"[^A-Za-z _\\/\-\.']",
}
MAX_ORDER_DESCRIPTION_LENGTH_DEFAULT = 150


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
    Return the deployment currency for the given cart.

    A deployment trades in one currency, so this returns the configured one rather than any item's own. The
    per-item comparison stays as a guard on rows written before that rule was enforced at save time: a valid save
    can no longer produce a mismatch, but a seeded or migrated database can still hold one.

    :param cart: The shopping cart instance.
    :type cart: Cart
    :raises ConfigurationError: If the deployment currency is not a supported currency.
    :raises CurrencyMismatchError: If any item's currency is not the deployment currency.
    :return: The valid currency code, in its canonical uppercase form.
    :rtype: str
    """
    configured = get_settings().valid_currency
    if not is_supported_currency(configured):
        raise ConfigurationError(
            f"ZeitLabs Payments: 'valid_currency' is not set to a supported ISO 4217 currency "
            f'(currently {configured!r}).'
        )
    valid_currency = canonical_currency(configured)
    for item in cart.items.all():
        item_currency = item.catalogue_item.currency
        if item_currency and item_currency.upper() != valid_currency:
            raise CurrencyMismatchError(f'Currency not supported: {item_currency}')
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


def learner_holds_course(user: get_user_model, course_key: Any, ignored_cart: Optional[Cart] = None) -> bool:
    """
    Return whether the learner holds the course: buying it again would give them nothing.

    They hold it when either is true:

    - A cart of theirs that is paid, or awaiting a refund, contains the course: on its own in any paid mode, or inside
      a bundle. It stays held after they unenrol; a refunded cart no longer counts.
    - They have an active enrolment in it in a mode Open edX does not upgrade from, however it was made, staff
      enrolling them by hand included. A course they only audit or honor is not held: buying it upgrades them.

    :param user: The learner.
    :param course_key: The course's id.
    :param ignored_cart: A cart not to count: the one being fulfilled, which is paid by then and would hold every
        course in it.
    :return: Whether the learner holds the course.
    """
    mode, is_active = CourseEnrollment.enrollment_mode_for_user(user, course_key)
    if is_active and not CourseMode.is_mode_upgradeable(mode):
        return True

    course_ref = str(course_key)
    purchases = Cart.objects.filter(
        user=user, status__in=(Cart.Status.PAID, Cart.Status.REFUND_REQUESTED),
    ).filter(
        Q(
            items__catalogue_item__type=CatalogueItem.ItemType.PAID_COURSE,
            items__catalogue_item__item_ref_id=course_ref,
        ) |
        Q(items__catalogue_item__bundle_courses__course_item__item_ref_id=course_ref),
    )
    if ignored_cart is not None:
        purchases = purchases.exclude(id=ignored_cart.id)
    return purchases.exists()


def check_user_enroll_conditions(user: get_user_model, course_mode: CourseMode) -> None:
    """
    Check whether a user can buy the given course mode: enrol in it, or upgrade to it from a free mode.

    - No one buys a mode past its ``expiration_datetime``, which is when Open edX stops enrolling in it.
    - A learner enrolled in a mode Open edX lets upgrade -- audit or honor -- upgrades. They hold a seat already, so
      whether the course's enrolment is closed, or the course full, does not stop them.
    - A learner enrolled in any other mode, the one bought included, is refused.
    - Anyone else enrols, which needs the course's enrolment open and the course not full.

    :param user: The user attempting to enroll.
    :param course_mode: The course mode of the course to check.
    :raises EnrollmentClosedError: If the mode has closed, or enrollment for the course is closed.
    :raises CourseFullError: If the course has reached its maximum allowed enrollments.
    :raises AlreadyEnrolledError: If the user is already enrolled in the course, in a mode they cannot upgrade from.
    :return: None
    """
    expiration = course_mode.expiration_datetime
    if expiration is not None and expiration <= now():
        logger.warning(
            'User %s failed to buy mode %s of course %s, which closed on %s.',
            user.username,
            course_mode.mode_slug,
            str(course_mode.course.id),
            expiration,
        )
        raise EnrollmentClosedError(f'The {course_mode.mode_slug} mode closed on {expiration:%Y-%m-%d}.')

    if CourseEnrollment.is_enrolled(user, course_mode.course.id):
        mode, _ = CourseEnrollment.enrollment_mode_for_user(user, course_mode.course.id)
        if CourseMode.is_mode_upgradeable(mode):
            return
        logger.warning(
            'User %s attempted to enroll in %s, but they were already enrolled',
            user.username,
            str(course_mode.course.id)
        )
        raise AlreadyEnrolledError('User is already enrolled in the course.')

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


def generate_invoice_number() -> str:
    """
    Reserve and return the next invoice number for the configured prefix.

    This resolves what the deployment's prefix is and refuses an unusable one; reserving the number itself
    belongs to the counter and lives on ``InvoiceNumberSequence``.

    The prefix is stripped and uppercased before use, so however the setting happens to be typed a deployment
    issues one series rather than several near-identical ones. Each prefix carries its own counter, so changing
    it opens a fresh series at 100001 -- or, if that prefix issued numbers before this counter existed, above
    the highest of them.

    :raises ConfigurationError: If ``invoice_prefix`` is unset, blank, or not a capitalized RFC 1123 label. An
        unprefixed number would be a bare integer with nothing naming the series it belongs to, and one outside
        that alphabet would not survive the places an invoice number travels, so both are refused here.
    :returns: The reserved number as ``<PREFIX>-<digits>`` (e.g. ``'DEV-100002'``), everything after the dash
        being numeric.
    """
    configured = get_settings().invoice_prefix
    prefix = str(configured or '').strip().upper()
    if not prefix:
        raise ConfigurationError(
            f"ZeitLabs Payments: 'invoice_prefix' is required and must not be blank (currently {configured!r}).",
        )
    if not is_valid_invoice_prefix(prefix):
        raise ConfigurationError(
            f"ZeitLabs Payments: 'invoice_prefix' must be letters, digits and inner hyphens, at most 63 "
            f'characters (currently {configured!r}).',
        )
    return f'{prefix}-{InvoiceNumberSequence.reserve_next_for(prefix)}'


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

    for cart in pending_carts:
        try:
            cart.change_status(Cart.Status.CANCELLED, action=AuditLog.AuditActions.CART_CANCELLED)
        except CartStatusError as exc:
            # Another request moved the cart out of pending since it was read -- handed it to a gateway, most likely.
            # It is no longer an open cart, so it is not this function's to cancel.
            logger.info(f'Pending cart {cart.id} of user {user} left as it is: {exc}')


def get_blocking_carts(user: get_user_model, catalogue_item: CatalogueItem) -> QuerySet:
    """
    Return the learner's carts that stop them buying the item again.

    Every cart blocks except one whose status ``Cart.NON_BLOCKING_STATUSES`` lists. For a course, a cart blocks when it
    holds any paid item of the same course -- Open edX keeps one enrolment per learner and course, so two paid modes of
    one course cannot both be fulfilled -- or a bundle containing the course, which pays for the course too. For a
    bundle, only a cart holding the same bundle blocks: a bundle is sold as is while the learner holds only some of its
    courses, so a cart for one of them does not stop it.

    :param user: The learner buying the item.
    :param catalogue_item: The course or bundle being bought.
    """
    if catalogue_item.type == CatalogueItem.ItemType.PAID_COURSE:
        holding_item = Q(
            items__catalogue_item__type=CatalogueItem.ItemType.PAID_COURSE,
            items__catalogue_item__item_ref_id=catalogue_item.item_ref_id,
        ) | Q(items__catalogue_item__bundle_courses__course_item__item_ref_id=catalogue_item.item_ref_id)
    else:
        holding_item = Q(items__catalogue_item=catalogue_item)
    return Cart.objects.filter(holding_item, user=user).exclude(status__in=Cart.NON_BLOCKING_STATUSES).distinct()


def check_carts_holding_item(user: get_user_model, catalogue_item: CatalogueItem) -> None:
    """
    Refuse the item while another of the learner's carts blocks buying it, or send them back to that cart's payment.

    See ``get_blocking_carts`` for which carts block. A payment in flight -- processing or awaiting payment, with its
    gateway recorded -- is raised first, so the learner can be sent back to it, told to wait, or let through once it
    expires. Any other blocking cart refuses the item.

    :param user: The learner buying the item.
    :param catalogue_item: The course or bundle being bought.
    :raises InFlightPaymentError: When a blocking cart has its payment in flight, carrying the latest.
    :raises DuplicateCartError: When any other blocking cart exists: paid, unreconciled, awaiting a refund, or handed
        off without its gateway recorded.
    :return: None
    """
    blocking_carts = get_blocking_carts(user, catalogue_item)

    inflight_cart = blocking_carts.filter(
        status__in=(Cart.Status.PROCESSING, Cart.Status.PAYMENT_PENDING), gateway__isnull=False,
    ).order_by('-updated_at').first()
    if inflight_cart:
        raise InFlightPaymentError(
            f'A payment for this item is still in flight in cart {inflight_cart.id} ({inflight_cart.status}).',
            cart=inflight_cart,
        )

    blocking_cart = blocking_carts.order_by('-updated_at').first()
    if blocking_cart is None:
        return
    if blocking_cart.status == Cart.Status.PAID and blocking_cart.invoices.exists():
        # TODO: implement the credit-note refund feature to allow refunding paid carts with invoices
        raise DuplicateCartError(
            f'It was already bought in cart {blocking_cart.id}.',
            cart_id=blocking_cart.id,
            reason=DuplicateCartError.BOUGHT,
        )
    raise DuplicateCartError(
        f'A payment for it in cart {blocking_cart.id} is still being resolved.',
        cart_id=blocking_cart.id,
        reason=DuplicateCartError.UNRESOLVED,
    )


def describe_error(error: BaseException) -> str:
    """
    Name an error and every error behind it, outermost first, each by its class and its message.

    The plugin raises its own errors from the ones it catches -- an enrolment Open edX refuses is raised again as a
    fulfilment error -- so the outermost error names the step that failed, and the ones behind it say what to fix. An
    error raised ``from None`` hides what it was raised from, and so does it here.

    :param error: The error caught.
    :return: Each error as ``ClassName: message``, or its class name alone when it has no message, joined by
        ``, caused by``.
    """
    described = []
    seen = set()
    current: Optional[BaseException] = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current)
        described.append(f'{type(current).__name__}: {message}' if message else type(current).__name__)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return ', caused by '.join(described)


def generate_invoice_qr_code(receipt_url: Optional[str]) -> str:
    """
    Generate the receipt QR code as inline SVG for the given absolute receipt URL.

    The URL is settled against the domain recorded on the invoice when it was issued, never against the
    request rendering it now: a printed receipt cannot be reissued, so the host the QR encodes has to be the
    one it encoded the day it was printed. Invoices issued before the domain was recorded get no QR.

    :param receipt_url: The absolute receipt URL to encode, or None for an invoice that has none.
    :returns: The QR code as an inline SVG safe string, or an empty string when there is no URL.
    """
    if not receipt_url:
        return ''

    qr = qrcode.QRCode(image_factory=qrcode.image.svg.SvgPathImage)
    qr.add_data(receipt_url)
    qr.make(fit=True)
    img = qr.make_image()

    return mark_safe(img.to_string(encoding='unicode'))
