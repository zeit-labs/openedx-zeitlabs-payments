"""
Unit tests for adding a new payment processor to zeitlabs-payments.

This test suite demonstrates how to:
1. Create a new payment processor class
2. Register it via entry points
3. Verify it integrates correctly with the payment system
4. Test processor-specific functionality
"""
# pylint: disable=unused-argument,redefined-outer-name,import-outside-toplevel
# pylint: disable=unused-import,too-many-positional-arguments,abstract-method
import types
from decimal import Decimal
from unittest.mock import MagicMock, Mock, patch

import pkg_resources
import pytest
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory
from django.urls import reverse

from zeitlabs_payments.exceptions import GatewayError, InvalidCartError
from zeitlabs_payments.models import Cart, CartItem, CatalogueItem
from zeitlabs_payments.providers.base import BaseProcessor
from zeitlabs_payments.providers.registry import PROCESSORS, get_processor, load_entrypoint_processors

User = get_user_model()


# =======================
# Sample Custom Processor
# =======================

class CustomPaymentProcessor(BaseProcessor):
    """
    Example custom payment processor for testing integration.

    This demonstrates the minimum required implementation for a new processor.
    Real-world processors would include actual gateway integration logic.
    """

    SLUG = 'custom_payment'
    NAME = 'Custom Payment Gateway'
    CHECKOUT_TEXT = 'Pay with Custom Gateway'
    PAYMENT_INITIALIZATION_URL = 'zeitlabs_payments:custom-checkout'

    def __init__(self):
        """Initialize the custom processor with configuration."""
        super().__init__()
        self.api_key = 'custom_api_key_12345'
        self.gateway_url = 'https://api.custompayment.example.com'
        self.initialized = True

    def get_transaction_parameters(
        self,
        cart: Cart,
        request=None,
        use_client_side_checkout=False,
        **kwargs
    ):
        """
        Generate transaction parameters for the custom payment gateway.

        :param cart: The cart object
        :param request: The HTTP request object
        :param use_client_side_checkout: Whether to use client-side checkout
        :return: Dictionary of transaction parameters
        """
        if not request:
            raise GatewayError('Request object is required for custom payment initialization.')

        base_params = self.get_transaction_parameters_base(cart, request)

        transaction_params = {
            'gateway_url': self.gateway_url,
            'api_key': self.api_key,
            'transaction_id': f'TXN-{cart.id}',
            'amount': float(cart.total),
            'currency': cart.items.first().catalogue_item.currency if cart.items.exists() else 'SAR',
            'order_reference': base_params['order_reference'],
            'customer_email': cart.user.email if cart.user else base_params.get('user_email'),
            'return_url': request.build_absolute_uri('/payments/custom/success/'),
            'cancel_url': request.build_absolute_uri(f'/payments/custom/cancel/?cart_id={cart.id}'),
            'webhook_url': request.build_absolute_uri('/payments/webhook/custom/'),
        }

        return transaction_params

    def payment_view(self, cart, request=None, use_client_side_checkout=False, **kwargs):
        """
        Return the payment view/response for the custom processor.

        :param cart: The cart object
        :param request: The HTTP request object
        :return: HttpResponse object
        """
        if not request:
            raise GatewayError('Request is required for payment view.')

        params = self.get_transaction_parameters(cart, request, use_client_side_checkout, **kwargs)

        response = HttpResponse(
            f'<html><body>Redirecting to {params["gateway_url"]}...</body></html>',
            content_type='text/html'
        )
        return response


# =======================
# Test Fixtures
# =======================

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
    return Site.objects.get_or_create(
        domain='example.com',
        defaults={'name': 'Example Site'}
    )[0]


@pytest.fixture
def course_overview(db):
    """Create a CourseOverview for testing."""
    from opaque_keys.edx.keys import CourseKey
    from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
    return CourseOverview.objects.create(
        id=CourseKey.from_string('course-v1:Test+Course+2025'),
        org='Test',
        display_name='Test Course 2025',
        course_image_url='',
    )


@pytest.fixture
def catalogue_item(db, course_overview):
    """Create a test catalogue item."""
    return CatalogueItem.objects.create(
        title='Test Product',
        sku='TEST-PRODUCT-001',
        type=CatalogueItem.ItemType.PAID_COURSE,
        item_ref_id=course_overview.id,
        price=Decimal('149.99'),
        currency='SAR',
        description='A test product for payment processing'
    )


@pytest.fixture
def cart(db, user, catalogue_item):
    """Create a test cart with items."""
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
def mock_request(request_factory, site):
    """Create a mock HTTP request."""
    request = request_factory.get('/payments/')
    request.site = site
    request.user = Mock()
    return request


def make_entry_point(name, cls):
    """Helper to create a fake entry point for testing."""
    ep = types.SimpleNamespace()
    ep.name = name
    ep.value = f'{cls.__module__}:{cls.__name__}'
    ep.load = lambda: cls
    return ep


# =======================
# Test Cases
# =======================

class TestCustomProcessorBasics:
    """Test basic processor creation and attributes."""

    def test_processor_has_required_attributes(self):
        """Test that custom processor has all required attributes."""
        processor = CustomPaymentProcessor()

        assert hasattr(processor, 'SLUG')
        assert hasattr(processor, 'NAME')
        assert hasattr(processor, 'CHECKOUT_TEXT')
        assert hasattr(processor, 'PAYMENT_INITIALIZATION_URL')

        assert processor.SLUG == 'custom_payment'
        assert processor.NAME == 'Custom Payment Gateway'
        assert processor.CHECKOUT_TEXT == 'Pay with Custom Gateway'
        assert processor.initialized is True

    def test_processor_initialization(self):
        """Test processor initializes with correct configuration."""
        processor = CustomPaymentProcessor()

        assert processor.api_key == 'custom_api_key_12345'
        assert processor.gateway_url == 'https://api.custompayment.example.com'

    def test_processor_inherits_from_base(self):
        """Test that custom processor inherits from BaseProcessor."""
        processor = CustomPaymentProcessor()
        assert isinstance(processor, BaseProcessor)


class TestProcessorRegistration:
    """Test processor registration via entry points."""

    def test_loads_custom_processor_via_entrypoint(self, monkeypatch):
        """Test loading custom processor through entry point mechanism."""

        from zeitlabs_payments.providers import registry

        original_processors = registry.PROCESSORS.copy()
        registry.PROCESSORS.clear()

        try:
            ep = make_entry_point('custom_payment', CustomPaymentProcessor)
            monkeypatch.setattr(pkg_resources, 'iter_entry_points', lambda group: [ep])

            load_entrypoint_processors()

            assert 'custom_payment' in registry.PROCESSORS
            assert registry.PROCESSORS['custom_payment'] is CustomPaymentProcessor
        finally:

            registry.PROCESSORS.clear()
            registry.PROCESSORS.update(original_processors)

    def test_get_processor_returns_custom_instance(self):
        """Test retrieving custom processor instance from registry."""

        PROCESSORS['custom_payment'] = CustomPaymentProcessor

        try:
            processor = get_processor('custom_payment')
            assert isinstance(processor, CustomPaymentProcessor)
            assert processor.SLUG == 'custom_payment'
        finally:

            PROCESSORS.pop('custom_payment', None)

    @patch('zeitlabs_payments.providers.registry.PROCESSORS', new={})
    def test_raises_error_for_processor_without_slug(self, monkeypatch):
        """Test that processors without SLUG attribute raise ValueError."""
        class InvalidProcessor(BaseProcessor):
            pass

        ep = make_entry_point('invalid', InvalidProcessor)
        monkeypatch.setattr(pkg_resources, 'iter_entry_points', lambda group: [ep])

        with pytest.raises(ValueError, match='must define a SLUG'):
            load_entrypoint_processors()


class TestProcessorTransactionParameters:
    """Test transaction parameter generation."""

    def test_get_transaction_parameters_success(self, cart, mock_request):
        """Test successful transaction parameter generation."""
        processor = CustomPaymentProcessor()

        params = processor.get_transaction_parameters(cart, mock_request)

        assert 'gateway_url' in params
        assert 'api_key' in params
        assert 'transaction_id' in params
        assert 'amount' in params
        assert 'currency' in params
        assert 'order_reference' in params
        assert 'customer_email' in params
        assert 'return_url' in params
        assert 'cancel_url' in params
        assert 'webhook_url' in params

        assert params['gateway_url'] == 'https://api.custompayment.example.com'
        assert params['api_key'] == 'custom_api_key_12345'
        assert params['transaction_id'] == f'TXN-{cart.id}'
        assert params['amount'] == float(cart.total)
        assert params['customer_email'] == cart.user.email

    def test_get_transaction_parameters_without_request_raises_error(self, cart):
        """Test that missing request raises GatewayError."""
        processor = CustomPaymentProcessor()

        with pytest.raises(GatewayError, match='Request object is required'):
            processor.get_transaction_parameters(cart, request=None)

    def test_transaction_parameters_include_cart_details(self, cart, mock_request):
        """Test that transaction parameters include cart-specific details."""
        processor = CustomPaymentProcessor()

        params = processor.get_transaction_parameters(cart, mock_request)

        assert f'TXN-{cart.id}' in params['transaction_id']
        assert params['amount'] == float(cart.total)
        assert 'order_reference' in params


class TestProcessorPaymentView:
    """Test payment view generation."""

    def test_payment_view_returns_response(self, cart, mock_request):
        """Test that payment_view returns an HttpResponse."""
        processor = CustomPaymentProcessor()

        response = processor.payment_view(cart, mock_request)

        assert isinstance(response, HttpResponse)
        assert response.status_code == 200
        assert 'text/html' in response['Content-Type']

    def test_payment_view_without_request_raises_error(self, cart):
        """Test that payment_view without request raises error."""
        processor = CustomPaymentProcessor()

        with pytest.raises(GatewayError, match='Request is required'):
            processor.payment_view(cart, request=None)

    def test_payment_view_contains_gateway_url(self, cart, mock_request):
        """Test that payment view includes gateway URL in response."""
        processor = CustomPaymentProcessor()

        response = processor.payment_view(cart, mock_request)
        content = response.content.decode('utf-8')

        assert 'https://api.custompayment.example.com' in content


class TestProcessorMetadata:
    """Test payment method metadata generation."""

    def test_get_payment_method_metadata(self, cart):
        """Test metadata generation for frontend display."""
        processor = CustomPaymentProcessor()

        metadata = processor.get_payment_method_metadata(cart)

        assert 'slug' in metadata
        assert 'title' in metadata
        assert 'checkout_text' in metadata
        assert 'url' in metadata

        assert metadata['slug'] == 'custom_payment'
        assert metadata['title'] == 'Custom Payment Gateway'
        assert metadata['checkout_text'] == 'Pay with Custom Gateway'
        assert 'custom_payment' in metadata['url']
        assert str(cart.id) in metadata['url']


class TestProcessorIntegrationWithCart:
    """Test processor integration with cart system."""

    def test_processor_handles_empty_cart(self, db, user, mock_request):
        """Test processor behavior with empty cart."""
        empty_cart = Cart.objects.create(user=user, status=Cart.Status.PROCESSING)
        processor = CustomPaymentProcessor()

        params = processor.get_transaction_parameters(empty_cart, mock_request)

        assert params['amount'] == 0.0
        assert params['currency'] == 'SAR'

    def test_processor_handles_multiple_items(self, db, user, mock_request):
        """Test processor with cart containing multiple items."""
        from openedx.core.djangoapps.content.course_overviews.models import CourseOverview

        cart = Cart.objects.create(user=user, status=Cart.Status.PROCESSING)

        from opaque_keys.edx.keys import CourseKey
        for i in range(3):
            course = CourseOverview.objects.create(
                id=CourseKey.from_string(f'course-v1:Test+Product{i}+2025'),
                org='Test',
                display_name=f'Product {i}',
                course_image_url='',
            )
            item = CatalogueItem.objects.create(
                title=f'Product {i}',
                sku=f'SKU-{i}',
                type=CatalogueItem.ItemType.PAID_COURSE,
                item_ref_id=course.id,
                price=Decimal('50.00'),
                currency='SAR'
            )
            CartItem.objects.create(
                cart=cart,
                catalogue_item=item,
                original_price=item.price,
                discount_amount=Decimal('0.00'),
                tax_amount=Decimal('0.00'),
                final_price=item.price,
            )

        processor = CustomPaymentProcessor()
        params = processor.get_transaction_parameters(cart, mock_request)

        assert params['amount'] == float(cart.total)
        assert params['amount'] == 150.0
        assert params['currency'] == 'SAR'


class TestProcessorErrorHandling:
    """Test error handling scenarios."""

    def test_processor_handles_missing_user_email(self, db, site, catalogue_item, request_factory, user):
        """Test processor handles cart without user email."""
        user.email = ''
        user.save()
        cart = Cart.objects.create(user=user, status=Cart.Status.PROCESSING)
        CartItem.objects.create(
            cart=cart,
            catalogue_item=catalogue_item,
            original_price=catalogue_item.price,
            discount_amount=Decimal('0.00'),
            tax_amount=Decimal('0.00'),
            final_price=catalogue_item.price,
        )

        request = request_factory.get('/payments/')
        request.site = site

        processor = CustomPaymentProcessor()

        params = processor.get_transaction_parameters(cart, request)
        assert 'customer_email' in params


class TestProcessorSetupPyIntegration:
    """Test that processor can be properly configured in setup.py."""

    def test_entry_point_format(self):
        """Test that processor follows correct entry point format."""
        entry_point_config = {
            'zeitlabs_payments.v1': [
                'custom_payment = tests.test_providers.test_new_processor_integration:CustomPaymentProcessor',
            ]
        }

        assert 'zeitlabs_payments.v1' in entry_point_config
        assert len(entry_point_config['zeitlabs_payments.v1']) > 0

        ep_string = entry_point_config['zeitlabs_payments.v1'][0]
        assert '=' in ep_string
        assert ':' in ep_string

        slug, module_path = ep_string.split('=')
        assert slug.strip() == 'custom_payment'
        assert 'CustomPaymentProcessor' in module_path


# =======================
# Integration Test
# =======================

class TestFullProcessorIntegration:
    """End-to-end integration tests for new processor."""

    def test_complete_payment_flow(self, cart, mock_request):
        """Test complete payment flow from initialization to view generation."""
        PROCESSORS['custom_payment'] = CustomPaymentProcessor

        try:
            processor = get_processor('custom_payment')
            assert processor is not None

            metadata = processor.get_payment_method_metadata(cart)
            assert metadata['slug'] == 'custom_payment'

            params = processor.get_transaction_parameters(cart, mock_request)
            assert params['amount'] > 0
            assert params['transaction_id'] is not None

            response = processor.payment_view(cart, mock_request)
            assert response.status_code == 200

        finally:
            PROCESSORS.pop('custom_payment', None)
