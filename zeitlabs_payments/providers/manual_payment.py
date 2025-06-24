"""Base processor."""
import logging
from typing import Any

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from zeitlabs_payments.models import AuditLog, Cart, CartItem, CatalogueItem
from zeitlabs_payments.providers.base import BaseProcessor

logger = logging.getLogger(__name__)


class ManualPaymentProcessor(BaseProcessor):
    """Manual payment processor."""

    SLUG = 'manual'
    CHECKOUT_TEXT = ''
    NAME = 'Manual Payment'

    def _create_paid_cart(self, user: get_user_model, catalog_item: CatalogueItem) -> Cart:
        """
        Create a new paid cart for the given user and add the specified catalogue item to it.

        This method:
        - Creates a Cart instance with status set to 'PAID'.
        - Adds a CartItem linked to the provided catalogue item.
        - Logs actions for tracking and debugging.

        :param user: The user who owns the cart.
        :param catalog_item: The catalogue item to add to the new paid cart.
        :return: The newly created Cart instance with the item added.
        """
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

    def process_payment(self, user: get_user_model, course_catalog_item: CatalogueItem, request: Any) -> dict:
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
            logger.info(f'Successfully fulfilled cart {cart.id} and created invoice {invoice.id}.')
            return {
                'created_cart': cart.id,
                'created_invoice': invoice.invoice_number
            }
