"""Base processor."""

import logging
from typing import Any, Optional

from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.utils import timezone
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _

from zeitlabs_payments.exceptions import CartFulfillmentError, GatewayError, InvalidCartError, InvoiceError
from zeitlabs_payments.helpers import (
    get_currency,
    get_language,
    get_merchant_reference,
    get_order_description,
    generate_invoice_number,
)
from zeitlabs_payments.models import Cart, CatalogueItem, Transaction, WebhookEvent, AuditLog, InvoiceItem, Invoice, CartItem
from zeitlabs_payments.fulfillment import FULFILLMENT_HANDLERS


logger = logging.getLogger(__name__)


class BaseProcessor:
    """Base class for all payment processors."""

    SLUG: str
    NAME: str
    CHECKOUT_TEXT: str
    PAYMENT_INITIALIZATION_URL: str

    def get_transaction_parameters(
        self,
        cart: Cart,
        request: Optional[HttpRequest] = None,
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

    def get_transaction_parameters_base(
        self,
        cart: Cart,
        request: HttpRequest
    ) -> dict:
        """
        Generate base parameters required for the transaction signature.
        """
        return {
            'language': get_language(request),
            'order_reference': get_merchant_reference(request.site.id, cart),
            'amount': int(round(cart.total * 100, 0)),
            'currency': get_currency(cart),
            'user_email': cart.user.email,
            'order_description': get_order_description(cart),
        }

    def payment_view(
        self,
        cart: Cart,
        request: Optional[HttpRequest] = None,
        use_client_side_checkout: bool = False,
        **kwargs: Any
    ) -> HttpResponse:
        """
        Render the payment redirection view.

        :param cart: The cart details
        :param request: The HTTP request
        :param use_client_side_checkout: Client-side flag (currently unused)
        :param kwargs: Additional arguments
        :return: Rendered HTML response to redirect to the payment gateway
        """
        transaction_parameters = self.get_transaction_parameters(
            cart=cart,
            request=request,
            use_client_side_checkout=use_client_side_checkout,
            **kwargs,
        )
        return render(request, f'zeitlabs_payments/processors/{self.SLUG}.html', {
            'transaction_parameters': transaction_parameters,
        })

    def get_cart(self, cart_id: str | int) -> Cart:
        """
        Retrieve a Cart instance from a string or integer cart ID.

        :param cart_id: The cart ID, as string or integer.
        :return: Cart instance if found.
        :raises GatewayError: If the cart does not exist or the ID is invalid.
        """
        try:
            cart_id_int = int(cart_id)
        except (ValueError, TypeError) as exc:
            raise InvalidCartError(f'Invalid cart ID: {cart_id}') from exc

        try:
            return Cart.objects.get(id=cart_id_int)
        except Cart.DoesNotExist as exc:
            raise InvalidCartError(f'Cart with ID {cart_id} does not exist.') from exc

    def get_site(self, site_id: str | int) -> Site:
        """
        Retrieve a Site instance from a string or integer site ID.

        :param site_id: The site ID, as string or integer.
        :return: Site instance if found.
        :raises GatewayError: If the site does not exist or the ID is invalid.
        """
        try:
            site_id_int = int(site_id)
        except (ValueError, TypeError) as exc:
            raise GatewayError(f'Invalid site ID: {site_id}') from exc

        try:
            return Site.objects.get(id=site_id_int)
        except Site.DoesNotExist as exc:
            raise GatewayError(f'Site with ID {site_id} does not exist.') from exc

    def create_invoice(self, cart: Cart, request: Any, transaction_record: Transaction = None) -> Invoice:
        """
        Create an invoice for the given cart.

        :param cart: The cart to create an invoice for.
        :raises InvoiceError: If the cart is not in PAID status.
        :return: The created Invoice instance.
        """
        if cart.status != Cart.Status.PAID:
            raise InvoiceError(
                f'Cannot create invoice: Cart {cart.id} is in status "{cart.status}", expected status "{Cart.Status.PAID}".'
            )
        invoice = Invoice.objects.create(
            invoice_number=generate_invoice_number(request),
            cart=cart,
            status=Invoice.InvoiceStatus.PAID,
            total=cart.total,
            discount_total=cart.discount_total,
            currency=get_currency(cart),
            paid_at=timezone.now(),
            related_transaction=transaction_record
        )
        for item in cart.items.all():
            InvoiceItem.objects.create(
                invoice=invoice,
                cart_item=item,
                original_price=item.original_price,
                discount_amount=item.discount_amount,
                price=item.final_price,
            )

        logger.info(
            f'Invoice (ID: {invoice.id}) with status "{Invoice.InvoiceStatus.PAID}" '
            f'successfully generated for cart {cart.id}.'
        )
        return invoice

    def handle_payment(  # pylint: disable= too-many-positional-arguments
        self,
        cart: Cart,
        user: get_user_model,
        transaction_status: str,
        transaction_id: str,
        method: str,
        amount: str,
        currency: str,
        reason: str,
        response: dict = None,
    ) -> Transaction:
        """
        Retrieve a Site instance from a string or integer site ID.

        :param site_id: The site ID, as string or integer.
        :return: Site instance if found.
        :raises GatewayError: If the site does not exist or the ID is invalid.
        """
        if Transaction.objects.filter(gateway_transaction_id=transaction_id).exists():
            logger.warning(f'Duplicate transaction detected while cart: {cart.id} processing.')
            AuditLog.log(
                action=AuditLog.AuditActions.DUPLICATE_TRANSACTION,
                cart=cart,
                gateway=self.SLUG,
                context={
                    'transaction_id': transaction_id,
                    'cart_status': cart.status
                }
            )
            return
        transaction_record = Transaction.objects.create(
            cart=cart,
            type=Transaction.TransactionType.PAYMENT,
            status=transaction_status,
            gateway=self.SLUG,
            gateway_transaction_id=transaction_id,
            method=method,
            amount=amount,
            currency=currency,
            response=response,
            reason=reason,
            initiator_user=user,
            created_at=now(),
        )
        logger.info(f'Transaction recorded successfully: {transaction_record.id}')

        WebhookEvent.objects.create(
            gateway=self.SLUG,
            event_type='direct-feedback',
            payload=response,
            related_transaction=transaction_record
        )

        cart.status = Cart.Status.PAID
        cart.save(update_fields=['status'])
        AuditLog.log(
            action=AuditLog.AuditActions.CART_STATUS_UPDATED,
            cart=cart,
            context={
                'old_status': Cart.Status.PROCESSING,
                'new_status': Cart.Status.PAID,
            }
        )
        logger.info(f'Cart marked as PAID: {cart.id}')
        return transaction_record

    def fulfill_cart(self, cart: Cart) -> None:
        """
        Fulfill the cart by processing each item using registered handlers.

        :param cart: Cart instance containing items to process.
        :raises CartFulfillmentError: If any error occurs during fulfillment or if no handler is found.
        :return: None
        """
        for item in cart.items.all():
            logger.debug(f'Processing item {item.id} of type {item.catalogue_item.type} in cart {cart.id}.')
            handler = FULFILLMENT_HANDLERS.get(item.catalogue_item.type)

            if not handler:
                logger.error(
                    f'No fulfillment handler registered for item type: {item.catalogue_item.type} '
                    f'for item {item.catalogue_item.id} in cart {cart.id}'
                )
                AuditLog.log(
                    action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR,
                    cart=cart,
                    context={
                        'item_id': item.id,
                        'catalogue_item_id': item.catalogue_item.id,
                        'sku': item.catalogue_item.sku,
                    }
                )
                raise CartFulfillmentError(f'Unsupported catalogue item type: {item.catalogue_item.type}')

            handler.fulfill(cart, item, self.SLUG)
