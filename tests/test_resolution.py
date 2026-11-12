"""Tests for staff resolving a cart whose payment did not end in an invoice: paid without one, or unreconciled."""
from decimal import Decimal
from unittest.mock import patch

import pytest
from common.djangoapps.student.models import CourseEnrollment
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.test import RequestFactory, override_settings

from test_utils.dummy_processor import DummyProcessor
from zeitlabs_payments.exceptions import ResolutionError
from zeitlabs_payments.models import AuditLog, Cart, CatalogueItem, Invoice, Transaction
from zeitlabs_payments.providers.base import BaseProcessor
from zeitlabs_payments.resolution import accept_payment, record_refund, retry_fulfillment

User = get_user_model()

PAYMENT_ID = 'TX-PAID'
REFUSED_ID = 'TX-REFUSED'


@pytest.fixture
def paid_cart(base_data):  # pylint: disable=unused-argument
    """A cart paid on the current site through the dummy processor, whose invoice and enrolment failed."""
    item = CatalogueItem.objects.get(sku='custom-sku-1')
    cart = Cart.objects.create(
        user=User.objects.get(id=3),
        status=Cart.Status.PAID,
        gateway=DummyProcessor.SLUG,
        payment_origin=f'https://{Site.objects.get_current().domain}',
    )
    cart.items.create(catalogue_item=item, original_price=item.price, final_price=item.price)
    cart.transactions.create(
        type=Transaction.TransactionType.PAYMENT, status='success', gateway=DummyProcessor.SLUG,
        gateway_transaction_id=PAYMENT_ID, method='card', amount=item.price, currency='SAR',
    )
    return cart


@pytest.fixture
def staff(base_data):  # pylint: disable=unused-argument
    """The staff member resolving the cart."""
    return User.objects.get(id=1)


def staff_request(staff_user, site=None):
    """Return a request of the staff member, served on the given site or else on the current one."""
    request = RequestFactory().post('/admin/zeitlabs_payments/cart/')
    request.user = staff_user
    request.site = site or Site.objects.get_current()
    return request


def trail(cart):
    """Return the actions on the cart's audit trail, oldest first."""
    return list(AuditLog.objects.filter(cart=cart).order_by('id').values_list('action', flat=True))


def leave_unpaid(cart):
    """Make the cart one whose payment never settled."""
    Cart.objects.filter(pk=cart.pk).update(status=Cart.Status.PAYMENT_PENDING)


def invoice_it(cart):
    """Give the cart the invoice a completed sale has."""
    Invoice.objects.create(
        status=Invoice.InvoiceStatus.PAID, invoice_number='TEST-900001', cart=cart, total=50, gross_total=50,
        currency='SAR', related_transaction=cart.transactions.get(),
    )


def forget_origin(cart):
    """Make the cart one paid before carts recorded their origin."""
    Cart.objects.filter(pk=cart.pk).update(payment_origin=None)


def move_origin_off_every_site(cart):
    """Make the cart's origin one no site serves now."""
    Cart.objects.filter(pk=cart.pk).update(payment_origin='https://gone.example.com')


def fail_the_payment(cart):
    """Make the cart's only payment one that did not settle."""
    cart.transactions.update(status='failed')


def charge_twice(cart):
    """Give the cart a second settled payment."""
    payment = cart.transactions.get()
    payment.pk, payment.gateway_transaction_id = None, 'TX-PAID-AGAIN'
    payment.save()


def uninstall_its_processor(cart):
    """Make the cart's payment one taken through a processor that is not installed."""
    cart.transactions.update(gateway='gone')


@pytest.mark.django_db
def test_retry_issues_the_invoice_and_enrols_the_learner(paid_cart, staff):  # pylint: disable=redefined-outer-name
    """
    Verify that a retry issues the cart's invoice for its payment and enrols the learner, as settlement would have,
    leaves the cart paid, and records who retried it.
    """
    invoice = retry_fulfillment(paid_cart.pk, staff_request(staff))

    paid_cart.refresh_from_db()
    assert invoice.related_transaction.gateway_transaction_id == PAYMENT_ID, 'The invoice does not bill the payment'
    assert paid_cart.status == Cart.Status.PAID, 'The retry moved the cart'
    assert CourseEnrollment.objects.filter(user=paid_cart.user).exists(), 'The learner was not enrolled'
    assert trail(paid_cart) == [
        AuditLog.AuditActions.FULFILLMENT_RETRIED,
        AuditLog.AuditActions.USER_ENROLLED,
        AuditLog.AuditActions.CART_FULFILLED,
    ], 'The trail does not show the retry and its outcome'
    assert AuditLog.objects.get(cart=paid_cart, action=AuditLog.AuditActions.FULFILLMENT_RETRIED).details == (
        f'Invoice and enrollment retried by: {staff.username}, for transaction: {PAYMENT_ID}.'
    ), 'The retry does not name who ran it, or for which payment'


@pytest.mark.django_db
def test_retry_runs_on_the_site_the_cart_was_paid_on(paid_cart, staff):  # pylint: disable=redefined-outer-name
    """
    Verify that a retry run from another site sets up the processor and issues the invoice on the site the cart was
    paid on, then serves the staff member's site again.

    A processor reads its settings for the site being served, and so does the organization stored on the invoice.
    """
    Site.objects.create(name='learn', domain='learn.example.com')
    Cart.objects.filter(pk=paid_cart.pk).update(payment_origin='https://learn.example.com')
    request = staff_request(staff)
    served_while_set_up = []

    def set_up_processor(slug):
        served_while_set_up.append((slug, request.site.domain))
        return DummyProcessor()

    with patch('zeitlabs_payments.resolution.get_processor', side_effect=set_up_processor):
        invoice = retry_fulfillment(paid_cart.pk, request)

    assert served_while_set_up == [('dummy', 'learn.example.com')], \
        "The payment's processor was not set up on the site the cart was paid on"
    assert invoice.receipt_url.startswith('https://learn.example.com/'), 'The invoice is not issued on the paying site'
    assert request.site == Site.objects.get_current(), "The staff member's site is not served again"


@pytest.mark.django_db
@pytest.mark.parametrize('break_cart, refusal, usecase', [
    (leave_unpaid, 'is payment_pending; only a paid cart', 'a cart whose payment has not settled'),
    (invoice_it, 'already has an invoice', 'a completed sale'),
    (forget_origin, 'has no payment origin', 'a cart paid before carts recorded their origin'),
    (move_origin_off_every_site, 'which is no site here', 'a cart paid on a site removed since'),
    (fail_the_payment, 'has no settled payment', 'a cart with no payment to bill'),
    (charge_twice, 'has more than one settled payment', 'a cart charged twice'),
    (uninstall_its_processor, 'whose processor is not installed', 'a cart paid through a removed processor'),
])
def test_retry_refuses_a_cart_it_cannot_resolve(
    paid_cart, staff, break_cart, refusal, usecase,  # pylint: disable=redefined-outer-name
):
    """
    Verify that a retry is refused, saying why, for every cart it cannot resolve, and changes nothing about it.
    """
    break_cart(paid_cart)
    status_before = Cart.objects.get(pk=paid_cart.pk).status
    invoices_before = Invoice.objects.filter(cart=paid_cart).count()

    with pytest.raises(ResolutionError, match=refusal):
        retry_fulfillment(paid_cart.pk, staff_request(staff))

    assert Cart.objects.get(pk=paid_cart.pk).status == status_before, f'Failed usecase: {usecase}'
    assert Invoice.objects.filter(cart=paid_cart).count() == invoices_before, f'Failed usecase: {usecase}'
    assert AuditLog.AuditActions.FULFILLMENT_RETRIED not in trail(paid_cart), f'Failed usecase: {usecase}'


@pytest.mark.django_db
def test_retry_refuses_a_processor_that_cannot_be_set_up(paid_cart, staff):  # pylint: disable=redefined-outer-name
    """
    Verify that a retry is refused when the payment's processor is installed but cannot be set up, as one whose
    settings were removed from the site.
    """
    with patch.object(DummyProcessor, '__init__', side_effect=RuntimeError('no settings for this site')):
        with pytest.raises(ResolutionError, match='cannot be set up: no settings for this site'):
            retry_fulfillment(paid_cart.pk, staff_request(staff))

    assert not Invoice.objects.filter(cart=paid_cart).exists(), 'An invoice was issued without the processor'


@pytest.mark.django_db
def test_retry_refuses_a_cart_that_does_not_exist(staff):  # pylint: disable=redefined-outer-name
    """Verify that a retry of a cart that does not exist is refused, naming it."""
    with pytest.raises(ResolutionError, match='Cart 0 does not exist'):
        retry_fulfillment(0, staff_request(staff))


@pytest.mark.django_db
@patch('zeitlabs_payments.providers.base.CART_HANDLER', new={})
def test_a_retry_that_fails_again_is_recorded_and_reported(paid_cart, staff):  # pylint: disable=redefined-outer-name
    """
    Verify that a retry failing again leaves the cart paid without an invoice, reports the failure, and keeps the
    retry and its failure on the audit trail.
    """
    with patch('zeitlabs_payments.providers.base.logger.exception'):
        with pytest.raises(ResolutionError, match=f'Cart {paid_cart.pk} failed again'):
            retry_fulfillment(paid_cart.pk, staff_request(staff))

    assert not Invoice.objects.filter(cart=paid_cart).exists(), 'A failed retry left an invoice'
    assert Cart.objects.get(pk=paid_cart.pk).status == Cart.Status.PAID, 'A failed retry moved the cart'
    assert trail(paid_cart) == [
        AuditLog.AuditActions.FULFILLMENT_RETRIED,
        AuditLog.AuditActions.INVOICE_CREATION_FAILED,
    ], 'The failed retry is not on the trail'


@pytest.mark.django_db
def test_a_cart_retried_twice_is_invoiced_once(paid_cart, staff):  # pylint: disable=redefined-outer-name
    """
    Verify that a second retry of a cart, as by a second staff member, is refused once the first has invoiced it.
    """
    retry_fulfillment(paid_cart.pk, staff_request(staff))

    with pytest.raises(ResolutionError, match='already has an invoice'):
        retry_fulfillment(paid_cart.pk, staff_request(staff))

    assert Invoice.objects.filter(cart=paid_cart).count() == 1, 'The cart was invoiced twice'


@pytest.mark.django_db
def test_refund_records_the_refund_beside_the_payment(paid_cart, staff):  # pylint: disable=redefined-outer-name
    """
    Verify that a refund is recorded as a refund transaction for the payment's amount, under the reference typed in,
    and moves the cart to refunded with an audit entry naming who recorded it.
    """
    refund = record_refund(paid_cart.pk, staff, '  RF-1 ')

    payment = paid_cart.transactions.get(type=Transaction.TransactionType.PAYMENT)
    assert (
        refund.type, refund.status, refund.gateway, refund.gateway_transaction_id, refund.method, refund.amount,
        refund.currency, refund.initiator_user,
    ) == (
        Transaction.TransactionType.REFUND, 'success', payment.gateway, 'RF-1', payment.method, payment.amount,
        payment.currency, staff,
    ), 'The refund does not mirror the payment, under the reference typed in'
    assert Cart.objects.get(pk=paid_cart.pk).status == Cart.Status.REFUNDED, 'The cart was not moved to refunded'
    assert AuditLog.objects.get(cart=paid_cart, action=AuditLog.AuditActions.REFUND_RECORDED).details == (
        f"Refund made in the gateway's dashboard recorded by: {staff.username}, under reference: RF-1. "
        'Status updated from: paid to: refunded.'
    ), 'The refund does not name who recorded it, or under which reference'


@pytest.mark.django_db
def test_refund_needs_no_payment_origin(paid_cart, staff):  # pylint: disable=redefined-outer-name
    """
    Verify that a cart paid before carts recorded their origin can still be refunded, since nothing is issued for it.
    """
    forget_origin(paid_cart)

    record_refund(paid_cart.pk, staff, 'RF-1')

    assert Cart.objects.get(pk=paid_cart.pk).status == Cart.Status.REFUNDED, 'A cart without an origin was refused'


@pytest.mark.django_db
@pytest.mark.parametrize('break_cart, reference, refusal, usecase', [
    (None, '  ', 'needs the reference the gateway gave it', 'no reference typed in'),
    (None, PAYMENT_ID, 'is already recorded on another transaction', "the payment's own id typed in"),
    (leave_unpaid, 'RF-1', 'is payment_pending; only a paid cart', 'a cart whose payment has not settled'),
    (invoice_it, 'RF-1', 'already has an invoice', 'a completed sale'),
    (charge_twice, 'RF-1', 'has more than one settled payment', 'a cart charged twice'),
    (uninstall_its_processor, 'RF-1', 'whose processor is not installed', 'a cart paid through a removed processor'),
])
def test_refund_refuses_what_it_cannot_record(  # pylint: disable=too-many-positional-arguments
    paid_cart, staff, break_cart, reference, refusal, usecase,  # pylint: disable=redefined-outer-name
):
    """
    Verify that a refund is refused, saying why, for every cart or reference it cannot record, and records nothing.
    """
    if break_cart:
        break_cart(paid_cart)
    status_before = Cart.objects.get(pk=paid_cart.pk).status

    with pytest.raises(ResolutionError, match=refusal):
        record_refund(paid_cart.pk, staff, reference)

    assert Cart.objects.get(pk=paid_cart.pk).status == status_before, f'Failed usecase: {usecase}'
    assert not Transaction.objects.filter(type=Transaction.TransactionType.REFUND).exists(), \
        f'Failed usecase: {usecase}'


@pytest.fixture
def unreconciled_cart(base_data):  # pylint: disable=unused-argument
    """An unreconciled cart handed to the dummy processor on the current site: its payment was taken and refused."""
    item = CatalogueItem.objects.get(sku='custom-sku-1')
    cart = Cart.objects.create(
        user=User.objects.get(id=3),
        status=Cart.Status.UNRECONCILED,
        gateway=DummyProcessor.SLUG,
        payment_origin=f'https://{Site.objects.get_current().domain}',
    )
    cart.items.create(catalogue_item=item, original_price=item.price, final_price=item.price)
    return cart


def record_refused_payment(cart):
    """Give the cart the row the plugin's own checks leave for a payment they refused, in the gateway's minor units."""
    return cart.transactions.create(
        type=Transaction.TransactionType.PAYMENT, status='failed', gateway=DummyProcessor.SLUG,
        gateway_transaction_id=REFUSED_ID, method='card', amount=cart.total * 100, currency='682',
        reason='Payment completed | not reconciled: the deposit is not the cart total',
    )


def accept(cart, staff_user, transaction_id=REFUSED_ID, amount=None):
    """Accept the cart's payment as the staff member, for the cart total unless another amount is named."""
    return accept_payment(cart.pk, staff_request(staff_user), transaction_id, cart.total if amount is None else amount)


@pytest.mark.django_db
def test_accepting_a_refused_payment_settles_its_row_and_fulfils_the_cart(
    unreconciled_cart, staff,  # pylint: disable=redefined-outer-name
):
    """
    Verify that accepting the payment of an unreconciled cart makes the row the plugin refused successful, for the cart
    total in its currency and keeping why it was refused, moves the cart to paid, and issues its invoice and enrols the
    learner. The acceptance on the trail keeps the amount and currency the row held before.
    """
    refused = record_refused_payment(unreconciled_cart)

    invoice = accept(unreconciled_cart, staff)

    refused.refresh_from_db()
    assert (refused.status, refused.amount, refused.currency) == ('success', unreconciled_cart.total, 'SAR'), \
        'The refused row was not settled for the cart total, in its currency'
    assert refused.reason == (
        f'Payment completed | not reconciled: the deposit is not the cart total | accepted by {staff.username}, as '
        'confirmed with the gateway'
    ), 'The row does not keep why it was refused, followed by who accepted it'
    assert invoice.related_transaction == refused, 'The invoice does not bill the accepted payment'
    assert Cart.objects.get(pk=unreconciled_cart.pk).status == Cart.Status.PAID, 'The cart was not moved to paid'
    assert CourseEnrollment.objects.filter(user=unreconciled_cart.user).exists(), 'The learner was not enrolled'
    assert trail(unreconciled_cart) == [
        AuditLog.AuditActions.PAYMENT_ACCEPTED,
        AuditLog.AuditActions.USER_ENROLLED,
        AuditLog.AuditActions.CART_FULFILLED,
    ], 'The trail does not show the payment accepted and the cart fulfilled'
    assert AuditLog.objects.get(cart=unreconciled_cart, action=AuditLog.AuditActions.PAYMENT_ACCEPTED).details == (
        f'Payment {REFUSED_ID} of {unreconciled_cart.total} SAR accepted by: {staff.username}, as confirmed with the '
        f'gateway. The plugin had recorded: {unreconciled_cart.total * 100} 682. Status updated from: unreconciled to: '
        'paid.'
    ), 'The acceptance does not name the payment, its amount, who accepted it, or what the row held before'


@pytest.mark.django_db
def test_accepting_a_payment_never_recorded_writes_its_row(
    unreconciled_cart, staff,  # pylint: disable=redefined-outer-name
):
    """
    Verify that accepting a payment the plugin has no row for -- its answer could not be read or recorded -- writes a
    successful one under the transaction id and amount staff confirmed, made by the learner, and that the acceptance on
    the trail says the plugin had recorded no payment.
    """
    invoice = accept(unreconciled_cart, staff, transaction_id='TX-UNREAD')

    payment = invoice.related_transaction
    assert (
        payment.cart, payment.type, payment.status, payment.gateway, payment.gateway_transaction_id, payment.method,
        payment.amount, payment.currency, payment.initiator_user,
    ) == (
        unreconciled_cart, Transaction.TransactionType.PAYMENT, 'success', DummyProcessor.SLUG, 'TX-UNREAD',
        DummyProcessor.SLUG, unreconciled_cart.total, 'SAR', unreconciled_cart.user,
    ), 'The payment was not recorded as confirmed with the gateway'
    assert 'The plugin had recorded: no payment.' in AuditLog.objects.get(
        cart=unreconciled_cart, action=AuditLog.AuditActions.PAYMENT_ACCEPTED,
    ).details, 'The acceptance does not say the plugin had recorded no payment'


def pay_cart(cart):
    """Make the cart one already paid."""
    Cart.objects.filter(pk=cart.pk).update(status=Cart.Status.PAID)


def other_cart(cart, status, item=None):
    """Give the cart's learner another cart in the given status, holding the item, or the cart's own item."""
    item = item or cart.items.get().catalogue_item
    other = Cart.objects.create(user=cart.user, status=status, gateway=DummyProcessor.SLUG)
    other.items.create(catalogue_item=item, original_price=item.price, final_price=item.price)
    return other


def buy_items_again(cart):
    """Buy the cart's items again in another cart of the same learner."""
    other_cart(cart, Cart.Status.PAID)


def pay_for_items_again(cart):
    """Start paying for the cart's items again in another cart, still at the gateway."""
    other_cart(cart, Cart.Status.PAYMENT_PENDING)


def hold_items_in_another_unreconciled_cart(cart):
    """Hold the cart's items in another cart whose payment was also taken and refused."""
    other_cart(cart, Cart.Status.UNRECONCILED)


def buy_another_mode_of_the_course(cart):
    """Buy another paid mode of the cart's course in another cart."""
    item = cart.items.get().catalogue_item
    other_mode = CatalogueItem.objects.create(
        sku=f'{item.sku}-other-mode', type=CatalogueItem.ItemType.PAID_COURSE, title='Same course, other mode',
        item_ref_id=item.item_ref_id, price=item.price, currency=item.currency,
    )
    other_cart(cart, Cart.Status.PAID, other_mode)


def buy_a_bundle_containing_the_course(cart):
    """Buy a bundle containing the cart's course in another cart."""
    other_cart(cart, Cart.Status.PAID, CatalogueItem.objects.get(sku='BUNDLE-PRO-CERT'))


def record_the_id_on_another_cart(cart):
    """Record the gateway's transaction id on another cart's payment."""
    other = Cart.objects.create(user=cart.user, status=Cart.Status.CANCELLED)
    other.transactions.create(
        type=Transaction.TransactionType.PAYMENT, status='failed', gateway=DummyProcessor.SLUG,
        gateway_transaction_id=REFUSED_ID, method='card', amount=1, currency='SAR',
    )


def forget_gateway(cart):
    """Make the cart one handed off before carts recorded their gateway."""
    Cart.objects.filter(pk=cart.pk).update(gateway=None)


def hand_to_a_removed_processor(cart):
    """Make the cart one handed to a processor that is not installed."""
    Cart.objects.filter(pk=cart.pk).update(gateway='gone')


@pytest.mark.django_db
@pytest.mark.parametrize('break_cart, transaction_id, amount_off, refusal, usecase', [
    (pay_cart, REFUSED_ID, 0, 'is paid; only an unreconciled cart is resolved here', 'a cart already paid'),
    (None, '  ', 0, 'needs the id the gateway gave it', 'no transaction id typed in'),
    (None, REFUSED_ID, 1, 'but the gateway took', 'a payment of another amount than the cart total'),
    (buy_items_again, REFUSED_ID, 0, 'holds the items of cart', 'a cart whose items were bought again'),
    (pay_for_items_again, REFUSED_ID, 0, 'holds the items of cart', 'items being paid for again at the gateway'),
    (hold_items_in_another_unreconciled_cart, REFUSED_ID, 0, 'holds the items of cart', 'another unreconciled cart'),
    (buy_another_mode_of_the_course, REFUSED_ID, 0, 'holds the items of cart', 'another paid mode of the course'),
    (buy_a_bundle_containing_the_course, REFUSED_ID, 0, 'holds the items of cart', 'a bundle containing the course'),
    (
        record_the_id_on_another_cart, REFUSED_ID, 0, 'is already recorded on another transaction',
        "a transaction id another cart's payment holds",
    ),
    (forget_origin, REFUSED_ID, 0, 'has no payment origin', 'a cart handed off before carts recorded their origin'),
    (forget_gateway, REFUSED_ID, 0, 'without its gateway recorded', 'a cart handed off before carts recorded it'),
    (hand_to_a_removed_processor, REFUSED_ID, 0, 'not installed or cannot be set up', 'a removed processor'),
])
def test_accepting_refuses_a_payment_it_cannot_settle(  # pylint: disable=too-many-positional-arguments
    unreconciled_cart, staff,  # pylint: disable=redefined-outer-name
    break_cart, transaction_id, amount_off, refusal, usecase,
):
    """
    Verify that accepting a payment is refused, saying why, for every cart or payment it cannot settle, and that it
    leaves the cart, its payment and its trail as they were.
    """
    if break_cart:
        break_cart(unreconciled_cart)
    status_before = Cart.objects.get(pk=unreconciled_cart.pk).status

    with pytest.raises(ResolutionError, match=refusal):
        accept(unreconciled_cart, staff, transaction_id=transaction_id, amount=unreconciled_cart.total + amount_off)

    assert Cart.objects.get(pk=unreconciled_cart.pk).status == status_before, f'Failed usecase: {usecase}'
    assert not Transaction.objects.filter(cart=unreconciled_cart).exists(), f'Failed usecase: {usecase}'
    assert AuditLog.AuditActions.PAYMENT_ACCEPTED not in trail(unreconciled_cart), f'Failed usecase: {usecase}'


@pytest.mark.django_db
@pytest.mark.parametrize('status', Cart.NON_BLOCKING_STATUSES)
def test_accepting_is_not_refused_for_another_cart_that_does_not_block(
    unreconciled_cart, staff, status,  # pylint: disable=redefined-outer-name
):
    """
    Verify that another cart for the same item is no reason to refuse accepting the payment when its status does not
    block buying the item: it ended unpaid, was given up on, or was refunded.
    """
    other_cart(unreconciled_cart, status)

    accept(unreconciled_cart, staff, transaction_id='TX-UNREAD')

    assert Cart.objects.get(pk=unreconciled_cart.pk).status == Cart.Status.PAID, f'Refused beside a {status} cart'


@pytest.mark.django_db
def test_a_cart_for_one_of_its_courses_does_not_block_settling_a_bundle(
    unreconciled_cart,  # pylint: disable=redefined-outer-name
):
    """
    Verify that another cart paying for one of a bundle's courses does not block settling the bundle's payment, while
    another cart for the same bundle does: a bundle is sold as is while its learner holds only some of its courses.
    """
    course_item = unreconciled_cart.items.get().catalogue_item
    bundle_item = CatalogueItem.objects.get(sku='BUNDLE-PRO-CERT')
    unreconciled_cart.items.update(catalogue_item=bundle_item)
    other_cart(unreconciled_cart, Cart.Status.PAID, course_item)

    assert BaseProcessor.find_blocking_cart(unreconciled_cart) is None, 'A cart for one of its courses blocks a bundle'

    same_bundle = other_cart(unreconciled_cart, Cart.Status.PAID, bundle_item)
    assert BaseProcessor.find_blocking_cart(unreconciled_cart) == same_bundle, 'The same bundle bought does not block'


@pytest.mark.django_db
@patch('zeitlabs_payments.providers.base.CART_HANDLER', new={})
def test_an_accepted_payment_whose_fulfilment_fails_stays_accepted(
    unreconciled_cart, staff,  # pylint: disable=redefined-outer-name
):
    """
    Verify that a payment accepted whose invoice and enrolment then fail stays accepted, the cart paid without an
    invoice for staff to retry, and that the failure is reported and on the trail.
    """
    with patch('zeitlabs_payments.providers.base.logger.exception'):
        with pytest.raises(ResolutionError, match='was accepted, but its invoice and enrollment failed'):
            accept(unreconciled_cart, staff, transaction_id='TX-UNREAD')

    assert Cart.objects.get(pk=unreconciled_cart.pk).status == Cart.Status.PAID, 'The accepted payment was undone'
    assert not Invoice.objects.filter(cart=unreconciled_cart).exists(), 'A failed fulfilment left an invoice'
    assert trail(unreconciled_cart) == [
        AuditLog.AuditActions.PAYMENT_ACCEPTED,
        AuditLog.AuditActions.INVOICE_CREATION_FAILED,
    ], 'The acceptance and its failed fulfilment are not on the trail'


@pytest.mark.django_db
def test_refunding_an_unreconciled_cart_records_the_amount_typed(
    unreconciled_cart, staff,  # pylint: disable=redefined-outer-name
):
    """
    Verify that the refund of an unreconciled cart is recorded for the amount staff typed, in the cart's currency and
    through the gateway the cart was handed to, and moves the cart to refunded, leaving the refused payment as it was.
    """
    refused = record_refused_payment(unreconciled_cart)

    refund = record_refund(unreconciled_cart.pk, staff, 'RF-9', Decimal('49.99'))

    assert (
        refund.type, refund.gateway, refund.gateway_transaction_id, refund.method, refund.amount, refund.currency,
    ) == (
        Transaction.TransactionType.REFUND, DummyProcessor.SLUG, 'RF-9', DummyProcessor.SLUG, Decimal('49.99'), 'SAR',
    ), 'The refund is not recorded for the amount typed, through the cart\'s gateway'
    assert Cart.objects.get(pk=unreconciled_cart.pk).status == Cart.Status.REFUNDED, 'The cart was not refunded'
    assert Transaction.objects.get(pk=refused.pk).status == 'failed', 'The refused payment was changed'


@pytest.mark.django_db
@pytest.mark.parametrize('break_cart, amount, refusal, usecase', [
    (None, None, 'needs the amount refunded', 'no amount typed in'),
    (None, Decimal('0'), 'needs the amount refunded', 'an amount of nothing'),
    (None, Decimal('1.234'), 'carries 3 decimal place', 'an amount finer than the currency allows'),
    (forget_gateway, Decimal('50'), 'without its gateway recorded', 'a cart handed off before carts recorded it'),
    (hand_to_a_removed_processor, Decimal('50'), 'whose processor is not installed', 'a removed processor'),
])
def test_refunding_an_unreconciled_cart_refuses_what_it_cannot_record(  # pylint: disable=too-many-positional-arguments
    unreconciled_cart, staff, break_cart, amount, refusal, usecase,  # pylint: disable=redefined-outer-name
):
    """
    Verify that the refund of an unreconciled cart is refused, saying why, without a usable amount refunded or a gateway
    to record it through, and records nothing.
    """
    if break_cart:
        break_cart(unreconciled_cart)

    with pytest.raises(ResolutionError, match=refusal):
        record_refund(unreconciled_cart.pk, staff, 'RF-9', amount)

    assert Cart.objects.get(pk=unreconciled_cart.pk).status == Cart.Status.UNRECONCILED, f'Failed usecase: {usecase}'
    assert not Transaction.objects.filter(type=Transaction.TransactionType.REFUND).exists(), \
        f'Failed usecase: {usecase}'


@pytest.mark.django_db
def test_refunding_a_paid_cart_takes_no_amount(paid_cart, staff):  # pylint: disable=redefined-outer-name
    """Verify that the refund of a paid cart, which is its payment, is refused an amount typed in."""
    with pytest.raises(ResolutionError, match='takes no amount'):
        record_refund(paid_cart.pk, staff, 'RF-1', Decimal('1'))

    assert Cart.objects.get(pk=paid_cart.pk).status == Cart.Status.PAID, 'The cart was refunded'


@pytest.mark.django_db
@override_settings(ZEITLABS_PAYMENTS_SETTINGS={'valid_currency': 'XYZ'})
def test_accepting_refuses_a_cart_whose_currency_cannot_be_told(
    unreconciled_cart, staff,  # pylint: disable=redefined-outer-name
):
    """
    Verify that accepting a payment is refused when the deployment's currency is not a supported one, since the payment
    would be recorded in it, and leaves the cart as it was.
    """
    with pytest.raises(ResolutionError, match=f'The currency of cart {unreconciled_cart.pk} cannot be told'):
        accept(unreconciled_cart, staff, transaction_id='TX-UNREAD')

    assert Cart.objects.get(pk=unreconciled_cart.pk).status == Cart.Status.UNRECONCILED, 'The cart was moved'
