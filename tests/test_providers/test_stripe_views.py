"""Tests for Stripe payment views."""
# pylint: disable=unused-argument,redefined-outer-name,import-outside-toplevel
# pylint: disable=unused-import,too-many-positional-arguments
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.http import HttpRequest
from django.test import RequestFactory

from zeitlabs_payments.models import Cart, CartItem, CatalogueItem
from zeitlabs_payments.providers.stripe_payment.views import (
    StripeCancelView,
    StripeCheckoutView,
    StripeSuccessView,
    StripeWebhookView,
)

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
def staff_user(db):
    """Create a staff user."""
    return User.objects.create_user(
        username='staffuser',
        email='staff@example.com',
        password='testpass123',
        is_staff=True
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


@pytest.mark.django_db
class TestStripeCheckoutView:
    """Test StripeCheckoutView."""

    @patch('zeitlabs_payments.providers.stripe_payment.views.StripeProcessor')
    def test_checkout_view_success(self, mock_processor_class, user, cart, site):
        """Test successful checkout view."""
        mock_processor = MagicMock()
        mock_processor.get_cart.return_value = cart
        mock_response = MagicMock()
        mock_processor.payment_view.return_value = mock_response
        mock_processor_class.return_value = mock_processor

        factory = RequestFactory()
        request = factory.get(f'/checkout/{cart.id}/')
        request.user = user
        request.site = site

        view = StripeCheckoutView()
        response = view.get(request, cart.id)

        mock_processor.get_cart.assert_called_once_with(cart.id)
        mock_processor.payment_view.assert_called_once_with(cart=cart, request=request)
        assert response == mock_response

    @patch('zeitlabs_payments.providers.stripe_payment.views.StripeProcessor')
    def test_checkout_view_unauthorized_access(self, mock_processor_class, user, staff_user, cart, site):
        """Test unauthorized cart access."""
        mock_processor = MagicMock()
        mock_processor.get_cart.return_value = cart
        mock_processor_class.return_value = mock_processor

        factory = RequestFactory()
        request = factory.get(f'/checkout/{cart.id}/')
        other_user = User.objects.create_user(username='other', email='other@example.com')
        request.user = other_user
        request.site = site

        view = StripeCheckoutView()
        response = view.get(request, cart.id)

        assert response.status_code == 403
        assert b'Unauthorized access to cart' in response.content

    @patch('zeitlabs_payments.providers.stripe_payment.views.StripeProcessor')
    def test_checkout_view_staff_can_access_any_cart(self, mock_processor_class, staff_user, cart, site):
        """Test that staff users can access any cart."""
        mock_processor = MagicMock()
        mock_processor.get_cart.return_value = cart
        mock_response = MagicMock()
        mock_processor.payment_view.return_value = mock_response
        mock_processor_class.return_value = mock_processor

        factory = RequestFactory()
        request = factory.get(f'/checkout/{cart.id}/')
        request.user = staff_user
        request.site = site

        view = StripeCheckoutView()
        response = view.get(request, cart.id)

        assert response == mock_response

    @patch('zeitlabs_payments.providers.stripe_payment.views.StripeProcessor')
    def test_checkout_view_invalid_cart_status(self, mock_processor_class, user, cart, site):
        """Test checkout with invalid cart status."""
        cart.status = Cart.Status.PAID
        cart.save()

        mock_processor = MagicMock()
        mock_processor.get_cart.return_value = cart
        mock_processor_class.return_value = mock_processor

        factory = RequestFactory()
        request = factory.get(f'/checkout/{cart.id}/')
        request.user = user
        request.site = site

        view = StripeCheckoutView()
        response = view.get(request, cart.id)

        assert response.status_code == 400
        assert b'not available for checkout' in response.content

    @patch('zeitlabs_payments.providers.stripe_payment.views.StripeProcessor')
    def test_checkout_view_invalid_cart_error(self, mock_processor_class, user, site):
        """Test checkout with invalid cart."""
        from zeitlabs_payments.exceptions import InvalidCartError

        mock_processor = MagicMock()
        mock_processor.get_cart.side_effect = InvalidCartError('Cart not found')
        mock_processor_class.return_value = mock_processor

        factory = RequestFactory()
        request = factory.get('/checkout/999/')
        request.user = user
        request.site = site

        view = StripeCheckoutView()
        response = view.get(request, 999)

        assert response.status_code == 404

    @patch('zeitlabs_payments.providers.stripe_payment.views.StripeProcessor')
    def test_checkout_view_gateway_error(self, mock_processor_class, user, cart, site):
        """Test checkout with gateway error."""
        from zeitlabs_payments.exceptions import GatewayError

        mock_processor = MagicMock()
        mock_processor.get_cart.return_value = cart
        mock_processor.payment_view.side_effect = GatewayError('Stripe API error')
        mock_processor_class.return_value = mock_processor

        factory = RequestFactory()
        request = factory.get(f'/checkout/{cart.id}/')
        request.user = user
        request.site = site

        view = StripeCheckoutView()
        response = view.get(request, cart.id)

        assert response.status_code == 500

    @patch('zeitlabs_payments.providers.stripe_payment.views.StripeProcessor')
    def test_checkout_view_unexpected_error(self, mock_processor_class, user, cart, site):
        """Test checkout with unexpected error."""
        mock_processor = MagicMock()
        mock_processor.get_cart.return_value = cart
        mock_processor.payment_view.side_effect = Exception('Unexpected error')
        mock_processor_class.return_value = mock_processor

        factory = RequestFactory()
        request = factory.get(f'/checkout/{cart.id}/')
        request.user = user
        request.site = site

        view = StripeCheckoutView()
        response = view.get(request, cart.id)

        assert response.status_code == 500
        assert b'Payment initialization failed' in response.content


@pytest.mark.django_db
class TestStripeSuccessView:
    """Test StripeSuccessView."""

    def test_success_view_no_session_id(self):
        """Test success view without session_id."""
        factory = RequestFactory()
        request = factory.get('/success/')

        view = StripeSuccessView()
        response = view.get(request)

        assert response.status_code == 400

    @patch('zeitlabs_payments.providers.stripe_payment.views.stripe.checkout.Session')
    @patch('zeitlabs_payments.providers.stripe_payment.views.StripeProcessor')
    @patch('zeitlabs_payments.providers.stripe_payment.views.render')
    def test_success_view_with_session_id(self, mock_render, mock_processor_class, mock_session_class, cart):
        """Test success view with valid session_id."""
        mock_session = MagicMock()
        mock_session.metadata = {'cart_id': cart.id}
        mock_session.payment_status = 'paid'
        mock_session.amount_total = 9999
        mock_session.currency = 'sar'
        mock_session_class.retrieve.return_value = mock_session

        mock_processor = MagicMock()
        mock_processor.get_cart.return_value = cart
        mock_processor.handle_successful_payment.return_value = MagicMock(invoice_number='INV-001')
        mock_processor_class.return_value = mock_processor

        factory = RequestFactory()
        request = factory.get('/success/?session_id=cs_test_123')

        view = StripeSuccessView()
        view.get(request)

        mock_render.assert_called_once()
        context = mock_render.call_args[0][2]
        assert context['session_id'] == 'cs_test_123'
        assert context['cart'] == cart

    @patch('zeitlabs_payments.providers.stripe_payment.views.StripeProcessor')
    def test_success_view_handles_errors(self, mock_processor_class):
        """Test success view error handling."""
        mock_processor = MagicMock()
        mock_processor_class.return_value = mock_processor

        factory = RequestFactory()
        request = factory.get('/success/?session_id=cs_test_123')

        with patch('zeitlabs_payments.providers.stripe_payment.views.render', side_effect=Exception('Error')):
            view = StripeSuccessView()
            response = view.get(request)

            assert response.status_code == 500


@pytest.mark.django_db
class TestStripeCancelView:
    """Test StripeCancelView."""

    def test_cancel_view_no_cart_id(self):
        """Test cancel view without cart_id."""
        factory = RequestFactory()
        request = factory.get('/cancel/')

        with patch('zeitlabs_payments.providers.stripe_payment.views.render') as mock_render:
            view = StripeCancelView()
            view.get(request)

            mock_render.assert_called_once()
            context = mock_render.call_args[0][2]
            assert context['cart_id'] is None

    @patch('zeitlabs_payments.providers.stripe_payment.views.render')
    def test_cancel_view_with_cart_id(self, mock_render, cart):
        """Test cancel view with valid cart_id."""
        factory = RequestFactory()
        request = factory.get(f'/cancel/?cart_id={cart.id}')

        view = StripeCancelView()
        view.get(request)

        mock_render.assert_called_once()
        context = mock_render.call_args[0][2]
        assert context['cart_id'] == str(cart.id)

    @patch('zeitlabs_payments.providers.stripe_payment.views.StripeProcessor')
    def test_cancel_view_handles_errors(self, mock_processor_class, cart):
        """Test cancel view error handling."""
        mock_processor = MagicMock()
        mock_processor.get_cart.side_effect = Exception('Cart error')
        mock_processor_class.return_value = mock_processor

        factory = RequestFactory()
        request = factory.get(f'/cancel/?cart_id={cart.id}')

        with patch('zeitlabs_payments.providers.stripe_payment.views.render') as mock_render:
            view = StripeCancelView()
            view.get(request)

            mock_render.assert_called_once()

    @patch('zeitlabs_payments.providers.stripe_payment.views.AuditLog')
    @patch('zeitlabs_payments.providers.stripe_payment.views.StripeProcessor')
    @patch('zeitlabs_payments.providers.stripe_payment.views.render')
    def test_cancel_view_with_valid_cart(self, mock_render, mock_processor_class, mock_audit, cart):
        """Test cancel view with valid cart_id logs audit trail."""
        mock_processor = MagicMock()
        mock_processor.get_cart.return_value = cart
        mock_processor_class.return_value = mock_processor

        factory = RequestFactory()
        request = factory.get(f'/cancel/?cart_id={cart.id}')

        view = StripeCancelView()
        view.get(request)

        mock_audit.log.assert_called_once()
        call_args = mock_audit.log.call_args
        assert call_args[1]['action'] == mock_audit.AuditActions.PAYMENT_CANCELLED
        assert call_args[1]['cart'] == cart
        assert call_args[1]['gateway'] == 'stripe'


@pytest.mark.django_db
class TestStripeWebhook:
    """Test Stripe webhook handler."""

    @patch('zeitlabs_payments.providers.stripe_payment.views.StripeProcessor')
    def test_webhook_success(self, mock_processor_class):
        """Test successful webhook handling."""
        mock_processor = MagicMock()
        mock_processor.verify_webhook_signature.return_value = {
            'type': 'checkout.session.completed',
            'data': {'object': {'id': 'session_123', 'payment_status': 'paid'}}
        }
        mock_processor.handle_successful_payment.return_value = MagicMock(invoice_number='INV-001')
        mock_processor_class.return_value = mock_processor

        factory = RequestFactory()
        request = factory.post(
            '/webhook/',
            data='{"type": "checkout.session.completed"}',
            content_type='application/json'
        )
        request.META['HTTP_STRIPE_SIGNATURE'] = 'test_signature'

        view = StripeWebhookView()
        response = view.post(request)

        assert response.status_code == 200
        mock_processor.verify_webhook_signature.assert_called_once()

    @patch('zeitlabs_payments.providers.stripe_payment.views.StripeProcessor')
    def test_webhook_no_signature(self, mock_processor_class):
        """Test webhook without signature."""
        factory = RequestFactory()
        request = factory.post(
            '/webhook/',
            data='{"type": "checkout.session.completed"}',
            content_type='application/json'
        )

        view = StripeWebhookView()
        response = view.post(request)

        assert response.status_code == 400

    @patch('zeitlabs_payments.providers.stripe_payment.views.StripeProcessor')
    def test_webhook_no_secret_configured(self, mock_processor_class):
        """Test webhook when STRIPE_WEBHOOK_SECRET is not configured."""
        mock_processor = MagicMock()
        mock_processor.verify_webhook_signature.return_value = None
        mock_processor_class.return_value = mock_processor

        factory = RequestFactory()
        request = factory.post(
            '/webhook/',
            data=(
                '{"type": "checkout.session.completed", '
                '"data": {"object": {"id": "session_123", "payment_status": "paid"}}}'
            ),
            content_type='application/json'
        )
        request.META['HTTP_STRIPE_SIGNATURE'] = 'test_signature'

        view = StripeWebhookView()
        response = view.post(request)

        assert response.status_code == 200
        assert b'checkout.session.completed' in response.content

    @patch('zeitlabs_payments.providers.stripe_payment.views.StripeProcessor')
    def test_webhook_gateway_error(self, mock_processor_class):
        """Test webhook with gateway error."""
        from zeitlabs_payments.exceptions import GatewayError

        mock_processor = MagicMock()
        mock_processor.verify_webhook_signature.side_effect = GatewayError('Invalid signature')
        mock_processor_class.return_value = mock_processor

        factory = RequestFactory()
        request = factory.post(
            '/webhook/',
            data='{"type": "checkout.session.completed"}',
            content_type='application/json'
        )
        request.META['HTTP_STRIPE_SIGNATURE'] = 'test_signature'

        view = StripeWebhookView()
        response = view.post(request)

        assert response.status_code == 400

    @patch('zeitlabs_payments.providers.stripe_payment.views.StripeProcessor')
    def test_webhook_unexpected_error(self, mock_processor_class):
        """Test webhook with unexpected error."""
        mock_processor = MagicMock()
        mock_processor.verify_webhook_signature.side_effect = Exception('Unexpected error')
        mock_processor_class.return_value = mock_processor

        factory = RequestFactory()
        request = factory.post(
            '/webhook/',
            data='{"type": "checkout.session.completed"}',
            content_type='application/json'
        )
        request.META['HTTP_STRIPE_SIGNATURE'] = 'test_signature'

        view = StripeWebhookView()
        response = view.post(request)

        assert response.status_code == 500

    @patch('zeitlabs_payments.providers.stripe_payment.views.StripeProcessor')
    def test_webhook_payment_intent_succeeded(self, mock_processor_class):
        """Test webhook for payment_intent.succeeded event."""
        mock_processor = MagicMock()
        mock_processor.verify_webhook_signature.return_value = {
            'type': 'payment_intent.succeeded',
            'data': {'object': {'id': 'pi_123'}}
        }
        mock_processor_class.return_value = mock_processor

        factory = RequestFactory()
        request = factory.post(
            '/webhook/',
            data='{"type": "payment_intent.succeeded"}',
            content_type='application/json'
        )
        request.META['HTTP_STRIPE_SIGNATURE'] = 'test_signature'

        view = StripeWebhookView()
        response = view.post(request)

        assert response.status_code == 200
        assert b'payment_intent.succeeded' in response.content

    @patch('zeitlabs_payments.providers.stripe_payment.views.StripeProcessor')
    def test_webhook_payment_intent_failed(self, mock_processor_class):
        """Test webhook for payment_intent.payment_failed event."""
        mock_processor = MagicMock()
        mock_processor.verify_webhook_signature.return_value = {
            'type': 'payment_intent.payment_failed',
            'data': {
                'object': {
                    'id': 'pi_123',
                    'last_payment_error': {'message': 'Card declined'}
                }
            }
        }
        mock_processor_class.return_value = mock_processor

        factory = RequestFactory()
        request = factory.post(
            '/webhook/',
            data='{"type": "payment_intent.payment_failed"}',
            content_type='application/json'
        )
        request.META['HTTP_STRIPE_SIGNATURE'] = 'test_signature'

        view = StripeWebhookView()
        response = view.post(request)

        assert response.status_code == 200
        assert b'payment_intent.payment_failed' in response.content

    @patch('zeitlabs_payments.providers.stripe_payment.views.StripeProcessor')
    def test_webhook_unhandled_event_type(self, mock_processor_class):
        """Test webhook for unhandled event type."""
        mock_processor = MagicMock()
        mock_processor.verify_webhook_signature.return_value = {
            'type': 'customer.created',
            'data': {'object': {'id': 'cus_123'}}
        }
        mock_processor_class.return_value = mock_processor

        factory = RequestFactory()
        request = factory.post(
            '/webhook/',
            data='{"type": "customer.created"}',
            content_type='application/json'
        )
        request.META['HTTP_STRIPE_SIGNATURE'] = 'test_signature'

        view = StripeWebhookView()
        response = view.post(request)

        assert response.status_code == 200
        assert b'customer.created' in response.content

    @patch('zeitlabs_payments.providers.stripe_payment.views.StripeProcessor')
    def test_webhook_checkout_not_paid(self, mock_processor_class):
        """Test webhook for checkout.session.completed but not paid."""
        mock_processor = MagicMock()
        mock_processor.verify_webhook_signature.return_value = {
            'type': 'checkout.session.completed',
            'data': {
                'object': {
                    'id': 'session_123',
                    'payment_status': 'unpaid'
                }
            }
        }
        mock_processor_class.return_value = mock_processor

        factory = RequestFactory()
        request = factory.post(
            '/webhook/',
            data='{"type": "checkout.session.completed"}',
            content_type='application/json'
        )
        request.META['HTTP_STRIPE_SIGNATURE'] = 'test_signature'

        view = StripeWebhookView()
        response = view.post(request)

        assert response.status_code == 200
        mock_processor.handle_successful_payment.assert_not_called()

    @patch('zeitlabs_payments.providers.stripe_payment.views.StripeProcessor')
    def test_webhook_checkout_paid_no_invoice(self, mock_processor_class):
        """Test webhook when payment succeeds but invoice creation fails."""
        mock_processor = MagicMock()
        mock_processor.verify_webhook_signature.return_value = {
            'type': 'checkout.session.completed',
            'data': {
                'object': {
                    'id': 'session_123',
                    'payment_status': 'paid'
                }
            }
        }
        mock_processor.handle_successful_payment.return_value = None
        mock_processor_class.return_value = mock_processor

        factory = RequestFactory()
        request = factory.post(
            '/webhook/',
            data='{"type": "checkout.session.completed"}',
            content_type='application/json'
        )
        request.META['HTTP_STRIPE_SIGNATURE'] = 'test_signature'

        view = StripeWebhookView()
        response = view.post(request)

        assert response.status_code == 200
        mock_processor.handle_successful_payment.assert_called_once()


@pytest.mark.django_db
class TestStripeSuccessViewEdgeCases:
    """Test StripeSuccessView edge cases."""

    @patch('zeitlabs_payments.providers.stripe_payment.views.stripe.checkout.Session')
    @patch('zeitlabs_payments.providers.stripe_payment.views.StripeProcessor')
    def test_success_view_no_cart_id_in_session(self, mock_processor_class, mock_session_class):
        """Test success view when session has no cart_id."""
        mock_session = MagicMock()
        mock_session.metadata = {}
        mock_session.client_reference_id = None
        mock_session_class.retrieve.return_value = mock_session

        factory = RequestFactory()
        request = factory.get('/success/?session_id=cs_test_123')

        view = StripeSuccessView()
        response = view.get(request)

        assert response.status_code == 400
        assert b'Invalid session data' in response.content

    @patch('zeitlabs_payments.providers.stripe_payment.views.stripe.checkout.Session')
    @patch('zeitlabs_payments.providers.stripe_payment.views.StripeProcessor')
    @patch('zeitlabs_payments.providers.stripe_payment.views.render')
    def test_success_view_cart_already_paid(self, mock_render, mock_processor_class, mock_session_class, cart):
        """Test success view when cart is already paid (webhook already processed)."""
        cart.status = Cart.Status.PAID
        cart.save()

        mock_session = MagicMock()
        mock_session.metadata = {'cart_id': cart.id}
        mock_session.payment_status = 'paid'
        mock_session.amount_total = 9999
        mock_session.currency = 'sar'
        mock_session_class.retrieve.return_value = mock_session

        mock_processor = MagicMock()
        mock_processor.get_cart.return_value = cart
        mock_processor_class.return_value = mock_processor

        factory = RequestFactory()
        request = factory.get('/success/?session_id=cs_test_123')

        view = StripeSuccessView()
        view.get(request)

        mock_render.assert_called_once()
        mock_processor.handle_successful_payment.assert_not_called()
