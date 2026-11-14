"""test for models."""

import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from string import Formatter
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import ProtectedError
from django.test import override_settings

from zeitlabs_payments.exceptions import CartStatusError
from zeitlabs_payments.models import (
    AuditLog,
    BundleCourseItem,
    Cart,
    CartItem,
    CatalogueItem,
    CreditMemo,
    Invoice,
    InvoiceNumberSequence,
    Transaction,
    WebhookEvent,
)
from zeitlabs_payments.providers.registry import SLUG_MAX_LENGTH

User = get_user_model()

# What a hand-off writes alongside the status.
HAND_OFF = {'gateway': 'dummy', 'payment_origin': 'https://learn.example.com'}


def trading_in(currency_code):
    """
    Return an ``override_settings`` putting the deployment on ``currency_code``.

    A catalogue item's currency must equal the deployment's, so a test covering per-currency precision has to move
    the deployment rather than the item -- on a SAR deployment an IQD item is unsaveable by design.
    """
    return override_settings(
        ZEITLABS_PAYMENTS_SETTINGS={'valid_currency': currency_code}
    )


def processor_named(slug):
    """
    Return the smallest thing log() accepts as a processor: something carrying a SLUG.

    What a slug may look like is settled where processors register, so these tests are free to name one
    whatever the case under test needs.
    """
    return SimpleNamespace(SLUG=slug)


@pytest.mark.django_db
class TestAuditLogModel:
    """
    Tests for AuditLog.log method.
    """
    cart = None

    def setup_method(self):
        self.cart = Cart.objects.create(user=User.objects.create(username='audit-log-buyer'))

    def test_log_with_valid_context(self):
        """
        Should create AuditLog entry with formatted details when context is complete.
        """
        context = {
            'item_id': 1,
            'catalogue_item_id': 2,
            'sku': 'SKU-123',
        }

        log = AuditLog.log(
            action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR,
            context=context,
            cart=self.cart,
            processor=processor_named('payfort'),
        )

        assert log.action == AuditLog.AuditActions.CART_FULFILLMENT_ERROR
        assert log.cart == self.cart
        assert log.gateway == 'payfort'
        assert 'Error during cart fulfillment for item: 1, catalogue_item: 2' in log.details

    def test_log_missing_context_key_raises_validation_error(self):
        """
        Should raise ValidationError when required context keys are missing.
        """
        incomplete_context = {
            'item_id': 1,
            # 'catalogue_item_id' is missing
            'sku': 'SKU-123',
        }

        with pytest.raises(ValidationError, match="Missing template parameters for action 'cart_fulfillment_error'"):
            AuditLog.log(
                action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR,
                context=incomplete_context,
                cart=self.cart,
                processor=processor_named('payfort'),
            )

    def test_log_unknown_action_raises_validation_error(self):
        """
        Verify that an action absent from TEMPLATES is refused rather than written with the raw context as its
        details, so a renamed or misspelled action cannot quietly produce an unreadable audit trail.
        """
        with pytest.raises(ValidationError, match="Unknown audit action 'unknown_action'"):
            AuditLog.log(
                action='unknown_action',
                context={'foo': 'bar'},
                cart=self.cart,
                processor=processor_named('payfort'),
            )

    def test_log_rejects_subtype_on_ordinary_action(self):
        """
        Verify that only GATEWAY_LOG accepts a subtype, so an ordinary action cannot be extended into a name the
        table does not describe.
        """
        with pytest.raises(ValidationError, match='takes a subtype'):
            AuditLog.log(
                action=f'{AuditLog.AuditActions.CART_FULFILLED}.extra',
                context={},
                cart=self.cart,
                processor=processor_named('payfort'),
            )

    def test_log_accepts_gateway_log_subtype(self):
        """
        Verify that an external processor can log under its own GATEWAY_LOG subtype and that the subtype is stored
        as given, which is what keeps its records distinguishable from the plugin's own.
        """
        log = AuditLog.log(
            action=f'{AuditLog.AuditActions.GATEWAY_LOG}.payfort_retry',
            context={'message': 'Retrying the capture.'},
            cart=self.cart,
            processor=processor_named('payfort'),
        )

        assert log.action == 'gw.payfort_retry', 'The subtype must reach the action column unchanged'
        assert log.details == 'Retrying the capture.', 'A gateway log entry renders its message verbatim'

    def test_log_rejects_malformed_gateway_log_subtype(self):
        """
        Verify that a GATEWAY_LOG subtype outside lowercase alphanumerics and underscores is refused, so the action
        column stays queryable by exact match.
        """
        with pytest.raises(ValidationError, match='must be lowercase alphanumerics and underscores'):
            AuditLog.log(
                action=f'{AuditLog.AuditActions.GATEWAY_LOG}.Payfort-Retry',
                context={'message': 'Retrying the capture.'},
                cart=self.cart,
                processor=processor_named('payfort'),
            )

    def test_log_rejects_gateway_log_subtype_ending_in_a_newline(self):
        """
        Verify that a subtype ending in a newline is refused rather than stored with the newline in it.

        An anchor written as ``$`` also matches just before a final newline, so it would let the subtype through,
        and the entry would then never turn up when the trail is filtered by the action it was meant to carry.
        """
        with pytest.raises(ValidationError, match='must be lowercase alphanumerics and underscores'):
            AuditLog.log(
                action=f'{AuditLog.AuditActions.GATEWAY_LOG}.payfort_retry\n',
                context={'message': 'Retrying the capture.'},
                cart=self.cart,
                processor=processor_named('payfort'),
            )

        assert not AuditLog.objects.exists(), 'No entry should have been written with a newline in its action'

    def test_log_rejects_action_longer_than_the_column(self):
        """
        Verify that an over-long action is refused by name rather than reaching the database, where the same
        mistake surfaces as an opaque truncation or driver error.
        """
        with pytest.raises(ValidationError, match='is longer than the 32 characters'):
            AuditLog.log(
                action=f"{AuditLog.AuditActions.GATEWAY_LOG}.{'x' * 40}",
                context={'message': 'Retrying the capture.'},
                cart=self.cart,
                processor=processor_named('payfort'),
            )

    def test_log_rejects_blank_mandatory_arguments(self):
        """
        Verify that an action which declares neither argument optional refuses a missing cart and a blank gateway,
        and names both in one message so the caller does not have to fix them one run at a time.
        """
        with pytest.raises(ValidationError, match="'cart_fulfilled': cart, processor"):
            AuditLog.log(
                action=AuditLog.AuditActions.CART_FULFILLED,
                context={},
                cart=None,
                processor=None,
            )

    def test_log_accepts_blank_optional_argument(self):
        """
        Verify that an argument listed in optional_args may be left out: a response whose merchant reference does
        not resolve is the same event as one that does, and must still be recorded.
        """
        log = AuditLog.log(
            action=AuditLog.AuditActions.RESPONSE_INVALID_CART,
            context={'cart_status': 'unknown', 'required_cart_state': Cart.Status.PROCESSING},
            cart=None,
            processor=processor_named('payfort'),
        )

        assert log.cart is None, 'An action that declares cart optional must be loggable without one'

    def test_log_accepts_cart_cancelled_without_gateway(self):
        """
        Verify that cancelling a cart before checkout logs without a gateway, since no processor has been chosen at
        that point, while CART_STATUS_UPDATED continues to require one.
        """
        log = AuditLog.log(
            action=AuditLog.AuditActions.CART_CANCELLED,
            context={'old_status': Cart.Status.PENDING, 'new_status': Cart.Status.CANCELLED},
            cart=self.cart,
        )

        assert log.gateway is None, 'A pre-checkout cancellation has no gateway to record'

        with pytest.raises(ValidationError, match="'cart_status_updated': processor"):
            AuditLog.log(
                action=AuditLog.AuditActions.CART_STATUS_UPDATED,
                context={'old_status': Cart.Status.PENDING, 'new_status': Cart.Status.PROCESSING},
                cart=self.cart,
            )

    def test_log_records_the_slug_the_processor_is_known_by(self):
        """
        Verify that the gateway column carries the processor's own SLUG, verbatim.

        What a slug may look like is settled at registration, so log() records the name rather than repairing
        it: a processor whose SLUG would need repairing never registers in the first place.
        """
        log = AuditLog.log(
            action=AuditLog.AuditActions.CART_FULFILLED,
            context={},
            cart=self.cart,
            processor=processor_named('hyperpay_mada'),
        )

        assert log.gateway == 'hyperpay_mada', 'The column should carry the slug the processor is known by'

    @pytest.mark.parametrize('processor, usecase', [
        ('payfort', 'the slug passed as a string instead of the processor'),
        (SimpleNamespace(), 'an object with no SLUG at all'),
        (SimpleNamespace(SLUG=''), 'a blank SLUG'),
        (SimpleNamespace(SLUG='   '), 'a whitespace SLUG'),
        (SimpleNamespace(SLUG=7), 'a SLUG that is not a string'),
    ])
    def test_log_rejects_anything_that_is_not_a_processor(self, processor, usecase):
        """
        Verify that only something carrying a usable SLUG is accepted as a processor.

        Passing the slug as a string is the mistake this catches: getattr would find no SLUG, and an action that
        declares the processor optional would then record an unattributable row without complaint.
        """
        with pytest.raises(ValidationError, match='must expose a non-blank SLUG'):
            AuditLog.log(
                action=AuditLog.AuditActions.CART_FULFILLED,
                context={},
                cart=self.cart,
                processor=processor,
            )

        assert not AuditLog.objects.exists(), f'No entry should have been written for {usecase}'

    def test_log_rejects_a_slug_longer_than_the_column(self):
        """
        Verify that an over-long slug is refused by name rather than reaching the database, where the same
        mistake surfaces as an opaque truncation or driver error.

        Registration refuses one this long, so reaching here means the processor was registered by some other
        route; the column is still the column.
        """
        with pytest.raises(ValidationError, match='is longer than the 50 characters'):
            AuditLog.log(
                action=AuditLog.AuditActions.CART_FULFILLED,
                context={},
                cart=self.cart,
                processor=processor_named('x' * 51),
            )

    def test_log_rejects_a_blank_processor_even_where_the_action_allows_none(self):
        """
        Verify that an action which may omit the processor still refuses one that names nothing.

        Omitting it says no processor was involved; handing over one with a blank SLUG says one was and names it
        nothing, which is the unattributable row the column exists to prevent.
        """
        with pytest.raises(ValidationError, match='must expose a non-blank SLUG'):
            AuditLog.log(
                action=AuditLog.AuditActions.CART_CANCELLED,
                context={'old_status': Cart.Status.PENDING, 'new_status': Cart.Status.CANCELLED},
                cart=self.cart,
                processor=processor_named('   '),
            )

    def test_log_records_the_violation_before_raising(self, caplog):
        """
        Verify that a guard violation is logged as well as raised, because log() is called from except blocks on
        the payment path where a broad handler upstream may swallow the exception.
        """
        with caplog.at_level(logging.ERROR):
            with pytest.raises(ValidationError):
                AuditLog.log(
                    action=AuditLog.AuditActions.CART_FULFILLED,
                    context={},
                    cart=self.cart,
                    processor=None,
                )

        assert "Blank mandatory argument(s) for audit action 'cart_fulfilled': processor" in caplog.text, \
            'The violation must reach the logs even if the exception never reaches a handler'

    def test_log_redacts_unmasked_card_number(self, caplog):
        """
        Verify that AuditLog.log replaces any unmasked card number in the rendered details before
        the entry is saved and a warning is logged, so callers that bypass handle_payment cannot
        leak a card number into the audit trail.
        """
        with caplog.at_level(logging.WARNING):
            AuditLog.log(
                action=AuditLog.AuditActions.DUPLICATE_TRANSACTION,
                context={'transaction_id': '4111111111111111', 'cart_status': 'processing'},
                cart=self.cart,
                processor=processor_named('dummy'),
            )

        entry = AuditLog.objects.get(
            action=AuditLog.AuditActions.DUPLICATE_TRANSACTION,
            details__contains='411111****1111',
        )
        assert '4111111111111111' not in entry.details, \
            'The unmasked card number must not reach the audit trail'
        assert 'Unmasked card number found and redacted' in caplog.text, \
            'The redaction must be logged for audit visibility'


def declared_audit_actions():
    """Return the action values declared on ``AuditLog.AuditActions``, by attribute name."""
    return {
        name: value for name, value in vars(AuditLog.AuditActions).items()
        if not name.startswith('_')
    }


def test_audit_actions_and_templates_are_in_sync():
    """
    Verify that the declared actions and the template table describe the same set of actions. An action with no
    template can never be logged, and a template with no action is unreachable; neither shows up at runtime.
    """
    declared = set(declared_audit_actions().values())

    without_template = sorted(declared - set(AuditLog.TEMPLATES))
    without_action = sorted(set(AuditLog.TEMPLATES) - declared)

    assert not without_template, f'Declared actions carrying no template: {without_template}'
    assert not without_action, f'Templates carrying no declared action: {without_action}'


def test_audit_action_names_are_storable():
    """
    Verify that every action name is lowercase alphanumerics and underscores and fits the action column, and that
    GATEWAY_LOG leaves an external processor room for a subtype after it.
    """
    max_length = AuditLog._meta.get_field('action').max_length

    for name, action in declared_audit_actions().items():
        assert AuditLog.ACTION_NAME_PATTERN.match(action), \
            f'{name} is {action!r}, which is not lowercase alphanumerics and underscores'
        assert len(action) <= max_length, \
            f'{name} is {len(action)} characters, past the {max_length} the action column holds'

    subtype_room = max_length - len(AuditLog.AuditActions.GATEWAY_LOG) - len(AuditLog.GATEWAY_LOG_SEPARATOR)
    assert subtype_room >= 20, \
        f'GATEWAY_LOG leaves only {subtype_room} characters for a subtype, which is too few to name one'


def test_audit_template_entries_hold_only_known_fields():
    """
    Verify that each entry holds a non-empty template and nothing beyond the fields the guard reads. A misspelled
    field is silently ignored by ``dict.get``, so an ``optional_arg`` typo would leave the rule unenforced.
    """
    for action, entry in AuditLog.TEMPLATES.items():
        unknown_fields = sorted(set(entry) - {'template', 'optional_args'})

        assert not unknown_fields, f"Entry for '{action}' holds fields the guard never reads: {unknown_fields}"
        assert isinstance(entry.get('template'), str) and entry['template'], \
            f"Entry for '{action}' needs a non-empty template string"


def test_audit_templates_use_plain_named_placeholders():
    """
    Verify that every template interpolates bare ``{name}`` placeholders only. AuditLog.log finds a template's
    required context keys with a regular expression, so a placeholder carrying an index, an attribute or a format
    spec would be invisible to it and the entry would render with a key the caller never had to supply.
    """
    for action, entry in AuditLog.TEMPLATES.items():
        template = entry['template']
        declared = {field for _, field, _, _ in Formatter().parse(template) if field is not None}
        detected = set(re.findall(r'{(\w+)}', template))

        assert declared == detected, \
            f"Template for '{action}' has placeholders log() cannot see: {sorted(declared - detected)}"


def test_audit_optional_args_name_guarded_arguments():
    """
    Verify that every optional_args entry is a distinct name the guard actually checks. A name outside GUARDED_ARGS
    -- a template placeholder, or a typo -- would read as a relaxed rule while relaxing nothing.
    """
    for action, entry in AuditLog.TEMPLATES.items():
        optional_args = entry.get('optional_args', [])

        assert isinstance(optional_args, list), f"optional_args for '{action}' must be a list"
        assert len(set(optional_args)) == len(optional_args), f"optional_args for '{action}' repeats a name"

        unguarded = sorted(set(optional_args) - set(AuditLog.GUARDED_ARGS))
        assert not unguarded, \
            f"optional_args for '{action}' names arguments the guard does not check: {unguarded}"


def test_gateway_log_declares_every_argument_mandatory():
    """
    Verify that GATEWAY_LOG requires both guarded arguments. An entry an external processor writes is only useful
    alongside the cart and gateway it belongs to, so neither may be left out.
    """
    entry = AuditLog.TEMPLATES[AuditLog.AuditActions.GATEWAY_LOG]

    assert 'optional_args' not in entry, 'Every GATEWAY_LOG argument is mandatory'


@pytest.mark.django_db
def test_valid_statuses_returns_all_choices():
    """Test that Cart.valid_statuses() returns all defined status values."""
    expected_statuses = {
        Cart.Status.PENDING,
        Cart.Status.PROCESSING,
        Cart.Status.PAYMENT_PENDING,
        Cart.Status.PAID,
        Cart.Status.CANCELLED,
        Cart.Status.CLOSED,
        Cart.Status.UNRECONCILED,
        Cart.Status.REFUND_REQUESTED,
        Cart.Status.REFUNDED,
    }
    result = set(Cart.valid_statuses())
    assert result == expected_statuses


def cart_trail(cart):
    """Return the cart's audit trail, oldest first: each entry's action and details."""
    return list(AuditLog.objects.filter(cart=cart).order_by('id').values_list('action', 'details'))


@pytest.mark.django_db
def test_change_status_moves_the_cart_and_records_the_move():
    """
    Verify that an allowed move is stored, held by the instance, stamped on updated_at and recorded with both statuses.
    """
    cart = Cart.objects.create(user=User.objects.create(username='change-status-buyer'), status=Cart.Status.PROCESSING)
    last_touched = datetime(2020, 1, 1, tzinfo=timezone.utc)
    Cart.objects.filter(pk=cart.pk).update(updated_at=last_touched)

    cart.change_status(Cart.Status.PAYMENT_PENDING, processor=processor_named('dummy'))

    stored = Cart.objects.get(pk=cart.pk)
    assert stored.status == Cart.Status.PAYMENT_PENDING, 'The move was not stored'
    assert cart.status == Cart.Status.PAYMENT_PENDING, 'The instance does not hold the status it moved the cart to'
    assert stored.updated_at > last_touched, 'The move did not stamp updated_at'
    assert cart.updated_at == stored.updated_at, 'The instance does not hold the stamp it stored'
    assert cart_trail(cart) == [
        (AuditLog.AuditActions.CART_STATUS_UPDATED, 'Status updated for cart from: processing to: payment_pending.'),
    ], 'The move is not on the record with both statuses'


@pytest.mark.django_db
def test_change_status_records_the_move_under_the_action_given():
    """
    Verify that a caller's own action and context replace the default entry, the statuses still filled in.
    """
    cart = Cart.objects.create(user=User.objects.create(username='change-status-buyer'))

    cart.change_status(Cart.Status.CANCELLED, action=AuditLog.AuditActions.CART_CANCELLED)

    assert cart_trail(cart) == [
        (
            AuditLog.AuditActions.CART_CANCELLED,
            'Cart cancelled before checkout. Status updated from: pending to: cancelled.',
        ),
    ], 'The move is not recorded under the action given'


@pytest.mark.django_db
@pytest.mark.parametrize('old_status, new_status', [
    (Cart.Status.PAID, Cart.Status.PROCESSING),
    (Cart.Status.PAID, Cart.Status.CANCELLED),
    (Cart.Status.CANCELLED, Cart.Status.PROCESSING),
    (Cart.Status.PENDING, Cart.Status.PAID),
    (Cart.Status.PAYMENT_PENDING, Cart.Status.PROCESSING),
    (Cart.Status.REFUNDED, Cart.Status.PAID),
    (Cart.Status.PENDING, Cart.Status.CLOSED),
    (Cart.Status.PAYMENT_PENDING, Cart.Status.CLOSED),
    (Cart.Status.CLOSED, Cart.Status.PENDING),
    (Cart.Status.CLOSED, Cart.Status.PROCESSING),
])
def test_change_status_refuses_a_move_the_rules_do_not_allow(old_status, new_status):
    """
    Verify that a move missing from the transition rules is refused, leaving the cart and its audit trail untouched.
    """
    cart = Cart.objects.create(user=User.objects.create(username='change-status-buyer'), status=old_status)

    with pytest.raises(CartStatusError, match=f'cannot move from {old_status} to {new_status}'):
        cart.change_status(new_status, processor=processor_named('dummy'))

    assert Cart.objects.get(pk=cart.pk).status == old_status, f'A {old_status} cart was moved to {new_status}'
    assert not cart_trail(cart), 'A refused move was recorded'


@pytest.mark.django_db
def test_change_status_refuses_when_another_request_moved_the_cart_first():
    """
    Verify that of two requests moving the same cart, the one that comes second is refused.

    Both read the cart as pending, so both pass the transition rules. The second finds the stored status no longer
    pending, is refused without writing anything, and is left holding the status the first one stored.
    """
    cart = Cart.objects.create(user=User.objects.create(username='change-status-buyer'))
    same_cart_in_another_request = Cart.objects.get(pk=cart.pk)
    same_cart_in_another_request.change_status(
        Cart.Status.PROCESSING, processor=processor_named('dummy'), fields=HAND_OFF,
    )

    with pytest.raises(CartStatusError, match='was moved to processing by another request'):
        cart.change_status(Cart.Status.CANCELLED, action=AuditLog.AuditActions.CART_CANCELLED)

    assert Cart.objects.get(pk=cart.pk).status == Cart.Status.PROCESSING, 'The second request overwrote the first'
    assert cart.status == Cart.Status.PROCESSING, 'The refused instance does not hold the stored status'
    assert [action for action, _ in cart_trail(cart)] == [AuditLog.AuditActions.CART_STATUS_UPDATED], \
        'The refused move was recorded'


@pytest.mark.django_db
def test_change_status_stores_nothing_when_the_move_cannot_be_recorded():
    """
    Verify that a move whose audit entry is refused is not stored either, and the instance keeps its old status.
    """
    cart = Cart.objects.create(user=User.objects.create(username='change-status-buyer'), status=Cart.Status.PROCESSING)

    with pytest.raises(ValidationError):
        cart.change_status(Cart.Status.PAYMENT_PENDING)

    assert Cart.objects.get(pk=cart.pk).status == Cart.Status.PROCESSING, 'A move with no record was stored'
    assert cart.status == Cart.Status.PROCESSING, 'The instance holds a status that was never stored'


@pytest.mark.django_db
def test_hand_off_moves_a_pending_cart_to_processing_and_records_its_gateway_and_origin():
    """
    Verify that a handed-off cart is processing and knows its gateway and the origin it is paid on, all stored by the
    one move it records.
    """
    cart = Cart.objects.create(user=User.objects.create(username='hand-off-buyer'))

    cart.change_status(Cart.Status.PROCESSING, processor=processor_named('dummy'), fields=HAND_OFF)

    handed_off = (Cart.Status.PROCESSING, 'dummy', 'https://learn.example.com')
    stored = Cart.objects.get(pk=cart.pk)
    assert (stored.status, stored.gateway, stored.payment_origin) == handed_off, 'The hand-off was not stored'
    assert (cart.status, cart.gateway, cart.payment_origin) == handed_off, 'The instance does not hold the hand-off'
    assert cart_trail(cart) == [
        (AuditLog.AuditActions.CART_STATUS_UPDATED, 'Status updated for cart from: pending to: processing.'),
    ], 'The hand-off is not on the record'


@pytest.mark.django_db
@pytest.mark.parametrize('new_status, fields, usecase', [
    (Cart.Status.PROCESSING, None, 'a hand-off naming nothing'),
    (Cart.Status.PROCESSING, {'payment_origin': 'https://learn.example.com'}, 'a hand-off naming no gateway'),
    (Cart.Status.PROCESSING, {**HAND_OFF, 'gateway': ''}, 'a hand-off naming an empty gateway'),
    (Cart.Status.PROCESSING, {'gateway': 'dummy'}, 'a hand-off naming no origin'),
    (Cart.Status.PROCESSING, {**HAND_OFF, 'payment_origin': ''}, 'a hand-off naming an empty origin'),
    (Cart.Status.PROCESSING, {**HAND_OFF, 'gateway_order_id': 'ORDER-1'}, 'a hand-off writing more'),
    (Cart.Status.CANCELLED, {'gateway': 'dummy'}, 'a move that writes no other column'),
])
def test_change_status_refuses_a_move_not_writing_what_it_requires(new_status, fields, usecase):
    """
    Verify that a move writes exactly the columns the transition rules give it, each with a value, or is refused.

    A hand-off records its gateway and origin in the same step, so a processing cart always knows where it was sent
    and where it was paid on; and no move writes a column the rules do not name for it.
    """
    cart = Cart.objects.create(user=User.objects.create(username='hand-off-buyer'))

    with pytest.raises(CartStatusError, match='each with a value'):
        cart.change_status(new_status, processor=processor_named('dummy'), fields=fields)

    stored = Cart.objects.get(pk=cart.pk)
    assert (stored.status, stored.gateway) == (Cart.Status.PENDING, None), f'Failed usecase: {usecase}'
    assert not cart_trail(cart), f'A refused move was recorded; usecase: {usecase}'


@pytest.mark.django_db
@pytest.mark.parametrize('cart_status', [Cart.Status.PROCESSING, Cart.Status.PAYMENT_PENDING])
def test_record_gateway_order_keeps_the_order_of_an_open_payment(cart_status):
    """
    Verify that the order a cart's gateway names is stored while the payment is open, and held by the instance.

    A gateway opening its order at hand-off names it while the cart is processing; one opening it only when the
    learner pays names it on their return, by when the cart may be awaiting payment.
    """
    cart = Cart.objects.create(user=User.objects.create(username='order-buyer'), status=cart_status, gateway='dummy')

    cart.record_gateway_order('dummy', 'ORDER-1')

    assert Cart.objects.get(pk=cart.pk).gateway_order_id == 'ORDER-1', 'The order was not stored on the cart'
    assert cart.gateway_order_id == 'ORDER-1', 'The instance does not hold the order'


@pytest.mark.django_db
@pytest.mark.parametrize('cart_status, gateway, recorded, usecase', [
    (Cart.Status.PENDING, None, None, 'a cart not handed off'),
    (Cart.Status.PAID, 'dummy', None, 'a cart whose payment is over'),
    (Cart.Status.PROCESSING, 'other', None, 'a cart handed to another gateway'),
    (Cart.Status.PROCESSING, 'dummy', 'ORDER-1', 'a cart whose order is already recorded'),
])
def test_record_gateway_order_refuses_a_cart_it_does_not_belong_on(cart_status, gateway, recorded, usecase):
    """
    Verify that an order is recorded only on an open payment with that gateway and none yet, never replacing one.
    """
    cart = Cart.objects.create(
        user=User.objects.create(username='order-buyer'),
        status=cart_status,
        gateway=gateway,
        gateway_order_id=recorded,
    )

    with pytest.raises(CartStatusError, match='cannot record order ORDER-2 of dummy'):
        cart.record_gateway_order('dummy', 'ORDER-2')

    assert Cart.objects.get(pk=cart.pk).gateway_order_id == recorded, f'Failed usecase: {usecase}'


@pytest.mark.django_db
def test_an_order_id_belongs_to_one_cart_per_gateway():
    """
    Verify that a gateway's order id is recorded on one cart only, while another gateway may issue the same id.

    Two gateways can each number their orders from one, so only a repeat inside one gateway is a collision.
    """
    user = User.objects.create(username='order-buyer')
    Cart.objects.create(user=user, status=Cart.Status.PROCESSING, gateway='dummy', gateway_order_id='ORDER-1')
    other_gateway = Cart.objects.create(user=user, status=Cart.Status.PROCESSING, gateway='other')
    same_gateway = Cart.objects.create(user=user, status=Cart.Status.PROCESSING, gateway='dummy')

    other_gateway.record_gateway_order('other', 'ORDER-1')
    with pytest.raises(IntegrityError):
        same_gateway.record_gateway_order('dummy', 'ORDER-1')

    assert Cart.objects.get(pk=other_gateway.pk).gateway_order_id == 'ORDER-1', "Another gateway's id was refused"
    assert Cart.objects.get(pk=same_gateway.pk).gateway_order_id is None, "One gateway's id was given to two carts"


@pytest.mark.django_db
def test_record_payment_origin_records_the_origin_of_a_cart_without_one():
    """
    Verify that a cart handed off before origins were recorded is given the origin its payment settles on.
    """
    cart = Cart.objects.create(user=User.objects.create(username='origin-buyer'), status=Cart.Status.PROCESSING)

    cart.record_payment_origin('https://learn.example.com')

    assert Cart.objects.get(pk=cart.pk).payment_origin == 'https://learn.example.com', 'The origin was not stored'
    assert cart.payment_origin == 'https://learn.example.com', 'The instance does not hold the origin'


@pytest.mark.django_db
def test_record_payment_origin_keeps_an_origin_already_recorded():
    """
    Verify that an origin already recorded is never replaced, and the instance is left holding it.

    The instance here was read before another request recorded the origin, so it still holds none.
    """
    cart = Cart.objects.create(user=User.objects.create(username='origin-buyer'), status=Cart.Status.PROCESSING)
    Cart.objects.filter(pk=cart.pk).update(payment_origin='https://learn.example.com')

    cart.record_payment_origin('https://other.example.com')

    assert Cart.objects.get(pk=cart.pk).payment_origin == 'https://learn.example.com', \
        'The recorded origin was replaced'
    assert cart.payment_origin == 'https://learn.example.com', 'The instance does not hold the stored origin'


@pytest.mark.django_db
def test_record_payment_origin_refuses_an_unusable_origin():
    """
    Verify that an origin that is not a bare scheme and host is refused and not stored.
    """
    cart = Cart.objects.create(user=User.objects.create(username='origin-buyer'), status=Cart.Status.PROCESSING)

    with pytest.raises(ValidationError, match='Payment origin'):
        cart.record_payment_origin('learn.example.com')

    assert Cart.objects.get(pk=cart.pk).payment_origin is None, 'An unusable origin was stored'


@pytest.mark.django_db
def test_a_failed_hand_off_closes_a_processing_cart_and_records_the_error():
    """
    Verify that a processing cart whose hand-off failed is closed, with the error on the record beside the move.
    """
    cart = Cart.objects.create(user=User.objects.create(username='handoff-buyer'), status=Cart.Status.PROCESSING)

    cart.change_status(
        Cart.Status.CLOSED,
        processor=processor_named('dummy'),
        action=AuditLog.AuditActions.PAYMENT_HANDOFF_FAILED,
        context={'error': 'TimeoutError: the gateway did not answer'},
    )

    assert Cart.objects.get(pk=cart.pk).status == Cart.Status.CLOSED, 'The cart was not closed'
    assert cart.status == Cart.Status.CLOSED, 'The instance does not hold the closed status'
    assert cart_trail(cart) == [
        (
            AuditLog.AuditActions.PAYMENT_HANDOFF_FAILED,
            'Hand-off to the gateway failed: TimeoutError: the gateway did not answer. '
            'Status updated from: processing to: closed.',
        ),
    ], 'The failed hand-off is not on the record with its error'


@pytest.mark.django_db
@pytest.mark.parametrize('action', [
    None,
    AuditLog.AuditActions.CART_STATUS_UPDATED,
    AuditLog.AuditActions.PAYMENT_DECLINED,
])
def test_only_a_failed_hand_off_closes_a_cart(action):
    """
    Verify that a move to closed recorded as anything but a failed hand-off is refused, the cart left processing.
    """
    cart = Cart.objects.create(user=User.objects.create(username='handoff-buyer'), status=Cart.Status.PROCESSING)

    with pytest.raises(CartStatusError, match='only as payment_handoff_failed'):
        cart.change_status(
            Cart.Status.CLOSED, processor=processor_named('dummy'), action=action, context={'response': {}},
        )

    assert Cart.objects.get(pk=cart.pk).status == Cart.Status.PROCESSING, f'A cart was closed as {action}'
    assert not cart_trail(cart), 'A refused close was recorded'


@pytest.mark.django_db
def test_only_a_recorded_refund_moves_a_paid_cart_to_refunded():
    """
    Verify that a paid cart moves to refunded only as a refund staff recorded, never as a plain status update.
    """
    cart = Cart.objects.create(user=User.objects.create(username='refund-buyer'), status=Cart.Status.PAID)

    with pytest.raises(CartStatusError, match='only as refund_recorded'):
        cart.change_status(Cart.Status.REFUNDED, processor=processor_named('dummy'))
    cart.change_status(
        Cart.Status.REFUNDED,
        processor=processor_named('dummy'),
        action=AuditLog.AuditActions.REFUND_RECORDED,
        context={'staff_username': 'support', 'refund_reference': 'RF-1'},
    )

    assert Cart.objects.get(pk=cart.pk).status == Cart.Status.REFUNDED, 'The recorded refund did not move the cart'
    assert [action for action, _ in cart_trail(cart)] == [AuditLog.AuditActions.REFUND_RECORDED], \
        'Only the recorded refund may be on the trail'


@pytest.mark.django_db
@pytest.mark.parametrize('new_status, action, context', [
    (
        Cart.Status.PAID, AuditLog.AuditActions.PAYMENT_ACCEPTED,
        {
            'transaction_id': 'TX-1', 'amount': '50', 'currency': 'SAR', 'staff_username': 'support',
            'recorded': 'no payment',
        },
    ),
    (
        Cart.Status.REFUNDED, AuditLog.AuditActions.REFUND_RECORDED,
        {'staff_username': 'support', 'refund_reference': 'RF-1'},
    ),
])
def test_only_staff_move_an_unreconciled_cart_on(new_status, action, context):
    """
    Verify that an unreconciled cart moves to paid only as a payment staff accepted, and to refunded only as a refund
    staff recorded, never as a plain status update.
    """
    cart = Cart.objects.create(user=User.objects.create(username='held-buyer'), status=Cart.Status.UNRECONCILED)

    with pytest.raises(CartStatusError, match=f'only as {action}'):
        cart.change_status(new_status, processor=processor_named('dummy'))
    cart.change_status(new_status, processor=processor_named('dummy'), action=action, context=context)

    assert Cart.objects.get(pk=cart.pk).status == new_status, f'The cart was not moved to {new_status} as {action}'


def test_every_action_the_transition_rules_demand_is_an_audit_action():
    """
    Verify that an action the transition rules require a move to be recorded under is one the audit log knows.

    The rules spell the action out, since the audit log is defined after the cart, so a renamed action would otherwise
    go unnoticed until the move was refused at run time.
    """
    demanded = {transition.action for transition in Cart.STATUS_TRANSITIONS.values() if transition.action}

    assert demanded <= set(AuditLog.TEMPLATES), f'Unknown actions demanded: {demanded - set(AuditLog.TEMPLATES)}'


@pytest.mark.django_db
def test_save_refuses_to_change_the_stored_status():
    """
    Verify that a save writing a status other than the stored one is refused, and the stored status kept.

    The instance here was read before another request moved the cart, so the save would put back a status the cart
    has already left.
    """
    cart = Cart.objects.create(user=User.objects.create(username='save-buyer'))
    Cart.objects.get(pk=cart.pk).change_status(
        Cart.Status.PROCESSING, processor=processor_named('dummy'), fields=HAND_OFF,
    )

    with pytest.raises(CartStatusError, match='is processing; saving it as pending is refused'):
        cart.save()

    assert Cart.objects.get(pk=cart.pk).status == Cart.Status.PROCESSING, 'The save overwrote the stored status'


@pytest.mark.django_db
def test_save_refuses_an_unusable_payment_origin():
    """
    Verify that a cart is saved with a bare origin or none, and never with an origin that is not a bare scheme and host.

    Both QuerySet.create() and Model.save() skip full_clean(), so the rule is enforced in save(), where an origin fixed
    by hand is held to it too.
    """
    user = User.objects.create(username='save-buyer')
    cart = Cart.objects.create(user=user)
    cart.payment_origin = 'https://learn.example.com'
    cart.save()

    with pytest.raises(ValidationError, match='Payment origin'):
        Cart.objects.create(user=user, payment_origin='learn.example.com')
    cart.payment_origin = 'https://learn.example.com/'
    with pytest.raises(ValidationError, match='Payment origin'):
        cart.save()

    assert list(Cart.objects.filter(user=user).values_list('payment_origin', flat=True)) == [
        'https://learn.example.com',
    ], 'An unusable origin was stored, or a usable one was not'


@pytest.mark.django_db
def test_save_changes_the_stored_status_when_allowed():
    """
    Verify that a save told to change the status writes it, whatever the transition rules say.
    """
    cart = Cart.objects.create(user=User.objects.create(username='save-buyer'), status=Cart.Status.PAID)
    cart.status = Cart.Status.PENDING

    cart.save(allow_status_change=True)

    assert Cart.objects.get(pk=cart.pk).status == Cart.Status.PENDING, 'The allowed status change was not saved'


@pytest.mark.django_db
def test_save_accepts_a_save_that_leaves_the_status_alone():
    """
    Verify that a save is accepted when it writes the stored status back, or does not write the status at all.
    """
    cart = Cart.objects.create(user=User.objects.create(username='save-buyer'))
    other_user = User.objects.create(username='other-save-buyer')

    cart.user = other_user
    cart.save()
    cart.status = Cart.Status.PAID
    cart.save(update_fields=['updated_at'])

    stored = Cart.objects.get(pk=cart.pk)
    assert stored.user == other_user, 'A save keeping the status was not written'
    assert stored.status == Cart.Status.PENDING, 'A save that does not write the status changed it'


@pytest.mark.django_db
def test_cart_totals_add_up_the_lines_they_are_named_for():
    """
    Verify that a cart's total bills the final prices while its gross total keeps the original ones.

    The two are separate columns on the line precisely so a cart can be shown what it costs and what it
    would have cost; summing the same column into both would hide every reduction.
    """
    item = CatalogueItem.objects.create(
        sku='COURSE-TOTALS',
        type=CatalogueItem.ItemType.PAID_COURSE,
        title='Course billed twice in one cart',
        item_ref_id='course-v1:course+totals+1',
        price=Decimal('50.00'),
        currency='SAR',
    )
    cart = Cart.objects.create(user=User.objects.create(username='cart-totals-buyer'))
    CartItem.objects.create(
        cart=cart, catalogue_item=item, original_price=Decimal('50.00'), final_price=Decimal('40.00'),
    )
    CartItem.objects.create(
        cart=cart, catalogue_item=item, original_price=Decimal('30.00'), final_price=Decimal('30.00'),
    )

    assert cart.total == Decimal('70.00'), 'The total should sum the final price of every line'
    assert cart.gross_total == Decimal('80.00'), 'The gross total should sum the original price of every line'


@pytest.mark.django_db
def test_empty_cart_totals_are_zero_rather_than_nothing():
    """
    Verify that a cart with no lines totals a Decimal zero, which arithmetic and formatting can both use.
    """
    cart = Cart.objects.create(user=User.objects.create(username='empty-cart-buyer'))

    assert cart.total == Decimal('0'), 'An empty cart should total zero'
    assert cart.gross_total == Decimal('0'), 'An empty cart should gross zero'


@pytest.mark.django_db
def test_valid_item_types_returns_all_choices():
    """Verify that CatalogueItem.valid_item_types() returns all defined item type values."""
    expected_item_types = [
        CatalogueItem.ItemType.PAID_COURSE,
        CatalogueItem.ItemType.PROGRAM_BUNDLE,
    ]
    result = CatalogueItem.valid_item_types()
    assert result == expected_item_types
    assert len(result) == len(set(result))
    assert all(item_type in dict(CatalogueItem.ItemType.choices) for item_type in result)


@pytest.mark.django_db
def test_bundle_course_item_str():
    """Verify that BundleCourseItem.__str__ formats the bundle -> course mapping."""
    bundle = CatalogueItem.objects.create(
        sku='BUNDLE-1',
        type=CatalogueItem.ItemType.PROGRAM_BUNDLE,
        title='Bundle 1',
        item_ref_id='course-v1:bundle+1+1',
        price=Decimal('100.00'),
        currency='SAR',
    )
    course = CatalogueItem.objects.create(
        sku='COURSE-1',
        type=CatalogueItem.ItemType.PAID_COURSE,
        title='Course 1',
        item_ref_id='course-v1:course+1+1',
        price=Decimal('50.00'),
        currency='SAR',
    )
    link = BundleCourseItem.objects.create(bundle=bundle, course_item=course)
    assert str(link) == 'BUNDLE-1 -> COURSE-1'


@pytest.mark.django_db
def test_catalogue_item_str():
    """Verify that CatalogueItem.__str__ is the SKU, in place of the model's default string."""
    item = CatalogueItem.objects.create(
        sku='COURSE-7',
        type=CatalogueItem.ItemType.PAID_COURSE,
        title='Course 7',
        item_ref_id='course-v1:course+7+7',
        price=Decimal('50.00'),
        currency='SAR',
    )

    assert str(item) == 'COURSE-7', 'The item should read as its SKU'


@pytest.mark.django_db
def test_cart_item_str():
    """Verify that CartItem.__str__ is the SKU of the item it bills, in place of the model's default string."""
    item = CatalogueItem.objects.create(
        sku='COURSE-8',
        type=CatalogueItem.ItemType.PAID_COURSE,
        title='Course 8',
        item_ref_id='course-v1:course+8+8',
        price=Decimal('50.00'),
        currency='SAR',
    )
    line = CartItem.objects.create(
        cart=Cart.objects.create(user=User.objects.create(username='cart-item-str-buyer')),
        catalogue_item=item,
        original_price=Decimal('50.00'),
        final_price=Decimal('50.00'),
    )

    assert str(line) == 'COURSE-8', 'The line should read as the SKU it bills'


@pytest.mark.django_db
def test_catalogue_item_clean_applies_the_same_pricing_rules_as_save():
    """
    Verify that the form path refuses what save refuses, so the admin renders a field error rather than a 500.

    ``clean`` exists only so the admin ModelForm surfaces the refusal; if it stopped applying the rules the
    admin would accept a row that save then rejects deeper in the request.
    """
    item = CatalogueItem(
        sku='COURSE-CLEAN',
        type=CatalogueItem.ItemType.PAID_COURSE,
        title='Course priced in the wrong currency',
        item_ref_id='course-v1:course+clean+1',
        price=Decimal('50.00'),
        currency='USD',
    )

    with pytest.raises(ValidationError) as refusal:
        item.clean()

    assert 'A deployment trades in one currency' in str(refusal.value), \
        'The form path should refuse a currency the deployment does not trade in'


@pytest.mark.django_db
def test_catalogue_item_clean_accepts_a_sellable_item():
    """
    Verify that an item priced in the deployment currency passes the form path.
    """
    item = CatalogueItem(
        sku='COURSE-CLEAN-OK',
        type=CatalogueItem.ItemType.PAID_COURSE,
        title='Course priced in the deployment currency',
        item_ref_id='course-v1:course+clean+2',
        price=Decimal('50.00'),
        currency='SAR',
    )

    assert item.clean() is None, 'A sellable item should pass the form path without a refusal'


@pytest.mark.django_db
def test_catalogue_item_save_rejects_over_precision_price_for_sar():
    """A 3-decimal price for a 2-decimal currency is refused at save() (C2)."""
    with pytest.raises(ValidationError) as exc:
        CatalogueItem.objects.create(
            sku='PRECISION-1',
            type=CatalogueItem.ItemType.PAID_COURSE,
            title='Over-precision SAR price',
            item_ref_id='course-v1:precision+1+1',
            price=Decimal('9.999'),
            currency='SAR',
        )
    assert 'SAR' in str(exc.value)
    assert not CatalogueItem.objects.filter(sku='PRECISION-1').exists()


@pytest.mark.django_db
@trading_in('IQD')
def test_catalogue_item_save_accepts_three_decimal_price_for_iqd():
    """Verify that a 3-decimal price for IQD is accepted on an IQD deployment."""
    item = CatalogueItem.objects.create(
        sku='PRECISION-2',
        type=CatalogueItem.ItemType.PAID_COURSE,
        title='IQD three-decimal price',
        item_ref_id='course-v1:precision+2+1',
        price=Decimal('1.234'),
        currency='IQD',
    )
    item.refresh_from_db()
    assert item.price == Decimal('1.234')


@pytest.mark.django_db
@trading_in('JPY')
def test_catalogue_item_save_accepts_integer_price_for_jpy():
    """A 0-decimal currency rejects fractional prices."""
    CatalogueItem.objects.create(
        sku='PRECISION-3',
        type=CatalogueItem.ItemType.PAID_COURSE,
        title='JPY integer price',
        item_ref_id='course-v1:precision+3+1',
        price=Decimal('1500'),
        currency='JPY',
    )
    with pytest.raises(ValidationError):
        CatalogueItem.objects.create(
            sku='PRECISION-4',
            type=CatalogueItem.ItemType.PAID_COURSE,
            title='JPY fractional price',
            item_ref_id='course-v1:precision+4+1',
            price=Decimal('1500.5'),
            currency='JPY',
        )


@pytest.mark.django_db
def test_catalogue_item_save_rejects_missing_currency():
    """Verify that a sellable catalogue item must carry a currency."""
    with pytest.raises(ValidationError) as exc:
        CatalogueItem.objects.create(
            sku='PRECISION-5',
            type=CatalogueItem.ItemType.PAID_COURSE,
            title='No currency',
            item_ref_id='course-v1:precision+5+1',
            price=Decimal('9.99'),
            currency=None,
        )
    assert 'not a supported ISO 4217 currency' in str(exc.value), \
        f'The error should say the currency is unusable, got: {exc.value}'
    assert not CatalogueItem.objects.filter(sku='PRECISION-5').exists(), \
        'The row must not reach the database when validation refuses it'


@pytest.mark.django_db
def test_catalogue_item_save_rejects_missing_price():
    """
    Verify that a sellable catalogue item must carry a price.

    A null price means *not purchasable*, which is not the same as free -- zero is a valid price, and is covered
    separately below.
    """
    with pytest.raises(ValidationError) as exc:
        CatalogueItem.objects.create(
            sku='PRECISION-7',
            type=CatalogueItem.ItemType.PAID_COURSE,
            title='No price',
            item_ref_id='course-v1:precision+7+1',
            price=None,
            currency='SAR',
        )
    assert 'price' in str(exc.value), f'The error should name the offending field, got: {exc.value}'
    assert not CatalogueItem.objects.filter(sku='PRECISION-7').exists(), \
        'The row must not reach the database when validation refuses it'


@pytest.mark.django_db
def test_catalogue_item_save_accepts_zero_price():
    """Verify that zero is a valid price and is not confused with a missing one."""
    item = CatalogueItem.objects.create(
        sku='PRECISION-8',
        type=CatalogueItem.ItemType.PAID_COURSE,
        title='Free but purchasable',
        item_ref_id='course-v1:precision+8+1',
        price=Decimal('0'),
        currency='SAR',
    )
    item.refresh_from_db()
    assert item.price == Decimal('0'), 'Zero must survive the round trip rather than being treated as unset'


@pytest.mark.django_db
def test_catalogue_item_save_rejects_currency_other_than_the_deployments():
    """
    Verify that a deployment trades in one currency, enforced at save.

    IQD is a real ISO code with a valid 3-decimal price here; what refuses it is that the deployment trades in SAR.
    """
    with pytest.raises(ValidationError) as exc:
        CatalogueItem.objects.create(
            sku='PRECISION-9',
            type=CatalogueItem.ItemType.PAID_COURSE,
            title='Wrong currency for this deployment',
            item_ref_id='course-v1:precision+9+1',
            price=Decimal('1.234'),
            currency='IQD',
        )
    assert 'IQD' in str(exc.value) and 'SAR' in str(exc.value), \
        f'The error should name both the rejected and the expected currency, got: {exc.value}'
    assert not CatalogueItem.objects.filter(sku='PRECISION-9').exists(), \
        'The row must not reach the database when validation refuses it'


@pytest.mark.django_db
@override_settings(ZEITLABS_PAYMENTS_SETTINGS={})
def test_catalogue_item_save_rejects_when_deployment_currency_unset():
    """
    Verify that an unconfigured deployment cannot save a catalogue item at all.

    The old sentinel default made this pass silently and fail later at the gateway; refusing here is the point of
    removing it.
    """
    with pytest.raises(ValidationError) as exc:
        CatalogueItem.objects.create(
            sku='PRECISION-10',
            type=CatalogueItem.ItemType.PAID_COURSE,
            title='Unconfigured deployment',
            item_ref_id='course-v1:precision+10+1',
            price=Decimal('9.99'),
            currency='SAR',
        )
    assert 'valid_currency' in str(exc.value), \
        f'The error should name the setting an operator has to fill in, got: {exc.value}'


@pytest.mark.django_db
def test_catalogue_item_save_rejects_price_too_large_for_the_column():
    """
    Verify that a price too large for the money columns is refused before it reaches the database.

    The money columns hold nine digits ahead of the decimal point. A larger amount satisfies its currency's
    precision and still cannot be stored, and what the database does with it differs by engine -- SQLite errors,
    MySQL truncates -- so a truncated amount would be a wrong amount nobody was told about.
    """
    with pytest.raises(ValidationError) as exc:
        CatalogueItem.objects.create(
            sku='RANGE-1',
            type=CatalogueItem.ItemType.PAID_COURSE,
            title='Too large to store',
            item_ref_id='course-v1:range+1+1',
            price=Decimal('9999999999.99'),
            currency='SAR',
        )
    assert 'digits' in str(exc.value), f'The error should say the amount has too many digits, got: {exc.value}'
    assert not CatalogueItem.objects.filter(sku='RANGE-1').exists(), \
        'The row must not reach the database when validation refuses it'


@pytest.mark.django_db
def test_catalogue_item_save_accepts_the_largest_storable_price():
    """
    Verify that the largest price the columns can hold is still accepted.

    Guards the boundary from the other side: a range check that refused everything would satisfy the test above
    while making the catalogue unusable.
    """
    item = CatalogueItem.objects.create(
        sku='RANGE-2',
        type=CatalogueItem.ItemType.PAID_COURSE,
        title='Largest storable',
        item_ref_id='course-v1:range+2+1',
        price=Decimal('999999999.99'),
        currency='SAR',
    )
    item.refresh_from_db()
    assert item.price == Decimal('999999999.99'), 'Nine integer digits fit and must survive the round trip'


@pytest.mark.django_db
def test_catalogue_item_save_rejects_unknown_currency():
    """An unknown ISO 4217 code raises ValidationError at save() (M4)."""
    with pytest.raises(ValidationError):
        CatalogueItem.objects.create(
            sku='PRECISION-6',
            type=CatalogueItem.ItemType.PAID_COURSE,
            title='Unknown currency',
            item_ref_id='course-v1:precision+6+1',
            price=Decimal('1.00'),
            currency='XYZ',
        )


def issue_invoice(invoice_number, sequence=1):
    """Write an invoice carrying the given number, as code predating the counter would have left it."""
    user = User.objects.create(username=f'buyer-{sequence}')
    cart = Cart.objects.create(user=user, status=Cart.Status.PAID)
    payment = Transaction.objects.create(
        cart=cart, amount=100, gateway='dummy', gateway_transaction_id=f'TX-{sequence}',
    )
    return Invoice.objects.create(
        status=Invoice.InvoiceStatus.PAID,
        invoice_number=invoice_number, total=100, cart=cart, gross_total=100, related_transaction=payment,
    )


@pytest.mark.django_db
def test_invoice_number_sequence_opens_a_series_at_100001():
    """
    Verify that a prefix with no counter and nothing issued behind it opens its series at 100001.
    """
    assert InvoiceNumberSequence.reserve_next_for('TEST') == 100001, 'A fresh prefix starts at 100001'


@pytest.mark.django_db
def test_invoice_number_sequence_reserves_each_number_once():
    """
    Verify that consecutive reservations hand out consecutive numbers rather than repeating one.
    """
    reserved = [InvoiceNumberSequence.reserve_next_for('TEST') for _ in range(3)]
    assert reserved == [100001, 100002, 100003], f'Numbers came out of sequence: {reserved}'


@pytest.mark.django_db
def test_invoice_number_sequence_keeps_one_counter_per_prefix():
    """
    Verify that each prefix advances its own counter and leaves the others where they were.
    """
    assert InvoiceNumberSequence.reserve_next_for('TEST') == 100001, 'The first prefix opens its series'
    assert InvoiceNumberSequence.reserve_next_for('OTHER') == 100001, 'A second prefix opens one of its own'
    assert InvoiceNumberSequence.reserve_next_for('TEST') == 100002, 'The first prefix resumes where it left off'


@pytest.mark.django_db
def test_invoice_number_sequence_opens_above_numbers_issued_before_it_existed():
    """
    Verify that a prefix whose invoices predate its counter opens above them rather than restarting.

    Nothing seeds the counter when the table is created, so the first number a prefix reserves has to account for
    whatever that prefix issued beforehand. Restarting would hand back a number the unique constraint holds.
    """
    issue_invoice('TEST-900000')
    assert InvoiceNumberSequence.reserve_next_for('TEST') == 900001, 'The first reservation clears what exists'


@pytest.mark.django_db
def test_invoice_number_sequence_reads_invoices_only_to_open_a_counter():
    """
    Verify that once a prefix has a counter, an invoice written directly no longer moves it.

    The invoice table is consulted to open a series, never to continue one. A row written afterwards -- by a
    fixture, an import or a hand repair -- is not a reservation.
    """
    assert InvoiceNumberSequence.reserve_next_for('TEST') == 100001, 'The counter opens the series'
    issue_invoice('TEST-900000')
    assert InvoiceNumberSequence.reserve_next_for('TEST') == 100002, 'The counter continues it, not the table'


@pytest.mark.django_db
def test_invoice_number_sequence_opens_under_a_prefix_carrying_its_own_hyphen():
    """
    Verify that a prefix containing a hyphen continues its own series and leaves the shorter one alone.

    A number is split on its last hyphen, so 'EDUBA-DEV-10003' names the series 'EDUBA-DEV'. Reading it as
    'EDUBA' would both restart that series and leave 'EDUBA' set up to reissue numbers it never owned.
    """
    issue_invoice('EDUBA-DEV-10003')
    assert InvoiceNumberSequence.reserve_next_for('EDUBA-DEV') == 10004, 'The hyphenated prefix carries on'
    assert InvoiceNumberSequence.reserve_next_for('EDUBA') == 100001, 'The shorter prefix opens a series of its own'


@pytest.mark.django_db
def test_invoice_number_sequence_opens_across_prefix_spellings():
    """
    Verify that numbers issued under another casing of the prefix still hold their place.

    MySQL's default collation is case-insensitive, so 'test-100009' and 'TEST-100009' are one value to the unique
    constraint over invoice numbers. A counter opened without regard for the old casing would reissue them.
    """
    issue_invoice('test-100009')
    assert InvoiceNumberSequence.reserve_next_for('TEST') == 100010, 'A differently cased number still counts'


@pytest.mark.django_db
@pytest.mark.parametrize(
    'existing, usecase',
    [
        ('TEST-EU-900000', 'a longer prefix that merely starts the same way'),
        ('OTHER-900000', 'an unrelated prefix'),
        ('TEST-INVALID', 'a number whose tail is not numeric'),
    ],
)
def test_invoice_number_sequence_opens_from_its_own_series_only(existing, usecase):
    """
    Verify that opening a counter reads only the numbers belonging to its own prefix.
    """
    issue_invoice(existing)
    assert InvoiceNumberSequence.reserve_next_for('TEST') == 100001, f'Failed usecase: {usecase}'


@pytest.mark.parametrize(
    'origin, usecase',
    [
        (
            'https://paid-here.example.com',
            'the plain origin the cart was paid on',
        ),
        (
            'http://localhost:18000',
            'a deployment reachable only over plain HTTP, on a port',
        ),
    ],
)
def test_invoice_receipt_url_is_addressed_on_the_recorded_origin(origin, usecase):
    """
    Verify that the receipt URL joins the reversed receipt path onto the origin the invoice's cart was paid on.

    Scheme, host and port all come from that one recorded string, so a receipt is addressed exactly where the
    payment was made -- including a deployment that answers only over plain HTTP.
    """
    invoice = Invoice(invoice_number='TEST-100001', cart=Cart(payment_origin=origin))
    expected = f'{origin}/payment/v2/invoice/{invoice.public_token}/'
    assert invoice.receipt_url == expected, f'Failed usecase: {usecase}; got {invoice.receipt_url!r}'


def test_invoice_receipt_url_is_addressed_by_token_not_by_invoice_number():
    """
    Verify that the receipt URL carries the invoice's token and never its number.

    The numbered address is private to the invoice's owner, so a QR built on it is readable by one person.
    The token is what lets whoever holds the printed copy check it, which is the reason the QR is there.
    """
    invoice = Invoice(invoice_number='TEST-100001', cart=Cart(payment_origin='https://paid-here.example.com'))

    assert str(invoice.public_token) in invoice.receipt_url, \
        f'The receipt URL must be addressed by token, got {invoice.receipt_url!r}'
    assert 'TEST-100001' not in invoice.receipt_url, \
        f'The invoice number must not appear in a publicly readable URL, got {invoice.receipt_url!r}'


def test_invoice_public_token_is_unique_per_invoice():
    """
    Verify that each invoice is born with its own token rather than sharing one.

    The token is the whole permission to read an invoice, so two invoices sharing one would let the holder
    of either copy open the other.
    """
    tokens = {Invoice(invoice_number=f'TEST-10000{index}').public_token for index in range(5)}

    assert len(tokens) == 5, f'Five invoices must carry five distinct tokens, got {len(tokens)}'


@pytest.mark.parametrize(
    'origin, usecase',
    [
        (None, 'a cart paid before origins were recorded'),
        ('', 'an origin column left empty by a bad write'),
    ],
)
def test_invoice_receipt_url_is_none_without_an_origin(origin, usecase):
    """
    Verify that an invoice whose cart has no origin has no receipt URL, so its receipt prints no QR code.

    The origin has to be recorded on the cart before a QR is worth printing onto paper that cannot be reissued.
    """
    invoice = Invoice(invoice_number='TEST-100001', cart=Cart(payment_origin=origin))

    assert invoice.receipt_url is None, f'Failed usecase: {usecase}; got {invoice.receipt_url!r}'


@pytest.mark.django_db
@pytest.mark.parametrize('invoice_status, usecase', [
    (None, 'no status given, which a field without a default would store as an empty string'),
    ('unpaid', 'a status the model does not name'),
])
def test_invoice_cannot_be_saved_without_a_status_it_names(invoice_status, usecase):
    """
    Verify that an invoice is saved only with a status InvoiceStatus names, which it must be given explicitly.

    An invoice is drawn once its payment has settled, so paid is the one status it can have. The column has no
    default, and both QuerySet.create() and Model.save() skip full_clean(), so the rule is enforced in save().
    """
    user = User.objects.create(username='buyer-status')
    cart = Cart.objects.create(user=user, status=Cart.Status.PAID)
    payment = Transaction.objects.create(cart=cart, amount=100, gateway='dummy', gateway_transaction_id='TX-STATUS')
    fields = {
        'invoice_number': 'TEST-100003', 'cart': cart, 'total': 100, 'gross_total': 100,
        'related_transaction': payment,
    }
    if invoice_status is not None:
        fields['status'] = invoice_status

    with pytest.raises(ValidationError, match='is not one of paid'):
        Invoice.objects.create(**fields)

    assert not Invoice.objects.filter(invoice_number='TEST-100003').exists(), f'Failed usecase: {usecase}'

    Invoice.objects.create(**{**fields, 'status': Invoice.InvoiceStatus.PAID})
    assert Invoice.objects.filter(invoice_number='TEST-100003').exists(), 'A paid invoice must be saved'


# Every column that stores a gateway name, and the rule deciding what a name may be, are one decision spelled
# in four places. Pinned here so that changing one of them without the others fails loudly.
GATEWAY_NAME_LENGTH = 50

# What both gateway-length guards report when they fail, kept in one place so they say the same thing.
GATEWAY_LENGTH_INCONSISTENCY = (
    'All gateway columns and SLUG_MAX_LENGTH must hold exactly the same number of characters. They do not, which'
    ' means the data is now inconsistent: a slug the registry accepts can be too long for a column that stores'
    ' it, so the same payment is recorded under its full name in one table and a truncated name -- or no row at'
    ' all -- in another, and no query can reconcile the two afterwards. Change Transaction.gateway,'
    ' WebhookEvent.gateway, AuditLog.gateway and SLUG_MAX_LENGTH together, or change none of them.'
)


def test_gateway_column_length_guard():
    """
    Verify that no one changes the width of a gateway column by mistake.

    A gateway name is written into four tables and nothing in Django ties their widths together, so this test is
    the only thing keeping them equal.
    """
    widths = {
        'Cart.gateway': Cart._meta.get_field('gateway').max_length,
        'Transaction.gateway': Transaction._meta.get_field('gateway').max_length,
        'WebhookEvent.gateway': WebhookEvent._meta.get_field('gateway').max_length,
        'AuditLog.gateway': AuditLog._meta.get_field('gateway').max_length,
    }

    for column, width in widths.items():
        assert width == GATEWAY_NAME_LENGTH, (
            f'{column} holds {width} characters, expected {GATEWAY_NAME_LENGTH}. {GATEWAY_LENGTH_INCONSISTENCY}'
        )


def test_slug_max_length_guard():
    """
    Verify that no one changes SLUG_MAX_LENGTH by mistake.

    The registry decides whether a processor loads at all, so a cap wider than the columns admits a name the
    database cannot hold, and a cap narrower than them turns a storable name into a processor that refuses to
    register.
    """
    assert SLUG_MAX_LENGTH == GATEWAY_NAME_LENGTH, (
        f'SLUG_MAX_LENGTH is {SLUG_MAX_LENGTH}, expected {GATEWAY_NAME_LENGTH}. {GATEWAY_LENGTH_INCONSISTENCY}'
    )


@pytest.mark.django_db
def test_deleting_a_learner_takes_the_carts_nothing_was_paid_through():
    """
    Verify that deleting a learner whose carts never saw a payment deletes those carts and their audit trail with them.

    Such a cart is the learner's working state only, so it has no reason to outlive the account.
    """
    learner = User.objects.create(username='abandoner')
    cart = Cart.objects.create(user=learner, status=Cart.Status.CANCELLED)
    AuditLog.log(
        action=AuditLog.AuditActions.CART_CANCELLED,
        cart=cart,
        context={'old_status': Cart.Status.PENDING, 'new_status': Cart.Status.CANCELLED},
    )

    learner.delete()

    assert not Cart.objects.filter(pk=cart.pk).exists(), 'The abandoned cart outlived its learner'
    assert not AuditLog.objects.filter(cart_id=cart.pk).exists(), 'The abandoned cart left its trail behind'


@pytest.mark.django_db
def test_deleting_a_learner_with_a_payment_on_record_is_refused():
    """
    Verify that a learner whose cart has a payment on record cannot be deleted, and nothing of theirs is.

    The payment would otherwise lose the cart and learner it was made for. That holds for a payment that was never
    settled too: an unreconciled cart has no invoice, and its transaction is the only record that money was taken.
    """
    learner = User.objects.create(username='payer')
    cart = Cart.objects.create(user=learner, status=Cart.Status.UNRECONCILED)
    payment = Transaction.objects.create(cart=cart, amount=100, gateway='dummy', gateway_transaction_id='TX-1')

    with pytest.raises(ProtectedError):
        learner.delete()

    assert User.objects.filter(pk=learner.pk).exists(), 'The learner was deleted despite the payment'
    assert Cart.objects.filter(pk=cart.pk).exists(), 'The cart was deleted with its learner'
    assert Transaction.objects.get(pk=payment.pk).cart_id == cart.pk, 'The payment lost its cart'


@pytest.mark.django_db
def test_deleting_a_learner_with_an_invoice_is_refused():
    """Verify that a learner with an issued invoice cannot be deleted, so the invoice is never deleted with them."""
    invoice = issue_invoice('TEST-100001')

    with pytest.raises(ProtectedError):
        invoice.cart.user.delete()

    assert User.objects.filter(pk=invoice.cart.user_id).exists(), 'The learner was deleted despite the invoice'
    assert Invoice.objects.filter(pk=invoice.pk).exists(), 'The invoice was deleted with its learner'
    assert Cart.objects.filter(pk=invoice.cart_id).exists(), 'The cart was deleted with its learner'
    assert Transaction.objects.filter(pk=invoice.related_transaction_id).exists(), 'The payment was deleted'


@pytest.mark.django_db
def test_deleting_an_invoice_with_a_credit_memo_is_refused():
    """Verify that an invoice a credit memo reverses cannot be deleted, so the credit memo is never deleted with it."""
    invoice = issue_invoice('TEST-100001')
    memo = CreditMemo.objects.create(invoice=invoice, total=100, reason='refund', gateway_refund_transaction_id='RF-1')

    with pytest.raises(ProtectedError):
        invoice.delete()

    assert CreditMemo.objects.filter(pk=memo.pk).exists(), 'The credit memo was deleted with its invoice'
