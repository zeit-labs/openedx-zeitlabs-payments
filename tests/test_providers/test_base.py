"""Test base processsor"""

import logging
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from common.djangoapps.course_modes.models import CourseMode
from common.djangoapps.student.models import CourseEnrollment, CourseEnrollmentException, EnrollmentClosedError
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.db import IntegrityError
from django.http import HttpRequest
from django.test import override_settings
from django.urls import reverse

from test_utils.dummy_processor import DummyProcessor
from zeitlabs_payments.exceptions import (
    CartFulfillmentError,
    DuplicateTransactionError,
    GatewayError,
    InvalidCartError,
    InvoiceError,
)
from zeitlabs_payments.helpers import get_currency
from zeitlabs_payments.models import AuditLog, BundleCourseItem, Cart, CatalogueItem, Invoice, InvoiceItem, Transaction
from zeitlabs_payments.providers.base import BaseProcessor

User = get_user_model()


@pytest.fixture
def base_processor():
    """processor fixture"""
    return BaseProcessor()


@pytest.fixture
def cart(base_data):  # pylint: disable=unused-argument
    """cart fixture: handed off on the current site"""
    item = CatalogueItem.objects.get(sku='custom-sku-1')
    user_cart = Cart.objects.create(
        user=User.objects.get(id=3),
        status=Cart.Status.PROCESSING,
        payment_origin=f'https://{Site.objects.get_current().domain}',
    )
    user_cart.items.create(
        catalogue_item=item,
        original_price=item.price,
        final_price=item.price
    )
    return user_cart


@pytest.fixture
def transaction_record(cart):  # pylint: disable=redefined-outer-name
    """settled payment fixture"""
    return Transaction.objects.create(
        cart=cart, amount=cart.total, gateway='dummy', gateway_transaction_id='TX-1'
    )


@pytest.mark.django_db
@patch('zeitlabs_payments.providers.base.render')
@patch.object(BaseProcessor, 'get_transaction_parameters')
def test_payment_view_renders_template_with_correct_context(
    mock_get_transaction_parameters, mock_render, cart, base_processor   # pylint: disable=redefined-outer-name
):
    """Test payment_view calls get_transaction_parameters and renders with correct context."""
    mock_get_transaction_parameters.return_value = {'test': 'test_123'}
    fake_request = MagicMock(spec=HttpRequest)
    base_processor.TEMPLATE_NAME = 'dummy-template.html'
    base_processor.payment_view(cart=cart, request=fake_request)
    mock_get_transaction_parameters.assert_called_once_with(
        cart=cart,
        request=fake_request,
        use_client_side_checkout=False,
    )
    mock_render.assert_called_once_with(
        fake_request,
        'dummy-template.html',
        {'transaction_parameters': mock_get_transaction_parameters.return_value},
    )


@pytest.mark.django_db
@patch('zeitlabs_payments.providers.base.render')
@patch.object(BaseProcessor, 'get_transaction_parameters', return_value={'order': 'GW-1'})
def test_payment_view_records_the_order_the_processor_names(
    _, mock_render, cart, base_processor   # pylint: disable=redefined-outer-name
):
    """
    Verify that the order a processor names at hand-off is recorded on the cart, and that none is by default.

    The id is how the payment can be found again without the learner's browser, so it is kept as soon as the gateway
    has opened the order.
    """
    base_processor.TEMPLATE_NAME = 'dummy-template.html'
    base_processor.SLUG = 'base'
    Cart.objects.filter(pk=cart.pk).update(gateway='base')
    cart.refresh_from_db()

    base_processor.payment_view(cart=cart, request=MagicMock(spec=HttpRequest))
    cart.refresh_from_db()
    assert cart.gateway_order_id is None, 'An order was recorded although the processor names none'

    with patch.object(BaseProcessor, 'get_gateway_order_id', side_effect=lambda parameters: parameters['order']):
        base_processor.payment_view(cart=cart, request=MagicMock(spec=HttpRequest))

    cart.refresh_from_db()
    assert cart.gateway_order_id == 'GW-1', 'The named order was not recorded'
    assert mock_render.call_count == 2, 'The hand-off page was not rendered each time'


def test_is_hidden_for_default_returns_false(base_processor):  # pylint: disable=redefined-outer-name
    """Verify that the base hide check returns False so processors are visible by default."""
    request = MagicMock(spec=HttpRequest)
    assert base_processor.is_hidden_for(request) is False


@pytest.mark.django_db
@patch('zeitlabs_payments.providers.base.render')
@patch.object(BaseProcessor, 'get_transaction_parameters')
def test_payment_view_leaves_a_failed_hand_off_to_its_caller(
    mock_get_transaction_parameters, mock_render, cart, base_processor   # pylint: disable=redefined-outer-name
):
    """
    Verify that what get_transaction_parameters raises reaches the caller, with nothing rendered.

    The caller closes the cart. A page rendered here would let it record a redirect that never happened.
    """
    mock_get_transaction_parameters.side_effect = Exception('unexpected error.')

    with pytest.raises(Exception, match='unexpected error.'):
        base_processor.payment_view(cart=cart, request=MagicMock(spec=HttpRequest))

    assert not mock_render.called, 'A page was rendered for a hand-off that failed'


def test_get_transaction_parameters_raises_not_implemented(base_processor):  # pylint: disable=redefined-outer-name
    request = HttpRequest()
    with pytest.raises(NotImplementedError):
        base_processor.get_transaction_parameters(cart={}, request=request)


@pytest.mark.django_db
def test_get_cart_valid_and_invalid(base_processor, cart):  # pylint: disable=redefined-outer-name
    assert base_processor.get_cart(cart.id).id == cart.id

    with pytest.raises(InvalidCartError):
        base_processor.get_cart('invalid-id')

    with pytest.raises(InvalidCartError):
        base_processor.get_cart(999999)


@pytest.mark.django_db
def test_get_site_valid_and_invalid(base_processor):  # pylint: disable=redefined-outer-name
    site = Site.objects.get_current()
    assert base_processor.get_site(site.id).id == site.id

    with pytest.raises(GatewayError):
        base_processor.get_site('invalid-id')

    with pytest.raises(GatewayError):
        base_processor.get_site(999999)


@pytest.mark.django_db
def test_create_invoice_raises_for_non_paid_cart(  # pylint: disable=redefined-outer-name
    base_processor, cart, transaction_record
):
    with pytest.raises(InvoiceError):
        base_processor.create_invoice(cart, transaction_record, HttpRequest())


@pytest.mark.django_db
def test_create_invoice_success(base_processor, cart, transaction_record):  # pylint: disable=redefined-outer-name
    cart.status = Cart.Status.PAID
    request = MagicMock(scheme='https', site=Site.objects.get_current())
    invoice = base_processor.create_invoice(cart, transaction_record, request)

    assert invoice.cart == cart
    assert invoice.total == cart.total
    assert invoice.status == Invoice.InvoiceStatus.PAID
    assert invoice.invoice_number is not None
    assert invoice.related_transaction == transaction_record, 'The invoice records the payment it bills for'

    assert InvoiceItem.objects.filter(invoice=invoice).count() == cart.items.count()


def learner_info_from_a_broken_extension(prev_fn, user, site):
    """Replace get_learner_info the way a broken extension plugin would."""
    raise RuntimeError('the identity provider is down')


@pytest.mark.django_db
def test_create_invoice_stores_the_learner_and_organization_info(  # pylint: disable=redefined-outer-name
    base_processor, cart, transaction_record
):
    """
    Verify that create_invoice stores what get_learner_info and get_organization_info answer for the paying site, so
    later changes to the account or the settings never rewrite the invoice.
    """
    cart.status = Cart.Status.PAID
    cart.user.first_name, cart.user.last_name = 'Omar', 'Saleh'

    invoice = base_processor.create_invoice(
        cart, transaction_record, MagicMock(scheme='https', site=Site.objects.get_current()),
    )

    invoice.refresh_from_db()
    assert invoice.learner_info == {'name': 'Omar Saleh'}, 'The learner info was not stored'
    assert invoice.organization_info == {'name': 'test_org', 'vat_no': '112233'}, 'The organization info was not stored'


@pytest.mark.django_db
@override_settings(
    OVERRIDE_ZEITLABS_PAYMENTS_LEARNER_INFO='tests.test_providers.test_base.learner_info_from_a_broken_extension',
)
def test_create_invoice_refuses_the_invoice_when_an_info_function_fails(  # pylint: disable=redefined-outer-name
    base_processor, cart, transaction_record
):
    """
    Verify that an info function failing refuses the invoice rather than issuing it without that info, which it could
    never be given later.
    """
    cart.status = Cart.Status.PAID

    with pytest.raises(RuntimeError, match='the identity provider is down'):
        base_processor.create_invoice(
            cart, transaction_record, MagicMock(scheme='https', site=Site.objects.get_current()),
        )

    assert not Invoice.objects.filter(cart=cart).exists(), 'An invoice was issued without its learner info'


@pytest.mark.django_db
def test_create_invoice_addresses_the_receipt_on_the_cart_origin(  # pylint: disable=redefined-outer-name
    base_processor, cart, transaction_record
):
    """
    Verify that the receipt of an invoice is addressed on the origin its cart was paid on, not on the scheme of the
    request settling it.

    The receipt's QR code is built on that origin, so a printed copy and the live page always name the same address.
    """
    cart.status = Cart.Status.PAID

    invoice = base_processor.create_invoice(
        cart, transaction_record, MagicMock(scheme='http', site=Site.objects.get_current()),
    )

    assert invoice.receipt_url.startswith('https://example.com/'), (
        f'The receipt must be addressed on the origin the cart was paid on, not {invoice.receipt_url!r}'
    )


@pytest.mark.django_db
def test_create_invoice_refuses_a_cart_without_a_payment_origin(  # pylint: disable=redefined-outer-name
    base_processor, cart, transaction_record
):
    """
    Verify that create_invoice refuses a cart with no payment origin rather than issue an invoice whose receipt has no
    address and is shown on any site.
    """
    cart.status = Cart.Status.PAID
    cart.payment_origin = None

    with pytest.raises(InvoiceError, match='has no payment origin'):
        base_processor.create_invoice(
            cart, transaction_record, MagicMock(scheme='https', site=Site.objects.get_current()),
        )

    assert not Invoice.objects.filter(cart=cart).exists(), 'A refused invoice must leave no row behind.'


@pytest.mark.django_db
def test_create_invoice_refuses_a_request_serving_another_site(  # pylint: disable=redefined-outer-name
    base_processor, cart, transaction_record
):
    """
    Verify that create_invoice refuses a request serving another site than the one the cart was paid on.

    The info stored on the invoice is read for the request's site, so it would name another site's organization than
    the one the receipt is addressed on.
    """
    cart.status = Cart.Status.PAID
    other_site = Site.objects.create(name='other', domain='other.example.com')

    with pytest.raises(InvoiceError, match='was paid on https://example.com, but the request settling it serves other'):
        base_processor.create_invoice(cart, transaction_record, MagicMock(scheme='https', site=other_site))

    assert not Invoice.objects.filter(cart=cart).exists(), 'A refused invoice must leave no row behind.'


@pytest.mark.django_db
def test_create_invoice_refuses_a_request_carrying_no_site(  # pylint: disable=redefined-outer-name
    base_processor, cart, transaction_record
):
    """
    Verify that create_invoice raises when the request carries no site rather than guessing one.

    There is no site to read the invoice's info for, and a guessed one would print another organization onto paper
    that cannot be reissued. The caller logs the refusal to the audit trail; the settled transaction stays on record.
    """
    cart.status = Cart.Status.PAID

    with pytest.raises(InvoiceError, match='carries no site'):
        base_processor.create_invoice(cart, transaction_record, HttpRequest())

    assert not Invoice.objects.filter(cart=cart).exists(), 'A refused invoice must leave no row behind.'


@pytest.mark.django_db
def test_get_transaction_parameters_base(cart):  # pylint: disable=redefined-outer-name
    request = MagicMock(spec=HttpRequest)
    request.site = Site.objects.get(domain='example.com')
    processor = DummyProcessor()
    result = processor.get_transaction_parameters_base(cart, request)
    assert result['user_email'] == 'user3@example.com'
    assert result['language'] == 'en'
    assert result['amount'] == cart.total, 'amount must be the full-precision Decimal, not a rounded unit'
    assert isinstance(result['amount'], Decimal)
    assert result['order_reference'] == f'{cart.id}-{request.site.id}'
    assert result['currency'] == cart.items.all()[0].catalogue_item.currency
    assert result['order_description'] == 'course-v1:org1_1_1'


@patch('zeitlabs_payments.providers.base.get_plugin_settings')
def test_get_settings_resolves_declared_root_key(  # pylint: disable=redefined-outer-name
    mock_get_plugin_settings, base_processor,
):
    """Verify that get_settings resolves the declared root key through the site-aware settings layer."""
    base_processor.SETTINGS_ROOT_KEY = 'DUMMY_SETTINGS'
    mock_get_plugin_settings.return_value.get_by_root_key.return_value = {'CLIENT_ID': 'site-aware-value'}
    result = base_processor.get_settings()
    assert result == {'CLIENT_ID': 'site-aware-value'}
    mock_get_plugin_settings.return_value.get_by_root_key.assert_called_once_with('DUMMY_SETTINGS')


def test_get_settings_returns_empty_dict_without_root_key(base_processor):  # pylint: disable=redefined-outer-name
    """Verify that get_settings returns an empty dict when the processor declares no root key."""
    assert base_processor.get_settings() == {}


@pytest.mark.django_db
def test_get_transaction_parameters_base_preserves_fractional_total(cart):  # pylint: disable=redefined-outer-name
    """Verify that a fractional cart total reaches the parameters at full precision, unrounded."""
    request = MagicMock(spec=HttpRequest)
    request.site = Site.objects.get(domain='example.com')
    processor = DummyProcessor()
    cart.items.all().update(final_price=Decimal('113.850'))
    result = processor.get_transaction_parameters_base(cart, request)
    assert result['amount'] == cart.total == Decimal('113.850')
    assert result['amount'] != int(round(cart.total, 0))


@pytest.mark.django_db
def test_handle_payment_for_duplicate_transaction(cart):  # pylint: disable=redefined-outer-name
    processor = DummyProcessor()
    Transaction.objects.create(
        gateway_transaction_id='already-there',
        gateway='dummy',
        amount=500,
    )
    with pytest.raises(DuplicateTransactionError):
        processor.handle_payment(cart, cart.user, 'anything', 'already-there', 'dummy', '500', 'usd', 'any reason')


@pytest.mark.django_db
def test_handle_payment_creates_transaction(cart):  # pylint: disable=redefined-outer-name
    processor = DummyProcessor()
    transaction_id = '1111'
    gateway_response = {'mode': 'test'}

    assert not Transaction.objects.filter(gateway='dummy', cart=cart).exists(), \
        'Transaction should not exist before test'
    assert cart.status == cart.Status.PROCESSING, \
        'Cart should be in PROCESSING state'

    processor.handle_payment(
        cart, cart.user, 'success', transaction_id, processor.SLUG, str(cart.total),
        get_currency(cart), 'transaction success', gateway_response,
    )

    assert Transaction.objects.filter(
        gateway=processor.SLUG, cart=cart, gateway_transaction_id=transaction_id
    ).exists(), \
        'Transaction should exist after payment'

    cart.refresh_from_db()
    assert cart.status == cart.Status.PAID, \
        'Cart status should be PAID after successful payment'


@pytest.mark.django_db
@patch('zeitlabs_payments.providers.base.CART_HANDLER', new={})
def test_fulfill_cart_for_missing_cart_handler(cart):  # pylint: disable=redefined-outer-name
    processor = DummyProcessor()
    assert not AuditLog.objects.filter(action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR, cart=cart).exists(), \
        'Audit log is not there with cart fullfillment error'

    with pytest.raises(CartFulfillmentError):
        processor.fulfill_cart(cart)

    assert AuditLog.objects.filter(
        action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR, cart=cart
    ).exists(), \
        'Audit log should exist with cart fullfillment error as cart handler is missing for catalog item'


@pytest.mark.django_db
def test_process_payment_success(cart):  # pylint: disable=redefined-outer-name
    """Test full successful payment, invoice, and fulfillment flow."""
    processor = DummyProcessor()
    request = MagicMock(scheme='https')
    request.user = cart.user
    transaction_id = '12345'

    assert not Transaction.objects.filter(gateway='dummy', cart=cart).exists(), \
        'Transaction should not exist before test'
    assert cart.status == cart.Status.PROCESSING, \
        'Cart should be in PROCESSING state'

    invoice = processor.process_payment_and_update_records(
        cart=cart,
        data={'response': 'ok'},
        request=request,
        transaction_id=transaction_id,
        transaction_status='SUCCESS',
        method='card',
        amount=100.00,
        currency='SAR',
        reason='Payment completed',
        site_id=1
    )

    cart.refresh_from_db()
    assert cart.status == cart.Status.PAID, \
        'Cart status should be PAID after successful payment'

    assert invoice.cart.id == cart.id
    assert invoice.gross_total == cart.gross_total
    assert invoice.total == cart.total
    assert invoice.related_transaction.gateway_transaction_id == '12345'


@pytest.mark.django_db
@patch('zeitlabs_payments.cart_handler.CourseEnrollment.enroll')
def test_a_learner_already_holding_the_mode_is_still_invoiced(
    mock_enroll, cart, transaction_record,  # pylint: disable=redefined-outer-name
):
    """
    Verify that a paid cart whose learner already holds the course's mode is invoiced once, enrols no one, and records
    both that the learner held the mode and that the cart was fulfilled.
    """
    course_mode = CourseMode.objects.get(sku=cart.items.get().catalogue_item.sku)
    CourseEnrollment.objects.create(
        user=cart.user, course=course_mode.course, mode=course_mode.mode_slug, is_active=True,
    )
    cart.status = Cart.Status.PAID
    cart.save(allow_status_change=True)
    request = MagicMock(scheme='https', site=Site.objects.get_current())

    invoice = DummyProcessor().issue_invoice_and_fulfill(cart, transaction_record, request)

    assert invoice is not None, 'The cart was not invoiced'
    assert Invoice.objects.filter(cart=cart).count() == 1, 'The cart was not invoiced exactly once'
    assert not mock_enroll.called, 'A learner already holding the mode was enrolled again'
    assert list(AuditLog.objects.filter(cart=cart).order_by('id').values_list('action', flat=True)) == [
        AuditLog.AuditActions.USER_ALREADY_ENROLLED,
        AuditLog.AuditActions.CART_FULFILLED,
    ], 'The trail does not record both the mode held and the cart fulfilled'


@pytest.mark.django_db
def test_process_payment_records_the_origin_of_a_cart_handed_off_before_origins_were_recorded(
    cart,  # pylint: disable=redefined-outer-name
):
    """
    Verify that a cart handed off with no origin is given the origin of the site settling it, scheme included, and its
    invoice is issued on it.
    """
    Cart.objects.filter(pk=cart.pk).update(payment_origin=None)
    cart.refresh_from_db()
    request = MagicMock(scheme='http')
    request.user = cart.user

    invoice = DummyProcessor().process_payment_and_update_records(
        cart=cart, data={}, request=request, transaction_id='TX-LEGACY', transaction_status='SUCCESS', method='card',
        amount='50', currency='SAR', reason='ok', site_id=Site.objects.get_current().id,
    )

    assert Cart.objects.get(pk=cart.pk).payment_origin == 'http://example.com', 'The origin was not recorded'
    assert invoice.receipt_url.startswith('http://example.com/'), 'The invoice is not issued on the recorded origin'


@pytest.mark.django_db
@patch('zeitlabs_payments.providers.base.CART_HANDLER', new={})
def test_process_payment_keeps_the_origin_it_records_when_the_invoice_fails(
    cart,  # pylint: disable=redefined-outer-name
):
    """
    Verify that the origin recorded for a cart handed off with none is kept when its invoice then fails, so the cart
    can still be invoiced once the cause is fixed.
    """
    Cart.objects.filter(pk=cart.pk).update(payment_origin=None)
    cart.refresh_from_db()
    request = MagicMock(scheme='https')
    request.user = cart.user

    with patch('zeitlabs_payments.providers.base.logger.exception'):
        invoice = DummyProcessor().process_payment_and_update_records(
            cart=cart, data={}, request=request, transaction_id='TX-LEGACY', transaction_status='SUCCESS',
            method='card', amount='50', currency='SAR', reason='ok', site_id=Site.objects.get_current().id,
        )

    assert invoice is None, 'The fulfilment was expected to fail'
    assert Cart.objects.get(pk=cart.pk).payment_origin == 'https://example.com', 'The recorded origin was rolled back'


@pytest.mark.django_db
def test_process_payment_refuses_the_invoice_when_the_site_gives_no_usable_origin(
    cart,  # pylint: disable=redefined-outer-name
):
    """
    Verify that a cart handed off with no origin, settling on a site whose domain makes no usable origin, is left
    without one and refused its invoice, with the failure on the audit trail rather than raised to the gateway's poll.
    """
    Cart.objects.filter(pk=cart.pk).update(payment_origin=None)
    cart.refresh_from_db()
    request = MagicMock(scheme='https')
    request.user = cart.user

    with patch('zeitlabs_payments.providers.base.logger.exception') as logged:
        invoice = DummyProcessor().process_payment_and_update_records(
            cart=cart, data={}, request=request, transaction_id='TX-LEGACY', transaction_status='SUCCESS',
            method='card', amount='50', currency='SAR', reason='ok',
            site_id=Site.objects.create(name='lms', domain='learn.example.com/lms').id,
        )

    assert invoice is None, 'An invoice was issued with no origin to address it on'
    assert Cart.objects.get(pk=cart.pk).payment_origin is None, 'An unusable origin was recorded'
    assert logged.call_args_list[0].args == (f'Cart {cart.id} could not record the origin it was paid on.',), \
        'The origin that could not be recorded is not logged'
    assert AuditLog.objects.filter(cart=cart, action=AuditLog.AuditActions.INVOICE_CREATION_FAILED).exists(), \
        'The refused invoice is not on the audit trail'


@pytest.mark.django_db
def test_a_failed_fulfillment_names_the_error_behind_it_on_the_audit_trail(
    cart,  # pylint: disable=redefined-outer-name
):
    """
    Verify that the audit entry of a failed fulfilment names the error and the one it was raised from, so staff see
    that Open edX refused the enrolment and why, not only that the fulfilment failed.
    """
    request = MagicMock(scheme='https', site=Site.objects.get_current())
    request.user = cart.user

    refused = EnrollmentClosedError('Enrollment is closed.')

    with patch('zeitlabs_payments.cart_handler.CourseEnrollment.enroll', side_effect=refused):
        with patch('zeitlabs_payments.providers.base.logger.exception'):
            invoice = DummyProcessor().process_payment_and_update_records(
                cart=cart, data={}, request=request, transaction_id='TX-CLOSED', transaction_status='SUCCESS',
                method='card', amount='50', currency='SAR', reason='ok',
            )

    assert invoice is None, 'The fulfilment was expected to fail'
    assert AuditLog.objects.get(cart=cart, action=AuditLog.AuditActions.INVOICE_CREATION_FAILED).details == (
        f'Invoice could not be created or cart could not be fulfilled for cart: {cart.id} after a settled '
        'transaction. Error: CartFulfillmentError: Unexpected enrollment error, caused by EnrollmentClosedError: '
        'Enrollment is closed.'
    ), 'The audit entry does not name the error behind the failure'


@pytest.mark.django_db
def test_process_payment_invalid_cart_status(cart):  # pylint: disable=redefined-outer-name
    """Test when cart is not in PROCESSING state."""
    processor = DummyProcessor()
    cart.status = Cart.Status.PENDING
    cart.save(allow_status_change=True)

    assert not AuditLog.objects.filter(
        gateway='dummy', cart=cart, action=AuditLog.AuditActions.RESPONSE_INVALID_CART
    ).exists(), \
        'AuditLog should not exist before test'

    result = processor.process_payment_and_update_records(
        cart=cart,
        data={},
        request=MagicMock(),
        transaction_id='t1',
        transaction_status='SUCCESS',
        method='card',
        amount='10',
        currency='USD',
        reason='ok'
    )

    assert result is None
    audit_log = AuditLog.objects.filter(
        gateway='dummy', cart=cart, action=AuditLog.AuditActions.RESPONSE_INVALID_CART
    )[0]
    assert audit_log.details == (
        'Invalid cart state found. Cart is in state: '
        'pending instead of processing, payment_pending or expired.'
    )


@pytest.mark.django_db
@pytest.mark.parametrize('site_id_param', ['invalid', '00011', 10000])
def test_process_payment_with_invalid_site_ids(cart, site_id_param):  # pylint: disable=redefined-outer-name
    """
    Test the process_payment_and_update_records method with different site_id inputs.
    """
    processor = DummyProcessor()
    result = processor.process_payment_and_update_records(
        cart=cart,
        data={},
        request=MagicMock(),
        transaction_id='t1',
        transaction_status='SUCCESS',
        method='card',
        amount='10',
        currency='USD',
        reason='ok',
        site_id=site_id_param
    )
    assert result is None


@pytest.mark.django_db
def test_process_payment_duplicate_transaction(cart):  # pylint: disable=redefined-outer-name
    """Test DuplicateTransactionError handling."""
    processor = DummyProcessor()
    transaction_id = '12345'

    Transaction.objects.create(
        gateway_transaction_id=transaction_id,
        gateway='dummy',
        amount=5000,
    )

    assert not AuditLog.objects.filter(
        gateway='dummy', cart=cart, action=AuditLog.AuditActions.DUPLICATE_TRANSACTION
    ).exists(), \
        'AuditLog should not exist before test'

    request = MagicMock()
    request.user = cart.user
    result = processor.process_payment_and_update_records(
        cart=cart,
        data={},
        request=request,
        transaction_id=transaction_id,
        transaction_status='SUCCESS',
        method='card',
        amount='10',
        currency='USD',
        reason='ok'
    )

    assert result is None

    audit_log = AuditLog.objects.filter(
        gateway='dummy', cart=cart, action=AuditLog.AuditActions.DUPLICATE_TRANSACTION
    )[0]
    assert audit_log.details == 'Transaction with id: 12345 already existed. Cart has status: processing.'


@pytest.mark.django_db
@patch('zeitlabs_payments.providers.base.BaseProcessor.handle_payment')
def test_process_payment_handle_payment_exception(mock_handle_payment, cart):  # pylint: disable=redefined-outer-name
    """Test generic exception in handle_payment branch."""
    processor = DummyProcessor()
    mock_handle_payment.side_effect = Exception('unexpected')
    assert not AuditLog.objects.filter(
        gateway='dummy', cart=cart, action=AuditLog.AuditActions.TRANSACTION_ROLLED_BACK
    ).exists(), \
        'AuditLog should not exist before test'

    result = processor.process_payment_and_update_records(
        cart=cart,
        data={},
        request=MagicMock(),
        transaction_id='t3',
        transaction_status='FAILED',
        method='card',
        amount='10',
        currency='USD',
        reason='error'
    )

    assert result is None
    assert AuditLog.objects.filter(
        gateway='dummy', cart=cart, action=AuditLog.AuditActions.TRANSACTION_ROLLED_BACK
    ).exists()
    assert not Transaction.objects.filter(
        gateway='dummy', gateway_transaction_id='t3',
    ).exists()


@pytest.mark.django_db
@patch('zeitlabs_payments.providers.base.CART_HANDLER', new={})
@patch('zeitlabs_payments.providers.base.logger.exception')
def test_process_payment_invoice_or_fulfillment_fails(mock_exc, cart):  # pylint: disable=redefined-outer-name
    """Verify that an invoice or fulfillment failure is logged to the audit trail."""
    processor = DummyProcessor()

    request = MagicMock(scheme='https', site=Site.objects.get_current())
    request.user = cart.user

    result = processor.process_payment_and_update_records(
        cart=cart,
        data={},
        request=request,
        transaction_id='t4',
        transaction_status='SUCCESS',
        method='card',
        amount='10',
        currency='SAR',
        reason='ok'
    )
    assert result is None
    mock_exc.assert_called_once_with(
        'Failed to fulfill cart 1 or to create invoice: Unsupported catalogue item type: paid_course'
    )
    assert AuditLog.objects.filter(
        gateway='dummy', cart=cart, action=AuditLog.AuditActions.INVOICE_CREATION_FAILED
    ).exists(), 'The failure must reach the audit trail so an admin can see the invoice was never created'


@pytest.mark.django_db
def test_get_site_and_cart_from_reference_success(base_processor, cart):   # pylint: disable=redefined-outer-name
    base_processor.SLUG = 'base'
    site = Site.objects.create(name='test.com', domain='test.com')
    reference = f'{site.id}-{cart.id}'

    actual_cart = base_processor.get_cart_from_reference(reference)
    assert isinstance(actual_cart, Cart)
    assert actual_cart.id == cart.id

    actual_site = base_processor.get_site_from_reference(reference)
    assert isinstance(actual_site, Site)
    assert actual_site.id == site.id


@pytest.mark.django_db
def test_get_cart_and_site_from_reference_invalid(base_processor):   # pylint: disable=redefined-outer-name
    base_processor.SLUG = 'base'
    invalid_reference = 'invalid-reference'
    assert not AuditLog.objects.filter(
        gateway=base_processor.SLUG, action=AuditLog.AuditActions.RESPONSE_INVALID_CART).exists()
    result = base_processor.get_cart_from_reference(invalid_reference)
    assert result is None
    assert AuditLog.objects.filter(
        gateway=base_processor.SLUG, action=AuditLog.AuditActions.RESPONSE_INVALID_CART
    ).exists()

    result = base_processor.get_site_from_reference(invalid_reference)
    assert result is None


class _RejectNonIQDProcessor(DummyProcessor):
    """Stand-in for a processor that settles only in IQD on top of the standard ISO translation."""

    def map_currency(self, value):
        mapped = super().map_currency(value)
        if mapped != 'IQD':
            raise ValueError(f'Unsupported currency from gateway: {mapped!r}')
        return mapped


@pytest.mark.django_db
def test_map_currency_translates_iso_numeric_codes_to_alpha():
    """
    Verify that the base implementation consults the standard ISO 4217 numeric register, so any processor that
    returns a numeric code is translated without per-processor knowledge.
    """
    processor = BaseProcessor()
    assert processor.map_currency('978') == 'EUR'
    assert processor.map_currency('400') == 'JOD'
    assert processor.map_currency('368') == 'IQD'
    assert processor.map_currency('784') == 'AED'
    assert processor.map_currency('840') == 'USD'
    assert processor.map_currency('392') == 'JPY'
    assert processor.map_currency('826') == 'GBP'


@pytest.mark.django_db
def test_map_currency_passes_alpha_codes_through_unchanged():
    """
    Verify that a gateway echoing an ISO alpha code (Hyperpay) is not translated; canonical_currency
    is the one that normalizes the case and validates against the list.
    """
    processor = BaseProcessor()
    assert processor.map_currency('SAR') == 'SAR'
    assert processor.map_currency('USD') == 'USD'
    assert processor.map_currency('sar') == 'sar'


@pytest.mark.django_db
def test_map_currency_passes_unknown_numeric_through_for_canonical_to_reject():
    """
    Verify that a three-digit string that is not on the ISO 4217 register passes through; canonical_currency is what
    raises, not map_currency -- map_currency is a translation, not a gatekeeper.
    """
    processor = BaseProcessor()
    assert processor.map_currency('999') == '999'
    assert processor.map_currency('001') == '001'


@pytest.mark.django_db
def test_handle_payment_canonicalizes_lowercase_currency_to_uppercase(cart):  # pylint: disable=redefined-outer-name
    """Verify that lowercase ISO alpha codes are stored uppercase; the column is the canonical form."""
    processor = DummyProcessor()
    record = processor.handle_payment(
        cart, cart.user, 'success', 'lower-1', processor.SLUG, str(cart.total),
        'usd', 'lowercase test', None,
    )
    assert record.currency == 'USD'
    assert Transaction.objects.get(pk=record.pk).currency == 'USD'


@pytest.mark.django_db
def test_handle_payment_records_gateway_currency_unchanged_when_iso(cart):  # pylint: disable=redefined-outer-name
    """Verify that Hyperpay-style gateways that echo an ISO alpha code get the canonical uppercase form."""
    processor = DummyProcessor()
    record = processor.handle_payment(
        cart, cart.user, 'success', 'iso-1', processor.SLUG, str(cart.total),
        'SAR', 'iso alpha echo', None,
    )
    assert record.currency == 'SAR'


@pytest.mark.django_db
def test_handle_payment_translates_numeric_via_map_currency(cart):  # pylint: disable=redefined-outer-name
    """Verify that a numeric ISO 4217 code from the gateway is translated to its alpha form before storage."""
    processor = DummyProcessor()
    record = processor.handle_payment(
        cart, cart.user, 'success', 'numeric-1', processor.SLUG, str(cart.total),
        '978', 'Euro numeric code', None,
    )
    assert record.currency == 'EUR'


@pytest.mark.django_db
def test_handle_payment_records_unsupported_currency_as_not_reconciled(  # pylint: disable=redefined-outer-name
    cart, caplog
):
    """
    Verify that a three-digit code that is not in the ISO 4217 register is persisted on the transaction row with
    the failure annotated, and a warning is logged for audit visibility.
    """
    processor = DummyProcessor()
    with caplog.at_level(logging.WARNING):
        record = processor.handle_payment(
            cart, cart.user, 'success', 'unsupported-1', processor.SLUG, str(cart.total),
            '999', 'unsupported currency', None,
        )
    assert 'recorded but not reconciled' in caplog.text, 'The reconciliation warning must be logged'
    assert Transaction.objects.filter(gateway_transaction_id='unsupported-1').exists(), \
        'The row must survive verification failure'
    assert record.status == processor.TRANSACTION_STATUS_FAILED
    assert record.currency == '999', 'The offending value is persisted, not sanitized'
    assert 'not reconciled' in record.reason
    assert AuditLog.objects.filter(action=AuditLog.AuditActions.TRANSACTION_NOT_RECONCILED, cart=cart).exists()

    cart.refresh_from_db()
    assert cart.status == Cart.Status.PROCESSING, 'The cart must not be marked paid'


@pytest.mark.django_db
def test_handle_payment_records_alpha_code_not_on_plugin_list(cart, caplog):  # pylint: disable=redefined-outer-name
    """
    Verify that an alpha code that is not in ISO_4217_CURRENCY_EXPONENTS is persisted on the transaction row with
    the failure annotated and a warning logged, even when it parses as a three-letter string.
    """
    processor = DummyProcessor()
    with caplog.at_level(logging.WARNING):
        record = processor.handle_payment(
            cart, cart.user, 'success', 'unsupported-alpha-1', processor.SLUG, str(cart.total),
            'XYZ', 'unsupported alpha currency', None,
        )
    assert 'recorded but not reconciled' in caplog.text, 'The reconciliation warning must be logged'
    assert Transaction.objects.filter(gateway_transaction_id='unsupported-alpha-1').exists()
    assert record.status == processor.TRANSACTION_STATUS_FAILED
    assert record.currency == 'XYZ'

    cart.refresh_from_db()
    assert cart.status == Cart.Status.PROCESSING


@pytest.mark.django_db
def test_handle_payment_annotates_processor_domain_restriction(cart, caplog):  # pylint: disable=redefined-outer-name
    """
    Verify that a processor overriding map_currency for a domain restriction (e.g. IQD only) leaves a persisted,
    annotated row and a warning log instead of a rollback, and that a passing value settles.
    """
    processor = _RejectNonIQDProcessor()
    with caplog.at_level(logging.WARNING):
        record = processor.handle_payment(
            cart, cart.user, 'success', 'domain-1', processor.SLUG, str(cart.total),
            '978', 'Euro to a processor that settles only in IQD', None,
        )
    assert 'recorded but not reconciled' in caplog.text, 'The reconciliation warning must be logged'
    assert Transaction.objects.filter(gateway_transaction_id='domain-1').exists(), \
        'The row must survive a domain restriction'
    assert record.status == processor.TRANSACTION_STATUS_FAILED
    assert record.currency == '978'
    assert 'not reconciled' in record.reason

    cart.refresh_from_db()
    assert cart.status == Cart.Status.PROCESSING, 'The cart must not be marked paid'

    settled = processor.handle_payment(
        cart, cart.user, 'success', 'domain-2', processor.SLUG, str(cart.total),
        '368', 'IQD to a processor that settles only in IQD', None,
    )
    assert settled.currency == 'IQD'
    assert settled.status == processor.TRANSACTION_STATUS_SUCCESS


@pytest.mark.django_db
def test_handle_payment_records_a_payment_for_money_the_cart_did_not_expect(  # pylint: disable=redefined-outer-name
    cart,
):
    """
    Verify that a payment the processor finds is not for the cart's total is recorded all the same, and not settled.

    The gateway took money for this cart, so the row is written as it would be for a settled payment, then marked
    failed with the processor's reason; the cart stays unpaid.
    """
    processor = DummyProcessor()
    with patch.object(
        DummyProcessor, 'reconcile_payment', side_effect=GatewayError('the payment is for 51.000, not 50.000'),
    ):
        record = processor.handle_payment(
            cart, cart.user, 'success', 'mismatch-1', processor.SLUG, '51.000',
            get_currency(cart), 'charged more than the cart', {'amount': '51.000'},
        )

    assert Transaction.objects.filter(gateway_transaction_id='mismatch-1').exists(), 'The payment was not recorded'
    assert record.status == processor.TRANSACTION_STATUS_FAILED, 'A payment for other money was recorded as settled'
    assert 'not reconciled: the payment is for 51.000, not 50.000' in record.reason, \
        'The row does not say why the payment was not reconciled'
    assert AuditLog.objects.filter(cart=cart, action=AuditLog.AuditActions.TRANSACTION_NOT_RECONCILED).exists(), \
        'The unreconciled payment is not on the audit trail'
    cart.refresh_from_db()
    assert cart.status == Cart.Status.PROCESSING, 'The cart of a payment for other money was marked paid'


@pytest.mark.django_db
def test_handle_payment_holds_payments_a_processor_never_reconciles(  # pylint: disable=redefined-outer-name
    cart,
):
    """
    Verify that a processor not checking the money it was paid has its payments recorded but never settled.

    The default refuses rather than accepts, so a processor that leaves the check out loses no money and enrols
    nobody unchecked: every payment waits for a person to reconcile it.
    """
    class UncheckingProcessor(DummyProcessor):
        """A processor relying on the default reconciliation."""

        reconcile_payment = BaseProcessor.reconcile_payment

    processor = UncheckingProcessor()
    record = processor.handle_payment(
        cart, cart.user, 'success', 'unchecked-1', processor.SLUG, str(cart.total),
        get_currency(cart), 'a payment nobody checked', None,
    )

    assert record.status == processor.TRANSACTION_STATUS_FAILED, 'An unchecked payment was recorded as settled'
    assert 'UncheckingProcessor does not reconcile payments' in record.reason, \
        'The row does not say the payment was never checked'
    cart.refresh_from_db()
    assert cart.status == Cart.Status.PROCESSING, 'The cart of an unchecked payment was marked paid'


@pytest.mark.django_db
def test_handle_payment_refuses_to_mark_cart_paid_on_non_success_status(  # pylint: disable=redefined-outer-name
    cart, caplog
):
    """
    Verify that a gateway-reported non-success status is persisted verbatim, the cart stays in its pre-payment
    state and a warning is logged; handle_payment never trusts the processor to have checked first.
    """
    processor = DummyProcessor()
    with caplog.at_level(logging.WARNING):
        record = processor.handle_payment(
            cart, cart.user, processor.TRANSACTION_STATUS_PENDING, 'pending-1', processor.SLUG,
            str(cart.total), 'SAR', 'pending status', None,
        )
    assert 'recorded but not reconciled' in caplog.text, 'The reconciliation warning must be logged'
    assert Transaction.objects.filter(gateway_transaction_id='pending-1').exists()
    assert record.status == processor.TRANSACTION_STATUS_PENDING, \
        'The gateway-reported status is kept verbatim on the row'
    assert 'not reconciled' in record.reason

    cart.refresh_from_db()
    assert cart.status == Cart.Status.PROCESSING


def enrol(user, sku, mode=None):
    """Enrol the learner, actively, in the course of the catalogue item, in the given mode or the item's own."""
    course_mode = CourseMode.objects.get(sku=sku)
    CourseEnrollment.objects.create(
        user=user, course=course_mode.course, mode=mode or course_mode.mode_slug, is_active=True,
    )


def buy_in_another_cart(user, sku):
    """Have the learner buy the catalogue item in another, paid, cart."""
    item = CatalogueItem.objects.get(sku=sku)
    bought = Cart.objects.create(user=user, status=Cart.Status.PAID)
    bought.items.create(catalogue_item=item, original_price=item.price, final_price=item.price)


def settle(cart, transaction_status='success'):  # pylint: disable=redefined-outer-name
    """Record a payment of the cart's total for the cart through the dummy processor, and return the row."""
    processor = DummyProcessor()
    return processor.handle_payment(
        cart, cart.user, transaction_status, f'TX-{cart.id}', processor.SLUG, str(cart.total), get_currency(cart),
        'gateway reason', {'id': f'TX-{cart.id}'},
    )


@pytest.mark.django_db
@pytest.mark.parametrize('come_to_hold, usecase', [
    (lambda user: enrol(user, 'custom-sku-1'), 'enrolled by staff in the mode bought'),
    (lambda user: enrol(user, 'custom-sku-1', mode=CourseMode.VERIFIED), 'enrolled by staff in another paid mode'),
    (lambda user: buy_in_another_cart(user, 'custom-sku-1'), 'bought in another cart'),
])
def test_handle_payment_holds_a_payment_for_a_course_its_learner_came_to_hold(  # pylint: disable=redefined-outer-name
    cart, come_to_hold, usecase,
):
    """
    Verify that a payment for a course its learner came to hold while it was at the gateway is recorded, marked failed
    saying why, and not settled.

    Checkout would refuse the course now, so settling it would sell nothing new; the payment waits for a person to
    accept or refund it.
    """
    come_to_hold(cart.user)

    record = settle(cart)

    assert record.status == DummyProcessor.TRANSACTION_STATUS_FAILED, f'Settled: {usecase}'
    assert 'not reconciled: the learner already holds custom-sku-1' in record.reason, f'No reason: {usecase}'
    assert AuditLog.objects.filter(cart=cart, action=AuditLog.AuditActions.TRANSACTION_NOT_RECONCILED).exists(), \
        f'Not on the audit trail: {usecase}'
    cart.refresh_from_db()
    assert cart.status == Cart.Status.PROCESSING, f'Marked paid: {usecase}'


@pytest.mark.django_db
def test_handle_payment_settles_a_course_its_learner_only_audits(cart):  # pylint: disable=redefined-outer-name
    """Verify that a payment for a course its learner only audits settles: buying it upgrades them."""
    enrol(cart.user, 'custom-sku-1', mode=CourseMode.AUDIT)

    record = settle(cart)

    assert record.status == DummyProcessor.TRANSACTION_STATUS_SUCCESS, 'A payment upgrading an audit was not settled'
    cart.refresh_from_db()
    assert cart.status == Cart.Status.PAID, 'A payment upgrading an audit did not mark the cart paid'


@pytest.mark.django_db
def test_handle_payment_keeps_the_gateways_reason_for_a_held_course(cart):  # pylint: disable=redefined-outer-name
    """
    Verify that a payment the gateway did not report a success for keeps that as its reason, even for a course its
    learner holds: what the gateway said comes first.
    """
    enrol(cart.user, 'custom-sku-1')

    record = settle(cart, transaction_status=DummyProcessor.TRANSACTION_STATUS_PENDING)

    assert 'Gateway reported transaction status' in record.reason, 'The gateway\'s answer is not the reason'
    assert 'already holds' not in record.reason, 'The held course was given as the reason over the gateway\'s answer'


@pytest.mark.django_db
@pytest.mark.parametrize('held_skus, settled, usecase', [
    (('custom-sku-1', 'course1-org2-no-id-professional'), False, 'every course held'),
    (('custom-sku-1',), True, 'only some courses held'),
])
def test_handle_payment_holds_a_bundle_only_when_every_course_is_held(  # pylint: disable=redefined-outer-name
    cart, held_skus, settled, usecase,
):
    """
    Verify that a payment for a bundle is held when its learner came to hold every one of its courses, and settled
    when they hold only some: the bundle is sold as checkout sells it, fulfilment skipping the courses held.
    """
    bundle = CatalogueItem.objects.get(sku='BUNDLE-PRO-CERT')
    cart.items.update(catalogue_item=bundle, original_price=bundle.price, final_price=bundle.price)
    for sku in held_skus:
        enrol(cart.user, sku)

    record = settle(cart)

    assert (record.status == DummyProcessor.TRANSACTION_STATUS_SUCCESS) == settled, f'Failed usecase: {usecase}'
    assert ('already holds BUNDLE-PRO-CERT' in record.reason) != settled, f'Failed usecase: {usecase}'


@pytest.mark.django_db
def test_handle_payment_records_the_callers_refusal_as_the_reason(cart):  # pylint: disable=redefined-outer-name
    """
    Verify that a payment the caller already refused is recorded as failed for the caller's reason, in place of every
    other check, and leaves the cart as it was.
    """
    processor = DummyProcessor()

    record = processor.handle_payment(
        cart, cart.user, 'success', 'refused-1', processor.SLUG, str(cart.total), '999', 'gateway reason',
        {'id': 'refused-1'}, refusal='a second order paid for the cart',
    )

    assert record.status == processor.TRANSACTION_STATUS_FAILED, 'A refused payment was recorded as settled'
    assert record.reason == 'gateway reason | not reconciled: a second order paid for the cart', \
        'The row does not carry the caller\'s reason alone'
    cart.refresh_from_db()
    assert cart.status == Cart.Status.PROCESSING, 'A refused payment moved the cart'


@pytest.mark.django_db
def test_handle_payment_keeps_raw_evidence_on_failure(cart, caplog):  # pylint: disable=redefined-outer-name
    """
    Verify that a verification failure never writes less than a success: the row keeps the raw response, the
    transmitted amount and the method next to the annotation, and a warning is logged.
    """
    processor = DummyProcessor()
    gateway_response = {'OrderNumber': 'evidence-1', 'currency': '999'}
    with caplog.at_level(logging.WARNING):
        record = processor.handle_payment(
            cart, cart.user, 'success', 'evidence-1', processor.SLUG, str(cart.total),
            '999', 'gateway reason', gateway_response,
        )
    assert 'recorded but not reconciled' in caplog.text, 'The reconciliation warning must be logged'
    assert record.response == gateway_response
    record.refresh_from_db()
    assert record.amount == cart.total
    assert record.method == processor.SLUG
    assert record.reason.startswith('gateway reason')
    assert 'not reconciled' in record.reason


@pytest.mark.django_db
def test_handle_payment_converts_db_duplicate_to_duplicate_error(cart):  # pylint: disable=redefined-outer-name
    """
    Verify that a database-level unique violation on the gateway and gateway_transaction_id pair surfaces as
    DuplicateTransactionError; the constraint is the backstop for duplicate detection.
    """
    processor = DummyProcessor()
    with patch('zeitlabs_payments.providers.base.Transaction.objects.create') as mock_create:
        mock_create.side_effect = IntegrityError('unique constraint violated')
        with pytest.raises(DuplicateTransactionError):
            processor.handle_payment(
                cart, cart.user, 'success', 'race-1', processor.SLUG, str(cart.total),
                'SAR', 'race', None,
            )


@pytest.mark.django_db
def test_transaction_gateway_id_is_unique_per_gateway_at_database_level(cart):  # pylint: disable=redefined-outer-name
    """
    Verify that the database itself refuses a second row for the same gateway and gateway_transaction_id pair.
    """
    Transaction.objects.create(
        cart=cart, type=Transaction.TransactionType.PAYMENT, status='success', gateway='dummy',
        gateway_transaction_id='db-unique-1', method='dummy', amount=10, currency='SAR',
    )
    with pytest.raises(IntegrityError):
        Transaction.objects.create(
            cart=cart, type=Transaction.TransactionType.PAYMENT, status='success', gateway='dummy',
            gateway_transaction_id='db-unique-1', method='dummy', amount=10, currency='SAR',
        )


@pytest.mark.django_db
def test_transaction_same_gateway_id_across_different_gateways_is_allowed(cart):  # pylint: disable=redefined-outer-name
    """Verify that two different gateways may hold the same gateway_transaction_id."""
    Transaction.objects.create(
        cart=cart, type=Transaction.TransactionType.PAYMENT, status='success', gateway='dummy',
        gateway_transaction_id='shared-id-1', method='dummy', amount=10, currency='SAR',
    )
    Transaction.objects.create(
        cart=cart, type=Transaction.TransactionType.PAYMENT, status='success', gateway='hyperpay',
        gateway_transaction_id='shared-id-1', method='card', amount=10, currency='SAR',
    )


@pytest.mark.django_db
def test_process_payment_leaves_row_on_verification_failure(cart, caplog):  # pylint: disable=redefined-outer-name
    """
    Verify that the full record-update flow leaves the persisted, annotated transaction row behind when
    verification fails -- no invoice, no fulfillment, the cart is not paid -- and that the skip is logged for
    audit visibility.
    """
    processor = DummyProcessor()
    request = MagicMock()
    request.user = cart.user

    with caplog.at_level(logging.WARNING):
        result = processor.process_payment_and_update_records(
            cart=cart,
            data={'raw': 'response'},
            request=request,
            transaction_id='flow-1',
            transaction_status='success',
            method='card',
            amount=cart.total,
            currency='999',
            reason='payment completed',
        )
    assert 'is not settled' in caplog.text, 'The skip must be logged for audit visibility'
    assert result is None
    record = Transaction.objects.get(gateway_transaction_id='flow-1')
    assert record.status == processor.TRANSACTION_STATUS_FAILED
    assert record.currency == '999'
    assert not Invoice.objects.filter(cart=cart).exists()

    cart.refresh_from_db()
    assert cart.status == Cart.Status.PROCESSING


@pytest.mark.django_db
def test_handle_payment_redacts_unmasked_card_number(cart, caplog):  # pylint: disable=redefined-outer-name
    """
    Verify that an unmasked card number inside the gateway response or the reason is replaced by
    its masked form before the row is written, and a warning is logged.
    """
    processor = DummyProcessor()
    gateway_response = {'Pan': '4111111111111111', 'Ok': True}
    with caplog.at_level(logging.WARNING):
        record = processor.handle_payment(
            cart, cart.user, 'success', 'redact-1', processor.SLUG, str(cart.total),
            'SAR', 'paid with 4111111111111111', gateway_response,
        )

    record.refresh_from_db()
    assert record.response == {'Pan': '411111****1111', 'Ok': 'True'}, \
        'The unmasked card number must not reach the response column'
    assert record.reason == 'paid with 411111****1111', \
        'The unmasked card number must not reach the reason column'
    assert 'Unmasked card number found and redacted' in caplog.text, \
        'The redaction must be logged for audit visibility'


@pytest.mark.django_db
def test_handle_payment_keeps_gateway_masked_block(cart, caplog):  # pylint: disable=redefined-outer-name
    """
    Verify that the masked block the gateways already send (BIN, stars, last four) is kept
    verbatim and no redaction fires on it.
    """
    processor = DummyProcessor()
    gateway_response = {'Pan': '422245****7895'}
    with caplog.at_level(logging.WARNING):
        record = processor.handle_payment(
            cart, cart.user, 'success', 'redact-2', processor.SLUG, str(cart.total),
            'SAR', 'paid with 422245****7895', gateway_response,
        )

    record.refresh_from_db()
    assert record.response == {'Pan': '422245****7895'}, \
        'The gateway-masked block is kept deliberately and must not be stripped'
    assert record.reason == 'paid with 422245****7895'
    assert caplog.text == '', 'No redaction may fire on an already-masked value'


@pytest.mark.django_db
def test_bundle_settlement_failure_rolls_back_and_records_landed(cart):  # pylint: disable=redefined-outer-name
    """
    Verify that a bundle fulfillment failure part-way through rolls the enrollment rows back and
    leaves a surviving audit record of which member courses landed.

    The first member enrolls for real, the second fails. The settle transaction rolls back, so no
    enrollment row, no invoice and no in-transaction audit entry remains; the post-rollback audit
    entry lists the course that had landed and the one that failed, and the transaction record
    written first is untouched.
    """
    processor = DummyProcessor()
    bundle = CatalogueItem.objects.get(sku='BUNDLE-PRO-CERT')
    bundle_cart = Cart.objects.create(user=cart.user, status=Cart.Status.PROCESSING)
    bundle_cart.items.create(
        catalogue_item=bundle,
        original_price=bundle.price,
        final_price=bundle.price,
    )
    request = MagicMock(scheme='https', site=Site.objects.get_current())
    request.user = cart.user

    real_enroll = CourseEnrollment.enroll
    call_count = {'n': 0}

    def flaky_enroll(user, course_key, mode=None, check_access=False, **kwargs):
        call_count['n'] += 1
        if call_count['n'] == 1:
            return real_enroll(user, course_key, mode=mode, check_access=check_access)
        raise CourseEnrollmentException('Simulated midway failure')

    with patch('zeitlabs_payments.cart_handler.CourseEnrollment.enroll', side_effect=flaky_enroll):
        result = processor.process_payment_and_update_records(
            cart=bundle_cart,
            data={'raw': 'response'},
            request=request,
            transaction_id='bundle-flow-1',
            transaction_status='SUCCESS',
            method='card',
            amount=str(bundle_cart.total),
            currency='SAR',
            reason='bundle payment',
        )

    assert result is None, 'A failed settlement returns no invoice'
    assert CourseEnrollment.objects.filter(user=cart.user).count() == 0, \
        'The enrollment row of the first member must be rolled back with the settle transaction'
    assert not Invoice.objects.filter(cart=bundle_cart).exists(), \
        'The invoice must be rolled back with the settle transaction'
    assert Transaction.objects.filter(gateway_transaction_id='bundle-flow-1').exists(), \
        'The transaction record written first is never rolled back'
    assert not AuditLog.objects.filter(
        action=AuditLog.AuditActions.USER_ENROLLED, cart=bundle_cart,
    ).exists(), \
        'In-transaction audit entries roll back with the settle transaction'

    failure_record = AuditLog.objects.filter(
        action=AuditLog.AuditActions.BUNDLE_FULFILLMENT_FAILED, cart=bundle_cart,
    ).first()
    assert failure_record is not None, \
        'The surviving record of which member courses landed must be written after the rollback'
    assert 'course-v1:org1+1+1' in failure_record.details, \
        'The record names the course whose enrollment had completed'
    assert 'course-v1:org2+1+1' in failure_record.details, \
        'The record names the course that failed'
    assert AuditLog.objects.filter(
        action=AuditLog.AuditActions.INVOICE_CREATION_FAILED, cart=bundle_cart,
    ).exists(), \
        'The generic settlement-failure entry is written as well'


@pytest.mark.django_db
def test_a_failed_bundle_records_the_courses_its_learner_already_held(cart):  # pylint: disable=redefined-outer-name
    """
    Verify that the record written after a bundle's fulfilment fails names a course the learner already held, whose own
    entry rolled back with the rest, so staff know it needs nothing on retry.
    """
    bundle = CatalogueItem.objects.get(sku='BUNDLE-PRO-CERT')
    held_mode = CourseMode.objects.get(sku=BundleCourseItem.objects.filter(bundle=bundle).first().course_item.sku)
    CourseEnrollment.objects.create(user=cart.user, course=held_mode.course, mode=held_mode.mode_slug, is_active=True)
    bundle_cart = Cart.objects.create(user=cart.user, status=Cart.Status.PROCESSING)
    bundle_cart.items.create(catalogue_item=bundle, original_price=bundle.price, final_price=bundle.price)
    request = MagicMock(scheme='https', site=Site.objects.get_current())
    request.user = cart.user

    failing_enroll = CourseEnrollmentException('Simulated failure')
    with patch('zeitlabs_payments.cart_handler.CourseEnrollment.enroll', side_effect=failing_enroll):
        result = DummyProcessor().process_payment_and_update_records(
            cart=bundle_cart,
            data={'raw': 'response'},
            request=request,
            transaction_id='bundle-flow-held',
            transaction_status='SUCCESS',
            method='card',
            amount=str(bundle_cart.total),
            currency='SAR',
            reason='bundle payment',
        )

    assert result is None, 'The bundle was invoiced although a course failed to enrol'
    failure_record = AuditLog.objects.get(action=AuditLog.AuditActions.BUNDLE_FULFILLMENT_FAILED, cart=bundle_cart)
    assert f'Courses the learner already held: {held_mode.course.id}.' in failure_record.details, \
        'The record does not name the course the learner already held'


@pytest.mark.django_db
def test_payment_method_metadata_names_the_processor_and_its_url(cart):  # pylint: disable=redefined-outer-name
    """
    Verify that the metadata a checkout page lists a processor from carries its slug, its labels and its URL.

    The URL is reversed for this cart rather than assembled by the page, so a checkout page never has to know
    how a processor's route is spelled.
    """
    metadata = DummyProcessor.get_payment_method_metadata(cart)

    assert metadata['slug'] == DummyProcessor.SLUG, 'The metadata should name the processor by its slug'
    assert metadata['title'] == DummyProcessor.NAME, 'The metadata should carry the processor name to display'
    assert metadata['checkout_text'] == DummyProcessor.CHECKOUT_TEXT, \
        'The metadata should carry the call to action to display'
    assert metadata['url'] == reverse(
        'zeitlabs_payments:initiate-payment', kwargs={'provider': DummyProcessor.SLUG, 'cart_id': cart.id},
    ), 'The metadata should carry the initiate-payment URL for this cart'


@pytest.mark.django_db
def test_payment_method_metadata_carries_no_availability_flag(cart):  # pylint: disable=redefined-outer-name
    """
    Verify that availability is absent from the metadata, so no page can decide it from a key it reads here.

    Whether a processor is offered is ``is_hidden_for``'s decision, made by the processor; a ``disabled``-style
    key here would move it into presentation code.
    """
    metadata = DummyProcessor.get_payment_method_metadata(cart)

    assert set(metadata) == {'slug', 'title', 'checkout_text', 'url'}, \
        'The metadata should carry exactly what a checkout page renders, and nothing that decides availability'
