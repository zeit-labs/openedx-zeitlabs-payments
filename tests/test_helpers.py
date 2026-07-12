"""Test zeitlabse payment helpers"""

from typing import Any, Optional, Union
from unittest.mock import MagicMock, patch

import pytest
from common.djangoapps.course_modes.models import CourseMode
from common.djangoapps.student.models import AlreadyEnrolledError, CourseFullError, EnrollmentClosedError
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site

from zeitlabs_payments.exceptions import DuplicateCartError, GatewayError
from zeitlabs_payments.helpers import (
    MAX_ORDER_DESCRIPTION_LENGTH_DEFAULT,
    cancel_old_pending_carts,
    check_duplicate_cart_with_item,
    check_user_enroll_conditions,
    generate_invoice_number,
    get_course_id,
    get_currency,
    get_customer_name,
    get_first_course_for_cart,
    get_first_course_url,
    get_invoice_item_navigation,
    get_language,
    get_merchant_reference,
    get_order_description,
    relative_url_to_absolute_url,
    sanitize_text,
    verify_param,
)
from zeitlabs_payments.models import AuditLog, BundleCourseItem, Cart, CartItem, CatalogueItem, Invoice

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
    Test get_currency raises Exception for invalid currency.

    :param base_data: Fixture to load base data (assumed)
    :return: None
    """
    user = User.objects.get(id=3)
    course_item = CatalogueItem.objects.get(sku='custom-sku-2')
    user_cart = Cart.objects.create(user=user, status=Cart.Status.PENDING)
    user_cart.items.create(
        catalogue_item=course_item,
        original_price=course_item.price,
        final_price=course_item.price
    )
    with pytest.raises(Exception, match='Currency not supported: invlaid-curr'):
        get_currency(user_cart)


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
        item = CatalogueItem.objects.create(sku='abcd', type='unsupported', price=50)
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
def test_generate_invoice_number_no_previous_invoice():
    """
    Should generate invoice number starting from 100001 when no previous invoice exists.
    """
    invoice_number = generate_invoice_number(request=None)
    assert invoice_number == 'TEST-100001'


@pytest.mark.django_db
@pytest.mark.parametrize(
    'existing_invoice_number, expected_invoice_number',
    [
        ('TEST-100005', 'TEST-100006'),
        ('TEST-INVALID', 'TEST-100001'),
    ]
)
def test_generate_invoice_number_with_existing_invoice(existing_invoice_number, expected_invoice_number):
    """
    Should increment when last invoice number is valid,
    or reset when last invoice number is invalid.
    """
    Invoice.objects.create(
        invoice_number=existing_invoice_number,
        total=100,
        cart=Cart.objects.create(user=User.objects.get(id=3), status=Cart.Status.PAID),
        gross_total=100
    )
    invoice_number = generate_invoice_number(request=None)
    assert invoice_number == expected_invoice_number


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
        action=AuditLog.AuditActions.CART_STATUS_UPDATED,
        cart__user=user,
    )
    assert audit_logs.count() == 2, 'Only pending carts should generate audit logs'

    updated_ids = {pending_cart_1.id, pending_cart_2.id}
    logged_ids = set(audit_logs.values_list('cart_id', flat=True))
    assert logged_ids == updated_ids, 'Audit logs should only exist for carts that were updated from PENDING'


@pytest.mark.django_db
def test_no_duplicate_cart_found():
    """No duplicate → should NOT raise."""
    user = User.objects.get(id=3)
    course_mode = CourseMode.objects.get(sku='custom-sku-1')
    check_duplicate_cart_with_item(user, course_mode)


@pytest.mark.django_db
def test_duplicate_exists_but_different_status():
    """Duplicate exists but NOT in PROCESSING → should NOT raise."""
    user = User.objects.get(id=3)
    course_mode = CourseMode.objects.get(sku='custom-sku-1')
    item = CatalogueItem.objects.get(sku='custom-sku-1')
    cart = Cart.objects.create(user=user, status=Cart.Status.PENDING)
    cart.items.create(catalogue_item=item, original_price=item.price, final_price=item.price)
    # Default checks only PROCESSING
    check_duplicate_cart_with_item(user, course_mode)


@pytest.mark.django_db
def test_duplicate_exists_but_different_item_type():
    """Duplicate exists but item_type filter mismatch → should NOT raise."""
    user = User.objects.get(id=3)
    course_mode = CourseMode.objects.get(sku='custom-sku-1')
    item = CatalogueItem.objects.get(sku='custom-sku-1')
    cart = Cart.objects.create(user=user, status=Cart.Status.PROCESSING)
    cart.items.create(
        catalogue_item=item,
        original_price=item.price,
        final_price=item.price
    )
    item.type = 'other_type'
    item.save()

    # Requesting a specific item_type; mismatch → no exception
    check_duplicate_cart_with_item(user, course_mode, item_type=item.ItemType.PAID_COURSE)


@pytest.mark.django_db
def test_duplicate_exists_with_matching_item_type():
    """Duplicate exists and item_type matches → should raise."""
    user = User.objects.get(id=3)
    course_mode = CourseMode.objects.get(sku='custom-sku-1')
    item = CatalogueItem.objects.get(sku='custom-sku-1')
    cart = Cart.objects.create(user=user, status=Cart.Status.PROCESSING)
    cart.items.create(
        catalogue_item=item,
        original_price=item.price,
        final_price=item.price
    )

    with pytest.raises(DuplicateCartError):
        check_duplicate_cart_with_item(user, course_mode, item_type=item.ItemType.PAID_COURSE)


@pytest.mark.django_db
def test_duplicate_exists_but_different_course():
    """Cart has item, but a different course → should NOT raise."""
    user = User.objects.get(id=3)
    course_mode_requested = CourseMode.objects.get(sku='custom-sku-1')
    requested_item_ref = str(course_mode_requested.course.id)

    another_item = CatalogueItem.objects.exclude(item_ref_id=requested_item_ref).first()
    cart = Cart.objects.create(user=user, status=Cart.Status.PROCESSING)
    cart.items.create(
        catalogue_item=another_item,
        original_price=another_item.price,
        final_price=another_item.price
    )

    check_duplicate_cart_with_item(user, course_mode_requested)


@pytest.mark.django_db
def test_duplicate_exists_when_custom_status_provided():
    """Check duplicate only for the explicitly provided status."""
    user = User.objects.get(id=3)
    course_mode = CourseMode.objects.get(sku='custom-sku-1')
    item = CatalogueItem.objects.get(sku='custom-sku-1')

    cart = Cart.objects.create(user=user, status=Cart.Status.PENDING)
    cart.items.create(
        catalogue_item=item,
        original_price=item.price,
        final_price=item.price
    )

    # Should raise when checking PENDING explicitly
    with pytest.raises(DuplicateCartError):
        check_duplicate_cart_with_item(user, course_mode, status=Cart.Status.PENDING)


@pytest.mark.django_db
def test_multiple_carts_but_one_matching():
    """Only one cart matches criteria → should raise."""
    user = User.objects.get(id=3)
    course_mode = CourseMode.objects.get(sku='custom-sku-1')
    item = CatalogueItem.objects.get(sku='custom-sku-1')

    # Non-matching cart
    cart1 = Cart.objects.create(user=user, status=Cart.Status.PENDING)
    cart1.items.create(catalogue_item=item, original_price=item.price, final_price=item.price)

    # Matching cart
    cart2 = Cart.objects.create(user=user, status=Cart.Status.PROCESSING)
    cart2.items.create(catalogue_item=item, original_price=item.price, final_price=item.price)

    with pytest.raises(DuplicateCartError) as exc:
        check_duplicate_cart_with_item(user, course_mode)

    assert str(exc.value) == (
        f'Duplicate cart found ID: {cart2.id}, state: {Cart.Status.PROCESSING}.'
    )


@pytest.mark.django_db
def test_get_course_id_program_bundle(base_data: Any) -> None:  # pylint: disable=unused-argument
    """
    Test get_course_id returns item_ref_id directly for program_bundle type.

    :param base_data: Fixture data for test setup.
    :return: None
    """
    bundle_item = CatalogueItem.objects.get(sku='BUNDLE-PRO-CERT')
    cart = Cart.objects.create(user=User.objects.get(id=3), status=Cart.Status.PENDING)
    cart_item = CartItem.objects.create(
        catalogue_item=bundle_item,
        original_price=bundle_item.price,
        final_price=bundle_item.price,
        cart=cart,
    )
    result = get_course_id(cart_item)
    assert result == 'program-uuid-pro-cert'


@pytest.mark.django_db
def test_get_order_description_with_program_bundle(base_data: Any) -> None:  # pylint: disable=unused-argument
    """
    Test get_order_description works with a program_bundle item in the cart.

    :param base_data: Fixture data for test setup.
    :return: None
    """
    bundle_item = CatalogueItem.objects.get(sku='BUNDLE-PRO-CERT')
    cart = Cart.objects.create(user_id=3, status=Cart.Status.PENDING)
    cart.items.create(
        catalogue_item=bundle_item,
        original_price=bundle_item.price,
        final_price=bundle_item.price,
    )
    result = get_order_description(cart)
    # program_bundle's get_course_id returns item_ref_id directly
    assert 'program-uuid-pro-cert' in result
    assert len(result) <= MAX_ORDER_DESCRIPTION_LENGTH_DEFAULT


@pytest.mark.django_db
def test_get_order_description_mixed_items(base_data: Any) -> None:  # pylint: disable=unused-argument
    """
    Test get_order_description with both a paid_course and a program_bundle in the same cart.

    :param base_data: Fixture data for test setup.
    :return: None
    """
    course_item = CatalogueItem.objects.get(sku='custom-sku-1')
    bundle_item = CatalogueItem.objects.get(sku='BUNDLE-PRO-CERT')

    cart = Cart.objects.create(user_id=3, status=Cart.Status.PENDING)
    cart.items.create(
        catalogue_item=course_item,
        original_price=course_item.price,
        final_price=course_item.price,
    )
    cart.items.create(
        catalogue_item=bundle_item,
        original_price=bundle_item.price,
        final_price=bundle_item.price,
    )

    result = get_order_description(cart)
    # Both items should appear separated by ' // '
    assert 'course-v1:org1_1_1' in result  # + replaced with _ by sanitize
    assert 'program-uuid-pro-cert' in result
    assert ' // ' in result
    assert len(result) <= MAX_ORDER_DESCRIPTION_LENGTH_DEFAULT


@pytest.mark.django_db
def test_get_first_course_for_cart_paid_course(base_data: Any) -> None:  # pylint: disable=unused-argument
    """
    A cart with a single paid_course catalogue item resolves to that course.

    :param base_data: Fixture data for test setup.
    :return: None
    """
    course_item = CatalogueItem.objects.get(sku='custom-sku-1')
    cart = Cart.objects.create(user_id=3, status=Cart.Status.PAID)
    cart.items.create(
        catalogue_item=course_item,
        original_price=course_item.price,
        final_price=course_item.price,
    )

    result = get_first_course_for_cart(cart)

    assert result is not None
    assert result['course_id'] == course_item.item_ref_id
    assert result['is_program'] is False


@pytest.mark.django_db
def test_get_first_course_for_cart_program_bundle_returns_first_course(  # pylint: disable=unused-argument
    base_data: Any,
) -> None:
    """
    A cart with a single program_bundle resolves to the first course linked
    to that bundle, in link order.

    :param base_data: Fixture data for test setup.
    :return: None
    """
    bundle_item = CatalogueItem.objects.get(sku='BUNDLE-PRO-CERT')
    cart = Cart.objects.create(user_id=3, status=Cart.Status.PAID)
    cart.items.create(
        catalogue_item=bundle_item,
        original_price=bundle_item.price,
        final_price=bundle_item.price,
    )

    result = get_first_course_for_cart(cart)

    assert result is not None
    assert result['is_program'] is True
    # _create_program_bundles links custom-sku-1 first, then course1-org2-no-id-professional
    assert result['course_id'] == 'course-v1:org1+1+1'


@pytest.mark.django_db
def test_get_first_course_for_cart_empty_bundle_returns_none(base_data: Any) -> None:  # pylint: disable=unused-argument
    """
    A program bundle with no linked courses yields no first course so the
    template falls back to the generic CTA.

    :param base_data: Fixture data for test setup.
    :return: None
    """
    bundle_item = CatalogueItem.objects.get(sku='BUNDLE-EMPTY')
    cart = Cart.objects.create(user_id=3, status=Cart.Status.PAID)
    cart.items.create(
        catalogue_item=bundle_item,
        original_price=bundle_item.price,
        final_price=bundle_item.price,
    )

    assert get_first_course_for_cart(cart) is None


@pytest.mark.django_db
def test_get_first_course_for_cart_empty_cart_returns_none(base_data: Any) -> None:  # pylint: disable=unused-argument
    """
    A cart with no items has no first course.

    :param base_data: Fixture data for test setup.
    :return: None
    """
    cart = Cart.objects.create(user_id=3, status=Cart.Status.PAID)
    assert get_first_course_for_cart(cart) is None


@pytest.mark.django_db
def test_get_first_course_for_cart_multi_item_returns_none(base_data: Any) -> None:  # pylint: disable=unused-argument
    """
    A cart with more than one item is intentionally not auto-routed to a
    course; we only render the primary CTA when the purchase maps to a
    single course/program.

    :param base_data: Fixture data for test setup.
    :return: None
    """
    course_item = CatalogueItem.objects.get(sku='custom-sku-1')
    bundle_item = CatalogueItem.objects.get(sku='BUNDLE-PRO-CERT')
    cart = Cart.objects.create(user_id=3, status=Cart.Status.PAID)
    cart.items.create(
        catalogue_item=course_item,
        original_price=course_item.price,
        final_price=course_item.price,
    )
    cart.items.create(
        catalogue_item=bundle_item,
        original_price=bundle_item.price,
        final_price=bundle_item.price,
    )
    assert get_first_course_for_cart(cart) is None


def test_get_first_course_url_uses_legacy_course_route() -> None:
    """
    When ``LEARNING_MICROFRONTEND_URL`` is not configured, fall back to
    the legacy LMS course URL so the CTA still works in that environment.

    :return: None
    """
    assert get_first_course_url('course-v1:OrgX+Y+Run') == '/courses/course-v1:OrgX+Y+Run/course/'


@pytest.mark.django_db
def test_get_first_course_url_uses_mfe_when_configured(settings) -> None:
    """
    When ``LEARNING_MICROFRONTEND_URL`` is configured, the helper returns
    the MFE course-home URL so learners land on the modern course
    experience instead of the legacy LMS view.

    :return: None
    """
    settings.LEARNING_MICROFRONTEND_URL = 'http://apps.www.myopenedx.com:2000/learning'
    assert get_first_course_url('course-v1:OrgX+Y+Run') == (
        'http://apps.www.myopenedx.com:2000/learning/course/course-v1:OrgX+Y+Run/home'
    )


@pytest.mark.django_db
def test_get_first_course_url_strips_trailing_slash_from_mfe(settings) -> None:
    """
    Trailing slashes in ``LEARNING_MICROFRONTEND_URL`` don't produce
    double-slashes in the final URL.

    :return: None
    """
    settings.LEARNING_MICROFRONTEND_URL = 'http://apps.www.myopenedx.com:2000/learning/'
    assert get_first_course_url('course-v1:OrgX+Y+Run') == (
        'http://apps.www.myopenedx.com:2000/learning/course/course-v1:OrgX+Y+Run/home'
    )


@pytest.mark.django_db
def test_get_first_course_for_cart_unsupported_type_returns_none(  # pylint: disable=unused-argument
    base_data: Any,
) -> None:
    """
    A cart whose single item is not a paid_course or program_bundle has no
    first course to navigate to.

    :param base_data: Fixture data for test setup.
    :return: None
    """
    # Build a catalogue item of an unsupported type directly in the DB.
    other_item = CatalogueItem.objects.create(
        sku='UNSUPPORTED-ITEM',
        type='mystery_type',
        title='Mystery',
        item_ref_id='x',
        price=10,
        currency='SAR',
    )
    cart = Cart.objects.create(user_id=3, status=Cart.Status.PAID)
    cart.items.create(
        catalogue_item=other_item,
        original_price=other_item.price,
        final_price=other_item.price,
    )

    assert get_first_course_for_cart(cart) is None


@pytest.mark.django_db
def test_get_invoice_item_navigation_paid_course() -> None:
    """An invoice line for a paid_course yields a paid_course navigation target."""
    user_id = 3
    paid_course = CatalogueItem.objects.create(
        sku='INV-NAV-1',
        type=CatalogueItem.ItemType.PAID_COURSE,
        title='Nav Test Course',
        item_ref_id='course-v1:navorg+nav1+nav1',
        price=50,
        currency='SAR',
    )
    cart = Cart.objects.create(user_id=user_id, status=Cart.Status.PAID)
    cart_item = cart.items.create(
        catalogue_item=paid_course,
        original_price=paid_course.price,
        final_price=paid_course.price,
    )
    invoice = Invoice.objects.create(
        cart=cart, invoice_number='NAV-INV-1', total=50, gross_total=50,
    )
    invoice_item = invoice.items.create(
        cart_item=cart_item,
        original_price=50,
        price=50,
    )

    result = get_invoice_item_navigation(invoice_item)

    assert result is not None
    assert result['url'] == '/courses/course-v1:navorg+nav1+nav1/course/'
    assert result['is_program'] is False


@pytest.mark.django_db
def test_get_invoice_item_navigation_program_bundle_returns_first_course() -> None:
    """An invoice line for a program_bundle yields a bundle navigation target
    pointing at the first linked course.
    """
    user_id = 3
    paid_course = CatalogueItem.objects.create(
        sku='INV-NAV-2A',
        type=CatalogueItem.ItemType.PAID_COURSE,
        title='Bundle course A',
        item_ref_id='course-v1:bundlenav+A+A',
        price=50,
        currency='SAR',
    )
    bundle = CatalogueItem.objects.create(
        sku='INV-NAV-2-BUNDLE',
        type=CatalogueItem.ItemType.PROGRAM_BUNDLE,
        title='Nav Test Bundle',
        item_ref_id='nav-bundle-uuid',
        price=100,
        currency='SAR',
    )
    BundleCourseItem.objects.create(bundle=bundle, course_item=paid_course)
    cart = Cart.objects.create(user_id=user_id, status=Cart.Status.PAID)
    cart_item = cart.items.create(
        catalogue_item=bundle,
        original_price=bundle.price,
        final_price=bundle.price,
    )
    invoice = Invoice.objects.create(
        cart=cart, invoice_number='NAV-INV-2', total=100, gross_total=100,
    )
    invoice_item = invoice.items.create(
        cart_item=cart_item,
        original_price=100,
        price=100,
    )

    result = get_invoice_item_navigation(invoice_item)

    assert result is not None
    assert result['url'] == '/courses/course-v1:bundlenav+A+A/course/'
    assert result['is_program'] is True


@pytest.mark.django_db
def test_get_invoice_item_navigation_empty_bundle_returns_none() -> None:
    """A program_bundle with no linked courses yields no navigation target."""
    user_id = 3
    empty_bundle = CatalogueItem.objects.create(
        sku='INV-NAV-3-EMPTY-BUNDLE',
        type=CatalogueItem.ItemType.PROGRAM_BUNDLE,
        title='Empty Nav Bundle',
        item_ref_id='nav-empty-uuid',
        price=50,
        currency='SAR',
    )
    cart = Cart.objects.create(user_id=user_id, status=Cart.Status.PAID)
    cart_item = cart.items.create(
        catalogue_item=empty_bundle,
        original_price=empty_bundle.price,
        final_price=empty_bundle.price,
    )
    invoice = Invoice.objects.create(
        cart=cart, invoice_number='NAV-INV-3', total=50, gross_total=50,
    )
    invoice_item = invoice.items.create(
        cart_item=cart_item,
        original_price=50,
        price=50,
    )

    assert get_invoice_item_navigation(invoice_item) is None


@pytest.mark.django_db
def test_get_invoice_item_navigation_unsupported_type_returns_none() -> None:
    """A line whose catalogue item is an unsupported type yields no navigation target."""
    user_id = 3
    other_item = CatalogueItem.objects.create(
        sku='INV-NAV-4-MYSTERY',
        type='mystery',
        title='Mystery',
        item_ref_id='x',
        price=10,
        currency='SAR',
    )
    cart = Cart.objects.create(user_id=user_id, status=Cart.Status.PAID)
    cart_item = cart.items.create(
        catalogue_item=other_item,
        original_price=other_item.price,
        final_price=other_item.price,
    )
    invoice = Invoice.objects.create(
        cart=cart, invoice_number='NAV-INV-4', total=10, gross_total=10,
    )
    invoice_item = invoice.items.create(
        cart_item=cart_item,
        original_price=10,
        price=10,
    )

    assert get_invoice_item_navigation(invoice_item) is None
