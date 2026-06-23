"""Base processor."""

import logging
from typing import Any, Optional

from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.db import transaction as db_transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.timezone import now

from zeitlabs_payments.cart_handler import CART_HANDLER
from zeitlabs_payments.exceptions import (
    CartFulfillmentError,
    DuplicateTransactionError,
    GatewayError,
    InvalidCartError,
    InvoiceError,
)
from zeitlabs_payments.helpers import (
    generate_invoice_number,
    get_currency,
    get_language,
    get_merchant_reference,
    get_order_description,
)
from zeitlabs_payments.models import AuditLog, Cart, Invoice, InvoiceItem, Transaction, WebhookEvent

logger = logging.getLogger(__name__)


class BaseProcessor:
    """Base class for all payment processors."""

    SLUG: str
    NAME: str
    CHECKOUT_TEXT: str
    PAYMENT_INITIALIZATION_URL: str
    TEMPLATE_NAME: str

    TRANSACTION_STATUS_PENDING = 'pending'
    TRANSACTION_STATUS_SUCCESS = 'success'

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
            'url': reverse('zeitlabs_payments:initiate-payment', kwargs={'provider': cls.SLUG, 'cart_id': cart.id}),
            'disabled': None,
        }

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
            'amount': int(round(cart.total, 0)),
            'currency': get_currency(cart),
            'user_email': cart.user.email,
            'order_description': get_order_description(cart),
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
        except Exception:  # pylint: disable=broad-exception-caught
            return render(request, 'zeitlabs_payments/payment_error.html')
        return render(
            request,
            self.TEMPLATE_NAME,
            {'transaction_parameters': transaction_parameters},
        )

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

    def get_cart_from_reference(self, reference: str) -> Optional[Cart]:
        """Get cart from reference which should be in format siteID-cartID."""
        try:
            _, cart_id = reference.split('-')
            return self.get_cart(cart_id)
        except (ValueError, InvalidCartError):
            AuditLog.log(
                action=AuditLog.AuditActions.RESPONSE_INVALID_CART,
                cart=None,
                gateway=self.SLUG,
                context={
                    'cart_status': (
                        'None, unable to retrieve cart from merchant reference '
                        f'id {reference}'
                    ),
                    'required_cart_state': Cart.Status.PROCESSING
                }
            )
            return None

    def get_site_from_reference(self, reference: str) -> Optional[Site]:
        """Get site from reference which should be in format siteID-cartID."""
        try:
            site_id_str, _ = reference.split('-')
            site_id = int(site_id_str)
            return self.get_site(site_id)
        except (ValueError, GatewayError):
            logger.error(f'Payfort Error! merchant_reference: {reference} is invalid. Unable to extract site.')
            return None

    def create_invoice(self, cart: Cart, request: Any, transaction_record: Transaction = None) -> Invoice:
        """
        Create an invoice for the given cart.

        :param cart: The cart to create an invoice for.
        :raises InvoiceError: If the cart is not in PAID status.
        :return: The created Invoice instance.
        """
        if cart.status != Cart.Status.PAID:
            raise InvoiceError(
                f'Cannot create invoice: Cart {cart.id} is in status "{cart.status}", '
                f'expected status "{Cart.Status.PAID}".'
            )
        invoice = Invoice.objects.create(
            invoice_number=generate_invoice_number(request),
            cart=cart,
            status=Invoice.InvoiceStatus.PAID,
            gross_total=cart.gross_total,
            discount_total=cart.discount_total,
            tax_total=cart.tax_total,
            total=cart.total,
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
                tax_amount=item.tax_amount,
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
        record_webhook_event: bool = True,
    ) -> Transaction:
        """
        Handle payment processing and create a transaction record.

        :param cart: The cart being paid for.
        :param user: The user making the payment.
        :param transaction_status: Status of the transaction (e.g., 'success', 'failed').
        :param transaction_id: Unique identifier from the payment gateway.
        :param method: Payment method used (e.g., 'credit_card', 'bank_transfer').
        :param amount: Payment amount as string.
        :param currency: Currency code (e.g., 'SAR', 'USD').
        :param reason: Reason or description for the transaction.
        :param response: Optional raw response from payment gateway.
        :param record_webhook_event: Whether to record webhook event.
        :return: Transaction instance.
        :raises DuplicateTransactionError: If transaction with same ID already exists.
        """
        if Transaction.objects.filter(gateway_transaction_id=transaction_id).exists():
            logger.warning(f'Duplicate transaction detected while cart: {cart.id} processing.')
            raise DuplicateTransactionError(f'Transaction already exist with given transaction_id: {transaction_id}')
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

        if record_webhook_event:
            WebhookEvent.objects.create(
                gateway=self.SLUG,
                event_type='direct-feedback',
                payload=response,
                related_transaction=transaction_record
            )
        old_status = cart.status
        cart.status = Cart.Status.PAID
        cart.save(update_fields=['status'])

        AuditLog.log(
            action=AuditLog.AuditActions.CART_STATUS_UPDATED,
            cart=cart,
            context={
                'old_status': old_status,
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
            handler = CART_HANDLER.get(item.catalogue_item.type)

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

            handler.fulfill(item, self.SLUG)

    def process_payment_and_update_records(  # pylint: disable= too-many-positional-arguments
        self,
        cart: Cart,
        data: dict,
        request: Any,
        transaction_id: str,
        transaction_status: str,
        method: str,
        amount: str,
        currency: str,
        reason: str,
        site_id: Optional[int] = None,
        record_webhook_event: bool = True,
    ) -> Optional[Invoice]:
        """
        Generic method to handle payment, invoice creation, and fulfillment.

        :param cart: Cart instance
        :param data: Raw payment data from gateway
        :param request: Django request object
        :param transaction_id: Gateway transaction ID
        :param transaction_status: Payment status string
        :param method: Payment method used
        :param amount: Transaction amount
        :param currency: Currency code
        :param reason: Gateway-provided reason/description
        :param site_id: Optional site ID for context logging
        :param record_webhook_event: Whether to record webhook payload
        :return: Created Invoice instance or None
        """
        if cart.status not in [Cart.Status.PROCESSING, Cart.Status.PAYMENT_PENDING]:
            AuditLog.log(
                action=AuditLog.AuditActions.RESPONSE_INVALID_CART,
                cart=cart,
                gateway=self.SLUG,
                context={
                    'cart_status': cart.status,
                    'required_cart_state': f'{Cart.Status.PROCESSING} or {Cart.Status.PAYMENT_PENDING}'}
            )
            logger.warning(
                f'Cart {cart.id} in invalid status: {cart.status} '
                '(expected: PROCESSING or PAYMENT_PENDING ).'
            )
            return None

        if site_id:
            try:
                site = Site.objects.get(id=site_id)
                request.site = site
            except (Site.DoesNotExist, ValueError):
                logger.warning(
                    f'Site with id: {site_id} is invalid and does not exist.'
                )
                return None

        try:
            with db_transaction.atomic():
                logger.info(f'Recording payment transaction for cart {cart.id}.')
                transaction_record = self.handle_payment(
                    cart=cart,
                    user=request.user if hasattr(request, 'user') and request.user.is_authenticated else None,
                    transaction_status=transaction_status,
                    transaction_id=transaction_id,
                    method=method,
                    amount=amount,
                    currency=currency,
                    reason=reason,
                    response=data,
                    record_webhook_event=record_webhook_event,
                )

        except DuplicateTransactionError:
            AuditLog.log(
                action=AuditLog.AuditActions.DUPLICATE_TRANSACTION,
                cart=cart,
                gateway=self.SLUG,
                context={
                    'transaction_id': transaction_id,
                    'cart_status': cart.status,
                },
            )
            logger.warning(f'Duplicate transaction for cart {cart.id}, ID {transaction_id}')
            return None

        except Exception as e:  # pylint: disable=broad-exception-caught
            AuditLog.log(
                action=AuditLog.AuditActions.TRANSACTION_ROLLED_BACK,
                cart=cart,
                gateway=self.SLUG,
                context={
                    'transaction_id': transaction_id,
                    'cart_id': cart.id,
                    'site_id': site_id,
                },
            )
            logger.exception(f'Payment transaction failed and rolled back for cart {cart.id}: {e}')
            return None

        try:
            cart.refresh_from_db()
            invoice = self.create_invoice(cart, request, transaction_record)
            self.fulfill_cart(cart)
            AuditLog.log(
                action=AuditLog.AuditActions.CART_FULFILLED,
                cart=cart,
                gateway=self.SLUG,
                context={},
            )
            logger.info(f'Successfully fulfilled cart {cart.id} and created invoice {invoice.id}.')
            return invoice

        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.exception(f'Failed to fulfill cart {cart.id} or to create invoice: {e}')
            return None
