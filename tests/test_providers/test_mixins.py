"""Tests for the view mixins processor packages build their return and status views from."""
import re
from unittest.mock import ANY, Mock, call, patch
from urllib.parse import quote

import pytest
from common.djangoapps.course_modes.models import CourseMode
from common.djangoapps.student.models import CourseEnrollment
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, TestCase, override_settings
from django.urls import resolve, reverse
from django.utils.html import escapejs

from test_utils.dummy_processor import DummyProcessor, DummyReturnView, DummyStatusView
from zeitlabs_payments.exceptions import GatewayError
from zeitlabs_payments.models import AuditLog, Cart, CatalogueItem, Invoice, Transaction
from zeitlabs_payments.providers.mixins import PaymentStatus, PaymentStatusViewMixin

User = get_user_model()
Outcome = PaymentStatusViewMixin.Outcome


class PaymentReturnViewMixinTest(TestCase):
    """Tests for PaymentReturnViewMixin, through the dummy processor's return view."""

    def setUp(self):
        """Setup"""
        self.gateway_order_id = 'ORDER-1'
        self.url = f"{reverse('zeitlabs_payments:processor-return', args=['dummy'])}?orderId={self.gateway_order_id}"
        self.error_url = reverse('zeitlabs_payments:payment-error', args=[self.gateway_order_id])
        self.decline_url = reverse('zeitlabs_payments:payment-decline', args=[self.gateway_order_id])
        self.reconciliation_url = reverse('zeitlabs_payments:payment-reconciliation', args=[self.gateway_order_id])
        self.unconfirmed_url = f'{self.reconciliation_url}?unconfirmed=1'
        self.login_url = f"{settings.LOGIN_URL}?next={quote(self.url, safe='/')}"

    def test_renders_the_waiting_page(self):
        """Verify that a redirect carrying a gateway order id renders the waiting page with all it polls with."""
        response = self.client.get(self.url)

        self.assertTemplateUsed(response, 'zeitlabs_payments/wait_feedback.html', 'the waiting page was not rendered')
        self.assertEqual(
            {
                key: response.context[key]
                for key in (
                    'gateway_order_id',
                    'status_url',
                    'error_url',
                    'decline_url',
                    'cancelled_url',
                    'reconciliation_url',
                    'unconfirmed_url',
                    'login_url',
                    'max_attempts',
                    'wait_time',
                )
            },
            {
                'gateway_order_id': self.gateway_order_id,
                'status_url': reverse('zeitlabs_payments:processor-status', args=['dummy']),
                'error_url': self.error_url,
                'decline_url': self.decline_url,
                'cancelled_url': f'{self.decline_url}?cancelled=1',
                'reconciliation_url': self.reconciliation_url,
                'unconfirmed_url': self.unconfirmed_url,
                'login_url': self.login_url,
                'max_attempts': 24,
                'wait_time': 5000,
            },
            'the waiting page was not given what it polls with',
        )

    def test_waiting_page_polls_with_the_context_it_is_given(self):
        """
        Verify that the rendered page polls the status view with the gateway order id, and routes to its URLs.

        The page reads its context by name inside a script, where a name the view does not provide renders as an empty
        string instead of failing. An empty status URL makes the page poll itself and an empty error URL reloads it, so
        a mismatch between the two keeps the learner on the waiting page forever rather than breaking visibly.
        """
        response = self.client.get(self.url)

        for fragment in (
            f'<span class="reference-value">{self.gateway_order_id}</span>',
            f'const statusUrl = "{escapejs(reverse("zeitlabs_payments:processor-status", args=["dummy"]))}";',
            f'const errorUrl = "{escapejs(self.error_url)}";',
            f'const reconciliationUrl = "{escapejs(self.reconciliation_url)}";',
            f'const unconfirmedUrl = "{escapejs(self.unconfirmed_url)}";',
            f'const loginUrl = "{escapejs(self.login_url)}";',
            f'gateway_order_id: "{escapejs(self.gateway_order_id)}"',
            'window.location.href = loginUrl;',
            'window.location.href = reconciliationUrl;',
            'window.location.href = unconfirmedUrl;',
            'outcome === "not_fulfilled" || outcome === "verification_failed"',
            'if (attempts < 24)',
            'setTimeout(checkStatus, 5000)',
        ):
            self.assertContains(response, fragment, msg_prefix=f'the rendered page does not carry {fragment!r}')

    def test_waiting_page_never_offers_a_retry_on_an_unknown_answer(self):
        """
        Verify that the waiting page sends an answer it cannot read to the unconfirmed page, never the error page.

        A gateway that could not be asked, an answer that could not be read, an order that could not be traced to a
        cart and a poll that failed outright all leave the payment unknown: the learner may have been charged, and the
        error page offers a retry. Only a poll naming no order at all, which the page itself never sends, still reaches
        the error page.
        """
        script = self.client.get(self.url).content.decode()

        self.assertEqual(
            re.findall(r'(?s)else if \(([^)]*)\) \{\s*window\.location\.href = errorUrl;', script),
            ['outcome === "missing_gateway_order_id"'],
            'the error page is reached by something other than a poll naming no order',
        )
        self.assertRegex(
            script,
            r'(?s)\} else \{\s*(//[^\n]*\n\s*)*window\.location\.href = unconfirmedUrl;\s*\}\s*\}\)',
            'an outcome the page does not recognise does not lead to the unconfirmed page',
        )
        self.assertRegex(
            script,
            r'(?s)\.catch\(error => \{.*?window\.location\.href = unconfirmedUrl;',
            'a poll that fails outright does not lead to the unconfirmed page',
        )

    def test_declined_and_cancelled_payments_lead_to_the_decline_page_in_their_own_wording(self):
        """
        Verify that the waiting page sends a declined payment to the decline page, and a cancelled one to its cancelled
        wording, never to the error page.
        """
        response = self.client.get(self.url)

        for fragment in (
            f'const declineUrl = "{escapejs(self.decline_url)}";',
            f'const cancelledUrl = "{escapejs(self.decline_url + "?cancelled=1")}";',
            'outcome === "declined") {\n                window.location.href = declineUrl;',
            'outcome === "cancelled") {\n                window.location.href = cancelledUrl;',
        ):
            self.assertContains(response, fragment, msg_prefix=f'the waiting page does not carry {fragment!r}')

    def test_redirect_without_a_gateway_order_id_renders_the_error_page(self):
        """Verify that a redirect without a gateway order id renders the error page, not a page with nothing to poll."""
        response = self.client.get(reverse('zeitlabs_payments:processor-return', args=['dummy']))

        self.assertTemplateUsed(response, 'zeitlabs_payments/payment_error.html', 'the error page was not rendered')
        self.assertTemplateNotUsed(
            response, 'zeitlabs_payments/wait_feedback.html', 'the waiting page was rendered with no order to wait on',
        )

    def test_no_gateway_order_id_until_the_processor_names_its_parameter(self):
        """
        Verify that no gateway order id is found, even in a redirect carrying one, until the processor names where.

        The parameter is the processor's, so a view naming no processor, or a processor naming no parameter, finds none.
        """
        for patched, attribute in ((DummyReturnView, 'processor_class'), (DummyProcessor, 'GATEWAY_ORDER_ID_PARAM')):
            with self.subTest(unset=f'{patched.__name__}.{attribute}'), patch.object(patched, attribute, None):
                response = self.client.get(self.url)

                self.assertTemplateUsed(
                    response, 'zeitlabs_payments/payment_error.html', 'a gateway order id was found',
                )

    @patch.object(DummyProcessor, 'STATUS_VIEW', None)
    def test_status_view_is_required(self):
        """Verify that a processor naming no status view is refused as misconfigured, rather than given a dead page."""
        with self.assertRaises(ImproperlyConfigured):
            self.client.get(self.url)

    @override_settings(MIDDLEWARE=[*settings.MIDDLEWARE, 'django.middleware.csrf.CsrfViewMiddleware'])
    def test_gateway_can_post_the_learner_back_without_a_csrf_token(self):
        """
        Verify that the view is exempt from CSRF checks, so a gateway posting the learner back reaches it.

        The status view is not exempt, and its POST being refused with a 403 shows the check is enforced here. The
        return view handles GET only, so a POST that gets past the check is answered 405 rather than 403.
        """
        client = Client(enforce_csrf_checks=True)

        status_route = reverse('zeitlabs_payments:processor-status', args=['dummy'])
        self.assertEqual(client.post(status_route).status_code, 403, 'CSRF checks are not enforced here')
        self.assertEqual(client.post(self.url).status_code, 405, 'the POST was refused before it reached the view')

    def test_the_return_route_is_exempt_from_csrf_and_the_request_transaction(self):
        """
        Verify that the route the plugin mounts the return view on is exempt from CSRF and from the request transaction.

        Django reads both from the view a URL resolves to, which is the plugin's route rather than the processor's view
        it calls: a gateway posting the learner back cannot carry this site's CSRF token, and what the view records
        must be committed whatever follows.
        """
        route = resolve(self.url.split('?')[0]).func

        self.assertTrue(getattr(route, 'csrf_exempt', False), 'the return route is not exempt from CSRF checks')
        self.assertEqual(
            getattr(route, '_non_atomic_requests', set()),
            {'default'},
            'the return route runs in the request transaction',
        )


@pytest.mark.usefixtures('base_data')
class PaymentStatusViewMixinTest(TestCase):
    """Tests for PaymentStatusViewMixin, through the dummy processor's status view."""

    def setUp(self):
        """Setup"""
        self.user = User.objects.get(id=3)
        item = CatalogueItem.objects.get(sku='custom-sku-1')
        self.site = Site.objects.get(id=1)
        self.cart = Cart.objects.create(
            user=self.user,
            status=Cart.Status.PROCESSING,
            gateway=DummyProcessor.SLUG,
            payment_origin=f'https://{self.site.domain}',
        )
        self.cart.items.create(catalogue_item=item, original_price=item.price, final_price=item.price)
        self.reference = f'{self.site.id}-{self.cart.id}'
        self.answer = {'state': 'whatever the gateway says'}
        self.details = {
            'transaction_id': 'TX-1',
            'method': 'card',
            'amount': self.cart.total,
            'currency': 'SAR',
            'reason': 'paid with 4222****7895',
        }
        self.url = f"{reverse('zeitlabs_payments:processor-status', args=['dummy'])}?gateway_order_id=ORDER-1"
        self.moved_to_awaiting_payment = (
            AuditLog.AuditActions.CART_STATUS_UPDATED, 'Status updated for cart from: processing to: payment_pending.',
        )
        self.held_unreconciled = (
            AuditLog.AuditActions.PAYMENT_UNRECONCILED,
            'Payment taken by the gateway but not reconciled; the learner is not enrolled. '
            'Status updated from: payment_pending to: unreconciled.',
        )
        self.client.force_login(self.user)

    def poll(self, url=None, **methods):
        """
        Poll the status view with each named gateway method answering as given, and return the response and the mocks.

        A value that is an exception is raised by its method; any other value is returned. A method not named keeps the
        mixin's default.
        """
        mocks = {
            name: Mock(side_effect=value) if isinstance(value, Exception) else Mock(return_value=value)
            for name, value in methods.items()
        }
        with patch.multiple(DummyStatusView, **mocks):
            response = self.client.get(url or self.url)
        return response, mocks

    def assert_outcome(self, response, outcome):
        """Assert that the response is the answer the mixin gives for the outcome."""
        status, message = PaymentStatusViewMixin.OUTCOME_RESPONSES[outcome]
        self.assertEqual(response.status_code, status, f'the poll was not answered as {outcome}')
        self.assertEqual(
            response.json(),
            {'outcome': outcome, 'error': message.format(processor=DummyProcessor.NAME)},
            f'the body is not {outcome}',
        )

    def assert_cart_status(self, status, message):
        """Assert that the cart, as stored, is in the given status."""
        self.cart.refresh_from_db()
        self.assertEqual(self.cart.status, status, message)

    def audit_trail(self):
        """Return the cart's audit trail the way support reads it, oldest first: each entry's action and details."""
        return list(AuditLog.objects.filter(cart=self.cart).order_by('id').values_list('action', 'details'))

    def rejected(self, error):
        """Return the trail entry recording that the gateway's answer was refused, and why."""
        return (
            AuditLog.AuditActions.GATEWAY_RESPONSE_REJECTED,
            f'Gateway response rejected: {error}. Response: {self.answer}.',
        )

    def test_anonymous_poll_is_answered_as_login_required(self):
        """
        Verify that a poll without a session is answered with a 401 in JSON, and asks the gateway nothing.

        A learner's session can expire while the waiting page polls. A redirect to log in would reach the page as HTML
        it cannot read, and send the learner to the error page while their payment may still be going through.
        """
        self.client.logout()

        response, mocks = self.poll(get_order_cart_reference=self.reference)

        self.assert_outcome(response, Outcome.LOGIN_REQUIRED)
        self.assertFalse(mocks['get_order_cart_reference'].called, 'the gateway was asked on an anonymous poll')

    def test_the_status_route_runs_outside_the_request_transaction_but_checks_csrf(self):
        """
        Verify that the route the plugin mounts the status view on runs outside the request transaction, with CSRF.

        Settling a payment writes the transaction row first, so that money a gateway moved is on record whatever
        follows; inside the per-request transaction the LMS runs, anything escaping the view would roll that row back.
        Only the waiting page calls the view, so it keeps its CSRF checks.
        """
        route = resolve(reverse('zeitlabs_payments:processor-status', args=['dummy'])).func

        self.assertEqual(
            getattr(route, '_non_atomic_requests', set()),
            {'default'},
            'the status route runs in the request transaction',
        )
        self.assertFalse(getattr(route, 'csrf_exempt', False), 'the status route is exempt from CSRF checks')

    def test_poll_without_a_gateway_order_id(self):
        """Verify that a poll naming no order is answered as such, without asking the gateway anything."""
        response, mocks = self.poll(
            url=reverse('zeitlabs_payments:processor-status', args=['dummy']), get_order_cart_reference=self.reference,
        )

        self.assert_outcome(response, Outcome.MISSING_GATEWAY_ORDER_ID)
        self.assertFalse(mocks['get_order_cart_reference'].called, 'the gateway was asked about no order')

    def test_reference_is_none_until_the_processor_asks_the_gateway(self):
        """Verify that a view relying on the default reference method finds no cart, and never asks for a status."""
        response, mocks = self.poll(get_order_status=self.answer)

        self.assert_outcome(response, Outcome.INVALID_REFERENCE)
        self.assertFalse(mocks['get_order_status'].called, 'the gateway was asked about an order with no cart')
        self.assert_cart_status(Cart.Status.PROCESSING, 'a cart moved although no poll could name it')

    def test_gateway_unreachable_for_the_reference(self):
        """Verify that a gateway that cannot be asked which cart an order is for is reported as unreachable."""
        response, _ = self.poll(get_order_cart_reference=GatewayError('connection refused'))

        self.assert_outcome(response, Outcome.GATEWAY_ERROR)

    def test_reference_naming_no_cart(self):
        """
        Verify that a reference naming a cart that does not exist is reported as invalid, and the gateway not asked.

        The processor records the unknown cart in the audit trail, attributed to itself, since there is no cart to
        attach the entry to.
        """
        response, mocks = self.poll(get_order_cart_reference=f'{self.site.id}-0', get_order_status=self.answer)

        self.assert_outcome(response, Outcome.INVALID_REFERENCE)
        self.assertFalse(mocks['get_order_status'].called, 'the gateway was asked about an order with no cart')
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.AuditActions.RESPONSE_INVALID_CART, cart=None, gateway=DummyProcessor.SLUG,
            ).exists(),
            'the unknown cart left no trace in the audit trail',
        )

    def test_reference_naming_an_unknown_site(self):
        """Verify that a reference naming a site that does not exist is reported as invalid, the gateway unasked."""
        response, mocks = self.poll(get_order_cart_reference=f'0-{self.cart.id}', get_order_status=self.answer)

        self.assert_outcome(response, Outcome.INVALID_REFERENCE)
        self.assertFalse(mocks['get_order_status'].called, 'the gateway was asked about an order with no site')

    def test_poll_by_another_learner_is_answered_as_naming_no_cart(self):
        """
        Verify that a poll by anyone but the cart's learner is answered as an order naming no cart, and changes nothing.

        An order id can be seen elsewhere -- a shared screenshot, a forwarded link -- and a poll that went on would
        record the order on the cart, settle its payment with the poller as the initiator, and hand them its invoice
        number. The gateway is not asked about the payment, and the poller is not told whose cart it is.
        """
        self.client.force_login(User.objects.get(id=4))

        with self.assertLogs('zeitlabs_payments.providers.mixins', level='WARNING') as logs:
            response, mocks = self.poll(
                get_order_cart_reference=self.reference,
                get_order_status=self.answer,
                get_payment_status=PaymentStatus.SUCCESS,
                get_transaction_details=self.details,
            )

        self.assert_outcome(response, Outcome.INVALID_REFERENCE)
        self.assertFalse(mocks['get_order_status'].called, 'the gateway was asked about another learner\'s payment')
        self.cart.refresh_from_db()
        self.assertEqual(
            (self.cart.status, self.cart.gateway_order_id), (Cart.Status.PROCESSING, None),
            'another learner\'s poll moved the cart or recorded its order',
        )
        self.assertEqual(self.audit_trail(), [], 'another learner\'s poll was recorded on the cart')
        self.assertFalse(
            Transaction.objects.filter(cart=self.cart).exists(), 'another learner\'s poll settled the cart',
        )
        self.assertIn(
            f'user 4 polled gateway order ORDER-1 of cart {self.cart.id}, which is user 3\'s', '\n'.join(logs.output),
            'the refused poll was not logged with who polled whose cart',
        )

    def test_gateway_unreachable_for_the_status(self):
        """
        Verify that a gateway that cannot be asked for the status is reported as unreachable, and nothing changes.

        No answer means nothing was learned about the payment, so the cart and its audit trail are left as they were.
        """
        response, _ = self.poll(
            get_order_cart_reference=self.reference, get_order_status=GatewayError('connection refused'),
        )

        self.assert_outcome(response, Outcome.GATEWAY_ERROR)
        self.assert_cart_status(Cart.Status.PROCESSING, 'the cart moved although the gateway never answered')
        self.assertEqual(self.audit_trail(), [], 'a poll the gateway never answered was recorded')

    def test_status_is_asked_about_the_order_and_its_cart(self):
        """Verify that the gateway is asked for the status with the gateway order id sent and the cart it resolved."""
        _, mocks = self.poll(get_order_cart_reference=self.reference, get_order_status=self.answer)

        self.assertEqual(mocks['get_order_cart_reference'].call_args, call('ORDER-1'), 'the wrong order was named')
        self.assertEqual(
            mocks['get_order_status'].call_args, call('ORDER-1', self.cart), 'the status was asked of the wrong cart',
        )

    def test_status_is_none_until_the_processor_asks_the_gateway(self):
        """Verify that a view relying on the default status method reports an unexpected response, changing nothing."""
        response, _ = self.poll(get_order_cart_reference=self.reference)

        self.assert_outcome(response, Outcome.UNEXPECTED_RESPONSE)
        self.assert_cart_status(Cart.Status.PROCESSING, 'the cart moved although the gateway gave no answer')
        self.assertEqual(self.audit_trail(), [], 'a poll the gateway gave no answer to was recorded')

    def test_poll_records_the_order_a_cart_has_none_for(self):
        """
        Verify that the order a poll names is recorded on its cart when the cart has none yet, writing no audit entry.

        A gateway that opens its order only when the learner pays names it first in the redirect back, which is what the
        waiting page polls with.
        """
        self.poll(get_order_cart_reference=self.reference, get_order_status=self.answer)

        self.cart.refresh_from_db()
        self.assertEqual(self.cart.gateway_order_id, 'ORDER-1', 'the order the poll named was not recorded')

    def test_poll_leaves_a_cart_handed_to_another_gateway_without_an_order(self):
        """
        Verify that a poll through one processor records no order on a cart handed to another, and still answers.
        """
        Cart.objects.filter(pk=self.cart.pk).update(gateway='another-gateway')

        with self.assertLogs('zeitlabs_payments.providers.mixins', level='WARNING') as logs:
            response, _ = self.poll(
                get_order_cart_reference=self.reference,
                get_order_status=self.answer,
                get_payment_status=PaymentStatus.PENDING,
            )

        self.assert_outcome(response, Outcome.PENDING)
        self.cart.refresh_from_db()
        self.assertIsNone(self.cart.gateway_order_id, 'an order was recorded for a gateway the cart was not handed to')
        self.assertIn('order ORDER-1 not recorded', '\n'.join(logs.output), 'the refusal was not logged')

    def test_payment_status_is_none_until_the_processor_reads_the_answer(self):
        """
        Verify that a view relying on the default reading of the answer reports it as unexpected.

        The gateway did answer, so the cart moves to awaiting payment all the same, and the answer the view could not
        read is recorded on it.
        """
        response, _ = self.poll(get_order_cart_reference=self.reference, get_order_status=self.answer)

        self.assert_outcome(response, Outcome.UNEXPECTED_RESPONSE)
        self.assert_cart_status(Cart.Status.PAYMENT_PENDING, 'the cart did not move once the gateway answered')
        self.assertEqual(
            self.audit_trail(),
            [self.moved_to_awaiting_payment, self.rejected('its payment status could not be read')],
            'the unreadable answer is not on the record',
        )

    def test_pending_payment(self):
        """
        Verify that a payment still pending is answered 202 on every poll, the cart awaiting payment.

        The move to awaiting payment is recorded once: a pending payment is polled repeatedly, and nothing changes on
        the polls after the first.
        """
        methods = {
            'get_order_cart_reference': self.reference,
            'get_order_status': self.answer,
            'get_payment_status': PaymentStatus.PENDING,
        }

        for attempt in (1, 2):
            response, _ = self.poll(**methods)

            self.assert_outcome(response, Outcome.PENDING)
            self.assert_cart_status(Cart.Status.PAYMENT_PENDING, f'the cart is not awaiting payment on poll {attempt}')

        self.assertEqual(self.audit_trail(), [self.moved_to_awaiting_payment], 'the move was not recorded exactly once')

    def test_declined_payment(self):
        """
        Verify that a payment the gateway declines is answered as declined, and its cart cancelled.

        The decline is recorded with the gateway's answer, which is the evidence support is asked for.
        """
        response, _ = self.poll(
            get_order_cart_reference=self.reference,
            get_order_status=self.answer,
            get_payment_status=PaymentStatus.DECLINED,
        )

        self.assert_outcome(response, Outcome.DECLINED)
        self.assert_cart_status(Cart.Status.CANCELLED, 'the cart of a declined payment was not cancelled')
        self.assertEqual(
            self.audit_trail(),
            [
                self.moved_to_awaiting_payment,
                (
                    AuditLog.AuditActions.PAYMENT_DECLINED,
                    'Payment declined by the gateway. Status updated from: payment_pending to: cancelled. '
                    f'Response: {self.answer}.',
                ),
            ],
            'the decline is not on the record with the answer that caused it',
        )

    def test_cancelled_payment(self):
        """
        Verify that a payment the learner cancels is answered as cancelled, not declined, and its cart cancelled.

        The cart ends as a declined one does, but the learner is told they cancelled it rather than that their bank
        refused it, and the trail says the same.
        """
        response, _ = self.poll(
            get_order_cart_reference=self.reference,
            get_order_status=self.answer,
            get_payment_status=PaymentStatus.CANCELLED,
        )

        self.assert_outcome(response, Outcome.CANCELLED)
        self.assert_cart_status(Cart.Status.CANCELLED, 'the cart of a cancelled payment was not cancelled')
        self.assertEqual(
            self.audit_trail(),
            [
                self.moved_to_awaiting_payment,
                (
                    AuditLog.AuditActions.PAYMENT_CANCELLED,
                    "Payment cancelled on the gateway's page. Status updated from: payment_pending to: cancelled. "
                    f'Response: {self.answer}.',
                ),
            ],
            'the cancellation is not on the record with the answer that caused it',
        )

    def test_cancellation_is_not_applied_to_a_cart_already_paid(self):
        """
        Verify that a cancellation reaching a paid cart leaves it paid, and the refused answer is recorded on it.
        """
        Cart.objects.filter(pk=self.cart.pk).update(status=Cart.Status.PAID)

        response, _ = self.poll(
            get_order_cart_reference=self.reference,
            get_order_status=self.answer,
            get_payment_status=PaymentStatus.CANCELLED,
        )

        self.assert_outcome(response, Outcome.CANCELLED)
        self.assert_cart_status(Cart.Status.PAID, 'a cancellation cancelled a paid cart')
        self.assertEqual(
            self.audit_trail(),
            [self.rejected('the cart is paid, so the cancellation was not applied')],
            'the cancellation that was not applied is not on the record',
        )

    def test_decline_is_not_applied_to_a_cart_already_paid(self):
        """
        Verify that a decline reaching a paid cart leaves it paid, and the refused answer is recorded on it.

        A late or repeated answer must not cancel a cart whose payment settled and whose learner is enrolled. The poll
        is still answered as declined.
        """
        Cart.objects.filter(pk=self.cart.pk).update(status=Cart.Status.PAID)

        response, _ = self.poll(
            get_order_cart_reference=self.reference,
            get_order_status=self.answer,
            get_payment_status=PaymentStatus.DECLINED,
        )

        self.assert_outcome(response, Outcome.DECLINED)
        self.assert_cart_status(Cart.Status.PAID, 'a decline cancelled a paid cart')
        self.assertEqual(
            self.audit_trail(),
            [self.rejected('the cart is paid, so the decline was not applied')],
            'the decline that was not applied is not on the record',
        )

    def test_poll_finding_the_cart_moved_by_another_poll(self):
        """
        Verify that a poll whose cart another poll moved to awaiting payment meanwhile carries on, recording no move.

        Both polls read the cart as processing before asking the gateway. Only the first moves it; the second acts on
        the cart as the first left it.
        """
        def answer_once_another_poll_moved_the_cart(*_):
            Cart.objects.filter(pk=self.cart.pk).update(status=Cart.Status.PAYMENT_PENDING)
            return self.answer

        with patch.multiple(
            DummyStatusView,
            get_order_cart_reference=Mock(return_value=self.reference),
            get_order_status=Mock(side_effect=answer_once_another_poll_moved_the_cart),
            get_payment_status=Mock(return_value=PaymentStatus.PENDING),
        ):
            response = self.client.get(self.url)

        self.assert_outcome(response, Outcome.PENDING)
        self.assert_cart_status(Cart.Status.PAYMENT_PENDING, 'the cart is not awaiting payment')
        self.assertEqual(self.audit_trail(), [], 'this poll recorded a move another poll made')

    def test_success_is_refused_until_the_processor_verifies_it(self):
        """
        Verify that a view relying on the default verification settles nothing, since the default refuses.

        The gateway may have taken the money, so the refused answer is recorded on the cart with the reason, and the
        cart is held unreconciled rather than left awaiting payment or cancelled.
        """
        response, _ = self.poll(
            get_order_cart_reference=self.reference,
            get_order_status=self.answer,
            get_payment_status=PaymentStatus.SUCCESS,
            get_transaction_details=self.details,
        )

        self.assert_outcome(response, Outcome.VERIFICATION_FAILED)
        self.assertFalse(Transaction.objects.filter(cart=self.cart).exists(), 'an unverified payment was recorded')
        self.assert_cart_status(Cart.Status.UNRECONCILED, 'the cart of an unverified payment was not held')
        self.assertEqual(
            self.audit_trail(),
            [
                self.moved_to_awaiting_payment,
                self.rejected(
                    'failed verification (DummyStatusView does not verify success responses, so none is accepted)',
                ),
                self.held_unreconciled,
            ],
            'the refused answer is not on the record',
        )

    def test_success_is_not_recorded_until_the_processor_describes_it(self):
        """
        Verify that a view relying on the default transaction details records no payment, and says so.

        The gateway reported a verified success, so the answer the view could not describe is recorded on the cart, and
        the cart is held unreconciled.
        """
        response, _ = self.poll(
            get_order_cart_reference=self.reference,
            get_order_status=self.answer,
            get_payment_status=PaymentStatus.SUCCESS,
            verify_success_response=None,
        )

        self.assert_outcome(response, Outcome.NOT_FULFILLED)
        self.assertFalse(Transaction.objects.filter(cart=self.cart).exists(), 'a payment nobody described was recorded')
        self.assert_cart_status(Cart.Status.UNRECONCILED, 'the cart of an undescribed payment was not held')
        self.assertEqual(
            self.audit_trail(),
            [
                self.moved_to_awaiting_payment,
                self.rejected('no transaction details could be read from it'),
                self.held_unreconciled,
            ],
            'the undescribed answer is not on the record',
        )

    def test_verified_success_settles_the_payment(self):
        """
        Verify that a verified success records the payment, pays the cart and answers with its invoice.

        The invoice is addressed on the site the merchant reference names, which shows the site reached the settlement.
        The trail opens with the move to awaiting payment and records neither a decline nor a refusal.
        """
        response, mocks = self.poll(
            get_order_cart_reference=self.reference,
            get_order_status=self.answer,
            get_payment_status=PaymentStatus.SUCCESS,
            verify_success_response=None,
            get_transaction_details=self.details,
        )

        invoice = Invoice.objects.get(cart=self.cart)
        self.assertEqual(response.status_code, 200, 'a settled payment was not answered 200')
        self.assertEqual(
            response.json(),
            {
                'outcome': Outcome.PAID,
                'invoice': invoice.invoice_number,
                'invoice_url': reverse('zeitlabs_payments:invoice', args=[invoice.invoice_number]),
            },
            'the answer does not lead to the invoice',
        )
        self.assertEqual(
            mocks['verify_success_response'].call_args, call(self.answer, self.cart), 'the wrong answer was verified',
        )
        self.assertEqual(
            invoice.related_transaction.gateway_transaction_id, 'TX-1', 'the transaction id is not the gateway\'s',
        )
        self.assertTrue(
            invoice.receipt_url.startswith(f'https://{self.site.domain}/'),
            'the invoice is not issued on the origin the cart was paid on',
        )
        self.assert_cart_status(Cart.Status.PAID, 'the cart of a settled payment is not paid')

        trail = self.audit_trail()
        actions = {action for action, _ in trail}
        self.assertEqual(trail[0], self.moved_to_awaiting_payment, 'the trail does not open with the move')
        self.assertNotIn(AuditLog.AuditActions.PAYMENT_DECLINED, actions, 'a settled payment was recorded as declined')
        self.assertNotIn(
            AuditLog.AuditActions.GATEWAY_RESPONSE_REJECTED, actions, 'a settled payment was recorded as refused',
        )

    def test_repeat_poll_after_settlement_answers_with_the_same_invoice(self):
        """
        Verify that a poll repeated after the payment settled is answered with its invoice, settling nothing again.

        A learner who reloads the waiting page, or comes back to it, polls a payment the gateway still reports as a
        success. Settling it again would be refused, the cart being paid, and the learner sent to the error page for a
        course they own. The repeat writes nothing, since nothing changed.
        """
        methods = {
            'get_order_cart_reference': self.reference,
            'get_order_status': self.answer,
            'get_payment_status': PaymentStatus.SUCCESS,
            'verify_success_response': None,
            'get_transaction_details': self.details,
        }
        first, _ = self.poll(**methods)
        trail_after_settling = self.audit_trail()

        repeat, _ = self.poll(**methods)

        self.assertEqual(first.status_code, 200, 'the payment did not settle')
        self.assertEqual(repeat.status_code, 200, 'the repeat poll was not answered as settled')
        self.assertEqual(repeat.json(), first.json(), 'the repeat poll does not lead to the same invoice')
        self.assertEqual(Invoice.objects.filter(cart=self.cart).count(), 1, 'the repeat poll issued another invoice')
        self.assertEqual(Transaction.objects.filter(cart=self.cart).count(), 1, 'the repeat poll recorded another row')
        self.assertEqual(self.audit_trail(), trail_after_settling, 'the repeat poll wrote to the trail')

    def success(self, **overrides):
        """Return the gateway methods answering a poll with a verified success, each overridable."""
        return {
            'get_order_cart_reference': self.reference,
            'get_order_status': self.answer,
            'get_payment_status': PaymentStatus.SUCCESS,
            'verify_success_response': None,
            'get_transaction_details': self.details,
            **overrides,
        }

    def test_success_under_another_transaction_on_the_cart_order_is_the_same_payment(self):
        """
        Verify that a success reported under another transaction id, on the order the cart was paid under, is answered
        with the cart's invoice, recording nothing.

        A processor hands each cart off to one order, which takes one payment, so the order is the payment: a gateway
        may report it under an authorisation's id and later its capture's, or under its latest operation's.
        """
        first, _ = self.poll(**self.success())
        trail_after_settling = self.audit_trail()

        repeat, _ = self.poll(**self.success(get_transaction_details={**self.details, 'transaction_id': 'TX-2'}))

        self.assertEqual(repeat.status_code, 200, 'the same payment under another id was not answered as settled')
        self.assertEqual(repeat.json(), first.json(), 'the same payment does not lead to its invoice')
        self.assertEqual(Transaction.objects.filter(cart=self.cart).count(), 1, 'the same payment was recorded again')
        self.assertEqual(self.audit_trail(), trail_after_settling, 'the same payment wrote to the trail')

    def test_success_under_an_order_the_cart_was_not_handed_off_to_is_recorded_but_not_settled(self):
        """
        Verify that a success for a paid cart under another order is recorded as a failed transaction saying why, kept
        on the trail with the gateway's answer, and leaves the cart and its invoice as they were.

        A second order paying for the cart is a gateway breaking the single-use rule, so money may have been taken
        twice: a person refunds it.
        """
        self.poll(**self.success())
        other_order = f"{reverse('zeitlabs_payments:processor-status', args=['dummy'])}?gateway_order_id=ORDER-2"

        with self.assertLogs('zeitlabs_payments.providers.mixins', level='ERROR') as logs:
            response, _ = self.poll(
                url=other_order, **self.success(get_transaction_details={**self.details, 'transaction_id': 'TX-2'}),
            )

        error = 'order ORDER-2 is not the order ORDER-1 the cart was handed off to, so it may be a second payment'
        self.assert_outcome(response, Outcome.NOT_FULFILLED)
        self.assert_cart_status(Cart.Status.PAID, 'a success under another order moved the cart')
        self.assertEqual(Invoice.objects.filter(cart=self.cart).count(), 1, 'the other order was invoiced')
        recorded = Transaction.objects.filter(cart=self.cart, gateway_transaction_id='TX-2').first()
        self.assertIsNotNone(recorded, 'the other order\'s payment was not recorded')
        self.assertEqual(recorded.status, DummyProcessor.TRANSACTION_STATUS_FAILED, 'the other order was settled')
        self.assertIn(f'not reconciled: {error}', recorded.reason, 'the row does not say why it was not settled')
        self.assertIn(self.rejected(error), self.audit_trail(), 'the gateway\'s answer is not on the trail')
        self.assertIn(error, '\n'.join(logs.output), 'the breach was not logged as an error')

    def test_success_under_another_order_keeps_the_answer_when_its_row_cannot_be_written(self):
        """
        Verify that a success under another order reporting the transaction id already settled keeps the gateway's
        answer on the trail, though no second row can be written under the same id.
        """
        self.poll(**self.success())
        other_order = f"{reverse('zeitlabs_payments:processor-status', args=['dummy'])}?gateway_order_id=ORDER-2"

        response, _ = self.poll(url=other_order, **self.success())

        self.assert_outcome(response, Outcome.NOT_FULFILLED)
        self.assertEqual(Transaction.objects.filter(cart=self.cart).count(), 1, 'a row was written under a taken id')
        self.assertIn(
            self.rejected(
                'order ORDER-2 is not the order ORDER-1 the cart was handed off to, so it may be a second payment',
            ),
            self.audit_trail(),
            'the gateway\'s answer is lost',
        )

    def test_cart_without_an_order_recorded_is_matched_on_the_transaction_id(self):
        """
        Verify that a paid cart handed off before carts recorded their order answers a repeat of its own transaction
        with its invoice, and a success under another transaction as not fulfilled.

        Without an order to know the payment by, only the transaction id tells a repeat from a second charge.
        """
        first, _ = self.poll(**self.success())
        Cart.objects.filter(pk=self.cart.pk).update(gateway_order_id=None)

        repeat, _ = self.poll(**self.success())
        other, _ = self.poll(**self.success(get_transaction_details={**self.details, 'transaction_id': 'TX-2'}))

        self.assertEqual(repeat.json(), first.json(), 'a repeat of the cart\'s own transaction is not its invoice')
        self.assert_outcome(other, Outcome.NOT_FULFILLED)

    def test_payment_for_other_money_is_recorded_but_not_settled(self):
        """
        Verify that a success that is the cart's payment, but not for the money the cart expected, is still recorded.

        The gateway took money for this cart, so the payment goes on the ledger as the gateway describes it -- the
        transaction row exists, marked failed, its reason saying why it was not reconciled -- while the cart is held
        unreconciled rather than paid, and no invoice is issued.
        """
        mismatch = GatewayError('the payment is for 51.000, but the cart totals 50.000')

        with patch.object(DummyProcessor, 'reconcile_payment', side_effect=mismatch):
            response, _ = self.poll(
                get_order_cart_reference=self.reference,
                get_order_status=self.answer,
                get_payment_status=PaymentStatus.SUCCESS,
                verify_success_response=None,
                get_transaction_details=self.details,
            )

        self.assert_outcome(response, Outcome.NOT_FULFILLED)
        recorded = Transaction.objects.get(cart=self.cart)
        self.assertEqual(
            (recorded.gateway_transaction_id, recorded.status),
            ('TX-1', DummyProcessor.TRANSACTION_STATUS_FAILED),
            'the payment is not on the ledger, marked unsettled',
        )
        self.assertIn(f'not reconciled: {mismatch}', recorded.reason, 'the row does not say why it was not reconciled')
        self.assertFalse(Invoice.objects.filter(cart=self.cart).exists(), 'an unreconciled payment was invoiced')
        self.assert_cart_status(Cart.Status.UNRECONCILED, 'the cart of an unreconciled payment was not held')
        self.assertEqual(
            [action for action, _ in self.audit_trail()],
            [
                AuditLog.AuditActions.CART_STATUS_UPDATED,
                AuditLog.AuditActions.TRANSACTION_NOT_RECONCILED,
                AuditLog.AuditActions.PAYMENT_UNRECONCILED,
            ],
            'the trail does not show the payment recorded unreconciled and the cart held',
        )

    def test_payment_completing_after_its_cart_expired_still_settles_it(self):
        """
        Verify that a payment completing after its cart expired is settled as any other, invoicing and paying the cart.

        Expiry is a judgement that the session lapsed; a gateway that took the money all the same proves it wrong.
        """
        Cart.objects.filter(pk=self.cart.pk).update(status=Cart.Status.EXPIRED)

        response, _ = self.poll(
            get_order_cart_reference=self.reference,
            get_order_status=self.answer,
            get_payment_status=PaymentStatus.SUCCESS,
            verify_success_response=None,
            get_transaction_details=self.details,
        )

        self.assertEqual(response.status_code, 200, 'a late payment on an expired cart was not settled')
        self.assert_cart_status(Cart.Status.PAID, 'the expired cart of a late payment was not paid')
        self.assertTrue(Invoice.objects.filter(cart=self.cart).exists(), 'the late payment was not invoiced')

    def test_payment_for_a_course_its_learner_came_to_hold_is_left_for_staff(self):
        """
        Verify that a success for a course its learner was enrolled in by staff while it was at the gateway is recorded
        but not settled, and the cart held unreconciled for a person to accept or refund.

        Settling it would leave a sale with an invoice for nothing new, which staff are never shown.
        """
        course_mode = CourseMode.objects.get(sku=self.cart.items.first().catalogue_item.sku)
        CourseEnrollment.objects.create(
            user=self.user, course=course_mode.course, mode=course_mode.mode_slug, is_active=True,
        )

        response, _ = self.poll(
            get_order_cart_reference=self.reference,
            get_order_status=self.answer,
            get_payment_status=PaymentStatus.SUCCESS,
            verify_success_response=None,
            get_transaction_details=self.details,
        )

        self.assert_outcome(response, Outcome.NOT_FULFILLED)
        self.assert_cart_status(Cart.Status.UNRECONCILED, 'a payment for a course already held was not left for staff')
        recorded = Transaction.objects.get(cart=self.cart)
        self.assertEqual(recorded.status, DummyProcessor.TRANSACTION_STATUS_FAILED, 'the payment was settled')
        self.assertIn('the learner already holds custom-sku-1', recorded.reason, 'the row does not say why')
        self.assertFalse(Invoice.objects.filter(cart=self.cart).exists(), 'a payment that bought nothing was invoiced')

    def test_payment_completing_after_the_course_was_bought_again_is_held(self):
        """
        Verify that a late payment on an expired cart whose course was bought again is recorded but not settled.

        Settling it would charge the learner twice for one course, so the payment goes on the ledger marked failed and
        the cart is held unreconciled for a person to refund.
        """
        Cart.objects.filter(pk=self.cart.pk).update(status=Cart.Status.EXPIRED)
        bought_again = Cart.objects.create(user=self.user, status=Cart.Status.PAID)
        item = self.cart.items.first().catalogue_item
        bought_again.items.create(catalogue_item=item, original_price=item.price, final_price=item.price)

        response, _ = self.poll(
            get_order_cart_reference=self.reference,
            get_order_status=self.answer,
            get_payment_status=PaymentStatus.SUCCESS,
            verify_success_response=None,
            get_transaction_details=self.details,
        )

        self.assert_outcome(response, Outcome.NOT_FULFILLED)
        self.assert_cart_status(Cart.Status.UNRECONCILED, 'a second payment for one course was not held')
        recorded = Transaction.objects.get(cart=self.cart)
        self.assertEqual(recorded.status, DummyProcessor.TRANSACTION_STATUS_FAILED, 'the second payment was settled')
        self.assertIn(
            f'cart {bought_again.id} (paid) holds its items',
            recorded.reason,
            'the row does not say why it was held',
        )

    def test_repeat_poll_of_an_unreconciled_cart_answers_without_asking_the_gateway(self):
        """
        Verify that a poll of a cart already held unreconciled is answered as not fulfilled at once, writing nothing.

        The refusal is on record already. Asking the gateway again would only run the same refused settlement and write
        it to the trail once more, every time the learner comes back.
        """
        Cart.objects.filter(pk=self.cart.pk).update(status=Cart.Status.UNRECONCILED)

        response, mocks = self.poll(get_order_cart_reference=self.reference, get_order_status=self.answer)

        self.assert_outcome(response, Outcome.NOT_FULFILLED)
        self.assertFalse(mocks['get_order_status'].called, 'the gateway was asked about a cart already held')
        self.assertEqual(self.audit_trail(), [], 'a poll of a cart already held wrote to the trail')

    def test_cart_settled_by_another_poll_meanwhile_is_not_held_unreconciled(self):
        """
        Verify that a cart another poll moved on while this one failed to settle it keeps the status it was given.
        """
        def settled_by_another_poll(**_):
            Cart.objects.filter(pk=self.cart.pk).update(status=Cart.Status.PAID)

        with patch.object(DummyProcessor, 'process_payment_and_update_records', side_effect=settled_by_another_poll):
            with self.assertLogs('zeitlabs_payments.providers.mixins', level='WARNING') as logs:
                response, _ = self.poll(
                    get_order_cart_reference=self.reference,
                    get_order_status=self.answer,
                    get_payment_status=PaymentStatus.SUCCESS,
                    verify_success_response=None,
                    get_transaction_details=self.details,
                )

        self.assert_outcome(response, Outcome.NOT_FULFILLED)
        self.assert_cart_status(Cart.Status.PAID, 'a cart another poll paid was held unreconciled')
        self.assertIn('was not held as unreconciled', '\n'.join(logs.output), 'the lost move was not logged')

    def test_settlement_that_issues_no_invoice(self):
        """
        Verify that a settlement handed everything it needs, but issuing no invoice, is answered as not fulfilled.

        The settlement records its own failures, so the view adds nothing to the trail for them beyond holding the cart
        unreconciled.
        """
        with patch.object(DummyProcessor, 'process_payment_and_update_records', return_value=None) as settle:
            response, _ = self.poll(
                get_order_cart_reference=self.reference,
                get_order_status=self.answer,
                get_payment_status=PaymentStatus.SUCCESS,
                verify_success_response=None,
                get_transaction_details=self.details,
            )

        self.assert_outcome(response, Outcome.NOT_FULFILLED)
        self.assertEqual(
            settle.call_args,
            call(
                cart=self.cart,
                data=self.answer,
                request=ANY,
                transaction_status=DummyProcessor.TRANSACTION_STATUS_SUCCESS,
                site_id=self.site.id,
                **self.details,
            ),
            'the settlement was not handed the payment as the gateway described it',
        )
        self.assert_cart_status(Cart.Status.UNRECONCILED, 'the cart of an unsettled success was not held')
        self.assertEqual(
            self.audit_trail(),
            [self.moved_to_awaiting_payment, self.held_unreconciled],
            'the view recorded what the settlement records',
        )

    @patch.object(DummyStatusView, 'processor_class', None)
    def test_processor_is_required(self):
        """Verify that a view naming no processor is refused as misconfigured."""
        with self.assertRaises(ImproperlyConfigured):
            self.client.get(self.url)
