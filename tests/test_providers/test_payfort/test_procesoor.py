"""Payfort processor tests."""
import hashlib
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.http import HttpRequest

from zeitlabs_payments.models import Cart, CatalogueItem
from zeitlabs_payments.providers.payfort.processor import PayFort

User = get_user_model()


@pytest.fixture
def fake_request():
    """mock request fixture"""
    request = MagicMock(spec=HttpRequest)
    request.build_absolute_uri.return_value = 'https://example.com'
    request.site = Site.objects.get(domain='example.com')
    return request


@pytest.fixture
def cart():
    """mock cart fixture"""
    item = CatalogueItem.objects.get(sku='custom-sku-1')
    user_cart = Cart.objects.create(user=User.objects.get(id=3), status=Cart.Status.PROCESSING)
    user_cart.items.create(
        catalogue_item=item,
        original_price=item.price,
        final_price=item.price
    )
    return user_cart


@pytest.mark.django_db
class TestPayFortProcessor:
    """PayfortProcessor Tests"""

    @patch('zeitlabs_payments.providers.payfort.processor.configuration_helpers.get_value')
    @patch('zeitlabs_payments.providers.payfort.processor.reverse')
    def test_init_sets_attributes(self, mock_reverse, mock_get_value):
        """Test PayFort __init__ properly sets attributes from settings and URL helpers."""
        mock_reverse.return_value = '/payfort/return/'
        mock_get_value.return_value = 'https://lms.example.com'
        processor = PayFort()
        assert processor.access_code == 'test-code'
        assert processor.merchant_identifier == 'test-identifier'
        assert processor.request_sha_phrase == 'test-request-phrase'
        assert processor.response_sha_phrase == 'test-response-phrase'
        assert processor.sha_method == 'SHA-256'
        assert processor.redirect_url == 'https://fake_payfort.com'
        assert processor.return_url == 'https://lms.example.com/payfort/return/'

    def test_get_payment_method_metadata_returns_expected(self, cart):  # pylint: disable=redefined-outer-name
        """Test get_payment_method_metadata returns correct dict with slug, title, checkout_text, and URL."""
        result = PayFort.get_payment_method_metadata(cart)
        assert result['slug'] == PayFort.SLUG
        assert result['title'] == PayFort.NAME
        assert 'checkout_text' in result
        assert 'url' in result
        assert str(cart.id) in result['url']

    def test_get_transaction_parameters_base_merges_params(
        self, fake_request, cart
    ):  # pylint: disable=redefined-outer-name
        """Test get_transaction_parameters_base correctly merges base params and adds PayFort-specific keys."""
        processor = PayFort()
        processor.access_code = 'AC123'
        processor.merchant_identifier = 'MID456'
        processor.return_url = 'https://return.url'

        result = processor.get_transaction_parameters_base(cart, fake_request)

        assert result['command'] == 'PURCHASE'
        assert result['access_code'] == 'AC123'
        assert result['merchant_identifier'] == 'MID456'
        assert result['merchant_reference'] == f'{cart.id}-{fake_request.site.id}'
        assert result['customer_email'] == 'user3@example.com'
        assert result['return_url'] == 'https://return.url'
        assert result['language'] == 'en'
        assert result['amount'] == 5000
        assert result['currency'] == cart.items.all()[0].catalogue_item.currency
        assert 'order_reference' not in result
        assert 'user_email' not in result

    def test_generate_signature_uses_default_sha_phrase(self):
        """Test generate_signature calls get_signature with default sha_phrase if none provided."""
        processor = PayFort()
        processor.request_sha_phrase = 'abcd11'
        processor.sha_method = 'SHA-256'
        params = {'foo': 'bar'}

        params_copy = params.copy()
        sorted_keys = sorted(params_copy, key=lambda arg: arg.lower())
        sorted_dict = {key: params_copy[key] for key in sorted_keys}
        result_string = f"abcd11{''.join(f'{key}={value}' for key, value in sorted_dict.items())}abcd11"
        expected_signature = hashlib.sha256(result_string.encode()).hexdigest()

        generated_signature = processor.generate_signature(params)
        assert generated_signature == expected_signature

    def test_generate_signature_uses_custom_sha_phrase(self):
        """Test generate_signature calls get_signature with default sha_phrase if none provided."""
        processor = PayFort()
        processor.request_sha_phrase = 'abcd11'
        processor.sha_method = 'SHA-256'
        params = {'foo': 'bar'}

        params_copy = params.copy()
        sorted_keys = sorted(params_copy, key=lambda arg: arg.lower())
        sorted_dict = {key: params_copy[key] for key in sorted_keys}
        result_string = f"xyz1234{''.join(f'{key}={value}' for key, value in sorted_dict.items())}xyz1234"
        expected_Signature = hashlib.sha256(result_string.encode()).hexdigest()

        generated_signature = processor.generate_signature(params, 'xyz1234')
        assert generated_signature == expected_Signature

    @patch('zeitlabs_payments.providers.payfort.processor.get_token')
    @patch.object(PayFort, 'generate_signature')
    def test_get_transaction_parameters_returns_expected(  # pylint: disable=redefined-outer-name
        self,
        mock_generate_signature,
        mock_get_token,
        cart,
        fake_request,
    ):
        """Test get_transaction_parameters returns parameters including signature, payment URL, and CSRF token."""
        mock_generate_signature.return_value = 'signature123'
        mock_get_token.return_value = 'csrf1234'
        keys_to_check = [
            'command', 'access_code', 'merchant_identifier', 'merchant_reference', 'customer_email', 'return_url',
            'language', 'amount', 'currency'
        ]
        processor = PayFort()
        processor.redirect_url = 'https://redirect.url'
        result = processor.get_transaction_parameters(cart, fake_request)
        assert result['signature'] == 'signature123'
        assert result['payment_page_url'] == 'https://redirect.url'
        assert result['csrfmiddlewaretoken'] == 'csrf1234'
        for key in keys_to_check:
            assert key in result
