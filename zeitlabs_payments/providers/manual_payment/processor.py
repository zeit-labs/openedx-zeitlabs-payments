"""Base processor."""
import logging
from typing import Any, Optional

from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.utils.translation import gettext_lazy as _
from django.shortcuts import render

from zeitlabs_payments.helpers import get_currency
from zeitlabs_payments.models import AuditLog, Cart, ManualManagement
from zeitlabs_payments.providers.base import BaseProcessor

logger = logging.getLogger(__name__)


class ManualPaymentProcessor(BaseProcessor):
    """Manual payment processor."""

    SLUG = 'manual'
    CHECKOUT_TEXT = 'Manual Payment'
    NAME = 'Manual Payment'
    TEMPLATE_NAME = 'zeitlabs_payments/manual_payment.html'
    def get_transaction_parameters(
        self,
        cart: Cart,
        request: Any = None,
        use_client_side_checkout: bool = False,
        **kwargs: Any
    ) -> dict:
        """
        Generate transaction parameters required by the processor.

        :param cart: The cart object/dictionary
        :param request: The incoming request object
        :param use_client_side_checkout: Flag for client-side checkout
        :param kwargs: Additional parameters
        :return: A dictionary of transaction parameters
        """
        transaction_parameters = self.get_transaction_parameters_base(cart, request)
        transaction_parameters.update({
            'payment_page_url': '/api/payment/v1/manual/',
            'user_id': cart.user.id
        })

        return transaction_parameters

    def process_payment(  # pylint: disable= too-many-positional-arguments
        self,
        request: Any,
        cart: Cart,
        transaction_id: str,
        transaction_status: str,
        reason: str = '',
    ) -> dict:
        """
        Creates a paid cart, invoice, fulfills the cart, and logs the action.
        :param user: User instance
        :param course_catalog_item: CatalogueItem instance
        :param request: DRF request object
        :return: dict with created_cart and created_invoice
        :raises Exception: if anything fails
        """
        with transaction.atomic():
            transaction_record = self.handle_payment(
                cart=cart,
                user=request.user,
                transaction_status=transaction_status,
                transaction_id=transaction_id,
                method=self.SLUG,
                amount=str(cart.total),
                currency=get_currency(cart),
                reason=reason,
                response=None,
                record_webhook_event=False
            )
            cart.refresh_from_db()
            invoice = self.create_invoice(cart, request, transaction_record)
            self.fulfill_cart(cart)
            AuditLog.log(
                action=AuditLog.AuditActions.CART_FULFILLED,
                cart=cart,
                gateway=self.SLUG,
                context={}
            )
            logger.info(f'Successfully fulfilled cart {cart.id} and created invoice {invoice.id}.')
            return {
                'created_cart': cart.id,
                'created_invoice': invoice.invoice_number
            }

    def payment_view(
        self,
        cart: Cart,
        request: Optional[HttpRequest] = None,
        use_client_side_checkout: bool = False,
        **kwargs: Any,
    ) -> HttpResponse:
        """
        Render the payment redirection view.
        """
        try:
            transaction_parameters = self.get_transaction_parameters(
                cart=cart,
                request=request,
                use_client_side_checkout=use_client_side_checkout,
                **kwargs,
            )
            ManualManagement.objects.create(
                    cart=cart,
                    user=cart.user,
                    status=ManualManagement.ManualManagementType.WAITING,
                    )
        except Exception:  # pylint: disable=broad-exception-caught
            return render(request, 'zeitlabs_payments/payment_error.html')
        return render(
            request,
            self.TEMPLATE_NAME,
            {'transaction_parameters': transaction_parameters},
        )
