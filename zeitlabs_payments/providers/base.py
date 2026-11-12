"""Base processor."""

import logging
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db import transaction as db_transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.timezone import now

from zeitlabs_payments.cart_handler import CART_HANDLER
from zeitlabs_payments.constants import DEFAULT_PAYMENT_SESSION_DURATION, ISO_4217_NUMERIC_TO_ALPHA
from zeitlabs_payments.exceptions import (
    CartFulfillmentError,
    DuplicateTransactionError,
    GatewayError,
    InvalidCartError,
    InvoiceError,
)
from zeitlabs_payments.helpers import (
    describe_error,
    generate_invoice_number,
    get_blocking_carts,
    get_currency,
    get_language,
    get_merchant_reference,
    get_order_description,
)
from zeitlabs_payments.helpers import get_settings as get_plugin_settings
from zeitlabs_payments.invoice_info import collect_info, get_learner_info, get_organization_info
from zeitlabs_payments.models import AuditLog, Cart, CatalogueItem, Invoice, InvoiceItem, Transaction
from zeitlabs_payments.redaction import mask_unmasked_card_numbers
from zeitlabs_payments.validators import canonical_currency

logger = logging.getLogger(__name__)


class BaseProcessor:
    """Base class for all payment processors."""

    SLUG: str
    NAME: str
    CHECKOUT_TEXT: str
    PAYMENT_INITIALIZATION_URL: str
    TEMPLATE_NAME: str
    SETTINGS_ROOT_KEY: str = ''

    # The processor's return view -- where its gateway sends the learner back, and the waiting page is rendered -- and
    # its status view, which the waiting page polls, as dotted paths: the views import the processor themselves. The
    # plugin mounts them for every processor at payment/v1/processor/<SLUG>/return/ and .../status/.
    RETURN_VIEW: Optional[str] = None
    STATUS_VIEW: Optional[str] = None
    # The query parameter the gateway's redirect back carries its order id in -- a name the gateway chooses. The return
    # view reads it from here, and a learner sent back to the waiting page later is sent with it.
    GATEWAY_ORDER_ID_PARAM: Optional[str] = None

    contract_version = 1

    TRANSACTION_STATUS_PENDING = 'pending'
    TRANSACTION_STATUS_SUCCESS = 'success'
    TRANSACTION_STATUS_FAILED = 'failed'

    def is_hidden_for(self, request: HttpRequest) -> bool:
        """
        Return True when this processor should not be offered to the learner.

        The decision belongs to the processor, not to the page that lists
        it: a processor that is not configured for the current site (or is
        otherwise unavailable) returns True and every consumer of the
        processor list goes through ``registry.available_processors`` to
        respect it.

        The base implementation returns False. Processors override this
        method; consumers must never read a ``disabled``-style key from
        ``get_payment_method_metadata`` because that places the decision
        in presentation code.
        """
        return False

    def get_settings(self) -> dict:
        """
        Return this processor's configuration from the site-aware settings layer (C1).

        The processor declares the root key of its settings block (for example ``'TABADUL_SETTINGS'``) in the
        ``SETTINGS_ROOT_KEY`` class attribute; the value is resolved through ``ZeitLabsPluginSettings`` so it can
        be overridden per site. This method is the only sanctioned read path for processor configuration: a
        processor must never read ``django.conf.settings`` directly, because that silently makes the gateway
        single-site. Returns an empty dict when the processor declares no root key.
        """
        root_key = self.SETTINGS_ROOT_KEY
        if not root_key:
            return {}
        return get_plugin_settings().get_by_root_key(root_key) or {}

    def map_currency(self, value: str) -> str:
        """
        Translate a currency value received from the gateway into the form the plugin records.

        The gateway's answer is the input, not the answer: a processor may speak a different dialect of currency
        codes than the plugin. The base implementation looks the value up in the standard ISO 4217 numeric
        register (``ISO_4217_NUMERIC_TO_ALPHA``) and returns the alpha form, so ``'978'`` becomes ``'EUR'`` and
        ``'368'`` becomes ``'IQD'`` without per-processor knowledge. Anything not in the register -- a non-ISO
        numeric code, a string that is not three digits, or an alpha code that is already canonical -- passes
        through unchanged; the write path validates the result with ``canonical_currency`` and a value that is
        not a supported ISO 4217 currency lands on a recorded-but-not-reconciled row, never on a settled one.

        A processor that needs a domain restriction on top of the standard translation (one that settles only in
        IQD, for example) overrides this method to call ``super().map_currency`` first and then check the
        result, raising when the gateway returns something the processor is not configured to settle in.
        """
        return ISO_4217_NUMERIC_TO_ALPHA.get(value, value)

    @staticmethod
    def find_blocking_cart(cart: Cart) -> Optional[Cart]:
        """
        Return another of the learner's carts that blocks buying any item of the given cart, or None.

        Asked before settling a payment its learner may have made again elsewhere: a cart that expired, whose learner
        was free to buy its items again, or an unreconciled one staff are about to accept. Settling it while another
        cart pays for, or is paying for, the same item would charge the learner twice, so the payment is held for a
        person to refund instead. See ``get_blocking_carts`` for which carts block.

        :param cart: The cart whose payment is about to settle.
        """
        for item in cart.items.select_related('catalogue_item'):
            blocking_cart = (
                get_blocking_carts(cart.user, item.catalogue_item).exclude(pk=cart.pk).order_by('-updated_at').first()
            )
            if blocking_cart is not None:
                return blocking_cart
        return None

    @staticmethod
    def find_held_item(cart: Cart) -> Optional[CatalogueItem]:
        """
        Return an item of the cart its learner already holds, so its payment would buy nothing new, or None.

        Asked before settling a payment, since the learner can come to hold the item while the payment is at the
        gateway: staff enrol them by hand, say, or every course of a bundle becomes held. Checkout refuses such an item,
        so settling it would sell nothing; the payment is held for a person to accept or refund instead. The cart
        itself is left out, as the purchase being settled. Each item type's handler says what holding it means.

        :param cart: The cart whose payment is about to settle.
        """
        for item in cart.items.select_related('catalogue_item'):
            handler = CART_HANDLER.get(item.catalogue_item.type)
            if handler is not None and handler.holds_item(cart.user, item.catalogue_item, ignored_cart=cart):
                return item.catalogue_item
        return None

    def reconcile_payment(self, response: Optional[dict], cart: Cart) -> None:
        """
        Check that the money the gateway reports taken is what the cart expected: its total, in its currency.

        Called by ``handle_payment`` once the payment is known to be the cart's own, beside ``map_currency``. Anything
        it raises is a verification outcome, never a refusal to record: the transaction row is written all the same,
        annotated with what was raised, and the cart is not settled. Only the processor can make the comparison, since
        how a gateway reports an amount -- in minor units, as a formatted string -- is its own.

        :param response: The gateway's answer the payment is recorded from.
        :param cart: The cart the payment is for.
        :raises GatewayError: When the money taken is not what the cart expected. Always, here: a processor that does
            not check the money it was paid has every payment recorded and held for a person to reconcile, rather
            than settled unchecked.
        """
        raise GatewayError(f'{type(self).__name__} does not reconcile payments, so none is settled')

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

        These are the common parameters every processor consumes to build its transaction payload: the
        request language, a site-scoped order reference, the amount in whole units, the ISO currency code,
        the learner's email, and a human-readable order description.
        """
        return {
            'language': get_language(request),
            'order_reference': get_merchant_reference(request.site.id, cart),
            'amount': cart.total,
            'currency': get_currency(cart),
            'user_email': cart.user.email,
            'order_description': get_order_description(cart),
        }

    def find_gateway_order_id(self, cart: Cart) -> Optional[str]:  # pylint: disable=unused-argument
        """
        Ask the gateway for the order it holds for the cart, and return its id, or None when there is none to find.

        Asked when a learner comes back to buy a course whose cart was handed off but never had its order recorded --
        a gateway that opens its order only when the learner pays. A gateway that can be searched by the merchant
        reference answers here. None here, so a processor that does not override it is treated as one that cannot be
        searched, and the cart is judged by its age instead.

        :param cart: The cart whose order is looked for.
        """
        return None

    def get_return_url(self) -> str:
        """Return the path of the processor's return view, which its gateway is to send the learner back to."""
        return reverse('zeitlabs_payments:processor-return', args=[self.SLUG])

    def get_session_end(self, cart: Cart) -> Optional[datetime]:
        """
        Return when the cart's payment session ends -- after which the gateway takes no payment for it -- or None.

        Asked of a cart whose payment cannot be resumed, when its learner comes back to buy the course again. A session
        that has ended expires the cart, so the learner may buy again; one still running makes them wait until it ends;
        None, a session whose end cannot be told, makes them wait without a time, and never expires the cart. A
        processor may judge by time, or by a status the gateway reports for the order -- answering now when the gateway
        says the session is over.

        The default is ``DEFAULT_PAYMENT_SESSION_DURATION`` after the hand-off: long, since expiring too soon risks a
        second charge while waiting too long only delays a learner.

        :param cart: The cart whose payment is in flight.
        """
        return cart.updated_at + DEFAULT_PAYMENT_SESSION_DURATION

    def get_gateway_order_id(self, transaction_parameters: dict) -> Optional[str]:  # pylint: disable=unused-argument
        """
        Return the id the gateway gave the order it opened for the hand-off, or None when there is none to give.

        The plugin keeps the id on the cart, which is how the payment can be found again without the learner's browser.
        A gateway that opens its order only when the learner pays has none yet; its id is recorded when the learner
        comes back. None here, so a processor that does not override it is treated as one of those.

        :param transaction_parameters: What ``get_transaction_parameters`` returned for the hand-off.
        """
        return None

    def payment_view(
        self,
        cart: Cart,
        request: Optional[HttpRequest] = None,
        use_client_side_checkout: bool = False,
        **kwargs: Any,
    ) -> HttpResponse:
        """
        Render the payment redirection view.

        Whatever ``get_transaction_parameters`` raises is left to the caller, which closes the cart: a hand-off that
        failed must not be recorded as one that redirected the learner. The order the gateway opened is recorded on the
        cart when ``get_gateway_order_id`` names it, and a failure to record it is left to the caller the same way.
        """
        transaction_parameters = self.get_transaction_parameters(
            cart=cart,
            request=request,
            use_client_side_checkout=use_client_side_checkout,
            **kwargs,
        )
        gateway_order_id = self.get_gateway_order_id(transaction_parameters)  # pylint: disable=assignment-from-none
        if gateway_order_id:
            cart.record_gateway_order(self.SLUG, gateway_order_id)
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
                processor=self,
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

    def create_invoice(self, cart: Cart, transaction_record: Transaction, request: Any) -> Invoice:
        """
        Create an invoice for the given cart.

        The invoice is issued for the site the cart was paid on, which its payment origin names, and the request must
        be serving that site: the learner and organization info stored on the invoice are read for it, and the
        receipt's QR code is addressed on the origin. A cart with no origin, or a request serving another site, is
        refused rather than issued an invoice that names a site nobody paid on: a printed receipt cannot be reissued.
        An info function failing to give its info refuses the invoice for the same reason: see ``collect_info``.

        :param cart: The cart to create an invoice for.
        :param transaction_record: The settled payment the invoice bills for. Required, and already persisted:
            an invoice is the record of a payment that happened, so there is no invoice without one to point at.
        :param request: The request the payment settled under, whose site the info is read for.
        :raises InvoiceError: If the cart is not in PAID status or has no payment origin, the request carries no site
            or another site than the cart's origin, or an info function answers with what an invoice cannot store.
            Whatever an info function raises is raised as it is.
        :return: The created Invoice instance.
        """
        if cart.status != Cart.Status.PAID:
            raise InvoiceError(
                f'Cannot create invoice: Cart {cart.id} is in status "{cart.status}", '
                f'expected status "{Cart.Status.PAID}".'
            )
        paying_site = getattr(request, 'site', None)
        if not paying_site:
            raise InvoiceError(
                f'Cannot create invoice: the request that paid for cart {cart.id} carries no site, '
                'so there is no site to issue it for.'
            )
        if not cart.payment_origin:
            raise InvoiceError(
                f'Cannot create invoice: cart {cart.id} has no payment origin, so there is no address to issue its '
                'receipt on.'
            )
        if urlsplit(cart.payment_origin).netloc != paying_site.domain:
            raise InvoiceError(
                f'Cannot create invoice: cart {cart.id} was paid on {cart.payment_origin}, but the request settling it '
                f'serves {paying_site.domain}.'
            )
        invoice = Invoice.objects.create(
            invoice_number=generate_invoice_number(),
            cart=cart,
            status=Invoice.InvoiceStatus.PAID,
            gross_total=cart.gross_total,
            total=cart.total,
            currency=get_currency(cart),
            paid_at=timezone.now(),
            related_transaction=transaction_record,
            learner_info=collect_info(get_learner_info, cart.user, paying_site),
            organization_info=collect_info(get_organization_info, paying_site),
        )
        for item in cart.items.all():
            InvoiceItem.objects.create(
                invoice=invoice,
                cart_item=item,
                original_price=item.original_price,
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
        Persist the payment record first, verify second, and never write less on failure.

        The transaction row and the raw gateway response are written **before** verification runs: once a gateway
        has moved money, refusing to record it would destroy the evidence. A row whose verification fails is
        annotated with the failure and kept -- it has every column a settled row has, plus the annotation -- and
        the cart is deliberately left un-paid. The raw currency value the gateway sent is what the row holds
        first; only a row that passes verification receives the canonical ISO 4217 form. Verification is the
        currency's translation by ``map_currency``, the processor's ``reconcile_payment``, for an expired cart that
        no other cart blocks its items (``find_blocking_cart``), and the status the gateway reported.

        :param cart: The cart being paid for.
        :param user: The user making the payment.
        :param transaction_status: Status of the transaction (e.g., 'success', 'failed').
        :param transaction_id: Unique identifier from the payment gateway. Recorded verbatim; duplicate detection keys
            on this column, so callers must pass the gateway's own identifier, not the merchant order reference the
            gateway echoes back (Hyperpay's ``data['id']``, Tabadul's ``data['OrderNumber']``).
        :param method: Payment method used (e.g., 'credit_card', 'bank_transfer', the processor ``SLUG`` when the
            gateway does not identify a sub-method). Must be a non-empty, processor-agnostic label.
        :param amount: Payment amount as string. This is the transmitted amount -- what the learner was told they would
            be charged -- in the cart's currency, full precision. Hyperpay verifies the gateway echo against the
            transmitted value; Tabadul converts the cart total to fils when it builds the order payload and passes the
            cart total here.
        :param currency: Currency code (e.g., 'SAR', 'USD'). This is whatever the gateway answered with -- an ISO 4217
            alpha code for Hyperpay, a numeric code such as ``'368'`` for Tabadul. Verified through
            ``map_currency`` (the standard ISO 4217 numeric register) and ``canonical_currency`` after the row is
            persisted; only a verified row is rewritten to the canonical alpha form.
        :param reason: Reason or description for the transaction. Any unmasked card number it
            contains is replaced by its masked form before the row is written.
        :param response: Optional raw response from payment gateway. Stored verbatim as the audit trail except
            for card numbers: any unmasked number found anywhere in it is replaced by its masked form before the
            row is written, while the masked block the gateways already send is kept as-is. The settled columns
            are derived from it via the mapping above, not read back from it.
        :return: Transaction instance. A row whose status is not ``TRANSACTION_STATUS_SUCCESS`` (either the gateway
            reported a non-success status or verification failed) is returned as recorded, annotated, with the cart
            left in its pre-payment state.
        :raises DuplicateTransactionError: If transaction with same ID already exists.
        """
        with db_transaction.atomic():
            verification_error = None
            try:
                canonical_currency_code = canonical_currency(self.map_currency(currency))
            except Exception as exc:  # pylint: disable=broad-exception-caught
                # Anything a processor's map_currency or the plugin's canonical_currency raises is a verification
                # outcome, never a write failure; it annotates the row below.
                verification_error = exc
                canonical_currency_code = None
            if verification_error is None:
                try:
                    self.reconcile_payment(response, cart)
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    # Like map_currency above: whatever the processor raises annotates the row, never stops it.
                    verification_error = exc
            if verification_error is None and cart.status == Cart.Status.EXPIRED:
                blocking_cart = self.find_blocking_cart(cart)
                if blocking_cart is not None:
                    verification_error = ValueError(
                        f'cart {cart.id} expired and cart {blocking_cart.id} ({blocking_cart.status}) holds its items',
                    )
            if verification_error is None and transaction_status.lower() != self.TRANSACTION_STATUS_SUCCESS:
                verification_error = ValueError(
                    f'Gateway reported transaction status {transaction_status!r}, expected '
                    f'{self.TRANSACTION_STATUS_SUCCESS!r}'
                )
            if verification_error is None:
                held_item = self.find_held_item(cart)
                if held_item is not None:
                    verification_error = ValueError(
                        f'the learner already holds {held_item.sku}, so the payment would buy nothing new',
                    )

            response = mask_unmasked_card_numbers(response)
            reason = mask_unmasked_card_numbers(reason)
            try:
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
            except IntegrityError as exc:
                raise DuplicateTransactionError(
                    f'Transaction already exist with given transaction_id: {transaction_id}'
                ) from exc
            logger.info(f'Transaction recorded successfully: {transaction_record.id}')

            if verification_error is not None:
                if transaction_record.status.lower() == self.TRANSACTION_STATUS_SUCCESS:
                    # The gateway reported success but our verification failed. The row must never read as settled,
                    # so the misleading status is overwritten; the reason column carries the whole story.
                    transaction_record.status = self.TRANSACTION_STATUS_FAILED
                transaction_record.reason = f'{reason} | not reconciled: {verification_error}'
                transaction_record.save(update_fields=['status', 'reason'])
                AuditLog.log(
                    action=AuditLog.AuditActions.TRANSACTION_NOT_RECONCILED,
                    cart=cart,
                    processor=self,
                    context={
                        'transaction_id': transaction_id,
                        'cart_id': cart.id,
                        'error': str(verification_error),
                        'cart_status': cart.status,
                    },
                )
                logger.warning(
                    f'Transaction {transaction_id} recorded but not reconciled for cart {cart.id}: {verification_error}'
                )
                return transaction_record

            transaction_record.currency = canonical_currency_code
            transaction_record.save(update_fields=['currency'])

            cart.change_status(Cart.Status.PAID, processor=self)
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
                    processor=self,
                    context={
                        'item_id': item.id,
                        'catalogue_item_id': item.catalogue_item.id,
                        'sku': item.catalogue_item.sku,
                    }
                )
                raise CartFulfillmentError(f'Unsupported catalogue item type: {item.catalogue_item.type}')

            handler.fulfill(item, self)

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
    ) -> Optional[Invoice]:
        """
        Record the payment first, settle second, and never roll the record back.

        ``handle_payment`` persists the transaction row (and the raw gateway response) before it verifies anything,
        so a verification failure annotates a record that already exists instead of deciding whether one is created.
        This method only proceeds to the invoice and fulfillment when the returned row is settled; otherwise it
        returns ``None`` and leaves the recorded, annotated row behind.

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
        :return: Created Invoice instance or None
        """
        if cart.status not in [Cart.Status.PROCESSING, Cart.Status.PAYMENT_PENDING, Cart.Status.EXPIRED]:
            AuditLog.log(
                action=AuditLog.AuditActions.RESPONSE_INVALID_CART,
                cart=cart,
                processor=self,
                context={
                    'cart_status': cart.status,
                    'required_cart_state': (
                        f'{Cart.Status.PROCESSING}, {Cart.Status.PAYMENT_PENDING} or {Cart.Status.EXPIRED}'
                    ),
                },
            )
            logger.warning(
                f'Cart {cart.id} in invalid status: {cart.status} '
                '(expected: PROCESSING, PAYMENT_PENDING or EXPIRED).'
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
            )

        except DuplicateTransactionError:
            AuditLog.log(
                action=AuditLog.AuditActions.DUPLICATE_TRANSACTION,
                cart=cart,
                processor=self,
                context={
                    'transaction_id': transaction_id,
                    'cart_status': cart.status,
                },
            )
            logger.warning(f'Duplicate transaction for cart {cart.id}, ID {transaction_id}')
            return None

        except Exception as e:  # pylint: disable=broad-exception-caught
            # A genuine write failure: nothing was committed, so a rollback note is still honest here.
            AuditLog.log(
                action=AuditLog.AuditActions.TRANSACTION_ROLLED_BACK,
                cart=cart,
                processor=self,
                context={
                    'transaction_id': transaction_id,
                    'cart_id': cart.id,
                    'site_id': site_id,
                },
            )
            logger.exception(f'Payment transaction failed to record for cart {cart.id}: {e}')
            return None

        if transaction_record.status.lower() != self.TRANSACTION_STATUS_SUCCESS:
            # The row exists and is annotated (see handle_payment); the cart is deliberately not paid, nothing is
            # fulfilled and no invoice is issued. The record is the evidence.
            logger.warning(
                f'Transaction {transaction_id} for cart {cart.id} is not settled '
                f'(status: {transaction_record.status}); skipping invoice and fulfillment.'
            )
            return None

        paying_site = getattr(request, 'site', None)
        if not cart.payment_origin and paying_site:
            # A cart handed off before carts recorded their origin: it was paid on the site settling it. Recorded before
            # the invoice and outside its transaction, so the origin is kept even if the invoice fails.
            try:
                cart.record_payment_origin(f'{request.scheme}://{paying_site.domain}')
            except ValidationError:
                # Left without an origin, the cart is refused its invoice next, and that failure is recorded.
                logger.exception(f'Cart {cart.id} could not record the origin it was paid on.')
        return self.issue_invoice_and_fulfill(cart, transaction_record, request)

    def issue_invoice_and_fulfill(self, cart: Cart, transaction_record: Transaction, request: Any) -> Optional[Invoice]:
        """
        Issue the invoice of a paid cart and fulfil it, both or neither, and return the invoice, or None on failure.

        The step that follows a settled payment: run by settlement, and by staff retrying a paid cart left without an
        invoice. The invoice and the enrolments are written in one transaction, so a failure leaves the cart paid with
        neither. The failure is recorded on the audit trail after the rollback, where it survives, together with the
        courses of a bundle whose enrolment went through before it.

        :param cart: The paid cart.
        :param transaction_record: The settled payment the invoice bills for.
        :param request: The request the step runs under, serving the site the cart was paid on.
        :return: The invoice, or None when issuing it or fulfilling the cart failed.
        """
        try:
            with db_transaction.atomic():
                cart.refresh_from_db()
                invoice = self.create_invoice(cart, transaction_record, request)
                self.fulfill_cart(cart)
                AuditLog.log(
                    action=AuditLog.AuditActions.CART_FULFILLED,
                    cart=cart,
                    processor=self,
                    context={},
                )
            logger.info(f'Successfully fulfilled cart {cart.id} and created invoice {invoice.id}.')
            return invoice

        except Exception as e:  # pylint: disable=broad-exception-caught
            AuditLog.log(
                action=AuditLog.AuditActions.INVOICE_CREATION_FAILED,
                cart=cart,
                processor=self,
                context={'cart_id': cart.id, 'error': describe_error(e)},
            )
            bundle_sku = getattr(e, 'bundle_sku', None)
            if isinstance(e, CartFulfillmentError) and bundle_sku:
                # The settlement transaction has rolled back, so the enrollment rows and every
                # audit entry written inside it are gone. The enrollment call's non-transactional
                # side effects may still have landed, so the record of which member courses
                # completed is written here, after the rollback, where it survives.
                AuditLog.log(
                    action=AuditLog.AuditActions.BUNDLE_FULFILLMENT_FAILED,
                    cart=cart,
                    processor=self,
                    context={
                        'sku': bundle_sku,
                        'cart_id': cart.id,
                        'landed_courses': ', '.join(getattr(e, 'landed_courses', [])) or 'none',
                        'held_courses': ', '.join(getattr(e, 'held_courses', [])) or 'none',
                        'failed_course': getattr(e, 'failed_course', None),
                    },
                )
            logger.exception(f'Failed to fulfill cart {cart.id} or to create invoice: {e}')
            return None
