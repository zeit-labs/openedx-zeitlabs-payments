"""Stripe payment views."""
import json
import logging

import stripe
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from zeitlabs_payments.exceptions import GatewayError, InvalidCartError
from zeitlabs_payments.models import AuditLog, Cart
from zeitlabs_payments.providers.stripe_payment.processor import StripeProcessor

logger = logging.getLogger(__name__)


class StripeCheckoutView(View):
    """View to initiate Stripe checkout."""

    def get(self, request: HttpRequest, cart_id: int) -> HttpResponse:
        """
        Initiate Stripe checkout for the given cart.

        :param request: HTTP request
        :param cart_id: Cart ID
        :return: Rendered checkout page or error response
        """
        try:
            processor = StripeProcessor()
            cart = processor.get_cart(cart_id)

            if not request.user.is_staff and cart.user != request.user:
                logger.warning(
                    f'User {request.user.id} attempted to access cart {cart_id} '
                    f'belonging to user {cart.user.id}'
                )
                return JsonResponse(
                    {'error': 'Unauthorized access to cart'},
                    status=403
                )

            if cart.status not in [Cart.Status.PROCESSING, Cart.Status.PAYMENT_PENDING]:
                logger.warning(
                    f'Invalid cart status for checkout: {cart.status} (cart {cart_id})'
                )
                return JsonResponse(
                    {'error': f'Cart is not available for checkout. Status: {cart.status}'},
                    status=400
                )

            return processor.payment_view(cart=cart, request=request)

        except InvalidCartError as e:
            logger.exception(f'Invalid cart error: {e}')
            return JsonResponse({'error': str(e)}, status=404)
        except GatewayError as e:
            logger.exception(f'Gateway error during Stripe checkout: {e}')
            return JsonResponse({'error': str(e)}, status=500)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.exception(f'Unexpected error during Stripe checkout: {e}')
            return JsonResponse({'error': 'Payment initialization failed'}, status=500)


class StripeSuccessView(View):
    """View to handle successful Stripe payment."""

    def get(self, request: HttpRequest) -> HttpResponse:
        """
        Handle successful payment callback from Stripe.

        :param request: HTTP request with session_id parameter
        :return: Success page or redirect
        """
        session_id = request.GET.get('session_id')

        if not session_id:
            logger.error('No session_id provided in success callback')
            return JsonResponse({'error': 'Invalid payment session'}, status=400)

        try:
            processor = StripeProcessor()

            session = stripe.checkout.Session.retrieve(session_id)
            metadata = session.metadata if session.metadata else {}
            cart_id = metadata.get('cart_id') or session.client_reference_id

            if not cart_id:
                logger.error(f'No cart_id in session metadata: {session_id}')
                return JsonResponse({'error': 'Invalid session data'}, status=400)

            cart = processor.get_cart(cart_id)

            logger.info(
                f'Success view: cart {cart_id} status={cart.status}, '
                f'session payment_status={session.payment_status}'
            )

            if cart.status != Cart.Status.PAID and session.payment_status == 'paid':
                logger.info(
                    f'Processing payment in success view for cart {cart_id} '
                    f'(webhook may not have fired yet)'
                )
                invoice = processor.handle_successful_payment(session, request)
                logger.info(f'Payment processed, invoice: {invoice}')

            context = {
                'cart': cart,
                'session_id': session_id,
                'payment_status': session.payment_status,
                'amount_total': (session.amount_total or 0) / 100,
                'currency': (session.currency or 'usd').upper(),
            }

            logger.info(
                f'Payment success view rendered for cart {cart_id}, session {session_id}'
            )

            return render(request, 'zeitlabs_payments/stripe_success.html', context)

        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.exception(f'Error in success view: {e}')
            return JsonResponse({'error': 'Failed to process payment confirmation'}, status=500)


class StripeCancelView(View):
    """View to handle cancelled Stripe payment."""

    def get(self, request: HttpRequest) -> HttpResponse:
        """
        Handle cancelled payment callback from Stripe.

        :param request: HTTP request with cart_id parameter
        :return: Cancel page
        """
        cart_id = request.GET.get('cart_id')

        context = {'cart_id': cart_id}

        if cart_id:
            try:
                processor = StripeProcessor()
                cart = processor.get_cart(cart_id)
                context['cart'] = cart

                AuditLog.log(
                    action=AuditLog.AuditActions.PAYMENT_CANCELLED,
                    cart=cart,
                    gateway='stripe',
                    context={'reason': 'User cancelled payment'}
                )

                logger.info(f'Payment cancelled for cart {cart_id}')
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.exception(f'Error retrieving cart in cancel view: {e}')

        return render(request, 'zeitlabs_payments/stripe_cancel.html', context)


@method_decorator(csrf_exempt, name='dispatch')
class StripeWebhookView(View):
    """View to handle Stripe webhooks."""

    def post(self, request: HttpRequest) -> HttpResponse:
        """
        Handle incoming Stripe webhook events.

        :param request: HTTP request with webhook payload
        :return: JSON response
        """
        payload = request.body
        sig_header = request.META.get('HTTP_STRIPE_SIGNATURE')

        if not sig_header:
            logger.error('No Stripe signature header found in webhook request')
            return JsonResponse({'error': 'Missing signature'}, status=400)

        try:
            processor = StripeProcessor()
            event = processor.verify_webhook_signature(payload, sig_header)

            if not event:
                logger.warning('Webhook signature verification skipped (no secret configured)')
                event = json.loads(payload)

            event_type = event['type']
            logger.info(f'Processing Stripe webhook event: {event_type}')

            if event_type == 'checkout.session.completed':
                session = event['data']['object']

                if session.get('payment_status') == 'paid':
                    invoice = processor.handle_successful_payment(session, request)
                    if invoice:
                        logger.info(
                            f'Successfully processed payment for session {session["id"]}, '
                            f'invoice {invoice.invoice_number}'
                        )
                    else:
                        logger.warning(
                            f'Payment processed but invoice not created for session {session["id"]}'
                        )
                else:
                    logger.info(
                        f'Checkout session completed but not paid: {session["id"]}, '
                        f'status: {session.get("payment_status")}'
                    )

            elif event_type == 'payment_intent.succeeded':
                logger.info('payment_intent.succeeded event received (handled via checkout.session.completed)')

            elif event_type == 'payment_intent.payment_failed':
                payment_intent = event['data']['object']
                logger.warning(
                    f'Payment failed for payment_intent: {payment_intent["id"]}, '
                    f'error: {payment_intent.get("last_payment_error", {}).get("message")}'
                )

            else:
                logger.info(f'Unhandled webhook event type: {event_type}')

            return JsonResponse({'status': 'success', 'event_type': event_type})

        except GatewayError as e:
            logger.exception(f'Gateway error processing webhook: {e}')
            return JsonResponse({'error': str(e)}, status=400)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.exception(f'Unexpected error processing webhook: {e}')
            return JsonResponse({'error': 'Webhook processing failed'}, status=500)
