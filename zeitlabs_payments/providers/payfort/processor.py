"""Payfort processor"""

import logging
from typing import Any, Dict, Optional
from urllib.parse import urljoin

from django.conf import settings
from django.http import HttpRequest
from django.middleware.csrf import get_token
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from openedx.core.djangoapps.site_configuration import helpers as configuration_helpers

from zeitlabs_payments.models import Cart
from zeitlabs_payments.providers.base import BaseProcessor

from .helpers import get_signature

logger = logging.getLogger(__name__)


class PayFort(BaseProcessor):
    """
    PayFort payment processor.

    For reference, see: https://paymentservices-reference.payfort.com/docs/api/build/index.html
    """
    SLUG = 'payfort'
    CHECKOUT_TEXT = _('Checkout with Payfort credit card')
    NAME = 'Payfort'

    def __init__(self) -> None:
        """Initialize the PayFort processor."""
        self.access_code = settings.PAYFORT_SETTINGS['access_code']
        self.merchant_identifier = settings.PAYFORT_SETTINGS['merchant_identifier']
        self.request_sha_phrase = settings.PAYFORT_SETTINGS['request_sha_phrase']
        self.response_sha_phrase = settings.PAYFORT_SETTINGS['response_sha_phrase']
        self.sha_method = settings.PAYFORT_SETTINGS['sha_method']
        self.redirect_url = settings.PAYFORT_SETTINGS['redirect_url']
        self.return_url = urljoin(
            configuration_helpers.get_value('LMS_ROOT_URL', settings.ECOMMERCE_PUBLIC_URL_ROOT),
            reverse('zeitlabs_payments:payfort:return')
        )

    @classmethod
    def get_payment_method_metadata(cls, cart: Cart) -> dict:
        """
        Return metadata for frontend display for this payment processor.
        :return: Dictionary with 'slug', 'title', and 'url'
        """
        return {
            'slug': cls.SLUG,
            'title': cls.NAME,
            'checkout_text': cls.CHECKOUT_TEXT,
            'url': reverse('zeitlabs_payments:initiate-payment', kwargs={'provider': cls.SLUG, 'cart_id': cart.id})
        }

    def get_transaction_parameters_base(
        self,
        cart: Cart,
        request: HttpRequest
    ) -> dict:
        """
        Generate base parameters required for the transaction signature.

        :param cart: The cart instance related to the transaction.
        :param request: The HTTP request object.
        :return: A dictionary of transaction parameters.
        """
        base_params = super().get_transaction_parameters_base(cart, request)
        user_email = base_params.pop('user_email', None)
        order_reference = base_params.pop('order_reference', None)
        return {
            **base_params,
            'command': 'PURCHASE',
            'access_code': self.access_code,
            'merchant_identifier': self.merchant_identifier,
            'merchant_reference': order_reference,
            'customer_email': user_email,
            'return_url': self.return_url
        }

    def generate_signature(self, params: Dict[str, Any], sha_phrase: str = '') -> str:
        """
        Generate a signature for the transaction using provided or base parameters.
        """
        if not sha_phrase:
            sha_phrase = self.request_sha_phrase
        return get_signature(
            sha_phrase,
            self.sha_method,
            params,
        )

    def get_transaction_parameters(
        self,
        cart: Cart,
        request: Optional[HttpRequest] = None,
        use_client_side_checkout: bool = False,
        **kwargs: Any
    ) -> dict:
        """
        Build the required parameters for initiating a payment.

        :param cart: The cart details
        :param request: The HTTP request
        :param use_client_side_checkout: Client-side flag (currently unused)
        :param kwargs: Additional arguments
        :return: A dictionary of transaction parameters
        """
        transaction_parameters = self.get_transaction_parameters_base(cart, request)
        transaction_parameters.update({
            'signature': self.generate_signature(transaction_parameters),
            'payment_page_url': self.redirect_url,
            'csrfmiddlewaretoken': get_token(request),
        })

        return transaction_parameters
