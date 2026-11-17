"""What staff do by hand to resolve a cart whose payment did not end in an invoice."""
import logging
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Iterator, Optional
from urllib.parse import urlsplit

from django.contrib.sites.models import Site
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db import transaction as db_transaction

from zeitlabs_payments.exceptions import ConfigurationError, CurrencyMismatchError, ResolutionError
from zeitlabs_payments.helpers import get_currency
from zeitlabs_payments.models import AuditLog, Cart, Invoice, Transaction
from zeitlabs_payments.providers.base import BaseProcessor
from zeitlabs_payments.providers.registry import PROCESSORS, get_processor
from zeitlabs_payments.validators import validate_price_against_currency

logger = logging.getLogger(__name__)

PAID_WITHOUT_INVOICE = 'a paid cart without an invoice'
UNRECONCILED = 'an unreconciled cart'


def retry_fulfillment(cart_id: int, request: Any) -> Invoice:
    """
    Issue the invoice of a paid cart left without one and fulfil it, as its settlement would have, and return it.

    Run once the cause of the failure is fixed. The cart is locked and checked again for an invoice first, so of two
    staff retrying it at once, the second waits and is then refused instead of issuing a second invoice. The step runs
    under the processor of the cart's payment and on the site the cart was paid on, which is what the invoice is issued
    for, with the same enrolment checks as settlement. The cart stays paid whether the retry succeeds or not.

    :param cart_id: The cart to retry.
    :param request: The staff member's request. Its site is swapped for the cart's while the step runs.
    :raises ResolutionError: When the cart cannot be retried, saying why, or when the retry failed again, whose cause
        its audit trail records.
    :return: The invoice issued.
    """
    with db_transaction.atomic():
        cart = _lock_cart(cart_id, {Cart.Status.PAID: PAID_WITHOUT_INVOICE})
        payment = _get_settled_payment(cart)
        with _serving_site(request, _get_paying_site(cart)):
            processor = _set_up_processor(cart, payment.gateway)
            AuditLog.log(
                action=AuditLog.AuditActions.FULFILLMENT_RETRIED,
                cart=cart,
                processor=processor,
                context={'staff_username': request.user.username, 'transaction_id': payment.gateway_transaction_id},
            )
            invoice = processor.issue_invoice_and_fulfill(cart, payment, request)
    # Raised only once the transaction is committed, so the retry and its failure stay on the audit trail.
    if invoice is None:
        raise ResolutionError(f'Cart {cart_id} failed again; its audit trail says why.')
    return invoice


def accept_payment(cart_id: int, request: Any, gateway_transaction_id: str, amount: Decimal) -> Invoice:
    """
    Accept an unreconciled cart's payment, as staff confirmed it with the gateway, then invoice and fulfil the cart.

    The plugin cannot read the money taken in a generic way -- one gateway's answer holds it in minor units, and some
    unreconciled carts have no payment row at all -- so staff copy the gateway's transaction id and the amount taken
    from its dashboard. Only the cart total is accepted: a payment that took another amount stays unreconciled.

    The cart is locked, and refused when another cart blocks buying its items -- paid, unreconciled or still at the
    gateway -- since accepting could sell them twice. The cart's row for that transaction is made successful, or a
    successful one written when there is none, and the cart moves to paid, its trail keeping the amount and currency
    the row held before next to the ones accepted.
    The invoice and enrolment then follow as on a settlement, on the site the cart was paid on.
    A failure there leaves the cart paid without an invoice, for staff to retry.

    :param cart_id: The cart whose payment is accepted.
    :param request: The staff member's request. Its site is swapped for the cart's while the payment is settled.
    :param gateway_transaction_id: The gateway's id for the payment, as its dashboard shows it.
    :param amount: The amount the gateway took, as its dashboard shows it, in the cart's currency.
    :raises ResolutionError: When the payment cannot be accepted, saying why, or when it was accepted but the invoice
        and enrolment then failed, whose cause the audit trail records.
    :return: The invoice issued.
    """
    gateway_transaction_id = (gateway_transaction_id or '').strip()
    if not gateway_transaction_id:
        raise ResolutionError(f'Accepting the payment of cart {cart_id} needs the id the gateway gave it.')
    with db_transaction.atomic():
        cart = _lock_cart(cart_id, {Cart.Status.UNRECONCILED: UNRECONCILED})
        if amount != cart.total:
            raise ResolutionError(
                f'Cart {cart.id} totals {cart.total}, but the gateway took {amount}; a payment of another amount than '
                'the cart total cannot be accepted.',
            )
        blocking_cart = BaseProcessor.find_blocking_cart(cart)
        if blocking_cart is not None:
            raise ResolutionError(
                f'Cart {blocking_cart.id} ({blocking_cart.status}) holds the items of cart {cart.id}, so accepting '
                'this payment could sell them twice; record its refund instead.',
            )
        if not cart.gateway:
            raise ResolutionError(f'Cart {cart.id} was handed off without its gateway recorded.')
        with _serving_site(request, _get_paying_site(cart)):
            processor = _set_up_processor(cart, cart.gateway)
            currency = _get_cart_currency(cart)
            payment, recorded = _record_accepted_payment(
                cart, processor, gateway_transaction_id, amount, currency, request.user,
            )
            cart.change_status(
                Cart.Status.PAID,
                processor=processor,
                action=AuditLog.AuditActions.PAYMENT_ACCEPTED,
                context={
                    'transaction_id': gateway_transaction_id,
                    'amount': amount,
                    'currency': currency,
                    'staff_username': request.user.username,
                    'recorded': recorded,
                },
            )
            invoice = processor.issue_invoice_and_fulfill(cart, payment, request)
    # Raised only once the transaction is committed: the payment stays accepted, and its failure on the audit trail.
    if invoice is None:
        raise ResolutionError(
            f'The payment of cart {cart_id} was accepted, but its invoice and enrollment failed; its audit trail says '
            'why, and they can be retried.',
        )
    return invoice


def record_refund(
    cart_id: int, staff_user: Any, refund_reference: str, amount: Optional[Decimal] = None,
) -> Transaction:
    """
    Record the refund, made in the gateway's dashboard, of a paid cart left without an invoice or an unreconciled one.

    Nothing here calls the gateway: the refund has already been made there. What is recorded is a refund transaction,
    under the reference the gateway gave the refund, and the cart's move to refunded, which stops it blocking the
    course. Both are written together, on the cart locked and checked again for an invoice, since a cart with one is a
    completed sale.

    A paid cart's refund is its settled payment: the same amount, currency, method and gateway. An unreconciled cart may
    have no payment row to copy, and one gateway's row holds the cart total whatever was charged, so the amount refunded
    is typed by staff, in the cart's currency, and the gateway is the one the cart was handed to.

    :param cart_id: The cart refunded.
    :param staff_user: The staff member recording the refund.
    :param refund_reference: The reference the gateway gave the refund, unique among that gateway's transactions.
    :param amount: The amount refunded, for an unreconciled cart only.
    :raises ResolutionError: When the refund cannot be recorded, saying why.
    :return: The refund transaction.
    """
    refund_reference = (refund_reference or '').strip()
    if not refund_reference:
        raise ResolutionError(f'The refund of cart {cart_id} needs the reference the gateway gave it.')
    with db_transaction.atomic():
        cart = _lock_cart(cart_id, {
            Cart.Status.PAID: PAID_WITHOUT_INVOICE,
            Cart.Status.UNRECONCILED: UNRECONCILED,
        })
        if cart.status == Cart.Status.PAID:
            if amount is not None:
                raise ResolutionError(f'The refund of paid cart {cart.id} is its payment, so it takes no amount.')
            payment = _get_settled_payment(cart)
            gateway, method, amount, currency = payment.gateway, payment.method, payment.amount, payment.currency
            reason = f'Refund of payment {payment.gateway_transaction_id}, made in the gateway.'
        else:
            if not cart.gateway:
                raise ResolutionError(f'Cart {cart.id} was handed off without its gateway recorded.')
            currency = _get_cart_currency(cart)
            _validate_amount(cart, amount, currency)
            gateway, method = cart.gateway, cart.gateway
            reason = 'Refund of a payment never reconciled, made in the gateway.'
        # The registered class is enough to name the gateway on the audit trail: nothing here calls it.
        processor = PROCESSORS.get(gateway)
        if processor is None:
            raise ResolutionError(f'Cart {cart.id} was paid through {gateway}, whose processor is not installed.')
        try:
            refund = Transaction.objects.create(
                cart=cart,
                type=Transaction.TransactionType.REFUND,
                status=BaseProcessor.TRANSACTION_STATUS_SUCCESS,
                gateway=gateway,
                gateway_transaction_id=refund_reference,
                method=method,
                amount=amount,
                currency=currency,
                reason=reason,
                initiator_user=staff_user,
            )
        except IntegrityError as exc:
            raise ResolutionError(
                f'{gateway} reference {refund_reference} is already recorded on another transaction.',
            ) from exc
        cart.change_status(
            Cart.Status.REFUNDED,
            processor=processor,
            action=AuditLog.AuditActions.REFUND_RECORDED,
            context={'staff_username': staff_user.username, 'refund_reference': refund_reference},
        )
    logger.info(f'Cart {cart.id} refunded under {gateway} reference {refund_reference}.')
    return refund


def _lock_cart(cart_id: int, resolvable: dict[str, str]) -> Cart:
    """
    Lock the cart for the rest of the transaction and return it, when its status is one the action resolves.

    Called only inside the caller's transaction: the lock, and so the check, holds until that transaction ends, and
    taking it outside one raises. A cart with an invoice is never resolved by hand: it is a completed sale.

    :param cart_id: The cart to lock.
    :param resolvable: The statuses the action resolves, each with how a refusal names it.
    :raises ResolutionError: When the cart does not exist, is in another status, or already has an invoice.
    """
    cart = Cart.objects.select_for_update().filter(pk=cart_id).first()
    if cart is None:
        raise ResolutionError(f'Cart {cart_id} does not exist.')
    if cart.status not in resolvable:
        raise ResolutionError(
            f'Cart {cart.id} is {cart.status}; only {", or ".join(resolvable.values())} is resolved here.',
        )
    if cart.invoices.exists():
        raise ResolutionError(f'Cart {cart.id} already has an invoice.')
    return cart


def _get_settled_payment(cart: Cart) -> Transaction:
    """
    Return the one settled payment of the cart.

    A cart with none has nothing to bill or refund, and one with several was charged more than once, which is a decision
    for staff to take on the gateway's records rather than a step to run again.

    :param cart: The paid cart.
    :raises ResolutionError: When the cart has no settled payment, or more than one.
    """
    payments = list(cart.transactions.filter(
        type=Transaction.TransactionType.PAYMENT,
        status__iexact=BaseProcessor.TRANSACTION_STATUS_SUCCESS,
    )[:2])
    if len(payments) != 1:
        raise ResolutionError(
            f'Cart {cart.id} has {"no settled payment" if not payments else "more than one settled payment"}; '
            'it is resolved by hand only with exactly one.',
        )
    return payments[0]


def _record_accepted_payment(  # pylint: disable=too-many-positional-arguments
    cart: Cart, processor: Any, gateway_transaction_id: str, amount: Decimal, currency: str, staff_user: Any,
) -> tuple[Transaction, str]:
    """
    Settle the cart's row for the accepted payment, or write a settled one when there is none, and return it.

    A row the plugin's own checks refused keeps its response, and its reason keeps why it was refused, followed by who
    accepted it. Its amount and currency are replaced by the ones accepted, so what it held before -- often the very
    reason it was refused -- is returned for the trail to keep. A row for that transaction belonging to another cart,
    or not a payment, is refused rather than taken over: the gateway's id names one payment.

    :param cart: The unreconciled cart.
    :param processor: The processor of the gateway the cart was handed to.
    :param gateway_transaction_id: The gateway's id for the payment.
    :param amount: The amount taken, the cart total.
    :param currency: The cart's currency.
    :param staff_user: The staff member accepting the payment.
    :raises ResolutionError: When the transaction id is recorded on another cart's row, or on a row that is not a
        payment.
    :return: The payment, and the amount and currency its row held before, or ``no payment`` when it was written here.
    """
    acceptance = f'accepted by {staff_user.username}, as confirmed with the gateway'
    payment = Transaction.objects.filter(gateway=processor.SLUG, gateway_transaction_id=gateway_transaction_id).first()
    if payment is None:
        return Transaction.objects.create(
            cart=cart,
            type=Transaction.TransactionType.PAYMENT,
            status=BaseProcessor.TRANSACTION_STATUS_SUCCESS,
            gateway=processor.SLUG,
            gateway_transaction_id=gateway_transaction_id,
            method=processor.SLUG,
            amount=amount,
            currency=currency,
            reason=f'Payment {acceptance}.',
            initiator_user=cart.user,
        ), 'no payment'
    if payment.cart_id != cart.id or payment.type != Transaction.TransactionType.PAYMENT:
        raise ResolutionError(
            f'{processor.SLUG} transaction {gateway_transaction_id} is already recorded on another transaction than a '
            f'payment of cart {cart.id}.',
        )
    recorded = f'{payment.amount} {payment.currency}'
    payment.status = BaseProcessor.TRANSACTION_STATUS_SUCCESS
    payment.amount = amount
    payment.currency = currency
    payment.reason = f'{payment.reason} | {acceptance}'
    payment.save(update_fields=['status', 'amount', 'currency', 'reason', 'updated_at'])
    return payment, recorded


def _validate_amount(cart: Cart, amount: Optional[Decimal], currency: str) -> None:
    """
    Refuse an amount refunded that is missing, not above zero, or more precise than the cart's currency allows.

    :param cart: The cart refunded.
    :param amount: The amount typed by staff.
    :param currency: The cart's currency.
    :raises ResolutionError: When the amount is refused.
    """
    if amount is None or amount <= 0:
        raise ResolutionError(f'The refund of unreconciled cart {cart.id} needs the amount refunded, above zero.')
    try:
        validate_price_against_currency(amount, currency)
    except ValidationError as exc:
        raise ResolutionError(f'The refund of cart {cart.id}: {exc.message}') from exc


def _get_cart_currency(cart: Cart) -> str:
    """
    Return the cart's currency, the deployment's.

    :param cart: The cart.
    :raises ResolutionError: When the deployment's currency is not set to a supported one, or the cart holds an item in
        another.
    """
    try:
        return get_currency(cart)
    except (ConfigurationError, CurrencyMismatchError) as exc:
        raise ResolutionError(f'The currency of cart {cart.id} cannot be told: {exc}') from exc


def _get_paying_site(cart: Cart) -> Site:
    """
    Return the site the cart was paid on, as its payment origin names it.

    :param cart: The cart.
    :raises ResolutionError: When the cart has no payment origin, or no site has the origin's host for its domain.
    """
    if not cart.payment_origin:
        raise ResolutionError(f'Cart {cart.id} has no payment origin, so the site it was paid on is not known.')
    site = Site.objects.filter(domain=urlsplit(cart.payment_origin).netloc).first()
    if site is None:
        raise ResolutionError(f'Cart {cart.id} was paid on {cart.payment_origin}, which is no site here.')
    return site


def _set_up_processor(cart: Cart, gateway: str) -> BaseProcessor:
    """
    Return the processor of the gateway, set up for the site being served.

    :param cart: The cart it is set up for, named in a refusal.
    :param gateway: The processor's SLUG.
    :raises ResolutionError: When the processor is not installed, or cannot be set up.
    """
    try:
        return get_processor(gateway)
    except Exception as exc:
        # Unknown and unconfigured alike: either way nothing can run the step for this gateway.
        raise ResolutionError(
            f'Cart {cart.id} was paid through {gateway}, whose processor is not installed or cannot be set up: {exc}',
        ) from exc


@contextmanager
def _serving_site(request: Any, site: Site) -> Iterator[None]:
    """
    Serve the given site on the request while the block runs, then the one it served before.

    What the plugin reads per site -- its settings, the organization an invoice names -- is read for the site of the
    request being served, so a step run for another site's cart runs as that site.

    :param request: The request being served.
    :param site: The site to serve meanwhile.
    """
    previous_site = getattr(request, 'site', None)
    request.site = site
    try:
        yield
    finally:
        request.site = previous_site
