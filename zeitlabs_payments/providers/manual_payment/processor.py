"""Base processor."""
import logging
from typing import Any

from django.db import transaction
from django.utils.translation import gettext_lazy as _

from zeitlabs_payments.helpers import get_currency
from zeitlabs_payments.models import AuditLog, Cart
from zeitlabs_payments.providers.base import BaseProcessor

logger = logging.getLogger(__name__)


class ManualPaymentProcessor(BaseProcessor):
    """Manual payment processor."""

    SLUG = 'manual'
    CHECKOUT_TEXT = ''
    NAME = 'Manual Payment'

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
        raise NotImplementedError

    def process_payment(
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
