"""Tests for resolving a payment still open when its learner comes back to buy the same course."""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.utils.timezone import now

from test_utils.dummy_processor import DummyProcessor
from zeitlabs_payments.cart_handler import CART_HANDLER
from zeitlabs_payments.constants import HANDOFF_GRACE_PERIOD, UNRECORDED_GATEWAY_GRACE_PERIOD
from zeitlabs_payments.exceptions import CartStatusError
from zeitlabs_payments.inflight_payment import (
    InFlightPayment,
    Resolution,
    create_cart_resolving_inflight_payments,
    resolve_inflight_payment,
    waiting_time,
)
from zeitlabs_payments.models import AuditLog, Cart, CatalogueItem

User = get_user_model()

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures('base_data')]


def inflight_cart(status=Cart.Status.PROCESSING, gateway='dummy', gateway_order_id=None, age=timedelta(0)):
    """Return a learner's cart for a paid course, handed to the gateway the given time ago."""
    item = CatalogueItem.objects.get(sku='custom-sku-1')
    cart = Cart.objects.create(
        user=User.objects.get(id=3), status=status, gateway=gateway, gateway_order_id=gateway_order_id,
    )
    cart.items.create(catalogue_item=item, original_price=item.price, final_price=item.price)
    Cart.objects.filter(pk=cart.pk).update(updated_at=now() - age)
    cart.refresh_from_db()
    return cart


def test_the_grace_periods_are_ten_minutes_and_a_day():
    """
    Verify that a hand-off is given ten minutes to record its order, and one from before carts recorded their gateway
    a day.

    Ten minutes outlasts any hand-off request still running; a day outlasts any gateway's payment session.
    """
    assert HANDOFF_GRACE_PERIOD == timedelta(minutes=10), 'The hand-off grace is not ten minutes'
    assert UNRECORDED_GATEWAY_GRACE_PERIOD == timedelta(hours=24), 'The grace of a cart with no gateway is not a day'


def test_a_payment_whose_order_is_known_resumes_on_its_waiting_page():
    """
    Verify that a cart whose order is known is resumed on the processor's waiting page for that order, however old.

    The waiting page is the processor's return view, with the order id where its gateway's redirect puts it, so the
    status view settles, declines or keeps waiting on the payment exactly as on the learner's first return.
    """
    cart = inflight_cart(gateway_order_id='ORDER 1', age=timedelta(days=30))

    resolved = resolve_inflight_payment(cart)

    assert resolved == InFlightPayment(
        Resolution.RESUME, cart, waiting_page_url=f'{DummyProcessor().get_return_url()}?orderId=ORDER+1',
    ), 'A payment whose order is known was not resumed on its waiting page'


@pytest.mark.parametrize('gateway, grace, usecase', [
    ('dummy', HANDOFF_GRACE_PERIOD, 'a hand-off that has not recorded its order yet'),
    (None, UNRECORDED_GATEWAY_GRACE_PERIOD, 'a hand-off from before carts recorded their gateway'),
])
def test_a_hand_off_with_no_order_is_in_progress_within_its_grace(gateway, grace, usecase):
    """
    Verify that a processing cart with no order recorded is reported in progress until its grace ends, and left as it
    is: its hand-off may still be running.
    """
    cart = inflight_cart(gateway=gateway, age=grace - timedelta(minutes=1))

    resolved = resolve_inflight_payment(cart)

    assert resolved == InFlightPayment(Resolution.IN_PROGRESS, cart, retry_after=cart.updated_at + grace), \
        f'Not in progress until its grace ends: {usecase}'
    assert Cart.objects.get(pk=cart.pk).status == Cart.Status.PROCESSING, f'The cart moved: {usecase}'


@pytest.mark.parametrize('gateway, grace, usecase', [
    ('dummy', HANDOFF_GRACE_PERIOD, 'a hand-off that never recorded its order'),
    (None, UNRECORDED_GATEWAY_GRACE_PERIOD, 'a hand-off from before carts recorded their gateway'),
])
def test_a_hand_off_with_no_order_past_its_grace_is_closed_as_failed(gateway, grace, usecase):
    """
    Verify that a processing cart with no order recorded, past its grace, is closed as a failed hand-off and frees its
    item: its learner never reached the gateway, so no payment can complete for it.
    """
    cart = inflight_cart(gateway=gateway, age=grace + timedelta(minutes=1))

    resolved = resolve_inflight_payment(cart)

    assert resolved == InFlightPayment(Resolution.CLOSED, cart), f'Not closed past its grace: {usecase}'
    assert Cart.objects.get(pk=cart.pk).status == Cart.Status.CLOSED, f'The cart was not closed: {usecase}'
    assert AuditLog.objects.filter(cart=cart, action=AuditLog.AuditActions.PAYMENT_HANDOFF_FAILED).exists(), \
        f'The failed hand-off is not on the record: {usecase}'


def test_a_payment_the_gateway_answered_about_is_never_closed_unasked():
    """
    Verify that a cart awaiting payment with no order recorded is refused however old, rather than closed.

    The gateway answered about it at least once, so money may have moved: only asking again can tell.
    """
    cart = inflight_cart(status=Cart.Status.PAYMENT_PENDING, age=timedelta(days=30))

    resolved = resolve_inflight_payment(cart)

    assert resolved == InFlightPayment(Resolution.BLOCKED, cart), 'A payment the gateway answered about was not refused'
    assert Cart.objects.get(pk=cart.pk).status == Cart.Status.PAYMENT_PENDING, 'It was given up on unasked'


def test_a_cart_another_request_moved_while_closing_it_is_in_progress():
    """Verify that a cart another request moved just as it was being closed is reported in progress, not closed."""
    cart = inflight_cart(age=HANDOFF_GRACE_PERIOD * 2)

    with patch.object(Cart, 'change_status', side_effect=CartStatusError('moved by another request')):
        resolved = resolve_inflight_payment(cart)

    assert resolved == InFlightPayment(Resolution.IN_PROGRESS, cart), 'A cart moved meanwhile was not left in progress'


def test_a_payment_to_a_processor_no_longer_registered_is_refused():
    """Verify that a cart handed to a processor no longer registered is refused, since nothing can be asked of it."""
    cart = inflight_cart(gateway='retired-gateway', gateway_order_id='ORDER-1')

    resolved = resolve_inflight_payment(cart)

    assert resolved.resolution == Resolution.BLOCKED, 'A payment nobody can ask about went through'


def test_creating_a_cart_after_a_failed_hand_off_makes_the_cart():
    """Verify that buying a course again past a failed hand-off's grace closes it and makes the new cart."""
    failed = inflight_cart(age=HANDOFF_GRACE_PERIOD * 2)
    item = CatalogueItem.objects.get(sku='custom-sku-1')

    cart, inflight_payment = create_cart_resolving_inflight_payments(CART_HANDLER[item.type], failed.user, item)

    assert inflight_payment is None, 'A failed hand-off was still reported open'
    assert cart is not None and cart.pk != failed.pk, 'No new cart was made'
    assert Cart.objects.get(pk=failed.pk).status == Cart.Status.CLOSED, 'The failed hand-off was not closed'


def test_creating_a_cart_after_a_hand_off_from_before_carts_recorded_their_gateway_makes_the_cart():
    """
    Verify that buying a course again a day after a hand-off that recorded no gateway closes it and makes the new
    cart, rather than refusing the learner for good.
    """
    stale = inflight_cart(gateway=None, age=UNRECORDED_GATEWAY_GRACE_PERIOD * 2)
    item = CatalogueItem.objects.get(sku='custom-sku-1')

    cart, inflight_payment = create_cart_resolving_inflight_payments(CART_HANDLER[item.type], stale.user, item)

    assert inflight_payment is None, 'A hand-off with no gateway was still reported open'
    assert cart is not None and cart.pk != stale.pk, 'No new cart was made'
    assert Cart.objects.get(pk=stale.pk).status == Cart.Status.CLOSED, 'The hand-off with no gateway was not closed'


def test_creating_a_cart_while_a_payment_is_open_makes_none():
    """Verify that buying a course again while its payment is still in progress makes no cart, and says why."""
    running = inflight_cart(age=timedelta(minutes=1))
    item = CatalogueItem.objects.get(sku='custom-sku-1')

    cart, inflight_payment = create_cart_resolving_inflight_payments(CART_HANDLER[item.type], running.user, item)

    assert cart is None, 'A second cart was made while a payment for the course was in progress'
    assert inflight_payment.resolution == Resolution.IN_PROGRESS and inflight_payment.cart == running, \
        'The payment in progress was not reported'


@pytest.mark.parametrize('seconds_ahead, wait', [
    (1, {'minutes': 1}),
    (60, {'minutes': 1}),
    (61, {'minutes': 2}),
    (-30, {'minutes': 1}),
    (59 * 60, {'minutes': 59}),
    (60 * 60, {'hours': 1}),
    (61 * 60, {'hours': 2}),
    (24 * 60 * 60, {'hours': 24}),
])
def test_waiting_time_rounds_up_in_minutes_then_hours(seconds_ahead, wait):
    """
    Verify that the time to wait is rounded up, never below one, in minutes under an hour and in hours from there.

    A learner told a day's wait in minutes would read 1440 of them.
    """
    assert waiting_time(now() + timedelta(seconds=seconds_ahead)) == wait, f'{seconds_ahead}s is not {wait}'


def test_waiting_time_of_no_moment_is_empty():
    """Verify that no moment gives no time to wait, which the page words as later."""
    assert not waiting_time(None), 'A missing moment gave a time to wait'
