"""Tests for Stripe payment processor."""
# pylint: disable=unused-argument,redefined-outer-name,import-outside-toplevel
# pylint: disable=unused-import,too-many-positional-arguments
import json
from decimal import Decimal
from unittest.mock import MagicMock, Mock, patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.http import HttpRequest
from django.test import RequestFactory

from zeitlabs_payments.exceptions import GatewayError, InvalidCartError
from zeitlabs_payments.models import Cart, CartItem, CatalogueItem
from zeitlabs_payments.providers.stripe_payment.processor import StripeProcessor

User = get_user_model()


@pytest.fixture
def user(db):
    """Create a test user."""
    return User.objects.create_user(
        username='testuser',
        email='test@example.com',
        password='testpass123'
    )


@pytest.fixture
def site(db):
    """Create a test site."""
    site, _ = Site.objects.get_or_create(
        domain='example.com',
        defaults={'name': 'Example Site'}
    )
    return site


@pytest.fixture
def catalogue_item(db):
    """Create a test catalogue item."""
    from opaque_keys.edx.keys import CourseKey
    from openedx.core.djangoapps.content.course_overviews.models import CourseOverview

    course = CourseOverview.objects.create(
        id=CourseKey.from_string('course-v1:Test+Course+001'),
        org='Test',
        display_name='Test Course',
        course_image_url='',
    )

    return CatalogueItem.objects.create(
        title='Test Course',
        sku='TEST-COURSE-001',
        type=CatalogueItem.ItemType.PAID_COURSE,
        item_ref_id=course.id,
        price=Decimal('99.99'),
        currency='SAR',
        description='A test course'
    )


@pytest.fixture
def cart(db, user, catalogue_item):
    """Create a test cart with an item."""
    cart = Cart.objects.create(
        user=user,
        status=Cart.Status.PROCESSING
    )
    CartItem.objects.create(
        cart=cart,
        catalogue_item=catalogue_item,
        original_price=catalogue_item.price,
        discount_amount=Decimal('0.00'),
        tax_amount=Decimal('0.00'),
        final_price=catalogue_item.price,
    )
    return cart


@pytest.fixture
def request_factory():
    """Create a request factory."""
    return RequestFactory()


@pytest.fixture
def mock_stripe_settings():
    """Mock Stripe settings."""
    with patch('zeitlabs_payments.providers.stripe_payment.processor.settings') as mock_settings:
        mock_settings.STRIPE_SECRET_KEY = 'sk_test_123456789'
        mock_settings.STRIPE_PUBLISHABLE_KEY = 'pk_test_123456789'
        mock_settings.STRIPE_WEBHOOK_SECRET = 'whsec_123456789'
        yield mock_settings


class TestStripeProcessor:
    """Test Stripe processor initialization and basic methods."""

    def test_processor_initialization_success(self, mock_stripe_settings):
        """Test successful processor initialization."""
        with patch('stripe.api_key', None):
            processor = StripeProcessor()
            assert processor.SLUG == 'stripe'
            assert processor.NAME == 'Stripe'
            assert processor.api_key == 'sk_test_123456789'
            assert processor.publishable_key == 'pk_test_123456789'

    def test_processor_initialization_missing_secret_key(self):
        """Test processor initialization fails without secret key."""
        with patch('zeitlabs_payments.providers.stripe_payment.processor.settings') as mock_settings:
            mock_settings.STRIPE_SECRET_KEY = None
            mock_settings.STRIPE_PUBLISHABLE_KEY = 'pk_test_123456789'

            with pytest.raises(GatewayError, match='STRIPE_SECRET_KEY is not configured'):
                StripeProcessor()

    def test_get_cart_success(self, mock_stripe_settings, cart):
        """Test successful cart retrieval."""
        processor = StripeProcessor()
        retrieved_cart = processor.get_cart(cart.id)
        assert retrieved_cart.id == cart.id
        assert retrieved_cart.user == cart.user

    def test_get_cart_invalid_id(self, mock_stripe_settings):
        """Test cart retrieval with invalid ID."""
        processor = StripeProcessor()
        with pytest.raises(InvalidCartError, match='Invalid cart ID'):
            processor.get_cart('invalid')

    @pytest.mark.django_db
    def test_get_cart_not_found(self, mock_stripe_settings):
        """Test cart retrieval when cart doesn't exist."""
        processor = StripeProcessor()
        with pytest.raises(InvalidCartError, match='does not exist'):
            processor.get_cart(99999)


class TestGetTransactionParameters:
    """Test transaction parameter generation."""

    @patch('zeitlabs_payments.providers.stripe_payment.processor.stripe')
    def test_get_transaction_parameters_success(
        self, mock_stripe, mock_stripe_settings, cart, user, site, request_factory
    ):
        """Test successful transaction parameter generation."""
        processor = StripeProcessor()
        request = request_factory.get('/checkout/')
        request.site = site
        request.user = user

        mock_session = Mock()
        mock_session.id = 'cs_test_123'
        mock_session.url = 'https://checkout.stripe.com/pay/cs_test_123'
        mock_stripe.checkout.Session.create.return_value = mock_session

        params = processor.get_transaction_parameters(cart=cart, request=request)

        assert params['session_id'] == 'cs_test_123'
        assert params['session_url'] == 'https://checkout.stripe.com/pay/cs_test_123'
        assert params['publishable_key'] == 'pk_test_123456789'
        assert 'order_reference' in params
        assert params['amount'] == 100  # 99.99 rounded

        mock_stripe.checkout.Session.create.assert_called_once()
        call_kwargs = mock_stripe.checkout.Session.create.call_args[1]
        assert call_kwargs['mode'] == 'payment'
        assert call_kwargs['customer_email'] == user.email
        assert call_kwargs['metadata']['cart_id'] == str(cart.id)

    def test_get_transaction_parameters_no_request(self, mock_stripe_settings, cart):
        """Test transaction parameters fail without request."""
        processor = StripeProcessor()
        with pytest.raises(GatewayError, match='Request object is required'):
            processor.get_transaction_parameters(cart=cart, request=None)

    @patch('zeitlabs_payments.providers.stripe_payment.processor.stripe')
    def test_get_transaction_parameters_stripe_error(
        self, mock_stripe, mock_stripe_settings, cart, user, site, request_factory
    ):
        """Test handling of Stripe API errors."""
        processor = StripeProcessor()
        request = request_factory.get('/checkout/')
        request.site = site
        request.user = user

        import stripe as stripe_module
        mock_stripe.error.StripeError = stripe_module.error.StripeError
        mock_stripe.checkout.Session.create.side_effect = stripe_module.error.StripeError('API Error')

        with pytest.raises(GatewayError, match='Failed to create Stripe checkout session'):
            processor.get_transaction_parameters(cart=cart, request=request)


class TestWebhookHandling:
    """Test webhook signature verification and payment handling."""

    @patch('zeitlabs_payments.providers.stripe_payment.processor.stripe')
    def test_verify_webhook_signature_success(self, mock_stripe, mock_stripe_settings):
        """Test successful webhook signature verification."""
        processor = StripeProcessor()

        mock_event = {'type': 'checkout.session.completed', 'data': {}}
        mock_stripe.Webhook.construct_event.return_value = mock_event

        payload = b'{"type": "checkout.session.completed"}'
        sig_header = 't=123456,v1=signature'

        event = processor.verify_webhook_signature(payload, sig_header)
        assert event == mock_event
        mock_stripe.Webhook.construct_event.assert_called_once_with(
            payload, sig_header, 'whsec_123456789'
        )

    @patch('zeitlabs_payments.providers.stripe_payment.processor.stripe')
    def test_verify_webhook_signature_invalid(self, mock_stripe, mock_stripe_settings):
        """Test webhook signature verification failure."""
        processor = StripeProcessor()

        import stripe as stripe_module
        mock_stripe.error.SignatureVerificationError = stripe_module.error.SignatureVerificationError
        mock_stripe.Webhook.construct_event.side_effect = (
            stripe_module.error.SignatureVerificationError('Invalid signature', 'sig')
        )

        payload = b'{"type": "test"}'
        sig_header = 'invalid'

        with pytest.raises(GatewayError, match='Invalid webhook signature'):
            processor.verify_webhook_signature(payload, sig_header)

    @patch('zeitlabs_payments.providers.stripe_payment.processor.StripeProcessor.process_payment_and_update_records')
    def test_handle_successful_payment(self, mock_process, mock_stripe_settings, cart):
        """Test successful payment handling from webhook."""
        processor = StripeProcessor()
        request = Mock()

        session_data = {
            'id': 'cs_test_123',
            'payment_intent': 'pi_test_123',
            'amount_total': 9999,  # in cents
            'currency': 'usd',
            'payment_status': 'paid',
            'metadata': {
                'cart_id': str(cart.id),
                'site_id': '1'
            }
        }

        mock_invoice = Mock()
        mock_process.return_value = mock_invoice

        result = processor.handle_successful_payment(session_data, request)

        assert result == mock_invoice
        mock_process.assert_called_once()
        call_kwargs = mock_process.call_args[1]
        assert call_kwargs['cart'] == cart
        assert call_kwargs['transaction_id'] == 'pi_test_123'
        assert call_kwargs['amount'] == '99.99'
        assert call_kwargs['currency'] == 'USD'

    def test_handle_successful_payment_no_cart_id(self, mock_stripe_settings):
        """Test payment handling fails without cart ID in session."""
        processor = StripeProcessor()
        request = Mock()

        session_data = {
            'id': 'cs_test_123',
            'payment_status': 'paid',
            'metadata': {}
        }

        result = processor.handle_successful_payment(session_data, request)
        assert result is None


class TestPaymentView:
    """Test payment view rendering."""

    @patch('zeitlabs_payments.providers.stripe_payment.processor.StripeProcessor.get_transaction_parameters')
    @patch('zeitlabs_payments.providers.stripe_payment.processor.render')
    def test_payment_view_success(
        self, mock_render, mock_get_params, mock_stripe_settings, cart, user, site, request_factory
    ):
        """Test successful payment view rendering."""
        processor = StripeProcessor()
        request = request_factory.get('/checkout/')
        request.site = site
        request.user = user

        mock_get_params.return_value = {
            'session_id': 'cs_test_123',
            'session_url': 'https://checkout.stripe.com/...',
            'publishable_key': 'pk_test_123'
        }

        processor.payment_view(cart=cart, request=request)

        cart.refresh_from_db()
        assert cart.status == Cart.Status.PAYMENT_PENDING

        mock_render.assert_called_once()
        call_args = mock_render.call_args
        assert call_args[0][1] == 'zeitlabs_payments/stripe_checkout.html'
        context = call_args[0][2]
        assert context['cart'] == cart
        assert context['session_id'] == 'cs_test_123'

    def test_payment_view_no_request(self, mock_stripe_settings, cart):
        """Test payment view fails without request."""
        processor = StripeProcessor()
        with pytest.raises(GatewayError, match='Request object is required'):
            processor.payment_view(cart=cart, request=None)

    @patch('zeitlabs_payments.providers.stripe_payment.processor.StripeProcessor.get_transaction_parameters')
    @patch('zeitlabs_payments.providers.stripe_payment.processor.render')
    def test_payment_view_cart_already_payment_pending(
        self, mock_render, mock_get_params, mock_stripe_settings, cart, user, site, request_factory
    ):
        """Test payment view when cart is already PAYMENT_PENDING."""
        cart.status = Cart.Status.PAYMENT_PENDING
        cart.save()

        processor = StripeProcessor()
        request = request_factory.get('/checkout/')
        request.site = site
        request.user = user

        mock_get_params.return_value = {
            'session_id': 'cs_test_123',
            'session_url': 'https://checkout.stripe.com/...',
            'publishable_key': 'pk_test_123'
        }

        processor.payment_view(cart=cart, request=request)

        cart.refresh_from_db()
        assert cart.status == Cart.Status.PAYMENT_PENDING
        mock_render.assert_called_once()


class TestWebhookSignatureVerification:
    """Test webhook signature verification."""

    def test_verify_webhook_signature_no_secret(self, mock_stripe_settings):
        """Test webhook verification when secret is not configured."""
        mock_stripe_settings.STRIPE_WEBHOOK_SECRET = None
        processor = StripeProcessor()

        result = processor.verify_webhook_signature(b'payload', 'signature')

        assert result is None

    @patch('stripe.Webhook.construct_event')
    def test_verify_webhook_signature_invalid_payload(self, mock_construct, mock_stripe_settings):
        """Test webhook verification with invalid payload."""
        mock_construct.side_effect = ValueError('Invalid JSON')
        processor = StripeProcessor()

        with pytest.raises(GatewayError, match='Invalid webhook payload'):
            processor.verify_webhook_signature(b'invalid', 'signature')

    @patch('stripe.Webhook.construct_event')
    def test_verify_webhook_signature_invalid_signature(self, mock_construct, mock_stripe_settings):
        """Test webhook verification with invalid signature."""
        import stripe
        mock_construct.side_effect = stripe.error.SignatureVerificationError('Invalid signature', 'sig')
        processor = StripeProcessor()

        with pytest.raises(GatewayError, match='Invalid webhook signature'):
            processor.verify_webhook_signature(b'payload', 'bad_signature')


class TestHandleSuccessfulPayment:
    """Test successful payment handling."""

    def test_handle_successful_payment_no_cart_id(self, mock_stripe_settings):
        """Test handling payment when cart_id is missing from session."""
        processor = StripeProcessor()
        session = {
            'id': 'session_123',
            'metadata': {},
        }

        result = processor.handle_successful_payment(session, None)

        assert result is None

    @patch('zeitlabs_payments.providers.stripe_payment.processor.StripeProcessor.get_cart')
    def test_handle_successful_payment_cart_retrieval_error(self, mock_get_cart, mock_stripe_settings):
        """Test handling payment when cart retrieval fails."""
        mock_get_cart.side_effect = Exception('Database error')
        processor = StripeProcessor()
        session = {
            'id': 'session_123',
            'metadata': {'cart_id': '999'},
        }

        result = processor.handle_successful_payment(session, None)

        assert result is None
