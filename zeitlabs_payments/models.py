"""Zeitlabs payments models."""
import logging
import re
import uuid
from decimal import Decimal
from typing import Any, NamedTuple, NoReturn, NotRequired, Optional, TypedDict
from urllib.parse import urljoin

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.urls import reverse
from django.utils.timezone import now

from zeitlabs_payments.exceptions import CartStatusError
from zeitlabs_payments.redaction import mask_unmasked_card_numbers
from zeitlabs_payments.settings_service import get_settings
from zeitlabs_payments.validators import validate_catalogue_item_pricing, validate_payment_origin

User = get_user_model()
logger = logging.getLogger(__name__)


class ActionTemplate(TypedDict):
    """What an audit action renders with, and which of its arguments may be left blank."""

    template: str
    optional_args: NotRequired[list[str]]


class TimeStampedModel(models.Model):
    """TimeStamped model."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class StatusTransition(NamedTuple):
    """What a cart's move from one status to another requires, beyond the move itself."""

    # The columns the move writes alongside the status, every one of them given a value.
    fields: frozenset[str] = frozenset()
    # The audit action the move must be recorded under, when only one reason may make it. None lets the caller name
    # the action.
    action: Optional[str] = None


class Cart(TimeStampedModel):
    """
    Cart model.

    .. no_pii:
    """

    class Status(models.TextChoices):
        """Cart states."""

        # Open: the learner's current cart, not yet handed to a gateway. A learner has one at a time; starting a new
        # cart cancels the older ones.
        PENDING = 'pending'
        # Handed to a gateway, which has not answered about it yet. A cart the learner abandons on the gateway's page
        # stays here, since nothing tells the plugin about it. Its move records the gateway and the payment origin in
        # the same step.
        PROCESSING = 'processing'
        # Settled: the payment is recorded. The invoice and the enrolment follow, so a paid cart without an invoice is
        # one whose fulfilment failed, until a person retries it or records its refund.
        PAID = 'paid'
        # Ended without payment: replaced by a newer cart before it was handed off, or declined by the gateway.
        CANCELLED = 'cancelled'
        # Ended without payment because the hand-off itself failed on our side -- the call to the gateway raised before
        # the learner was sent anywhere. Final rather than pending again: a cart that has left pending is never handed
        # off twice, since a call that timed out may still have opened an order at the gateway. Not cancelled either,
        # which reads as a decline. The learner starts a new cart instead.
        CLOSED = 'closed'
        # Handed to a gateway and given up on: the learner came back to buy the course again, no order could be found
        # for the cart, and its payment session had lapsed. Unlike a cancelled cart it can still be paid, since a
        # payment that completes late settles it -- or leaves it unreconciled, when the course was bought again
        # meanwhile.
        EXPIRED = 'expired'
        # The gateway has answered about the payment at least once, and it has not settled yet.
        PAYMENT_PENDING = 'payment_pending'
        # Charged, not reconciled: the gateway reports the payment taken, but our checks refused it or it could not be
        # recorded, so the learner is not enrolled. Held until a person accepts the payment, which enrols them, or
        # records its refund; nothing else moves it on.
        UNRECONCILED = 'unreconciled'
        # Kept for refund requests, which nothing writes yet.
        REFUND_REQUESTED = 'refund_requested'
        # Paid, and the payment refunded in the gateway's dashboard, as a person recorded it. So far only a cart paid
        # without an invoice, or one whose payment was never reconciled, is refunded.
        REFUNDED = 'refunded'

    # Every move a cart can make, and what each requires. A status nothing moves out of is final. A cart is moved only
    # through change_status, which enforces this table. Actions are spelled out as AuditLog.AuditActions values, since
    # AuditLog is defined below; a test keeps the two in step.
    STATUS_TRANSITIONS: dict[tuple[str, str], StatusTransition] = {
        # The hand-off records the gateway the cart is handed to, so a payment can be looked for again later, and the
        # origin it is handed off from, which its invoice is issued on.
        (Status.PENDING, Status.PROCESSING): StatusTransition(fields=frozenset({'gateway', 'payment_origin'})),
        (Status.PENDING, Status.CANCELLED): StatusTransition(),
        (Status.PROCESSING, Status.PAYMENT_PENDING): StatusTransition(),
        (Status.PROCESSING, Status.PAID): StatusTransition(),
        # Only a hand-off that failed on our side closes a cart.
        (Status.PROCESSING, Status.CLOSED): StatusTransition(action='payment_handoff_failed'),
        # Only a payment session given up on expires a cart.
        (Status.PROCESSING, Status.EXPIRED): StatusTransition(action='payment_expired'),
        # A payment completing after its cart expired still settles it, unless the course was bought again meanwhile.
        (Status.EXPIRED, Status.PAID): StatusTransition(),
        (Status.EXPIRED, Status.UNRECONCILED): StatusTransition(action='payment_unreconciled'),
        (Status.PAYMENT_PENDING, Status.PAID): StatusTransition(),
        (Status.PAYMENT_PENDING, Status.CANCELLED): StatusTransition(),
        # Only money the gateway reports taken, and the plugin could not reconcile, makes a cart unreconciled.
        (Status.PAYMENT_PENDING, Status.UNRECONCILED): StatusTransition(action='payment_unreconciled'),
        # Only a refund recorded by a person moves a paid cart on, and only one without an invoice: refunding a
        # completed sale is a refund flow nothing writes yet. The invoice is checked by the caller, since this table
        # cannot say it.
        (Status.PAID, Status.REFUNDED): StatusTransition(action='refund_recorded'),
        # Only a person who confirmed the payment with the gateway accepts an unreconciled cart's payment, and only a
        # recorded refund ends it otherwise.
        (Status.UNRECONCILED, Status.PAID): StatusTransition(action='payment_accepted'),
        (Status.UNRECONCILED, Status.REFUNDED): StatusTransition(action='refund_recorded'),
    }

    # The statuses of a cart that do not stop its learner buying what it holds again. Every other status blocks, so a
    # status added later blocks until it is listed here. Pending is listed although it is not finished: making the new
    # cart cancels the learner's pending ones. The others are finished without the learner keeping what the cart held:
    # ended unpaid, given up on, or refunded. An expired cart whose payment completes late while another cart blocks
    # its course is held as unreconciled rather than settled, see BaseProcessor.find_blocking_cart.
    NON_BLOCKING_STATUSES = (Status.PENDING, Status.CANCELLED, Status.CLOSED, Status.EXPIRED, Status.REFUNDED)

    # Cascade on purpose. A cart with no payment on record -- abandoned before checkout, or at the gateway's page with
    # nothing reported back -- is only the learner's working state, and goes with their account. A cart with a payment
    # on record is not deleted this way: its transactions and invoices PROTECT it, so deleting a learner with a payment
    # on record is refused as a whole. Do not delete such an account; retirement anonymises it and leaves these rows
    # untouched.
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='carts')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    # The gateway the cart was handed to, by its processor's SLUG, written when the cart is handed off. Empty for a cart
    # never handed off.
    gateway = models.CharField(max_length=50, null=True, blank=True)
    # The id the gateway gave the order it opened for the cart. A gateway that opens its order at hand-off names it
    # then; one that opens it only when the learner pays names it when the learner comes back. Written once, and empty
    # until then.
    gateway_order_id = models.CharField(max_length=255, null=True, blank=True)
    # The origin the learner paid on -- scheme and host together, 'https://learn.example.com'. Written once: at
    # hand-off, or when the payment settles for a cart handed off before origins were recorded. The cart's invoice is
    # issued for that site and shown only there, and its receipt's QR code is addressed on it, so the origin is never
    # changed: a printed receipt cannot be reissued. Only the origin is kept; the receipt's path is reversed at render
    # time, so a later routing change still reaches receipts nobody has printed yet. Empty until the hand-off, and on
    # carts paid before origins were recorded, whose invoices have no QR code and are shown on any site.
    payment_origin = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        unique_together = ('gateway', 'gateway_order_id')
        # What staff may do, from the cart admin, to resolve a cart whose payment did not end in an invoice: a paid cart
        # left without one, or an unreconciled one. One permission each, since each enrols a learner or records money
        # taken or given back.
        permissions = [
            ('retry_cart_fulfillment', 'Can retry the invoice and enrollment of a paid cart without an invoice'),
            (
                'record_cart_refund',
                'Can record the refund of a paid cart without an invoice, or of an unreconciled cart',
            ),
            ('accept_cart_payment', 'Can accept the payment of an unreconciled cart'),
        ]

    def save(self, *args: Any, allow_status_change: bool = False, **kwargs: Any) -> None:
        """
        Save the cart, refusing an unusable payment origin and any status change the caller has not asked for.

        A status change belongs to ``change_status``, which checks the move is allowed and makes it only if no other
        request moved the cart first. A save does neither: it writes whatever status the instance holds, even one
        another request has since moved the cart away from. So a save that would write a status other than the stored
        one is refused, unless ``allow_status_change`` is set: for a data fix run by hand, or a test setting up a cart.

        The payment origin is checked here as well as where it is first written, so an origin fixed by hand is held to
        the same rule. ``QuerySet.create()`` and ``Model.save()`` both skip ``full_clean()``.

        :param allow_status_change: Write the instance's status even when it differs from the stored one.
        :raises CartStatusError: When the save would change the stored status and ``allow_status_change`` is not set.
        :raises ValidationError: When the payment origin is set and unusable, see ``validate_payment_origin``.
        """
        if self.payment_origin is not None:
            validate_payment_origin(self.payment_origin)
        update_fields = kwargs.get('update_fields')
        writes_status = update_fields is None or 'status' in update_fields
        if writes_status and not allow_status_change and not self._state.adding:
            stored_status = Cart.objects.filter(pk=self.pk).values_list('status', flat=True).first()
            if stored_status != self.status:
                raise CartStatusError(
                    f'Cart {self.pk} is {stored_status}; saving it as {self.status} is refused. '
                    'Use change_status to move a cart.',
                )
        super().save(*args, **kwargs)

    def change_status(
        self,
        new_status: str,
        *,
        processor: Optional[Any] = None,
        action: Optional[str] = None,
        context: Optional[dict] = None,
        fields: Optional[dict] = None,
    ) -> None:
        """
        Move the cart to a new status and record the move in its audit trail.

        The move must be one ``STATUS_TRANSITIONS`` lists from the status the instance holds, made with what it
        requires: the columns it writes, and the action it must be recorded under. It is made by a single conditional
        update that matches the cart only while it still holds that status, so of two requests moving the same cart
        only the first succeeds; the second finds the status changed and is refused. A caller whose next step only one
        request may take -- calling the gateway, settling a payment -- moves the cart first and acts after.

        :param new_status: The status to move the cart to.
        :param processor: The processor the move is made for, recorded on the audit entry. None where the action
            allows it.
        :param action: The audit action recording the move, ``cart_status_updated`` when None. Its template is given
            ``old_status`` and ``new_status``.
        :param context: Any further values the action's template needs.
        :param fields: The columns the move writes alongside the status, exactly those it requires.
        :raises CartStatusError: When the move is not listed from the instance's status, is not made as it requires,
            or another request moved the cart first. In the last case the instance is refreshed to the stored status.
        """
        old_status = self.status
        transition = self.STATUS_TRANSITIONS.get((old_status, new_status))
        if transition is None:
            raise CartStatusError(f'Cart {self.pk} cannot move from {old_status} to {new_status}.')
        if transition.action and action != transition.action:
            raise CartStatusError(
                f'Cart {self.pk} moves from {old_status} to {new_status} only as {transition.action}.',
            )
        fields = fields or {}
        if set(fields) != transition.fields or not all(fields.values()):
            raise CartStatusError(
                f'Cart {self.pk} moving from {old_status} to {new_status} must write '
                f'{", ".join(sorted(transition.fields)) or "nothing else"}, each with a value; given {sorted(fields)}.',
            )

        updated_at = now()
        with transaction.atomic():
            moved = self._update_if(
                {'status': old_status}, {'status': new_status, 'updated_at': updated_at, **fields},
            )
            if not moved:
                self.refresh_from_db(fields=['status'])
                raise CartStatusError(
                    f'Cart {self.pk} was moved to {self.status} by another request before it could move from '
                    f'{old_status} to {new_status}.',
                )
            AuditLog.log(
                action=action or AuditLog.AuditActions.CART_STATUS_UPDATED,
                cart=self,
                processor=processor,
                context={**(context or {}), 'old_status': old_status, 'new_status': new_status},
            )
        self.status = new_status
        self.updated_at = updated_at
        for name, value in fields.items():
            setattr(self, name, value)

    def record_gateway_order(self, gateway: str, gateway_order_id: str) -> None:
        """
        Record the id the gateway gave the order it opened for the cart.

        Written once, while the payment is open and only for the gateway the cart was handed to, so a recorded order is
        never replaced.

        :param gateway: The SLUG of the processor naming the order.
        :param gateway_order_id: The id the gateway gave the order.
        :raises CartStatusError: When the cart is not processing or awaiting payment, was handed to another gateway, or
            already has an order recorded.
        :raises IntegrityError: When the gateway already gave that id to another cart's order.
        """
        recorded = self._update_if(
            {
                'status__in': [self.Status.PROCESSING, self.Status.PAYMENT_PENDING],
                'gateway': gateway,
                'gateway_order_id__isnull': True,
            },
            {'gateway_order_id': gateway_order_id},
        )
        if not recorded:
            raise CartStatusError(
                f'Cart {self.pk} cannot record order {gateway_order_id} of {gateway}: it is not open for payment with '
                'that gateway, or already has an order recorded.',
            )
        self.gateway_order_id = gateway_order_id

    def record_payment_origin(self, payment_origin: str) -> None:
        """
        Record the origin a cart handed off before origins were recorded was paid on, unless it already has one.

        An origin is written once, so one already recorded -- at hand-off, or by another request settling the same
        payment -- is kept, and the instance is left holding whichever is stored.

        :param payment_origin: The origin the cart was paid on, scheme and host together.
        :raises ValidationError: When the origin is unusable, see ``validate_payment_origin``.
        """
        validate_payment_origin(payment_origin)
        self._update_if({'payment_origin__isnull': True}, {'payment_origin': payment_origin})
        self.refresh_from_db(fields=['payment_origin'])

    def _update_if(self, conditions: dict, values: dict) -> bool:
        """
        Write the values to the cart's row only while it still matches the conditions, and return whether it did.

        One conditional update, so a request that read the cart before another one changed it writes nothing rather
        than overwriting what the other wrote. It runs in a savepoint of its own: a caller that catches a database
        error the write raises, such as a duplicate order id, can keep using the connection afterwards.

        :param conditions: Lookups the row must match, beside its primary key.
        :param values: The columns to write.
        """
        with transaction.atomic():
            return bool(Cart.objects.filter(pk=self.pk, **conditions).update(**values))

    @property
    def total(self) -> Decimal:
        """Calculate total."""
        return sum((item.final_price for item in self.items.all()), Decimal('0'))

    @property
    def gross_total(self) -> Decimal:
        """Calculate raw total from the original item prices."""
        return sum((item.original_price for item in self.items.all()), Decimal('0'))

    @classmethod
    def valid_statuses(cls) -> list[str]:
        """Return all valid status values."""
        return [choice[0] for choice in cls.Status.choices]


class Transaction(TimeStampedModel):
    """
    Transaction model.

    .. pii: response and reason, as the gateway answered: the payer's name, email, IP, address and masked card.
    .. pii_types: name, email_address, ip, location, other
    .. pii_retirement: retained
    """

    class TransactionType(models.TextChoices):
        """Transaction types."""

        PAYMENT = 'payment'
        REFUND = 'refund'

    # PROTECT: a payment must keep pointing at the cart, and through it the learner, it was made for. Clearing the link
    # would leave money on record that nobody can be traced to. Nullable only because rows exist that lost their cart
    # before this rule.
    cart = models.ForeignKey(Cart, on_delete=models.PROTECT, related_name='transactions', null=True)
    type = models.CharField(max_length=20, choices=TransactionType.choices)
    status = models.CharField(max_length=50)
    gateway = models.CharField(max_length=50)
    gateway_transaction_id = models.CharField(max_length=255)
    method = models.CharField(max_length=50)
    amount = models.DecimalField(max_digits=12, decimal_places=3)
    currency = models.CharField(max_length=3)
    response = models.JSONField(blank=True, null=True)
    reason = models.TextField(blank=True, null=True)
    initiator_user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        """Transaction model metadata."""

        unique_together = ('gateway', 'gateway_transaction_id')


class WebhookEvent(TimeStampedModel):
    """
    WebhookEvent model is not used yet.

    .. pii: payload, as the gateway sent it: the payer's name, email, IP, address and masked card.
    .. pii_types: name, email_address, ip, location, other
    .. pii_retirement: retained
    """

    gateway = models.CharField(max_length=50)
    event_type = models.CharField(max_length=100)
    payload = models.JSONField()
    related_transaction = models.ForeignKey(Transaction, on_delete=models.SET_NULL, null=True, blank=True)
    handled = models.BooleanField(default=False)


class AuditLog(TimeStampedModel):
    """
    AuditLog model.

    .. pii: details embeds gateway answers -- the payer's name, email, IP, address, masked card -- and staff usernames.
    .. pii_types: name, email_address, ip, location, username, other
    .. pii_retirement: retained
    """

    class AuditActions:
        """Audit log actions."""

        BUNDLE_FULFILLMENT_FAILED = 'bundle_fulfillment_failed'
        CART_CANCELLED = 'cart_cancelled'
        CART_FULFILLED = 'cart_fulfilled'
        CART_FULFILLMENT_ERROR = 'cart_fulfillment_error'
        CART_STATUS_UPDATED = 'cart_status_updated'
        DUPLICATE_TRANSACTION = 'duplicate_transaction_detected'
        FULFILLMENT_RETRIED = 'fulfillment_retried'
        GATEWAY_RESPONSE_REJECTED = 'gateway_response_rejected'
        INVOICE_CREATION_FAILED = 'invoice_creation_failed'
        PAYMENT_ACCEPTED = 'payment_accepted'
        PAYMENT_CANCELLED = 'payment_cancelled'
        PAYMENT_DECLINED = 'payment_declined'
        PAYMENT_EXPIRED = 'payment_expired'
        PAYMENT_HANDOFF_FAILED = 'payment_handoff_failed'
        PAYMENT_UNRECONCILED = 'payment_unreconciled'
        REDIRECT_TO_PAYMENT = 'redirect_to_payment_gateway'
        REFUND_RECORDED = 'refund_recorded'
        RESPONSE_INVALID_CART = 'response_for_invalid_cart'
        TRANSACTION_NOT_RECONCILED = 'transaction_not_reconciled'
        TRANSACTION_ROLLED_BACK = 'transaction_rolled_back'
        USER_ALREADY_ENROLLED = 'user_already_enrolled'
        USER_ENROLLED = 'user_enrolled'
        USER_ENROLLED_ERROR = 'user_enrolled_error'
        USER_UPGRADED = 'user_upgraded'

        # Not an event this package records. A processor extends it with a subtype of its own,
        # 'gw.payfort_retry', so its entries stay distinguishable in one shared trail. Last here
        # because it is the only name that is a prefix rather than a whole action.
        GATEWAY_LOG = 'gw'

    # The log() arguments the guard checks. Both map to a nullable column -- processor to gateway --
    # so nothing but this table decides whether an action is allowed to leave one of them out.
    GUARDED_ARGS = ('cart', 'processor')

    # Separates GATEWAY_LOG from the subtype an external processor picks for its own records.
    GATEWAY_LOG_SEPARATOR = '.'

    # Every action name, and every GATEWAY_LOG subtype, is lowercase alphanumerics and underscores.
    ACTION_NAME_PATTERN = re.compile(r'^[a-z0-9_]+\Z')

    # Each entry is the action's template plus, optionally, the GUARDED_ARGS names that may be left
    # blank for it. An argument is listed only when the same event genuinely occurs both with and
    # without that value -- a gateway naming a cart nothing can find has no cart to point at. An
    # argument that is absent because the caller is really a different kind of event belongs in its
    # own action instead, which is why cancelling a cart before checkout is not a status update.
    #
    # Every action here is one this package writes. A processor records its own events under
    # GATEWAY_LOG, which names both a cart and a processor because an entry attributed to neither is
    # not a record of anything.
    TEMPLATES: dict[str, ActionTemplate] = {
        AuditActions.BUNDLE_FULFILLMENT_FAILED: {
            'template': (
                'Bundle: {sku} for cart: {cart_id} was not fully fulfilled. '
                'Courses whose enrollment completed before the failure: {landed_courses}. '
                'Courses the learner already held: {held_courses}. '
                'Failed course: {failed_course}.'
            ),
        },
        AuditActions.CART_CANCELLED: {
            'template': 'Cart cancelled before checkout. Status updated from: {old_status} to: {new_status}.',
            'optional_args': ['processor'],
        },
        AuditActions.CART_FULFILLED: {
            'template': 'Cart fulfilled successfully.',
        },
        AuditActions.CART_FULFILLMENT_ERROR: {
            'template': (
                'Error during cart fulfillment for item: {item_id}, '
                'catalogue_item: {catalogue_item_id} due to invalid SKU: {sku} or unsupported type.'
            ),
        },
        AuditActions.CART_STATUS_UPDATED: {
            'template': 'Status updated for cart from: {old_status} to: {new_status}.',
        },
        AuditActions.DUPLICATE_TRANSACTION: {
            'template': 'Transaction with id: {transaction_id} already existed. Cart has status: {cart_status}.',
        },
        AuditActions.FULFILLMENT_RETRIED: {
            'template': 'Invoice and enrollment retried by: {staff_username}, for transaction: {transaction_id}.',
        },
        AuditActions.GATEWAY_RESPONSE_REJECTED: {
            'template': 'Gateway response rejected: {error}. Response: {response}.',
        },
        AuditActions.INVOICE_CREATION_FAILED: {
            'template': (
                'Invoice could not be created or cart could not be fulfilled for cart: {cart_id} '
                'after a settled transaction. Error: {error}'
            ),
        },
        AuditActions.PAYMENT_ACCEPTED: {
            'template': (
                'Payment {transaction_id} of {amount} {currency} accepted by: {staff_username}, as confirmed with the '
                'gateway. The plugin had recorded: {recorded}. Status updated from: {old_status} to: {new_status}.'
            ),
        },
        AuditActions.PAYMENT_CANCELLED: {
            'template': (
                "Payment cancelled on the gateway's page. Status updated from: {old_status} to: {new_status}. "
                'Response: {response}.'
            ),
        },
        AuditActions.PAYMENT_DECLINED: {
            'template': (
                'Payment declined by the gateway. Status updated from: {old_status} to: {new_status}. '
                'Response: {response}.'
            ),
        },
        AuditActions.PAYMENT_EXPIRED: {
            'template': (
                'Payment session expired with no payment confirmed. Status updated from: {old_status} to: {new_status}.'
            ),
        },
        AuditActions.PAYMENT_HANDOFF_FAILED: {
            'template': (
                'Hand-off to the gateway failed: {error}. Status updated from: {old_status} to: {new_status}.'
            ),
        },
        AuditActions.PAYMENT_UNRECONCILED: {
            'template': (
                'Payment taken by the gateway but not reconciled; the learner is not enrolled. '
                'Status updated from: {old_status} to: {new_status}.'
            ),
        },
        AuditActions.REDIRECT_TO_PAYMENT: {
            'template': 'Redirecting to payment page.',
        },
        AuditActions.REFUND_RECORDED: {
            'template': (
                "Refund made in the gateway's dashboard recorded by: {staff_username}, under reference: "
                '{refund_reference}. Status updated from: {old_status} to: {new_status}.'
            ),
        },
        AuditActions.RESPONSE_INVALID_CART: {
            'template': (
                'Invalid cart state found. Cart '
                'is in state: {cart_status} instead of {required_cart_state}.'
            ),
            'optional_args': ['cart'],
        },
        AuditActions.TRANSACTION_NOT_RECONCILED: {
            'template': (
                'Transaction: {transaction_id} for cart: {cart_id} was recorded but is not reconciled: {error}.'
            ),
        },
        AuditActions.TRANSACTION_ROLLED_BACK: {
            'template': 'Transaction: {transaction_id} for cart: {cart_id} and site: {site_id} rolled back.',
        },
        AuditActions.USER_ALREADY_ENROLLED: {
            'template': (
                'User already enrolled in the course: {course_id} with mode: {mode_slug}, so nothing was changed '
                'during cart fulfillment for catalogue_item: {catalogue_item_id}.'
            ),
        },
        AuditActions.USER_ENROLLED: {
            'template': (
                'User enrolled to the course: {course_id} with mode: {mode_slug} '
                'during cart fulfillment for catalogue_item: {catalogue_item_id}.'
            ),
        },
        AuditActions.USER_ENROLLED_ERROR: {
            'template': (
                'Unable to complete user enrollment to course: {course_id} with mode: {mode_slug} '
                'during cart fulfillment for catalogue_item: {catalogue_item_id}.'
            ),
        },
        AuditActions.USER_UPGRADED: {
            'template': (
                'User upgraded in the course: {course_id} from mode: {from_mode} to mode: {mode_slug} '
                'during cart fulfillment for catalogue_item: {catalogue_item_id}.'
            ),
        },

        # The special one: a prefix rather than an action. See AuditActions.GATEWAY_LOG.
        AuditActions.GATEWAY_LOG: {
            'template': '{message}',
        },
    }

    action = models.CharField(max_length=32)
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name='audits', null=True)
    gateway = models.CharField(max_length=50, blank=True, null=True)
    details = models.TextField(blank=True, null=True)

    @classmethod
    def reject(cls, message: str) -> NoReturn:
        """
        Log a guard violation and raise it.

        Both, because log() is called from except blocks on the payment path where a broad handler
        upstream may swallow the exception; the log line is then the only trace left.

        :param message: What the caller got wrong, named precisely enough to fix it.
        :raises ValidationError: Always.
        """
        logger.error(message)
        raise ValidationError(message)

    @classmethod
    def resolve_action(cls, action: str) -> str:
        """
        Return the TEMPLATES key the given action is logged under.

        Each action is its own key, except GATEWAY_LOG, which external processors extend with a
        subtype of their own -- 'gw.payfort' -- so their records stay distinguishable in the trail.

        :param action: The action as passed to log().
        :raises ValidationError: If the action is unknown, carries a subtype it is not allowed to
            carry, or does not fit the action column.
        :return: The key into TEMPLATES.
        """
        max_length = cls._meta.get_field('action').max_length
        if len(action) > max_length:
            cls.reject(f"Audit action '{action}' is longer than the {max_length} characters the action column holds")

        key, separator, subtype = action.partition(cls.GATEWAY_LOG_SEPARATOR)

        if separator:
            if key != cls.AuditActions.GATEWAY_LOG:
                cls.reject(
                    f"Only the '{cls.AuditActions.GATEWAY_LOG}' audit action takes a subtype, got '{action}'",
                )
            if not cls.ACTION_NAME_PATTERN.match(subtype):
                cls.reject(
                    f"Audit action subtype '{subtype}' must be lowercase alphanumerics and underscores",
                )

        if key not in cls.TEMPLATES:
            cls.reject(f"Unknown audit action '{action}'")

        return key

    @classmethod
    def resolve_gateway(cls, action: str, processor: Optional[Any]) -> Optional[str]:
        """
        Return the gateway name to record for the given processor, or None when no processor was named.

        The name is the processor's SLUG, and what a slug may look like is settled where processors register:
        a malformed one costs that processor its registration, logged at boot, so every slug reaching this
        method is already the name the processor is known by everywhere else. What is checked here is only
        that a processor was handed over at all -- passing its slug as a string instead would otherwise read
        as "no processor took part" and write an unattributable row for an action that allows one.

        :param action: The action as passed to log(), used in the message.
        :param processor: The processor the entry belongs to, or None.
        :raises ValidationError: If the given object exposes no usable SLUG, or one too long for the column.
        :return: The gateway name, or None when no processor was named.
        """
        if processor is None:
            return None

        slug = getattr(processor, 'SLUG', None)
        if not isinstance(slug, str) or not slug.strip():
            cls.reject(
                f"Audit processor for action '{action}' must expose a non-blank SLUG, "
                f'got {type(processor).__name__}',
            )

        max_length = cls._meta.get_field('gateway').max_length
        if len(slug) > max_length:
            cls.reject(f"Audit gateway '{slug}' is longer than the {max_length} characters the gateway column holds")

        return slug

    @classmethod
    def validate_arguments(cls, action: str, key: str, **arguments: Any) -> None:
        """
        Check that every argument the action declares mandatory was given a value.

        :param action: The action as passed to log(), used in the message.
        :param key: The TEMPLATES key resolved from the action.
        :param arguments: The GUARDED_ARGS values, by name.
        :raises ValidationError: If a mandatory argument is None or blank.
        :return: None
        """
        optional_args = cls.TEMPLATES[key].get('optional_args', [])
        blank_args = sorted(
            name for name, value in arguments.items()
            if name not in optional_args and (value is None or (isinstance(value, str) and not value.strip()))
        )
        if blank_args:
            cls.reject(f"Blank mandatory argument(s) for audit action '{action}': {', '.join(blank_args)}")

    @classmethod
    def log(cls, action: str, context: dict = None, cart: Cart = None, processor: Optional[Any] = None) -> 'AuditLog':
        """
        Create a log entry for a given action, using its template and the given context.
        This method:
        - Resolves the action to its template, rejecting anything the table does not define.
        - Validates that the arguments the action declares mandatory were given a value.
        - Reads the gateway name off the processor.
        - Validates that all required parameters for the template exist in the context.
        - Formats the log details using the context.
        - Creates and saves a log entry associated with the cart and payment gateway.

        :param action: The name of the action to log (used to find the template).
        :param context: A dictionary of context values to fill into the template.
        :param cart: Cart instance related to the log entry, unless the action declares it optional.
        :param processor: The processor the entry belongs to, unless the action declares it optional. Its SLUG
            is what lands in the gateway column.
        :raises ValidationError: If the action is unknown, a mandatory argument is blank, the processor exposes
            no usable SLUG, or the context is missing required parameters for the template.
        :return: None
        """
        context = context or {}
        key = cls.resolve_action(action)
        cls.validate_arguments(action, key, cart=cart, processor=processor)
        gateway = cls.resolve_gateway(action, processor)

        template = cls.TEMPLATES[key]['template']
        required_keys = set(re.findall(r'{(\w+)}', template))
        missing_keys = required_keys - context.keys()
        if missing_keys:
            cls.reject(f"Missing template parameters for action '{action}': {', '.join(sorted(missing_keys))}")

        details = mask_unmasked_card_numbers(template.format(**context))
        return cls.objects.create(
            action=action,
            cart=cart,
            gateway=gateway,
            details=details,
        )


class Coupon(TimeStampedModel):
    """
    Coupon model is not used yet.

    .. no_pii:
    """

    class DiscountType(models.TextChoices):
        """Discount Types."""

        FIXED = 'fixed'
        PERCENTAGE = 'percentage'

    code = models.CharField(max_length=50, primary_key=True)
    discount_type = models.CharField(max_length=20, choices=DiscountType.choices)
    discount_value = models.DecimalField(max_digits=10, decimal_places=2)
    max_usage = models.PositiveIntegerField()
    expires_at = models.DateTimeField(blank=True, null=True)


class CouponUsage(TimeStampedModel):
    """
    CouponUsage model is not used yet.

    .. no_pii:
    """

    coupon = models.ForeignKey(Coupon, on_delete=models.CASCADE, related_name='usages')
    count = models.PositiveIntegerField(default=1)
    user = models.ForeignKey(User, on_delete=models.CASCADE)


class CatalogueItem(TimeStampedModel):
    """
    CatalogueItem model.

    .. no_pii:
    """

    class ItemType(models.TextChoices):
        """Catalogue Item Types."""

        PAID_COURSE = 'paid_course'
        PROGRAM_BUNDLE = 'program_bundle'
        # TODO add other types here like 'section_of_course', 'fremium_course', etc.

    sku = models.CharField(max_length=255, unique=True)
    type = models.CharField(max_length=20, choices=ItemType.choices)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    item_ref_id = models.CharField(max_length=255)
    price = models.DecimalField(max_digits=12, decimal_places=3, blank=True, null=True)
    currency = models.CharField(max_length=3, blank=True, null=True)

    def __str__(self) -> str:
        """Represent the item by its SKU, which is how a foreign key to it reads in an admin dropdown."""
        return self.sku

    def clean(self) -> None:
        """
        Run price and currency validation in a form-friendly path.

        Model-level validation that the admin ModelForm surfaces as a standard inline field error. ``save``
        enforces the same rules for callers that bypass ``full_clean()`` (``QuerySet.create()``,
        ``Model.save()``, DRF serializers); this method exists so the Django admin / form flow renders a clean
        error instead of a technical 500 page.
        """
        super().clean()
        validate_catalogue_item_pricing(self.price, self.currency, get_settings().valid_currency)

    def save(self, *args: Any, **kwargs: Any) -> None:
        """
        Refuse to save an item whose price or currency breaks the deployment's rules.

        ``QuerySet.create()`` and ``Model.save()`` both skip ``full_clean()``, so ``clean`` alone would leave
        the rules to whoever remembered to call it. Enforcing them here means a row that breaks them cannot
        reach the database by any ordinary path.
        """
        validate_catalogue_item_pricing(self.price, self.currency, get_settings().valid_currency)
        super().save(*args, **kwargs)

    @classmethod
    def valid_item_types(cls) -> list[str]:
        """Return all valid status values."""
        return [choice[0] for choice in cls.ItemType.choices]


class BundleCourseItem(TimeStampedModel):
    """
    Maps a program_bundle CatalogueItem to its constituent course CatalogueItems.

    .. no_pii:
    """

    bundle = models.ForeignKey(
        CatalogueItem,
        on_delete=models.CASCADE,
        related_name='bundle_courses',
        limit_choices_to={'type': CatalogueItem.ItemType.PROGRAM_BUNDLE},
        help_text='The program bundle CatalogueItem.',
    )
    course_item = models.ForeignKey(
        CatalogueItem,
        on_delete=models.CASCADE,
        related_name='in_bundles',
        limit_choices_to={'type': CatalogueItem.ItemType.PAID_COURSE},
        help_text='A paid_course CatalogueItem included in this bundle.',
    )

    class Meta:
        unique_together = ('bundle', 'course_item')

    def __str__(self) -> str:
        """Represent object as string."""
        return f'{self.bundle.sku} -> {self.course_item.sku}'


class CartItem(TimeStampedModel):
    """
    CartItem model.

    .. no_pii:
    """

    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name='items')
    catalogue_item = models.ForeignKey(CatalogueItem, on_delete=models.PROTECT)
    original_price = models.DecimalField(max_digits=12, decimal_places=3)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    coupon = models.ForeignKey(Coupon, on_delete=models.SET_NULL, null=True, blank=True)
    final_price = models.DecimalField(max_digits=12, decimal_places=3)

    def __str__(self) -> str:
        """Represent the line by the SKU it bills, which is how the invoice items of an invoice read."""
        return self.catalogue_item.sku


class Invoice(TimeStampedModel):
    """
    Invoice model.

    .. pii: learner_info, the learner's name as issued, and whatever an extension adds; hidden once they retire.
    .. pii_types: name, other
    .. pii_retirement: retained
    """

    class InvoiceStatus(models.TextChoices):
        """Invoice statuses."""

        # An invoice is drawn only once the payment it bills has settled, so it is born paid.
        PAID = 'paid'

    invoice_number = models.CharField(max_length=255, unique=True)
    # PROTECT: an issued invoice is an accounting document. Deleting its cart -- or the learner who owns it -- must be
    # refused rather than take the invoice with it.
    cart = models.ForeignKey(Cart, on_delete=models.PROTECT, related_name='invoices')
    # No default: whoever draws an invoice states its status, and save() refuses one that is missing.
    status = models.CharField(max_length=20, choices=InvoiceStatus.choices)
    gross_total = models.DecimalField(max_digits=12, decimal_places=3)
    discount_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=3)
    currency = models.CharField(max_length=3)
    paid_at = models.DateTimeField(blank=True, null=True)
    # An invoice records a payment that already happened: the transaction row is written and settled before the
    # invoice is drawn, so there is always one to point at. PROTECT rather than SET_NULL because deleting the
    # transaction under an issued invoice leaves a bill with nothing behind it -- that falsifies the record
    # rather than correcting it.
    related_transaction = models.ForeignKey(Transaction, on_delete=models.PROTECT)

    # Addresses the public receipt, which is what the QR code encodes. Random rather than derived from the
    # invoice number, so that no key exists to be rotated or leaked: a derived token would need one, and
    # whoever held it could mint a token for every invoice in a series that runs sequentially. Regenerating
    # this column revokes one leaked receipt without touching any other.
    public_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    # What the invoice says about the learner and the organization, as get_learner_info and get_organization_info in
    # invoice_info answered when it was issued. Stored rather than read when shown, so renaming the account or
    # reconfiguring the site never rewrites an issued invoice. A learner who has asked to be retired has learner_info
    # shown through retire_learner_info, while what is stored stays as it was. Empty on invoices issued before these
    # were stored.
    learner_info = models.JSONField(blank=True, default=dict)
    organization_info = models.JSONField(blank=True, default=dict)

    def save(self, *args: Any, **kwargs: Any) -> None:
        """
        Refuse to save an invoice with a status ``InvoiceStatus`` does not name.

        The status has no default, so an invoice drawn without one would otherwise be stored with an empty status.
        ``QuerySet.create()`` and ``Model.save()`` both skip ``full_clean()``, so the check lives here rather than in
        ``clean``.

        :raises ValidationError: When the status is refused.
        """
        if self.status not in self.InvoiceStatus.values:
            raise ValidationError(
                f'Invoice status {self.status!r} is not one of {", ".join(self.InvoiceStatus.values)}.',
            )
        super().save(*args, **kwargs)

    @property
    def receipt_url(self) -> Optional[str]:
        """
        Return the absolute URL of this invoice's public receipt, which is what its QR code encodes.

        The receipt is addressed by an unguessable token rather than by the invoice number, so whoever holds
        the printed copy can check it against the live page without an account, while the numbered URL stays
        private to the invoice's owner.

        The URL is addressed on the origin the invoice's cart was paid on. An invoice whose cart was paid before
        origins were recorded has no receipt URL and prints no QR: pointing a learner at an address unrelated to their
        payment is worse than giving them none. Recording the origin the cart was really paid on brings the QR back.

        :returns: The absolute receipt URL, or None while the cart has no payment origin.
        """
        payment_origin = self.cart.payment_origin
        if not payment_origin:
            return None

        return urljoin(payment_origin, reverse('zeitlabs_payments:public-invoice', args=[self.public_token]))


class InvoiceItem(TimeStampedModel):
    """
    InvoiceItem model.

    .. no_pii:
    """

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='items')
    cart_item = models.ForeignKey(CartItem, on_delete=models.SET_NULL, null=True)
    original_price = models.DecimalField(max_digits=12, decimal_places=3)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    price = models.DecimalField(max_digits=12, decimal_places=3)
    quantity = models.PositiveIntegerField(default=1)


class InvoiceNumberSequence(TimeStampedModel):
    """
    The last invoice number handed out under a given prefix.

    .. no_pii:
    """

    prefix = models.CharField(max_length=255, unique=True)
    last_number = models.PositiveBigIntegerField(default=100000)

    @classmethod
    def reserve_next_for(cls, prefix: str) -> int:
        """
        Reserve and return the next number under a prefix, opening its counter if this is the first.

        The row is locked for the rest of the caller's transaction rather than read and released. Two payments
        settling at the same moment would otherwise reserve the same number and one would die on the unique
        constraint over ``Invoice.invoice_number``. Holding the lock that long also means a fulfillment that
        rolls back returns its number, so the series stays gapless for whoever reads a run of these invoices
        as a record.

        :param prefix: The prefix to reserve under, already stripped and uppercased.
        :returns: The reserved number.
        """
        with transaction.atomic():
            if not cls.objects.filter(prefix=prefix).exists():
                already_issued = cls.last_number_issued_under(prefix)
                carried_over = {} if already_issued is None else {'last_number': already_issued}
                cls.objects.get_or_create(prefix=prefix, defaults=carried_over)
            sequence = cls.objects.select_for_update().get(prefix=prefix)
            sequence.last_number += 1
            sequence.save(update_fields=['last_number', 'updated_at'])
        return sequence.last_number

    @classmethod
    def last_number_issued_under(cls, prefix: str) -> Optional[int]:
        """
        Return the highest number already issued under a prefix, or None if that prefix has issued none.

        This is what lets the counter arrive without a seeding migration. A counter starting from scratch while
        invoices under its prefix already exist would hand out a number the unique constraint has already taken,
        so it has to begin above them. Reading that at first use rather than at migration time means the answer
        accounts for every invoice written up to that moment -- including any written by code still issuing
        numbers the old way while a deployment is only part-way through.

        Numbers are matched on their whole prefix, case aside, so a series named ``DEV`` does not swallow one
        named ``DEV-EU``. A number whose tail is not numeric belongs to no series this counter issues.

        :param prefix: The prefix to search under, already stripped and uppercased.
        :returns: The highest number issued under that prefix, or None if it has issued none.
        """
        issued = Invoice.objects.filter(invoice_number__istartswith=f'{prefix}-').values_list(
            'invoice_number', flat=True,
        )
        highest = 0
        for invoice_number in issued.iterator():
            head, _, digits = invoice_number.rpartition('-')
            if head.upper() == prefix and digits.isdigit():
                highest = max(highest, int(digits))
        return highest or None


class CreditMemo(TimeStampedModel):
    """
    CreditMemo model.

    .. no_pii:
    """

    # PROTECT, like the invoice it reverses: a credit memo is an accounting document of its own.
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='credit_memos')
    total = models.DecimalField(max_digits=12, decimal_places=3)
    reason = models.TextField()
    gateway_refund_transaction_id = models.CharField(max_length=255)
    transaction = models.ForeignKey(Transaction, on_delete=models.SET_NULL, null=True, blank=True)


class TaxRule(TimeStampedModel):
    """
    TaxRule model is not used yet.

    .. no_pii:
    """

    class TaxType(models.TextChoices):
        """Tax calculation type."""

        PERCENT = 'percentage', 'Percentage'
        FIXED = 'fixed', 'Fixed Amount'

    name = models.CharField(max_length=255, help_text='Internal name for the tax rule.')
    tax_type = models.CharField(
        max_length=20,
        choices=TaxType.choices,
        default=TaxType.PERCENT,
        help_text='Whether the tax is percentage-based or fixed amount.'
    )
    tax_value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text='If percentage: store rate (e.g., 15 for 15%). If fixed: store amount (e.g., 2.50).'
    )
    is_active = models.BooleanField(default=True)
