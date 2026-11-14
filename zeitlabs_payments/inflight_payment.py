"""What becomes of a payment still in flight when its learner comes back to buy the same item."""
import logging
import math
from datetime import datetime
from enum import Enum
from typing import Any, NamedTuple, Optional
from urllib.parse import urlencode

from django.utils.timezone import now

from zeitlabs_payments.constants import HANDOFF_GRACE_PERIOD, UNRECORDED_GATEWAY_GRACE_PERIOD
from zeitlabs_payments.exceptions import CartStatusError, InFlightPaymentError
from zeitlabs_payments.models import AuditLog, Cart, CatalogueItem
from zeitlabs_payments.providers.registry import PROCESSORS, get_processor

logger = logging.getLogger(__name__)


class Resolution(Enum):
    """The ways a payment still open can be resolved."""

    # Its order is known: the learner goes back to that order's waiting page.
    RESUME = 'resume'
    # No order is recorded and its hand-off may still be running: the learner waits out its grace.
    IN_PROGRESS = 'in_progress'
    # No order was recorded within its grace, so the learner never reached the gateway: the cart is closed as a failed
    # hand-off, and a new one may be made.
    CLOSED = 'closed'
    # Nothing can be asked or concluded about it: the learner is refused, for a person to resolve.
    BLOCKED = 'blocked'


class InFlightPayment(NamedTuple):
    """How an in-flight payment is resolved, and what the learner is sent to or told."""

    resolution: Resolution
    cart: Cart
    # The waiting page to resume on, for RESUME.
    waiting_page_url: Optional[str] = None
    # When the hand-off's grace runs out, for IN_PROGRESS; None when it is not known.
    retry_after: Optional[datetime] = None


def resolve_inflight_payment(cart: Cart) -> InFlightPayment:
    """
    Decide what becomes of a cart whose payment is open, now that its learner wants to buy the same item again.

    Every processor records its gateway order before sending the learner to the gateway, so a cart with an order is
    resumed on its processor's waiting page, where the status view settles, declines or keeps waiting on it as on the
    learner's first return. A processing cart without an order never reached the gateway, once its hand-off has had
    ``HANDOFF_GRACE_PERIOD`` to record one -- ``UNRECORDED_GATEWAY_GRACE_PERIOD`` for a cart handed off before carts
    recorded their gateway -- and is closed as a failed hand-off; within its grace the learner is asked to wait. A cart
    awaiting payment without an order was answered about by its gateway, so money may have moved: it is refused, for a
    person to resolve.

    :param cart: The in-flight cart: processing, or awaiting payment with its gateway recorded.
    """
    if cart.gateway_order_id:
        try:
            processor = get_processor(cart.gateway)
        except ValueError:
            logger.error(f'Cart {cart.id} was handed to {cart.gateway!r}, which is no longer registered.')
            return InFlightPayment(Resolution.BLOCKED, cart)
        return InFlightPayment(
            Resolution.RESUME, cart, waiting_page_url=_waiting_page_url(processor, cart.gateway_order_id),
        )

    if cart.status != Cart.Status.PROCESSING:
        return InFlightPayment(Resolution.BLOCKED, cart)

    grace = HANDOFF_GRACE_PERIOD if cart.gateway else UNRECORDED_GATEWAY_GRACE_PERIOD
    grace_end = cart.updated_at + grace
    if now() < grace_end:
        return InFlightPayment(Resolution.IN_PROGRESS, cart, retry_after=grace_end)
    processor_class = PROCESSORS.get(cart.gateway)
    try:
        cart.change_status(
            Cart.Status.CLOSED,
            processor=processor_class() if processor_class else None,
            action=AuditLog.AuditActions.PAYMENT_HANDOFF_FAILED,
            context={'error': f'no gateway order was recorded within {grace} of the hand-off'},
        )
    except CartStatusError as exc:
        logger.info(f'Cart {cart.id} was not closed, another request moved it first: {exc}')
        return InFlightPayment(Resolution.IN_PROGRESS, cart)
    return InFlightPayment(Resolution.CLOSED, cart)


def _waiting_page_url(processor: Any, gateway_order_id: str) -> str:
    """
    Return the processor's waiting page for the order -- its return view, reached as its gateway's redirect reaches it.

    Every registered processor names both, see ``registry.missing_hand_off_contract``.
    """
    return f'{processor.get_return_url()}?{urlencode({processor.GATEWAY_ORDER_ID_PARAM: gateway_order_id})}'


def create_cart_resolving_inflight_payments(
    handler: Any, user: Any, catalogue_item: CatalogueItem,
) -> tuple[Optional[Cart], Optional[InFlightPayment]]:
    """
    Create a cart holding the item, first resolving any payment for the same item that is still open.

    Each in-flight cart found is resolved in turn. One that is closed leaves the way clear, and creating the cart is
    tried again; any other resolution is returned for the caller to act on, and no cart is made.

    :param handler: The cart handler for the item's type.
    :param user: The learner buying the item.
    :param catalogue_item: The item to buy.
    :return: The new cart and None, or None and the in-flight payment the learner has to be told about.
    :raises InvalidCartError: When the item cannot be added for any other reason.
    """
    while True:
        try:
            return handler.validate_item_and_create_cart(user, catalogue_item), None
        except InFlightPaymentError as exc:
            inflight_payment = resolve_inflight_payment(exc.cart)
            if inflight_payment.resolution != Resolution.CLOSED:
                return None, inflight_payment


def waiting_time(moment: Optional[datetime]) -> dict:
    """
    Return how long from now until the moment, as the page words it: whole hours, or whole minutes under an hour.

    Rounded up and never below one, so the learner is not told to try again before they may. Empty when there is no
    moment, which the page words as later.
    """
    if moment is None:
        return {}
    minutes = max(1, math.ceil((moment - now()).total_seconds() / 60))
    if minutes < 60:
        return {'minutes': minutes}
    return {'hours': math.ceil(minutes / 60)}
