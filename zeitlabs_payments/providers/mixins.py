"""View mixins a processor package builds its return and status views from."""
import logging
from enum import Enum
from typing import Any, Optional

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.functional import cached_property

from zeitlabs_payments.exceptions import CartStatusError, GatewayError
from zeitlabs_payments.models import AuditLog, Cart, Invoice
from zeitlabs_payments.providers.base import BaseProcessor

logger = logging.getLogger(__name__)


class PaymentReturnViewMixin:
    """
    Park the learner on a page that polls the processor's status view until the gateway has an answer.

    A gateway sends the learner back once it has taken the card, before the outcome is known. A processor's return
    view combines this mixin with ``django.views.View`` and names its processor, which declares what the page cannot
    work out on its own: ``GATEWAY_ORDER_ID_PARAM``, the query parameter the gateway's redirect carries its order id
    in, and ``STATUS_VIEW``, the view the page polls. Everything else -- the page, how it polls, where the learner goes
    next -- is shared. The plugin mounts the view, as the processor's ``RETURN_VIEW``.
    """

    template_name = 'zeitlabs_payments/wait_feedback.html'
    error_template_name = 'zeitlabs_payments/payment_error.html'

    # The processor whose gateway sends the learner back here. Its GATEWAY_ORDER_ID_PARAM is where the redirect puts
    # the gateway's id for the order; left unset, no redirect carries one and every learner coming back lands on the
    # error page. A gateway that sends the id some other way overrides get_gateway_order_id instead.
    processor_class: Optional[type[BaseProcessor]] = None

    # How many times the waiting page polls, and how many milliseconds apart, before sending the learner to the
    # reconciliation page.
    MAX_ATTEMPTS = 24
    WAIT_TIME = 5000

    request: HttpRequest

    def get_gateway_order_id(self) -> Optional[str]:
        """
        Return the gateway's id for the order the learner came back from, or None when the redirect carries none.

        Reads the processor's ``GATEWAY_ORDER_ID_PARAM`` from the query string, so it is None until the view names its
        processor and the processor names that parameter.
        """
        param = self.processor_class.GATEWAY_ORDER_ID_PARAM if self.processor_class else None
        if not param:
            return None
        return self.request.GET.get(param) or None

    def get_status_url(self) -> str:
        """
        Return the URL of the processor's status view, where the plugin mounts it.

        :raises ImproperlyConfigured: When the processor names no status view. A waiting page with nothing to poll
            cannot end anywhere but the error page, whatever the gateway says, so this is refused rather than rendered.
        """
        if not (self.processor_class and self.processor_class.STATUS_VIEW):
            raise ImproperlyConfigured(
                f'{type(self).__name__} must name a processor whose STATUS_VIEW is its status view.',
            )
        return reverse('zeitlabs_payments:processor-status', args=[self.processor_class.SLUG])

    def get_login_url(self) -> str:
        """
        Return the page a 401 from the status view sends the learner to: the login page, coming back to this one.

        The status view answers 401 when the learner's session expired while the page polled. The payment is not
        affected, so the learner logs in and lands back on the waiting page, which polls the same order again.
        """
        return redirect_to_login(self.request.get_full_path()).url

    def get_context_data(self, gateway_order_id: str, **kwargs: Any) -> dict:
        """Return what the waiting page renders with: the order it waits on, what it polls, and where it goes next."""
        reconciliation_url = reverse('zeitlabs_payments:payment-reconciliation', args=[gateway_order_id])
        decline_url = reverse('zeitlabs_payments:payment-decline', args=[gateway_order_id])
        return {
            'gateway_order_id': gateway_order_id,
            'status_url': self.get_status_url(),
            'error_url': reverse('zeitlabs_payments:payment-error', args=[gateway_order_id]),
            'decline_url': decline_url,
            'cancelled_url': f'{decline_url}?cancelled=1',
            'reconciliation_url': reconciliation_url,
            'unconfirmed_url': f'{reconciliation_url}?unconfirmed=1',
            'login_url': self.get_login_url(),
            'max_attempts': self.MAX_ATTEMPTS,
            'wait_time': self.WAIT_TIME,
            **kwargs,
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        """Render the waiting page for the order the gateway sent the learner back with."""
        gateway_order_id = self.get_gateway_order_id()
        if not gateway_order_id:
            logger.error(f'{type(self).__name__}: the gateway sent the learner back without its order id.')
            return render(request, self.error_template_name)
        return render(request, self.template_name, self.get_context_data(gateway_order_id))


class PaymentStatus(Enum):
    """Where a gateway says a payment stands, in the terms the status view acts on."""

    SUCCESS = 'success'
    PENDING = 'pending'
    # Refused by the bank or the gateway. Nothing was charged.
    DECLINED = 'declined'
    # Cancelled by the learner on the gateway's page. Nothing was charged.
    CANCELLED = 'cancelled'


class PaymentStatusViewMixin(LoginRequiredMixin):
    """
    Answer the waiting page's poll with where the payment stands, and settle it once the gateway confirms it.

    A processor's status view combines this mixin with ``django.views.View``, is named as the processor's
    ``STATUS_VIEW`` for the plugin to mount, sets ``processor_class``, and overrides the methods that talk to its
    gateway: ``get_order_cart_reference``, ``get_order_status``, ``get_payment_status``,
    ``verify_success_response`` and ``get_transaction_details``. Each one's default is the answer of a gateway with
    nothing to say, so a method left unimplemented fails the poll instead of the request. The one exception is
    ``verify_success_response``, which refuses: every other default leaves the payment unsettled, and a default that
    accepted would settle payments nobody checked.

    A payment is its cart's gateway order, which a processor opens to take one payment only; see
    ``BaseProcessor.get_gateway_order_id``. The transaction id a processor reads from an answer is kept on the
    transaction as a detail, and never decides whether two answers are the same payment.

    The poll is answered in JSON on every path, because the waiting page reads every answer as JSON. That includes a
    poll whose session has expired, which gets a 401 rather than the redirect to log in ``LoginRequiredMixin`` gives.
    Every answer names its outcome under ``outcome``, which is what the page routes on; the HTTP status says the same
    in HTTP's terms, for whoever else reads the answer. The page never shows the message.

    What the view decides, it writes to the cart's audit trail: the cart moving to awaiting payment, a declined or
    cancelled payment, a gateway answer it refuses to act on, and a success it could not settle leaving the cart
    unreconciled. Declines, cancellations and refusals carry the answer as evidence. Settling the payment is recorded
    by ``process_payment_and_update_records``. A poll that learns nothing about the cart -- no cart found, no answer,
    still pending, a payment that already settled or is already held unreconciled -- writes nothing, since the trail is
    read one cart at a time and a pending payment is polled repeatedly. Nor does a poll by anyone but the cart's
    learner, which is answered as an order naming no cart before the gateway is asked about the payment.
    """

    class Outcome:
        """Every answer the status view gives the waiting page."""

        CANCELLED = 'cancelled'
        DECLINED = 'declined'
        GATEWAY_ERROR = 'gateway_error'
        INVALID_REFERENCE = 'invalid_reference'
        LOGIN_REQUIRED = 'login_required'
        MISSING_GATEWAY_ORDER_ID = 'missing_gateway_order_id'
        NOT_FULFILLED = 'not_fulfilled'
        PAID = 'paid'
        PENDING = 'pending'
        UNEXPECTED_RESPONSE = 'unexpected_response'
        VERIFICATION_FAILED = 'verification_failed'

    # The HTTP status each outcome is answered with, and the message the response carries for whoever reads it. A
    # settled payment is answered by paid_response instead. '{processor}' in a message is replaced with the
    # processor's NAME.
    #
    # 400 is a payment that ended without a charge, or a poll naming no payment; 409 is money the gateway reports
    # taken that has not been turned into an enrolment; 502 is a gateway that could not be asked or gave an answer
    # that cannot be read.
    OUTCOME_RESPONSES: dict[str, tuple[int, str]] = {
        Outcome.CANCELLED: (400, 'Your payment was cancelled. No charges were made. You may try again.'),
        Outcome.DECLINED: (
            400,
            'Your payment was declined. No charges were made. You may try again or use a different payment method.',
        ),
        Outcome.GATEWAY_ERROR: (502, 'Unable to connect with {processor} for payment status verification.'),
        Outcome.INVALID_REFERENCE: (
            400,
            'We couldn’t verify your payment because the transaction reference is missing/invalid.',
        ),
        Outcome.LOGIN_REQUIRED: (401, 'Your session has expired. Please log in again to see your payment status.'),
        Outcome.MISSING_GATEWAY_ORDER_ID: (
            400,
            'We couldn’t find your order details. Please contact support with your transaction reference.',
        ),
        Outcome.NOT_FULFILLED: (
            409,
            'Payment received successfully, but we couldn’t activate your enrollment. '
            'Please contact support with your transaction reference.',
        ),
        Outcome.PENDING: (202, 'Still processing, unable to confirm payment from {processor}.'),
        Outcome.UNEXPECTED_RESPONSE: (
            502,
            'We couldn’t confirm the payment status due to an unexpected response. '
            'Please try again or contact support.',
        ),
        Outcome.VERIFICATION_FAILED: (
            409,
            'We couldn’t verify the payment due to an unexpected response. '
            'Please try again or contact support with your reference.',
        ),
    }

    # How the view ends a payment the gateway reports as ended without a charge: the audit action recording the
    # cancelled cart, the word for it in the trail, and the outcome the poll is answered with.
    UNPAID_ENDINGS: dict[PaymentStatus, tuple[str, str, str]] = {
        PaymentStatus.DECLINED: (AuditLog.AuditActions.PAYMENT_DECLINED, 'decline', Outcome.DECLINED),
        PaymentStatus.CANCELLED: (AuditLog.AuditActions.PAYMENT_CANCELLED, 'cancellation', Outcome.CANCELLED),
    }

    # The processor whose payments the view settles.
    processor_class: Optional[type[BaseProcessor]] = None

    request: HttpRequest

    @cached_property
    def payment_processor(self) -> BaseProcessor:
        """
        Return the processor the view settles payments for, one instance per request.

        :raises ImproperlyConfigured: When the view names no processor.
        """
        if self.processor_class is None:
            raise ImproperlyConfigured(f'{type(self).__name__} must set processor_class to the processor it serves.')
        return self.processor_class()  # pylint: disable=not-callable

    def get_order_cart_reference(self, gateway_order_id: str) -> Optional[str]:  # pylint: disable=unused-argument
        """
        Return the merchant reference -- ``<site id>-<cart id>`` -- the gateway holds for the order, or None.

        Only the gateway knows which cart an order it names was for, so this is the first thing asked of it, before
        anything is known about the payment. None here, so a view that does not override it reports every poll as an
        invalid reference.

        :param gateway_order_id: The gateway's id for the order, as the waiting page sent it.
        :raises GatewayError: When the gateway cannot be asked.
        """
        return None

    def get_order_status(self, gateway_order_id: str, cart: Cart) -> Optional[dict]:  # pylint: disable=unused-argument
        """
        Return the gateway's answer about the order, or None when it gave none.

        Asked only once the order's cart is known. The processor need not record the answer: the one that decides the
        payment is kept by this view, on the transaction when it settles and in the cart's audit trail otherwise.

        :param gateway_order_id: The gateway's id for the order.
        :param cart: The cart the order pays for.
        :raises GatewayError: When the gateway cannot be asked.
        """
        return None

    def get_payment_status(self, data: dict) -> Optional[PaymentStatus]:  # pylint: disable=unused-argument
        """Return where the gateway's answer says the payment stands, or None when the answer cannot be read."""
        return None

    def verify_success_response(self, data: dict, cart: Cart) -> None:
        """
        Check that a success answer really pays for this cart.

        Whether the answer is this cart's payment at all: its reference, its transaction, its signature. An answer
        refused here is not recorded. Whether the money taken is what the cart expected is a separate check, the
        processor's ``reconcile_payment``, made once the payment is recorded.

        :param data: The gateway's answer, already read as a success.
        :param cart: The cart the answer claims to pay for.
        :raises GatewayError: When the answer does not pay for the cart. Always, here: a view that does not check
            success answers accepts none of them.
        """
        raise GatewayError(f'{type(self).__name__} does not verify success responses, so none is accepted')

    def get_transaction_details(self, data: dict, cart: Cart) -> Optional[dict]:  # pylint: disable=unused-argument
        """
        Return what the transaction row records from a verified success answer, or None when it cannot be described.

        The keys are those ``BaseProcessor.process_payment_and_update_records`` takes for the gateway's side of the
        payment: ``transaction_id``, ``method``, ``amount``, ``currency`` and ``reason``.
        """
        return None

    def outcome_response(self, outcome: str) -> JsonResponse:
        """Return the answer the waiting page gets for an outcome other than a settled payment."""
        status, message = self.OUTCOME_RESPONSES[outcome]
        return JsonResponse(
            {'outcome': outcome, 'error': message.format(processor=self.payment_processor.NAME)}, status=status,
        )

    def handle_no_permission(self) -> JsonResponse:
        """
        Answer a poll without a session in JSON, rather than redirecting it to log in.

        The waiting page follows a redirect, gets the login page's HTML where it expects JSON, and sends the learner to
        the error page while their payment may still be going through. A 401 is told apart from every failure instead.
        """
        return self.outcome_response(self.Outcome.LOGIN_REQUIRED)

    def paid_response(self, invoice: Invoice) -> JsonResponse:
        """Return the answer the waiting page gets once the payment is settled: the invoice to send the learner to."""
        return JsonResponse(
            {
                'outcome': self.Outcome.PAID,
                'invoice': invoice.invoice_number,
                'invoice_url': reverse('zeitlabs_payments:invoice', args=[invoice.invoice_number]),
            },
            status=200,
        )

    def log_rejected_response(self, cart: Cart, data: dict, error: str) -> None:
        """
        Record on the cart that the gateway answered and the view refused to act on the answer.

        :param cart: The cart the answer is about.
        :param data: The gateway's answer, kept as the evidence.
        :param error: Why the view refused it.
        """
        AuditLog.log(
            action=AuditLog.AuditActions.GATEWAY_RESPONSE_REJECTED,
            cart=cart,
            processor=self.payment_processor,
            context={'error': error, 'response': data},
        )

    def record_gateway_order(self, cart: Cart, gateway_order_id: str) -> None:
        """
        Record on the cart the order id the poll names, when the cart has none yet.

        A gateway that opens its order only when the learner pays names it first in its redirect back, which is what
        the waiting page polls with. The gateway has just said the order is for this cart. A cart the id cannot be
        recorded on -- handed to another gateway, past payment, or the id already taken -- is left as it is, and the
        poll goes on regardless.

        :param cart: The cart the gateway says the order is for.
        :param gateway_order_id: The gateway's id for the order, as the waiting page sent it.
        """
        try:
            cart.record_gateway_order(self.payment_processor.SLUG, gateway_order_id)
        except (CartStatusError, IntegrityError) as exc:
            logger.warning(
                f'{self.payment_processor.SLUG}: order {gateway_order_id} not recorded on cart {cart.id}: {exc}',
            )

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        """Report where the payment the waiting page asks about stands, settling it once the gateway confirms it."""
        slug = self.payment_processor.SLUG

        # The waiting page sends the gateway order id under this name.
        gateway_order_id = request.GET.get('gateway_order_id')
        if not gateway_order_id:
            logger.error(f'{slug}: payment status polled without a gateway order id.')
            return self.outcome_response(self.Outcome.MISSING_GATEWAY_ORDER_ID)

        try:
            reference = self.get_order_cart_reference(gateway_order_id)  # pylint: disable=assignment-from-none
        except GatewayError:
            logger.exception(f'{slug}: unable to ask the gateway which cart its order {gateway_order_id} is for.')
            return self.outcome_response(self.Outcome.GATEWAY_ERROR)
        if not reference:
            logger.error(f'{slug}: the gateway named no cart for its order {gateway_order_id}.')
            return self.outcome_response(self.Outcome.INVALID_REFERENCE)

        cart = self.payment_processor.get_cart_from_reference(reference)
        site = self.payment_processor.get_site_from_reference(reference)
        if not cart or not site:
            logger.error(
                f'{slug}: invalid merchant reference {reference!r} for gateway order {gateway_order_id}: '
                'no cart or site.',
            )
            return self.outcome_response(self.Outcome.INVALID_REFERENCE)

        if cart.user_id != request.user.id:
            # Answered as an order that names no cart, so an order id seen elsewhere neither settles another learner's
            # payment nor tells the poller anything about it.
            logger.warning(
                f'{slug}: user {request.user.id} polled gateway order {gateway_order_id} of cart {cart.id}, '
                f'which is user {cart.user_id}\'s.',
            )
            return self.outcome_response(self.Outcome.INVALID_REFERENCE)

        if not cart.gateway_order_id:
            self.record_gateway_order(cart, gateway_order_id)

        if cart.status == Cart.Status.UNRECONCILED:
            # Already recorded and held for a person: asking the gateway again would only repeat the refusal.
            return self.outcome_response(self.Outcome.NOT_FULFILLED)

        try:
            data = self.get_order_status(gateway_order_id, cart)  # pylint: disable=assignment-from-none
        except GatewayError:
            logger.exception(f'{slug}: unable to ask the gateway for the status of its order {gateway_order_id}.')
            return self.outcome_response(self.Outcome.GATEWAY_ERROR)
        if data is None:
            logger.error(f'{slug}: the gateway gave no status for its order {gateway_order_id}.')
            return self.outcome_response(self.Outcome.UNEXPECTED_RESPONSE)

        if cart.status == Cart.Status.PROCESSING:
            try:
                cart.change_status(Cart.Status.PAYMENT_PENDING, processor=self.payment_processor)
            except CartStatusError:
                # A poll running alongside this one moved the cart first. The cart now holds the status it moved it to,
                # which the rest of this poll acts on.
                logger.info(f'{slug}: cart {cart.id} was moved by another poll while this one asked the gateway.')

        payment_status = self.get_payment_status(data)  # pylint: disable=assignment-from-none
        if payment_status == PaymentStatus.SUCCESS:
            return self.settle_payment(cart, site.id, data, gateway_order_id)
        if payment_status == PaymentStatus.PENDING:
            return self.outcome_response(self.Outcome.PENDING)
        if payment_status in self.UNPAID_ENDINGS:
            return self.end_unpaid(cart, gateway_order_id, data, payment_status)

        logger.error(f'{slug}: unreadable status for gateway order {gateway_order_id} of cart {cart.id}.')
        self.log_rejected_response(cart, data, 'its payment status could not be read')
        return self.outcome_response(self.Outcome.UNEXPECTED_RESPONSE)

    def end_unpaid(
        self, cart: Cart, gateway_order_id: str, data: dict, payment_status: PaymentStatus,
    ) -> JsonResponse:
        """
        Cancel the cart of a payment that ended without a charge, and answer the poll with how it ended.

        A cart that is not awaiting payment is left as it is -- a late or repeated answer must not cancel a cart that
        has since been paid -- and the answer is recorded as refused instead. The poll is answered the same either way.

        :param cart: The cart the payment was for.
        :param gateway_order_id: The gateway's id for the order.
        :param data: The gateway's answer, kept as the evidence.
        :param payment_status: How the gateway says the payment ended: declined or cancelled.
        """
        slug = self.payment_processor.SLUG
        action, ending, outcome = self.UNPAID_ENDINGS[payment_status]
        logger.warning(f'{slug}: the gateway reports its order {gateway_order_id} for cart {cart.id} as {outcome}.')
        try:
            cart.change_status(
                Cart.Status.CANCELLED, processor=self.payment_processor, action=action, context={'response': data},
            )
        except CartStatusError as exc:
            logger.error(f'{slug}: the {ending} of its order {gateway_order_id} was not applied: {exc}')
            self.log_rejected_response(cart, data, f'the cart is {cart.status}, so the {ending} was not applied')
        return self.outcome_response(outcome)

    def get_settled_invoice(self, cart: Cart, gateway_transaction_id: str) -> Optional[Invoice]:
        """
        Return the invoice already issued for this payment, or None when it has not settled yet.

        The waiting page is polled again after the payment settled whenever the learner reloads it or comes back to it,
        and the gateway still answers success. Settling again would be refused, the cart being paid, and the learner
        told their enrolment failed; the invoice issued the first time is the answer instead.

        A payment is its cart's gateway order: a processor hands each cart off to one order, which takes one payment.
        So a success under the cart's order is the payment already settled, whatever transaction id it is reported
        under -- an authorisation's id and then its capture's, say. Only a cart handed off before carts recorded their
        order is matched on the transaction id instead.

        :param cart: The cart the answer pays for, polled under its own order.
        :param gateway_transaction_id: The gateway's id for the payment, the ``transaction_id`` that
            ``get_transaction_details`` read from its answer.
        """
        invoices = Invoice.objects.filter(cart=cart, status=Invoice.InvoiceStatus.PAID)
        if not cart.gateway_order_id:
            invoices = invoices.filter(
                related_transaction__gateway=self.payment_processor.SLUG,
                related_transaction__gateway_transaction_id=gateway_transaction_id,
            )
        return invoices.first()

    def record_foreign_order(self, cart: Cart, site_id: int, data: dict, gateway_order_id: str, details: dict) -> None:
        """
        Record a success reported for the cart under an order that is not the one it was handed off to.

        A processor hands each cart off to one order that takes one payment, so a second order paying for the cart is
        a gateway breaking that rule: money may have been taken twice. The payment is recorded as a failed transaction
        saying why, for a person to refund, and the gateway's answer kept on the audit trail too, in case the row
        cannot be written. The cart is left as it is: its own order still decides it.

        :param cart: The cart the gateway says the order pays for.
        :param site_id: The site the payment was made on, as the merchant reference names it.
        :param data: The gateway's answer, already read as a success.
        :param gateway_order_id: The order the poll names.
        :param details: What ``get_transaction_details`` read from the answer.
        """
        error = (
            f'order {gateway_order_id} is not the order {cart.gateway_order_id} the cart was handed off to, so it may '
            'be a second payment'
        )
        logger.error(f'{self.payment_processor.SLUG}: a success for cart {cart.id} was reported under {error}.')
        self.log_rejected_response(cart, data, error)
        self.payment_processor.process_payment_and_update_records(
            cart=cart,
            data=data,
            request=self.request,
            transaction_status=self.payment_processor.TRANSACTION_STATUS_SUCCESS,
            site_id=site_id,
            refusal=error,
            **details,
        )

    def hold_unreconciled(self, cart: Cart) -> None:
        """
        Move a cart awaiting payment, or expired, to unreconciled after a success the view could not settle.

        The money has been taken, so the cart is neither paid nor cancelled: it waits for a person to enrol or refund
        the learner, and keeps blocking the course meanwhile. A cart that is no longer awaiting payment keeps its
        status -- one already paid has the payment on record, and one that ended is not reopened.

        :param cart: The cart the reported success was for.
        """
        if cart.status not in (Cart.Status.PAYMENT_PENDING, Cart.Status.EXPIRED):
            return
        try:
            cart.change_status(
                Cart.Status.UNRECONCILED,
                processor=self.payment_processor,
                action=AuditLog.AuditActions.PAYMENT_UNRECONCILED,
            )
        except CartStatusError as exc:
            logger.warning(f'{self.payment_processor.SLUG}: cart {cart.id} was not held as unreconciled: {exc}')

    def settle_payment(self, cart: Cart, site_id: int, data: dict, gateway_order_id: str) -> JsonResponse:
        """
        Verify a success answer, then record the payment, issue the invoice and fulfil the cart.

        A success that cannot be settled leaves the cart unreconciled, see ``hold_unreconciled``. A poll arriving after
        the payment settled is answered with the invoice it was issued, rather than as not fulfilled, see
        ``get_settled_invoice``. A poll running alongside the one settling it is not covered: it finds no invoice yet,
        and its own settlement is refused. A success under an order the cart was not handed off to is recorded but
        never settled, see ``record_foreign_order``.

        :param cart: The cart the answer pays for.
        :param site_id: The site the payment was made on, as the merchant reference names it.
        :param data: The gateway's answer, already read as a success.
        :param gateway_order_id: The order the poll names.
        """
        slug = self.payment_processor.SLUG
        try:
            self.verify_success_response(data, cart)
        except GatewayError as exc:
            logger.exception(f'{slug}: the success response for cart {cart.id} failed verification.')
            self.log_rejected_response(cart, data, f'failed verification ({exc})')
            self.hold_unreconciled(cart)
            return self.outcome_response(self.Outcome.VERIFICATION_FAILED)

        invoice = None
        details = self.get_transaction_details(data, cart)  # pylint: disable=assignment-from-none
        if details is None:
            logger.error(f'{slug}: no transaction details could be read from the success response for cart {cart.id}.')
            self.log_rejected_response(cart, data, 'no transaction details could be read from it')
        elif cart.gateway_order_id and cart.gateway_order_id != gateway_order_id:
            self.record_foreign_order(cart, site_id, data, gateway_order_id, details)
            return self.outcome_response(self.Outcome.NOT_FULFILLED)
        else:
            invoice = self.get_settled_invoice(cart, details['transaction_id'])
            if invoice is None:
                invoice = self.payment_processor.process_payment_and_update_records(
                    cart=cart,
                    data=data,
                    request=self.request,
                    transaction_status=self.payment_processor.TRANSACTION_STATUS_SUCCESS,
                    site_id=site_id,
                    **details,
                )

        if invoice is None:
            logger.error(
                f'{slug}: payment was successful but unable to update enrollment record in db, '
                f'please check audit logs for the cart: {cart.id}',
            )
            self.hold_unreconciled(cart)
            return self.outcome_response(self.Outcome.NOT_FULFILLED)
        return self.paid_response(invoice)
