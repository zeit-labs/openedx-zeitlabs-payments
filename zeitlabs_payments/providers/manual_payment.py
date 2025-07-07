"""Base processor."""
import logging
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from zeitlabs_payments.models import Cart, AuditLog, CartItem
from zeitlabs_payments.fulfillment import FULFILLMENT_HANDLERS
from zeitlabs_payments.providers.base import BaseProcessor

logger = logging.getLogger(__name__)


class ManualPaymentProcessor(BaseProcessor):
    SLUG = 'manual'
    CHECKOUT_TEXT = ''
    NAME = 'Manual Payment'

    def _create_paid_cart(self, user, catalog_item):
        cart = Cart.objects.create(user=user, status=Cart.Status.PAID)
        logger.info(f'Created new paid cart {cart.id} for user {user}')
        CartItem.objects.create(
            cart=cart,
            catalogue_item=catalog_item,
            original_price=catalog_item.price,
            final_price=catalog_item.price,
        )
        logger.info(f'Added catalogue item {catalog_item.sku} to cart {cart.id}')
        return cart

    def process_payment(self, user, course_catalog_item, request):
        """
        Creates a paid cart, invoice, fulfills the cart, and logs the action.
        :param user: User instance
        :param course_catalog_item: CatalogueItem instance
        :param request: DRF request object
        :return: dict with created_cart and created_invoice
        :raises Exception: if anything fails
        """
        with transaction.atomic():
            cart = self._create_paid_cart(user, course_catalog_item)
            invoice = self.create_invoice(cart, request, None)
            self.fulfill_cart(cart)
            AuditLog.log(
                action=AuditLog.AuditActions.CART_FULFIlED,
                cart=cart,
                gateway='manual_payment',
                context={}
            )
            logger.info(f"Successfully fulfilled cart {cart.id} and created invoice {invoice.id}.")
            return {
                'created_cart': cart.id,
                'created_invoice': invoice.invoice_number
            }