"""Test base processsor"""

from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.http import HttpRequest

from test_utils.dummy_processor import DummyProcessor
from zeitlabs_payments.exceptions import (
    CartFulfillmentError,
    DuplicateTransactionError,
    GatewayError,
    InvalidCartError,
    InvoiceError,
)
from zeitlabs_payments.helpers import get_currency
from zeitlabs_payments.models import AuditLog, Cart, CatalogueItem, Invoice, InvoiceItem, Transaction, WebhookEvent
from zeitlabs_payments.providers.base import BaseProcessor

User = get_user_model()


@pytest.fixture
def base_processor():
    """processor fixture"""
    return BaseProcessor()


@pytest.fixture
def cart():
    """cart fixture"""
    item = CatalogueItem.objects.get(sku='custom-sku-1')
    user_cart = Cart.objects.create(user=User.objects.get(id=3), status=Cart.Status.PROCESSING)
    user_cart.items.create(
        catalogue_item=item,
        original_price=item.price,
        final_price=item.price
    )
    return user_cart


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
@patch.object(BaseProcessor, 'get_transaction_parameters')
def test_payment_view_for_exception(
    mock_get_transaction_parameters, mock_render, cart, base_processor   # pylint: disable=redefined-outer-name
):
    """Test payment_view calls get_transaction_parameters and renders with correct context."""
    mock_get_transaction_parameters.side_effect = Exception('unexpected error.')
    fake_request = MagicMock(spec=HttpRequest)
    base_processor.payment_view(cart=cart, request=fake_request)
    mock_render.assert_called_once_with(
        fake_request,
        'zeitlabs_payments/payment_error.html'
    )


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
def test_create_invoice_raises_for_non_paid_cart(base_processor, cart):  # pylint: disable=redefined-outer-name
    request = HttpRequest()
    with pytest.raises(InvoiceError):
        base_processor.create_invoice(cart, request)


@pytest.mark.django_db
def test_create_invoice_success(base_processor, cart):  # pylint: disable=redefined-outer-name
    request = HttpRequest()
    cart.status = Cart.Status.PAID
    invoice = base_processor.create_invoice(cart, request)

    assert invoice.cart == cart
    assert invoice.total == cart.total
    assert invoice.status == Invoice.InvoiceStatus.PAID
    assert invoice.invoice_number is not None

    assert InvoiceItem.objects.filter(invoice=invoice).count() == cart.items.count()


@pytest.mark.django_db
def test_get_transaction_parameters_base(cart):  # pylint: disable=redefined-outer-name
    request = MagicMock(spec=HttpRequest)
    request.site = Site.objects.get(domain='example.com')
    processor = DummyProcessor()
    result = processor.get_transaction_parameters_base(cart, request)
    assert result['user_email'] == 'user3@example.com'
    assert result['language'] == 'en'
    assert result['amount'] == int(cart.total)
    assert result['order_reference'] == f'{cart.id}-{request.site.id}'
    assert result['currency'] == cart.items.all()[0].catalogue_item.currency


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
@pytest.mark.parametrize('record_event', [
    (True),
    (False),
])
def test_handle_payment_creates_transaction_and_webhook(cart, record_event):  # pylint: disable=redefined-outer-name
    processor = DummyProcessor()
    transaction_id = '1111'
    gateway_response = {'mode': 'test'}

    assert not Transaction.objects.filter(gateway='dummy', cart=cart).exists(), \
        'Transaction should not exist before test'
    assert cart.status == cart.Status.PROCESSING, \
        'Cart should be in PROCESSING state'

    processor.handle_payment(
        cart, cart.user, 'success', transaction_id, processor.SLUG, str(cart.total),
        get_currency(cart), 'transaction success', gateway_response, record_event
    )

    webhook_exists = WebhookEvent.objects.filter(
        gateway=processor.SLUG, event_type='direct-feedback', payload=gateway_response
    ).exists()
    assert webhook_exists == record_event, \
        f'WebhookEvent existence should be {record_event}'

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
    request = MagicMock()
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
    assert invoice.discount_total == cart.discount_total
    assert invoice.tax_total == cart.tax_total
    assert invoice.total == cart.total
    assert invoice.related_transaction.gateway_transaction_id == '12345'


@pytest.mark.django_db
def test_process_payment_invalid_cart_status(cart):  # pylint: disable=redefined-outer-name
    """Test when cart is not in PROCESSING state."""
    processor = DummyProcessor()
    cart.status = Cart.Status.PENDING
    cart.save()

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
        'pending instead of processing or payment_pending.'
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

    result = processor.process_payment_and_update_records(
        cart=cart,
        data={},
        request=MagicMock(),
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
    """Test when invoice creation or fulfillment raises exception."""
    processor = DummyProcessor()

    request = MagicMock()
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
