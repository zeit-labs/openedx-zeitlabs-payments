"""Stripe payment processor."""
import logging
from typing import Any, Optional

import stripe
from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _

from zeitlabs_payments.exceptions import GatewayError
from zeitlabs_payments.helpers import get_currency
from zeitlabs_payments.models import Cart
from zeitlabs_payments.providers.base import BaseProcessor

logger = logging.getLogger(__name__)


class StripeProcessor(BaseProcessor):
    """Stripe payment processor for handling credit card and other Stripe payment methods."""

    SLUG = 'stripe'
    NAME = 'Stripe'
    CHECKOUT_TEXT = 'Pay with Credit Card'
    PAYMENT_INITIALIZATION_URL = 'zeitlabs_payments:stripe-checkout'

    def __init__(self) -> None:
        """Initialize Stripe processor with API keys from settings."""
        super().__init__()
        self.api_key = getattr(settings, 'STRIPE_SECRET_KEY', None)
        self.publishable_key = getattr(settings, 'STRIPE_PUBLISHABLE_KEY', None)
        self.webhook_secret = getattr(settings, 'STRIPE_WEBHOOK_SECRET', None)

        if not self.api_key:
            raise GatewayError('STRIPE_SECRET_KEY is not configured in settings.')

        stripe.api_key = self.api_key
        logger.info('Stripe processor initialized successfully.')

    def get_transaction_parameters(
        self,
        cart: Cart,
        request: Optional[HttpRequest] = None,
        use_client_side_checkout: bool = False,
        **kwargs: Any
    ) -> dict:
        """
        Generate transaction parameters required by Stripe.

        :param cart: The cart object
        :param request: The incoming request object
        :param use_client_side_checkout: Flag for client-side checkout
        :param kwargs: Additional parameters
        :return: A dictionary of transaction parameters including Stripe session
        """
        if not request:
            raise GatewayError('Request object is required for Stripe payment initialization.')

        base_params = self.get_transaction_parameters_base(cart, request)

        try:
            success_url = request.build_absolute_uri(
                '/payments/stripe/success/'
            ) + '?session_id={CHECKOUT_SESSION_ID}'
            cancel_url = request.build_absolute_uri(
                f'/payments/stripe/cancel/?cart_id={cart.id}'
            )

            line_items = []
            for item in cart.items.all():
                line_items.append({
                    'price_data': {
                        'currency': get_currency(cart).lower(),
                        'product_data': {
                            'name': item.catalogue_item.title,
                            'description': item.catalogue_item.description or '',
                        },
                        'unit_amount': int(item.final_price * 100),
                    },
                    'quantity': 1,
                })

            session = stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=line_items,  # type: ignore[arg-type]
                mode='payment',
                success_url=success_url,
                cancel_url=cancel_url,
                customer_email=cart.user.email if cart.user else base_params['user_email'],
                metadata={
                    'cart_id': str(cart.id),
                    'site_id': str(request.site.id),
                    'order_reference': base_params['order_reference'],
                },
                client_reference_id=str(cart.id),
            )

            logger.info(
                f'Stripe Checkout Session created for cart {cart.id}: {session.id}'
            )

            return {
                'session_id': session.id,
                'session_url': session.url,
                'publishable_key': self.publishable_key,
                **base_params,
            }

        except stripe.error.StripeError as e:
            logger.exception(f'Stripe API error while creating session for cart {cart.id}: {e}')
            raise GatewayError(f'Failed to create Stripe checkout session: {str(e)}') from e

    def payment_view(
        self,
        cart: Cart,
        request: Optional[HttpRequest] = None,
        use_client_side_checkout: bool = False,
        **kwargs: Any
    ) -> HttpResponse:
        """
        Render the payment redirection view for Stripe.

        :param cart: The cart details
        :param request: The HTTP request
        :param use_client_side_checkout: Client-side flag
        :param kwargs: Additional arguments
        :return: Rendered HTML response to redirect to Stripe Checkout
        """
        if not request:
            raise GatewayError('Request object is required for rendering payment view.')

        # Mark cart as payment pending
        if cart.status != Cart.Status.PAYMENT_PENDING:
            cart.status = Cart.Status.PAYMENT_PENDING
            cart.save(update_fields=['status'])
            logger.info(f'Cart {cart.id} marked as PAYMENT_PENDING.')

        transaction_params = self.get_transaction_parameters(
            cart=cart,
            request=request,
            use_client_side_checkout=use_client_side_checkout,
            **kwargs
        )

        context = {
            'cart': cart,
            'session_id': transaction_params['session_id'],
            'session_url': transaction_params['session_url'],
            'publishable_key': transaction_params['publishable_key'],
            'processor_name': self.NAME,
        }

        return render(request, 'zeitlabs_payments/stripe_checkout.html', context)

    def verify_webhook_signature(self, payload: bytes, sig_header: str) -> dict | None:
        """
        Verify Stripe webhook signature and return the event.

        :param payload: Raw webhook payload
        :param sig_header: Stripe signature header
        :return: Verified Stripe event dictionary
        :raises GatewayError: If signature verification fails
        """
        if not self.webhook_secret:
            logger.warning('STRIPE_WEBHOOK_SECRET not configured, skipping signature verification.')
            return None

        try:
            event = stripe.Webhook.construct_event(  # type: ignore[no-untyped-call]
                payload, sig_header, self.webhook_secret
            )
            logger.info(f'Webhook signature verified for event: {event["type"]}')
            return event
        except ValueError as e:
            logger.error(f'Invalid webhook payload: {e}')
            raise GatewayError('Invalid webhook payload') from e
        except stripe.error.SignatureVerificationError as e:
            logger.error(f'Webhook signature verification failed: {e}')
            raise GatewayError('Invalid webhook signature') from e

    def handle_successful_payment(self, session: dict, request: Any) -> Optional[Any]:
        """
        Handle successful payment from Stripe webhook.

        :param session: Stripe session object
        :param request: Django request object
        :return: Invoice if created, None otherwise
        """
        cart_id = session.get('metadata', {}).get('cart_id') or session.get('client_reference_id')
        site_id = session.get('metadata', {}).get('site_id')

        if not cart_id:
            logger.error('Cart ID not found in Stripe session metadata.')
            return None

        try:
            cart = self.get_cart(cart_id)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.exception(f'Failed to retrieve cart {cart_id}: {e}')
            return None

        payment_intent_id = session.get('payment_intent', '')
        amount_total = session.get('amount_total', 0) / 100
        currency = session.get('currency', 'usd').upper()
        payment_status = session.get('payment_status', '')

        transaction_status = (
            'success' if payment_status == 'paid' else payment_status
        )

        return self.process_payment_and_update_records(
            cart=cart,
            data=session,
            request=request,
            transaction_id=payment_intent_id,
            transaction_status=transaction_status,
            method='stripe',
            amount=str(amount_total),
            currency=currency,
            reason=f'Stripe payment: {payment_status}',
            site_id=int(site_id) if site_id else None,
            record_webhook_event=True,
        )
