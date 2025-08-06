"""Payfort Views"""

import logging
from typing import Any

from django.contrib.sites.models import Site
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from rest_framework.permissions import IsAuthenticated

from zeitlabs_payments.exceptions import DuplicateTransactionError, GatewayError, InvalidCartError
from zeitlabs_payments.models import AuditLog, Cart, Invoice
from zeitlabs_payments.providers.payfort.exceptions import PayFortBadSignatureException, PayFortException
from zeitlabs_payments.providers.payfort.helpers import SUCCESS_STATUS, verify_response_format, verify_signature
from zeitlabs_payments.providers.payfort.processor import PayFort

logger = logging.getLogger(__name__)


class PayFortBaseView(View):
    """Payfort Base View."""

    @property
    def payment_processor(self) -> PayFort:
        return PayFort()

    @property
    def cart(self) -> Cart | None:
        """Retrieve the cart from the database."""
        if not self.request or not self.request.POST.get('merchant_reference'):
            return None

        reference = self.request.POST.get('merchant_reference')
        try:
            _, cart_id = reference.split('-', 1)
            return self.payment_processor.get_cart(cart_id)
        except (ValueError, InvalidCartError):
            AuditLog.log(
                action=AuditLog.AuditActions.RESPONSE_INVALID_CART,
                cart=None,
                gateway=self.payment_processor.SLUG,
                context={'cart_status': 'None', 'required_cart_state': Cart.Status.PROCESSING}
            )
            logger.error(f'Payfort Error! merchant_reference: {reference} is invalid. Unable to get cart.')
            return None

    @property
    def site(self) -> Site | None:
        """Retrieve the site from the database."""
        if not self.request or not self.request.POST.get('merchant_reference'):
            return None

        reference = self.request.POST.get('merchant_reference')
        try:
            site_id, _ = reference.split('-', 1)
            return self.payment_processor.get_site(site_id)
        except (ValueError, GatewayError):
            logger.error(f'Payfort Error! merchant_reference: {reference} is invalid. Unable to extract site.')
            return None

    @method_decorator(csrf_exempt)
    def dispatch(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        """Dispatch the request to the appropriate handler."""
        return super().dispatch(request, *args, **kwargs)


class PayFortReturnView(PayFortBaseView):
    """
    Payfort redirection view after payment.
    """

    template_name = 'zeitlabs_payments/wait_feedback.html'
    MAX_ATTEMPTS = 24
    WAIT_TIME = 5000

    def post(self, request: Any) -> HttpResponse:
        """Handle the POST request from PayFort after processing payment page."""
        logger.debug(f'request.user: {request.user}, is_authenticated: {request.user.is_authenticated}')

        data = request.POST.dict()
        try:
            verify_signature(
                self.payment_processor.response_sha_phrase,
                self.payment_processor.sha_method,
                data,
            )
        except PayFortBadSignatureException:
            AuditLog.log(
                action=AuditLog.AuditActions.BAD_RESPONSE_SIGNATURE,
                cart=self.cart,
                gateway=self.payment_processor.SLUG,
                context={'data': data}
            )
            logger.error('Invalid signature received in response from payfort.')
            return render(request, 'zeitlabs_payments/payment_error.html')

        if data.get('status') == SUCCESS_STATUS:
            try:
                verify_response_format(data)
            except PayFortException as exc:
                logger.error(f'Payfort response validation failed: {str(exc)}')
                return render(request, 'zeitlabs_payments/payment_error.html')

            data['ecommerce_transaction_id'] = data['fort_id']
            data['ecommerce_status_url'] = reverse('zeitlabs_payments:payfort:status')
            data['ecommerce_error_url'] = reverse(
                'zeitlabs_payments:payment-error',
                args=[data['fort_id']]
            )
            data['ecommerce_success_url'] = reverse(
                'zeitlabs_payments:payment-success',
                args=[data['fort_id']]
            )
            data['ecommerce_max_attempts'] = self.MAX_ATTEMPTS
            data['ecommerce_wait_time'] = self.WAIT_TIME
            return render(request=request, template_name=self.template_name, context=data)

        logger.error(
            f"Payfort payment failed! with merchant_reference: {data.get('merchant_reference')}, status:"
            f" {data.get('status')} and response_code: {data.get('response_code')}"
        )
        return render(request, 'zeitlabs_payments/payment_error.html')


class PayfortFeedbackView(PayFortBaseView):
    """
    Callback endpoint for PayFort to notify about payment status.
    """

    def post(self, request: Any) -> HttpResponse:
        """Handle the POST request from PayFort for payment status or feedback"""
        data = request.POST.dict()
        AuditLog.log(
            action=AuditLog.AuditActions.RECEIVED_RESPONSE,
            cart=self.cart,
            gateway=self.payment_processor.SLUG,
            context={'data': data}
        )

        if not self.cart or not self.site:
            logger.warning(
                'PayFort response can not be processed further, unable to retrieve '
                'cart or site from given reference.'
            )
            return HttpResponse(status=400)

        try:
            verify_signature(
                self.payment_processor.response_sha_phrase,
                self.payment_processor.sha_method,
                data,
            )
        except PayFortBadSignatureException:
            logger.error('Invalid signature received in response from PayFort.')
            AuditLog.log(
                action=AuditLog.AuditActions.BAD_RESPONSE_SIGNATURE,
                cart=self.cart,
                gateway=self.payment_processor.SLUG,
                context={'data': data}
            )
            return HttpResponse(status=400)

        if data.get('status') != SUCCESS_STATUS:
            logger.warning(f"PayFort payment unsuccessful. Status: {data.get('status')}, Data: {data}")
            return HttpResponse(status=200)

        verify_response_format(data)

        if self.cart.status != Cart.Status.PROCESSING:
            AuditLog.log(
                action=AuditLog.AuditActions.RESPONSE_INVALID_CART,
                cart=self.cart,
                gateway=self.payment_processor.SLUG,
                context={'cart_status': self.cart.status, 'required_cart_state': Cart.Status.PROCESSING}
            )
            logger.warning(f'Cart {self.cart.id} in invalid status: {self.cart.status} (expected: PROCESSING).')
            return HttpResponse(status=200)

        try:
            with transaction.atomic():
                logger.info(f'Recording payment transaction for cart {self.cart.id}.')
                transaction_record = self.payment_processor.handle_payment(
                    cart=self.cart,
                    user=request.user if request.user.is_authenticated else None,
                    transaction_status=data['response_message'],
                    transaction_id=data['fort_id'],
                    method=data['payment_option'],
                    amount=data['amount'],
                    currency=data['currency'],
                    reason=data['acquirer_response_message'],
                    response=data
                )
        except DuplicateTransactionError:
            AuditLog.log(
                action=AuditLog.AuditActions.DUPLICATE_TRANSACTION,
                cart=self.cart,
                gateway=self.payment_processor.SLUG,
                context={
                    'transaction_id': data['fort_id'],
                    'cart_status': self.cart.status
                }
            )
            return HttpResponse(status=200)
        except Exception as e:  # pylint: disable=broad-exception-caught
            AuditLog.log(
                action=AuditLog.AuditActions.TRANSACTION_ROLLED_BACK,
                cart=self.cart,
                gateway=self.payment_processor.SLUG,
                context={
                    'transaction_id': data['fort_id'],
                    'cart_id': self.cart.id,
                    'site_id': self.site.id
                }
            )
            logger.error(f'Payment transaction failed and rolled back for cart {self.cart.id}: {str(e)}')
            return HttpResponse(status=200)

        try:
            self.cart.refresh_from_db()
            invoice = self.payment_processor.create_invoice(self.cart, request, transaction_record)
            self.payment_processor.fulfill_cart(self.cart)
            AuditLog.log(
                action=AuditLog.AuditActions.CART_FULFIlED,
                cart=self.cart,
                gateway=self.payment_processor.SLUG,
                context={}
            )
            logger.info(f'Successfully fulfilled cart {self.cart.id} and created invoice {invoice.id}.')
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(f'Failed to fulfill cart {self.cart.id} or to create invoice: {str(e)}')
        return HttpResponse(status=200)


class PayFortStatusView(PayFortBaseView):
    """View to check transaction and payment status."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Any) -> JsonResponse:
        """Verify transaction status."""
        params = {
            'transaction_id': request.GET.get('transaction_id'),
            'merchant_reference': request.GET.get('merchant_reference')
        }
        missing_fields = [key for key, value in params.items() if not value]
        if missing_fields:
            field_names = ', '.join(missing_fields).replace('_', ' ').title()
            logger.error(f'Payfort Error! {field_names} is required to verify payment status.')
            return JsonResponse(
                data={'error': f'{field_names} is required to verify payment status.'},
                status=400
            )

        try:
            _, cart_id = params['merchant_reference'].split('-', 1)
            cart = PayFort().get_cart(cart_id)
        except (ValueError, InvalidCartError):
            AuditLog.log(
                action=AuditLog.AuditActions.RESPONSE_INVALID_CART,
                cart=None,
                gateway=self.payment_processor.SLUG,
                context={'cart_status': 'None', 'required_cart_state': Cart.Status.PROCESSING}
            )
            return JsonResponse(
                {
                    'error': f"merchant_reference: {params['merchant_reference']} is invalid. Unable to retrieve cart."
                }, status=404)

        status_code = {
            Cart.Status.PAID: 200,
            Cart.Status.PROCESSING: 204,
        }.get(cart.status, 404)

        if status_code == 200:
            invoice = Invoice.objects.filter(
                cart=cart,
                status=Invoice.InvoiceStatus.PAID,
                related_transaction__gateway_transaction_id=params['transaction_id']).first()
            if invoice:
                return JsonResponse(
                    {
                        'invoice': invoice.invoice_number,
                        'invoice_url': reverse(
                            'zeitlabs_payments:invoice',
                            args=[invoice.invoice_number]
                        )
                    }, status=200)

            error_msg = f'Cart is in {Cart.Status.PAID} status, unable to retrieve invoice with given transaction id.'
            logger.error(error_msg)
            data = {'error': error_msg}
            status_code = 204
        else:
            data = {'error': f'cart is in status: {cart.status}.'}

        return JsonResponse(
            data=data,
            status=status_code
        )
