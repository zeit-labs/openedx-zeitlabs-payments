"""Test zeitlabse payment helpers"""

from typing import Any, Optional, Union
from unittest.mock import MagicMock, patch

import pytest
from common.djangoapps.course_modes.models import CourseMode
from common.djangoapps.student.models import (
    AlreadyEnrolledError,
    CourseEnrollment,
    CourseFullError,
    EnrollmentClosedError,
)
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.test import override_settings

from zeitlabs_payments.exceptions import (
    ConfigurationError,
    CurrencyMismatchError,
    DuplicateCartError,
    GatewayError,
    InFlightPaymentError,
)
from zeitlabs_payments.helpers import (
    MAX_ORDER_DESCRIPTION_LENGTH_DEFAULT,
    cancel_old_pending_carts,
    check_carts_holding_item,
    check_user_enroll_conditions,
    describe_error,
    generate_invoice_number,
    generate_invoice_qr_code,
    get_course_id,
    get_currency,
    get_customer_name,
    get_language,
    get_merchant_reference,
    get_order_description,
    learner_holds_course,
    relative_url_to_absolute_url,
    sanitize_text,
    verify_param,
)
from zeitlabs_payments.models import (
    AuditLog,
    Cart,
    CartItem,
    CatalogueItem,
    Invoice,
    InvoiceNumberSequence,
    Transaction,
)

User = get_user_model()


@pytest.mark.django_db
def test_get_currency_valid(base_data: None) -> None:  # pylint: disable=unused-argument
    """
    Test get_currency returns correct currency for a valid cart.

    :param base_data: Fixture to load base data (assumed)
    :return: None
    """
    user = User.objects.get(id=3)
    course_item = CatalogueItem.objects.get(sku='custom-sku-1')
    user_cart = Cart.objects.create(user=user, status=Cart.Status.PENDING)
    user_cart.items.create(
        catalogue_item=course_item,
        original_price=course_item.price,
        final_price=course_item.price
    )
    assert get_currency(user_cart) == 'SAR'


@pytest.mark.django_db
def test_get_currency_raises_for_invalid_currency(base_data: None) -> None:  # pylint: disable=unused-argument
    """
    Verify that get_currency raises CurrencyMismatchError for an unsupported currency.

    A valid save can no longer produce such a row, so the offending value is written straight to the database with
    ``update()``. This covers the runtime guard against a database seeded or migrated outside the application.

    :param base_data: Fixture to load base data (assumed)
    :return: None
    """
    user = User.objects.get(id=3)
    course_item = CatalogueItem.objects.get(sku='custom-sku-2')
    CatalogueItem.objects.filter(pk=course_item.pk).update(currency='invlaid-curr')
    course_item.refresh_from_db()
    user_cart = Cart.objects.create(user=user, status=Cart.Status.PENDING)
    user_cart.items.create(
        catalogue_item=course_item,
        original_price=course_item.price,
        final_price=course_item.price
    )
    with pytest.raises(CurrencyMismatchError, match='Currency not supported: invlaid-curr'):
        get_currency(user_cart)


@pytest.mark.django_db
@pytest.mark.parametrize(
    'configured, usecase',
    [
        ({}, 'the setting is absent altogether'),
        ({'valid_currency': ''}, 'the setting is present but empty'),
        ({'valid_currency': '   '}, 'the setting holds only whitespace'),
        ({'valid_currency': '  SAR  '}, 'a real code padded with whitespace is still a bad entry'),
        ({'valid_currency': 'XYZ'}, 'a well-formed code that no gateway settles'),
        ({'valid_currency': 368}, 'a numeric code rather than an alpha one'),
    ],
)
def test_get_currency_raises_when_deployment_currency_unset(
    configured: dict, usecase: str, base_data: None  # pylint: disable=unused-argument
) -> None:
    """
    Verify that get_currency raises ConfigurationError for every value that is not a supported currency.

    There is no placeholder default any more, so an unusable currency has to fail at first use rather than travel
    onward and render to the learner. One rule decides it: the uppercase code must be a supported ISO 4217
    currency. Padding is not repaired on the operator's behalf -- a padded code is a bad entry, and accepting it
    would hide the typo rather than report it.

    :param configured: The ZEITLABS_PAYMENTS_SETTINGS value under test
    :param usecase: Description of the case being covered
    :param base_data: Fixture to load base data (assumed)
    :return: None
    """
    user = User.objects.get(id=3)
    course_item = CatalogueItem.objects.get(sku='custom-sku-1')
    user_cart = Cart.objects.create(user=user, status=Cart.Status.PENDING)
    user_cart.items.create(
        catalogue_item=course_item,
        original_price=course_item.price,
        final_price=course_item.price
    )
    with override_settings(ZEITLABS_PAYMENTS_SETTINGS=configured):
        with pytest.raises(ConfigurationError, match='valid_currency') as exc:
            get_currency(user_cart)
    assert 'valid_currency' in str(exc.value), f'Failed usecase: {usecase}'


@pytest.mark.django_db
@override_settings(ZEITLABS_PAYMENTS_SETTINGS={'valid_currency': 'sar'})
def test_get_currency_matches_case_insensitively(base_data: None) -> None:  # pylint: disable=unused-argument
    """
    Verify that a lowercase configured currency resolves and still matches the catalogue.

    Uppercase is the canonical form, so case is not a second currency: a deployment configured as ``sar`` trades in
    ``SAR``, and comparing case-sensitively against a catalogue item would fail every payment over capitalisation.

    :param base_data: Fixture to load base data (assumed)
    :return: None
    """
    user = User.objects.get(id=3)
    course_item = CatalogueItem.objects.get(sku='custom-sku-1')
    user_cart = Cart.objects.create(user=user, status=Cart.Status.PENDING)
    user_cart.items.create(
        catalogue_item=course_item,
        original_price=course_item.price,
        final_price=course_item.price
    )
    assert get_currency(user_cart) == 'SAR', 'The canonical uppercase form is what callers receive'


@pytest.mark.parametrize(
    'language_code, expected',
    [
        ('en-us', 'en'),
        ('ar-SA', 'ar'),
        ('fr-FR', 'en'),
        (None, 'en'),
    ]
)
def test_get_language_with_code(language_code: str, expected: str) -> None:
    """
    Test get_language returns expected language from LANGUAGE_CODE.

    :param language_code: The language code from request
    :param expected: Expected normalized language
    :return: None
    """
    request = MagicMock()
    request.LANGUAGE_CODE = language_code
    assert get_language(request) == expected


def test_get_language_missing_attr() -> None:
    """
    Test get_language returns default when LANGUAGE_CODE is missing.

    :return: None
    """
    request = MagicMock()
    del request.LANGUAGE_CODE
    assert get_language(request) == 'en'


@pytest.mark.parametrize(
    'text, pattern, max_len, expected, usecase',
    [
        ('hello@world', r'[^a-zA-Z0-9]', None, 'hello_world',
         'Replaces "@" with underscore when no max length.'),
        ('hello@world.com', r'[^a-zA-Z0-9]', 10, 'hello_worl',
         'Truncates to 10 chars without dot in pattern.'),
        ('hello.world@example.com', r'[^a-zA-Z0-9\.]', 10, 'hello.w...',
         'Truncates with ellipsis if dot allowed in pattern.'),
        ('test', '', 10, '',
         'Returns empty string if pattern is empty.'),
        ('Name: Tehreem Sadat!', r'[^a-zA-Z0-9]', 20, 'Name__Tehreem_Sadat_',
         'Replaces spaces and special chars with underscores.'),
    ],
)
def test_sanitize_text_cases(
    text: str,
    pattern: str,
    max_len: int,
    expected: str,
    usecase: str
) -> None:
    """
    Test sanitize_text with multiple edge cases.

    :param text: Input text
    :param pattern: Regex pattern for invalid chars
    :param max_len: Maximum allowed length
    :param expected: Expected sanitized string
    :param usecase: Description of test case
    :return: None
    """
    result = sanitize_text(text, pattern, max_length=max_len)
    assert result == expected, f'Failed usecase: {usecase}'


@pytest.mark.django_db
def test_relative_url_to_absolute_url_valid() -> None:
    """
    Test relative_url_to_absolute_url returns full URL for valid request.

    :return: None
    """
    request = MagicMock(
        scheme='https',
        site=Site.objects.create(domain='test.com', name='test.com')
    )
    result = relative_url_to_absolute_url('/checkout', request)
    assert result == f'{request.scheme}://{request.site.domain}/checkout'


def test_relative_url_to_absolute_url_missing_site() -> None:
    """
    Test relative_url_to_absolute_url returns None if site is missing.

    :return: None
    """
    request = MagicMock(scheme='https', site=None)
    assert relative_url_to_absolute_url('/checkout', request) is None


def test_relative_url_to_absolute_url_none_request() -> None:
    """
    Test relative_url_to_absolute_url returns None if request is None.

    :return: None
    """
    assert relative_url_to_absolute_url('/checkout', None) is None


@pytest.mark.parametrize(
    'param, param_name, required_type, expected_error, usecase',
    [
        (123, 'amount', int, None, 'Correct type: int'),
        ('test', 'username', str, None, 'Correct type: str'),
        ([1, 2, 3], 'items', list, None, 'Correct type: list'),
        (None, 'amount', int, 'amount is required and must be (int)', 'Param is None'),
        ('123', 'amount', int, 'amount is required and must be (int)', 'Param is str, expected int'),
        ({'key': 'val'}, 'items', list, 'items is required and must be (list)', 'Param is dict, expected list'),
    ],
)
def test_verify_param(
    param: Any,
    param_name: str,
    required_type: type,
    expected_error: Optional[str],
    usecase: str,
) -> None:
    """
    Test verify_param function with various inputs.

    :param param: Parameter value to verify.
    :param param_name: Name of the parameter.
    :param required_type: Expected type of the parameter.
    :param expected_error: Expected error message substring or None.
    :param usecase: Description of the test case.
    :return: None
    """
    if expected_error is None:
        verify_param(param, param_name, required_type)
    else:
        with pytest.raises(GatewayError) as exc_info:
            verify_param(param, param_name, required_type)
        assert expected_error in str(exc_info.value), f'Failed for case: {usecase}.'


@pytest.mark.django_db
@pytest.mark.parametrize(
    'first_name, last_name, expected_result, expect_exception, usecase',
    [
        ('John', 'Doe', 'John Doe', False, 'Normal full name'),
        ('', '', 'Name not set', False, 'Empty full name fallback'),
        ('', None, 'Name not set', False, 'None last name fallback'),
        ('A' * 60, 'B' * 60, 'A' * 47 + '...', False, 'Long full name truncated'),
        (None, None, None, True, 'Cart is None'),
        ('not_a_cart', None, None, True, 'Cart wrong type'),
    ],
)
def test_get_customer_name(
    first_name: Optional[str],
    last_name: Optional[str],
    expected_result: Optional[str],
    expect_exception: bool,
    usecase: str,
) -> None:
    """
    Test get_customer_name with various cart and user name scenarios.

    :param first_name: User's first name.
    :param last_name: User's last name.
    :param expected_result: Expected customer name string or None.
    :param expect_exception: Whether an exception is expected.
    :param usecase: Description of the test case.
    :return: None
    """
    if expect_exception:
        with pytest.raises(GatewayError):
            get_customer_name('not-cart')
    else:
        user = User.objects.create(
            username=f'{first_name}_{last_name}', first_name=first_name or '', last_name=last_name or ''
        )
        cart = Cart.objects.create(user=user)
        result = get_customer_name(cart)
        assert result == expected_result, f'Failed for case: {usecase}.'


@pytest.mark.django_db
@pytest.mark.parametrize(
    'site_id, cart_id, expected_result, expect_exception, usecase',
    [
        (1, 100, '1-100', False, 'Valid site_id and cart'),
        (0, 5, '0-5', False, 'site_id zero valid'),
        ('1', 1, None, True, 'site_id not int'),
        (1, None, None, True, 'cart is None'),
    ],
)
def test_get_merchant_reference(
    site_id: Union[int, str],
    cart_id: Optional[int],
    expected_result: Optional[str],
    expect_exception: bool,
    usecase: str,
) -> None:
    """
    Test get_merchant_reference with different site and cart inputs.

    :param site_id: Site identifier, expected to be int.
    :param cart_id: Cart identifier or None.
    :param expected_result: Expected merchant reference or None.
    :param expect_exception: Whether an exception is expected.
    :param usecase: Description of the test case.
    :return: None
    """
    cart = None
    if cart_id:
        user = User.objects.get(id=3)
        cart = Cart.objects.create(user=user, id=cart_id)

    if expect_exception:
        with pytest.raises(GatewayError):
            get_merchant_reference(site_id, cart)
    else:
        result = get_merchant_reference(site_id, cart)
        assert result == expected_result, f'Failed for case: {usecase}.'


@pytest.mark.django_db
@pytest.mark.parametrize(
    'sku, expect_exception, expected_message_part',
    [
        ('custom-sku-1', False, None),
        ('custom-sku-with_invlaid_ref_id', True, 'Unable to get course'),
        (None, True, 'not supported'),
    ],
)
def test_get_course_id(sku: Optional[str], expect_exception: bool, expected_message_part: Optional[str]) -> None:
    """
    Test get_course_id with various SKU inputs.

    :param sku: SKU string or None.
    :param expect_exception: Whether an exception is expected.
    :param expected_message_part: Substring expected in exception message.
    :return: None
    """
    if not sku:
        item = CatalogueItem.objects.create(sku='abcd', type='unsupported', price=50, currency='SAR')
    else:
        item = CatalogueItem.objects.get(sku=sku)

    cart = Cart.objects.create(user=User.objects.get(id=3), status=Cart.Status.PENDING)
    cart_item = CartItem.objects.create(
        catalogue_item=item,
        original_price=item.price,
        final_price=item.price,
        cart=cart,
    )
    if expect_exception:
        with pytest.raises(GatewayError) as excinfo:
            get_course_id(cart_item)
        assert expected_message_part in str(excinfo.value)
    else:
        result = get_course_id(cart_item)
        assert result == 'course-v1:org1+1+1'


@pytest.mark.django_db
def test_get_order_description_multiple_items(base_data: Any) -> None:  # pylint: disable=unused-argument
    """
    Test get_order_description with multiple items in a cart.

    :param base_data: Fixture data for test setup.
    :return: None
    """
    course1 = CatalogueItem.objects.get(sku='custom-sku-1')
    course2 = CatalogueItem.objects.get(sku='custom-sku-2')

    cart = Cart.objects.create(user_id=3, status=Cart.Status.PENDING)
    cart.items.create(
        catalogue_item=course1,
        original_price=course1.price,
        final_price=course1.price
    )
    cart.items.create(
        catalogue_item=course2,
        original_price=course2.price,
        final_price=course2.price
    )

    result = get_order_description(cart)

    expected = f"{course1.item_ref_id.replace('+', '_')} // {course2.item_ref_id.replace('+', '_')}"
    assert expected == result
    assert len(result) <= MAX_ORDER_DESCRIPTION_LENGTH_DEFAULT


@pytest.mark.django_db
def test_get_order_description_invalid_cart():
    """
    Test that get_order_description raises PayFortException when
    called with an invalid cart argument (not a cart object).
    """
    with pytest.raises(GatewayError, match='cart is required and must be'):
        get_order_description('not-cart')


@pytest.mark.django_db
def test_generate_invoice_number_opens_a_series_at_100001():
    """
    Verify that the first number issued under a prefix opens that prefix's series at 100001.
    """
    assert generate_invoice_number() == 'TEST-100001', 'A prefix with no counter yet starts its series at 100001'


@pytest.mark.django_db
@pytest.mark.parametrize('configured', ['', '   ', None])
def test_generate_invoice_number_refuses_a_blank_prefix(configured):
    """
    Verify that a missing or blank prefix is refused rather than yielding a number that names no series.
    """
    with override_settings(ZEITLABS_PAYMENTS_SETTINGS={'invoice_prefix': configured}):
        with pytest.raises(ConfigurationError, match='invoice_prefix'):
            generate_invoice_number()


@pytest.mark.django_db
@pytest.mark.parametrize(
    'configured, usecase',
    [
        ('DEV_1', 'an underscore'),
        ('DEV-', 'a trailing hyphen'),
        ('-DEV', 'a leading hyphen'),
        ('DE V', 'an inner space that stripping does not reach'),
        ('A' * 64, 'a label past the length RFC 1123 allows'),
    ],
)
def test_generate_invoice_number_refuses_a_malformed_prefix(configured, usecase):
    """
    Verify that a configured prefix outside the allowed alphabet is refused rather than naming a series.
    """
    with override_settings(ZEITLABS_PAYMENTS_SETTINGS={'invoice_prefix': configured}):
        with pytest.raises(ConfigurationError, match='invoice_prefix'):
            generate_invoice_number()
    assert not InvoiceNumberSequence.objects.exists(), f'Failed usecase: {usecase}'


@pytest.mark.django_db
def test_generate_invoice_number_normalizes_the_configured_prefix():
    """
    Verify that casing and surrounding whitespace do not split one deployment's series into several.
    """
    with override_settings(ZEITLABS_PAYMENTS_SETTINGS={'invoice_prefix': 'dev'}):
        assert generate_invoice_number() == 'DEV-100001', 'A lowercase prefix is issued uppercased'
    with override_settings(ZEITLABS_PAYMENTS_SETTINGS={'invoice_prefix': '  DEV  '}):
        assert generate_invoice_number() == 'DEV-100002', 'Padding does not open a second series'


def _buy(user, catalogue_item, status=Cart.Status.PAID):
    """Return a cart of the learner's, in the given status, holding the catalogue item."""
    cart = Cart.objects.create(user=user, status=status)
    cart.items.create(
        catalogue_item=catalogue_item, original_price=catalogue_item.price, final_price=catalogue_item.price,
    )
    return cart


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
@pytest.mark.parametrize('enrolment, holds, usecase', [
    (None, False, 'a learner not enrolled in the course'),
    (('no-id-professional', True), True, 'a learner enrolled in the very mode sold'),
    (('verified', True), True, 'a learner enrolled in another paid mode'),
    (('no-id-professional', False), False, 'a learner who left the course and never bought it'),
    (('audit', True), False, 'a learner auditing the course'),
    (('honor', True), False, 'a learner on the honor track of the course'),
])
def test_learner_holds_course_through_an_active_paid_enrolment(enrolment, holds, usecase):
    """
    Verify that an active enrolment in any mode but audit or honor holds the course, however it was made, and that
    a free or inactive enrolment does not.
    """
    user = User.objects.get(id=3)
    course_mode = CourseMode.objects.get(sku='custom-sku-1')
    if enrolment:
        mode, is_active = enrolment
        CourseEnrollment.objects.create(user=user, course=course_mode.course, mode=mode, is_active=is_active)

    assert learner_holds_course(user, course_mode.course.id) is holds, f'Failed usecase: {usecase}'


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
@pytest.mark.parametrize('bought_as, status, holds, usecase', [
    ('custom-sku-1', Cart.Status.PAID, True, 'a course bought on its own'),
    ('custom-sku-1', Cart.Status.REFUND_REQUESTED, True, 'a course whose refund is awaited'),
    ('BUNDLE-PRO-CERT', Cart.Status.PAID, True, 'a course bought inside a bundle'),
    ('custom-sku-1', Cart.Status.REFUNDED, False, 'a course whose purchase was refunded'),
    ('custom-sku-1', Cart.Status.PAYMENT_PENDING, False, 'a course whose payment is not settled'),
    ('course1-org2-no-id-professional', Cart.Status.PAID, False, 'another course bought'),
])
def test_learner_holds_course_through_a_purchase(bought_as, status, holds, usecase):
    """
    Verify that a paid cart, or one awaiting a refund, holds every course in it, on its own or inside a bundle, even
    after the learner unenrolled; and that a refunded or unsettled cart, or one for another course, does not.
    """
    user = User.objects.get(id=3)
    course_mode = CourseMode.objects.get(sku='custom-sku-1')
    CourseEnrollment.objects.create(user=user, course=course_mode.course, mode=course_mode.mode_slug, is_active=False)
    _buy(user, CatalogueItem.objects.get(sku=bought_as), status=status)

    assert learner_holds_course(user, course_mode.course.id) is holds, f'Failed usecase: {usecase}'


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
def test_learner_holds_course_leaves_out_the_ignored_cart():
    """
    Verify that the cart being fulfilled does not hold its own courses, while another paid cart for the course does.
    """
    user = User.objects.get(id=3)
    course_item = CatalogueItem.objects.get(sku='custom-sku-1')
    fulfilling = _buy(user, course_item)

    assert learner_holds_course(user, course_item.item_ref_id, ignored_cart=fulfilling) is False, \
        'The cart being fulfilled holds its own course'

    _buy(user, CatalogueItem.objects.get(sku='BUNDLE-PRO-CERT'))
    assert learner_holds_course(user, course_item.item_ref_id, ignored_cart=fulfilling) is True, \
        'An earlier purchase of the course is not counted beside the ignored cart'


@pytest.mark.django_db
def test_check_user_enroll_conditions_success():
    """
    Should not raise anything when all conditions pass.
    """
    user = User.objects.get(id=3)
    course_mode = CourseMode.objects.get(sku='custom-sku-1')
    check_user_enroll_conditions(user, course_mode)


@pytest.mark.django_db
@pytest.mark.parametrize(
    'patch_target, return_value, expected_exception, expected_msg',
    [
        (
            'zeitlabs_payments.helpers.CourseEnrollment.is_enrollment_closed',
            True, EnrollmentClosedError, 'Enrollment is closed.'
        ),
        (
            'zeitlabs_payments.helpers.CourseEnrollment.objects.is_course_full',
            True, CourseFullError, 'Course is Full.'
        ),
        (
            'zeitlabs_payments.helpers.CourseEnrollment.is_enrolled',
            True, AlreadyEnrolledError, 'User is already enrolled in the course.'
        ),
    ]
)
def test_check_user_enroll_conditions_failures(patch_target, return_value, expected_exception, expected_msg):
    """
    Should raise correct exception when condition fails.
    """
    user = User.objects.get(id=3)
    course_mode = CourseMode.objects.get(sku='custom-sku-1')
    course_mode.course.max_student_enrollments_allowed = 2
    course_mode.course.save()
    with patch(patch_target, return_value=return_value):
        with pytest.raises(expected_exception, match=expected_msg):
            check_user_enroll_conditions(user, course_mode)


@pytest.mark.django_db
def test_cancel_old_pending_carts():
    user = User.objects.get(id=3)
    pending_cart_1 = Cart.objects.create(user=user, status=Cart.Status.PENDING)
    pending_cart_2 = Cart.objects.create(user=user, status=Cart.Status.PENDING)
    _ = Cart.objects.create(user=user, status=Cart.Status.CANCELLED)  # already cancelled cart.
    AuditLog.objects.all().delete()

    cancel_old_pending_carts(user)

    all_carts = Cart.objects.filter(user=user)
    assert all(c.status == Cart.Status.CANCELLED for c in all_carts), \
        'All user carts should be in CANCELLED status'

    audit_logs = AuditLog.objects.filter(
        action=AuditLog.AuditActions.CART_CANCELLED,
        cart__user=user,
    )
    assert audit_logs.count() == 2, 'Only pending carts should generate audit logs'

    updated_ids = {pending_cart_1.id, pending_cart_2.id}
    logged_ids = set(audit_logs.values_list('cart_id', flat=True))
    assert logged_ids == updated_ids, 'Audit logs should only exist for carts that were updated from PENDING'


def cart_holding(item, status, gateway='dummy', user_id=3):
    """Return a cart of the learner holding the item, in the given status and handed to the given gateway."""
    cart = Cart.objects.create(user=User.objects.get(id=user_id), status=status, gateway=gateway)
    cart.items.create(catalogue_item=item, original_price=item.price, final_price=item.price)
    return cart


def invoice_cart(cart):
    """Give the cart the invoice a fulfilled payment has."""
    item = cart.items.get().catalogue_item
    payment = Transaction.objects.create(
        cart=cart, amount=item.price, gateway='dummy', gateway_transaction_id=f'TX-{cart.id}',
    )
    Invoice.objects.create(
        status=Invoice.InvoiceStatus.PAID, invoice_number=f'TEST-{cart.id}', cart=cart, gross_total=item.price,
        total=item.price, currency='SAR', related_transaction=payment,
    )


# How a cart in each status, holding the item and handed to a gateway, answers its learner buying the item again: None
# when it lets them, or the error it raises. Every status is listed, so a status added later fails
# test_every_cart_status_is_decided_for_buying_again until someone decides how it answers.
BUYING_AGAIN_BESIDE = {
    Cart.Status.PENDING: None,
    Cart.Status.CANCELLED: None,
    Cart.Status.CLOSED: None,
    Cart.Status.REFUNDED: None,
    Cart.Status.PROCESSING: InFlightPaymentError,
    Cart.Status.PAYMENT_PENDING: InFlightPaymentError,
    Cart.Status.PAID: DuplicateCartError,
    Cart.Status.UNRECONCILED: DuplicateCartError,
    Cart.Status.REFUND_REQUESTED: DuplicateCartError,
}


def test_every_cart_status_is_decided_for_buying_again():
    """
    Verify that every cart status is decided as blocking its learner buying the same item again or not, and that the
    statuses decided not to are exactly the non-blocking ones.
    """
    assert set(BUYING_AGAIN_BESIDE) == set(Cart.Status.values), 'A cart status is not decided for buying again'
    assert set(Cart.NON_BLOCKING_STATUSES) == {
        status for status, error in BUYING_AGAIN_BESIDE.items() if error is None
    }, 'NON_BLOCKING_STATUSES does not list exactly the statuses decided not to block'


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
@pytest.mark.parametrize('status, error', BUYING_AGAIN_BESIDE.items())
def test_a_cart_holding_the_item_blocks_buying_it_unless_its_status_does_not(status, error):
    """
    Verify that a cart holding the item blocks its learner buying it again, sending them back to a payment in flight,
    unless its status is one that does not block.
    """
    item = CatalogueItem.objects.get(sku='custom-sku-1')
    cart_holding(item, status)

    if error is None:
        check_carts_holding_item(User.objects.get(id=3), item)
    else:
        with pytest.raises(error):
            check_carts_holding_item(User.objects.get(id=3), item)


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
def test_a_payment_in_flight_is_raised_with_its_cart_before_any_other():
    """
    Verify that a payment in flight is raised carrying its cart, even beside another blocking cart, so its learner can
    still be sent back to it.
    """
    item = CatalogueItem.objects.get(sku='custom-sku-1')
    cart_holding(item, Cart.Status.UNRECONCILED)
    inflight = cart_holding(item, Cart.Status.PAYMENT_PENDING)

    with pytest.raises(InFlightPaymentError) as raised:
        check_carts_holding_item(User.objects.get(id=3), item)

    assert raised.value.cart == inflight, 'The payment in flight is not the cart raised'


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
@pytest.mark.parametrize('status, invoiced, refusal, usecase', [
    (Cart.Status.PAID, True, 'It was already bought in cart {id}.', 'a completed sale'),
    (Cart.Status.PAID, False, 'A payment for it in cart {id} is still being resolved.', 'paid, not invoiced'),
    (
        Cart.Status.PAYMENT_PENDING, False, 'A payment for it in cart {id} is still being resolved.',
        'answered about by a gateway that was not recorded, which cannot be asked again',
    ),
])
def test_a_blocking_cart_not_in_flight_refuses_saying_why(status, invoiced, refusal, usecase):
    """
    Verify that a blocking cart whose payment is not in flight refuses the item, telling a completed sale from a
    payment still being resolved.
    """
    item = CatalogueItem.objects.get(sku='custom-sku-1')
    cart = cart_holding(item, status, gateway=None)
    if invoiced:
        invoice_cart(cart)

    with pytest.raises(DuplicateCartError) as raised:
        check_carts_holding_item(User.objects.get(id=3), item)

    assert str(raised.value) == refusal.format(id=cart.id), f'Failed usecase: {usecase}'


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
def test_a_hand_off_that_recorded_no_gateway_is_raised_as_in_flight():
    """
    Verify that a processing cart handed off before carts recorded their gateway is raised as a payment in flight,
    carrying its cart, so its age can decide it rather than refusing the learner for good.
    """
    item = CatalogueItem.objects.get(sku='custom-sku-1')
    stale = cart_holding(item, Cart.Status.PROCESSING, gateway=None)

    with pytest.raises(InFlightPaymentError) as raised:
        check_carts_holding_item(User.objects.get(id=3), item)

    assert raised.value.cart == stale, 'The hand-off with no gateway is not the cart raised'


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
def test_a_course_is_held_by_any_of_its_paid_items():
    """
    Verify that a cart holding another paid item of the same course blocks buying the course, while one holding another
    course does not.

    Open edX keeps one enrolment per learner and course, so two paid modes of one course cannot both be fulfilled.
    """
    item = CatalogueItem.objects.get(sku='custom-sku-1')
    same_course = CatalogueItem.objects.create(
        sku='custom-sku-1-other-mode', type=CatalogueItem.ItemType.PAID_COURSE, title='Same course, other mode',
        item_ref_id=item.item_ref_id, price=item.price, currency=item.currency,
    )
    other_course = CatalogueItem.objects.get(sku='course1-org2-no-id-professional')
    cart_holding(other_course, Cart.Status.UNRECONCILED)
    check_carts_holding_item(User.objects.get(id=3), item)

    cart_holding(same_course, Cart.Status.UNRECONCILED)
    with pytest.raises(DuplicateCartError):
        check_carts_holding_item(User.objects.get(id=3), item)


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
def test_a_cart_for_a_course_never_blocks_a_bundle_containing_it():
    """
    Verify that a cart for a course, whatever its blocking status, never blocks buying a bundle that contains it.

    Buying a course must never stop its learner buying a bundle that contains it; the bundle is sold as is, its
    fulfilment skipping the course they hold.
    """
    for status in (Cart.Status.PAYMENT_PENDING, Cart.Status.PAID, Cart.Status.UNRECONCILED):
        cart_holding(CatalogueItem.objects.get(sku='custom-sku-1'), status)

    check_carts_holding_item(User.objects.get(id=3), CatalogueItem.objects.get(sku='BUNDLE-PRO-CERT'))


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
def test_a_bundle_in_flight_sends_a_learner_buying_one_of_its_courses_back_to_it():
    """
    Verify that a bundle whose payment is in flight is raised to a learner buying one of its courses, carrying the
    bundle's cart, as a payment in flight for the course itself is.

    The bundle pays for the course too, so selling the course before the bundle's payment resolves charges for it twice.
    """
    bundle_cart = cart_holding(CatalogueItem.objects.get(sku='BUNDLE-PRO-CERT'), Cart.Status.PAYMENT_PENDING)

    with pytest.raises(InFlightPaymentError) as raised:
        check_carts_holding_item(User.objects.get(id=3), CatalogueItem.objects.get(sku='custom-sku-1'))

    assert raised.value.cart == bundle_cart, 'The bundle in flight is not the cart raised'


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
@pytest.mark.parametrize('status, invoiced, refusal, usecase', [
    (Cart.Status.PAID, True, 'It was already bought in cart {id}.', 'a bundle bought'),
    (Cart.Status.UNRECONCILED, False, 'A payment for it in cart {id} is still being resolved.', 'a bundle held'),
])
def test_a_bundle_containing_the_course_refuses_it_saying_why(status, invoiced, refusal, usecase):
    """
    Verify that a blocking cart for a bundle containing the course refuses the course, as a cart for the course does.
    """
    bundle_cart = cart_holding(CatalogueItem.objects.get(sku='BUNDLE-PRO-CERT'), status)
    if invoiced:
        invoice_cart(bundle_cart)

    with pytest.raises(DuplicateCartError) as raised:
        check_carts_holding_item(User.objects.get(id=3), CatalogueItem.objects.get(sku='custom-sku-1'))

    assert str(raised.value) == refusal.format(id=bundle_cart.id), f'Failed usecase: {usecase}'


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
def test_a_bundle_not_containing_the_course_or_not_blocking_lets_it_through():
    """
    Verify that a bundle cart lets the course through when the bundle does not contain it, or its status does not
    block.
    """
    other_course = CatalogueItem.objects.get(sku='custom-sku-2')
    for status in Cart.NON_BLOCKING_STATUSES:
        cart_holding(CatalogueItem.objects.get(sku='BUNDLE-PRO-CERT'), status)

    check_carts_holding_item(User.objects.get(id=3), CatalogueItem.objects.get(sku='custom-sku-1'))

    cart_holding(CatalogueItem.objects.get(sku='BUNDLE-PRO-CERT'), Cart.Status.PAID)
    check_carts_holding_item(User.objects.get(id=3), other_course)


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
def test_the_same_bundle_blocks_buying_it_again():
    """Verify that a bundle already bought blocks buying it again."""
    bundle = CatalogueItem.objects.get(sku='BUNDLE-PRO-CERT')
    cart = cart_holding(bundle, Cart.Status.PAID)
    invoice_cart(cart)

    with pytest.raises(DuplicateCartError, match=f'already bought in cart {cart.id}'):
        check_carts_holding_item(User.objects.get(id=3), bundle)


@pytest.mark.django_db
def test_cancel_old_pending_carts_leaves_a_cart_another_request_handed_off():
    """
    Verify that a pending cart another request moves to processing while carts are being cancelled is left as it is.

    The learner may be paying for it in another tab, so cancelling it would cancel a payment under way. The other
    pending carts are still cancelled.
    """
    user = User.objects.get(id=3)
    handed_off = Cart.objects.create(user=user, status=Cart.Status.PENDING)
    still_pending = Cart.objects.create(user=user, status=Cart.Status.PENDING)
    AuditLog.objects.all().delete()
    change_status = Cart.change_status

    def hand_off_before_cancelling(cart, *args, **kwargs):
        if cart.pk == handed_off.pk:
            Cart.objects.filter(pk=cart.pk).update(status=Cart.Status.PROCESSING)
        return change_status(cart, *args, **kwargs)

    with patch.object(Cart, 'change_status', autospec=True, side_effect=hand_off_before_cancelling):
        cancel_old_pending_carts(user)

    assert Cart.objects.get(pk=handed_off.pk).status == Cart.Status.PROCESSING, 'A handed-off cart was cancelled'
    assert Cart.objects.get(pk=still_pending.pk).status == Cart.Status.CANCELLED, 'A pending cart was not cancelled'
    assert list(AuditLog.objects.values_list('cart_id', flat=True)) == [still_pending.pk], \
        'Only the cancelled cart should be on the record'


@pytest.mark.django_db
def test_cancel_old_pending_carts_writes_nothing_when_there_is_nothing_to_cancel():
    """
    Verify that a user with no pending cart leaves both the cart table and the audit trail untouched.

    Every add-to-cart calls this first, so the common case is the one where there is nothing to cancel. An
    audit entry there would fill the trail with rows recording that nothing happened.
    """
    user = User.objects.get(id=4)
    Cart.objects.create(user=user, status=Cart.Status.CANCELLED)
    Cart.objects.create(user=user, status=Cart.Status.PAID)
    AuditLog.objects.all().delete()

    cancel_old_pending_carts(user)

    assert not AuditLog.objects.exists(), 'Cancelling nothing should write no audit entry'
    assert set(Cart.objects.filter(user=user).values_list('status', flat=True)) == {
        Cart.Status.CANCELLED, Cart.Status.PAID,
    }, 'Carts that were never pending should keep the status they had'


@pytest.mark.django_db
def test_generate_invoice_qr_code_returns_an_inline_svg():
    """
    Verify that a receipt URL becomes an inline SVG the invoice template can render without a file.
    """
    svg = generate_invoice_qr_code('https://learn.example.com/payment/v2/invoice/12345/')

    assert svg.startswith('<svg '), 'The QR code must be an inline SVG element'
    assert 'id="qr-path"' in svg, 'The SVG must carry the QR path it encodes'


@pytest.mark.django_db
@patch('zeitlabs_payments.helpers.qrcode.QRCode')
def test_generate_invoice_qr_code_encodes_exactly_the_url_it_is_given(mock_qr_code):
    """
    Verify that the URL is encoded unmodified, since a printed receipt cannot be reissued against a new host.
    """
    receipt_url = 'https://learn.example.com/payment/v2/invoice/12345/'
    mock_qr_code.return_value.make_image.return_value.to_string.return_value = '<svg/>'

    generate_invoice_qr_code(receipt_url)

    mock_qr_code.return_value.add_data.assert_called_once_with(receipt_url)


@pytest.mark.django_db
@pytest.mark.parametrize('receipt_url, usecase', [(None, 'no URL at all'), ('', 'nothing at all')])
def test_generate_invoice_qr_code_returns_nothing_without_a_url(receipt_url, usecase):
    """
    Verify that an invoice issued before the origin was recorded gets no QR code rather than an error.
    """
    assert generate_invoice_qr_code(receipt_url) == '', f'Failed usecase: {usecase}'


def test_describe_error_names_the_error_and_every_error_behind_it():
    """
    Verify that an error is named with the errors behind it, outermost first, by class and message: the one it was
    raised from, and the one being handled when that was raised.
    """
    with pytest.raises(GatewayError) as raised:
        try:
            try:
                raise KeyError('ACCESS_TOKEN')
            except KeyError:
                raise ValueError('settings incomplete')  # pylint: disable=raise-missing-from
        except ValueError as exc:
            raise GatewayError('cannot set up') from exc

    assert describe_error(raised.value) == (
        "GatewayError: cannot set up, caused by ValueError: settings incomplete, caused by KeyError: 'ACCESS_TOKEN'"
    ), 'The error is not named together with every error behind it'


def test_describe_error_names_an_error_without_a_message_by_its_class():
    """Verify that an error raised with no message, as Open edX raises its enrolment errors, is named by its class."""
    assert describe_error(EnrollmentClosedError()) == 'EnrollmentClosedError', 'The error is not named by its class'


def test_describe_error_leaves_out_what_an_error_hides():
    """Verify that an error raised ``from None`` is named without the error it hides."""
    with pytest.raises(GatewayError) as raised:
        try:
            raise KeyError('ACCESS_TOKEN')
        except KeyError:
            raise GatewayError('cannot set up') from None

    assert describe_error(raised.value) == 'GatewayError: cannot set up', 'The hidden error was named'


def test_describe_error_names_each_error_once_when_they_cause_each_other():
    """Verify that errors chained into a loop are each named once, rather than described without end."""
    first, second = ValueError('first'), ValueError('second')
    first.__cause__, second.__cause__ = second, first

    assert describe_error(first) == 'ValueError: first, caused by ValueError: second', 'The loop was not cut'
