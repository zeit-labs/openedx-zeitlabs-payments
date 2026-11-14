"""Test views for the zeitlabs_payments app"""

import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import call, patch

import pytest
from common.djangoapps.course_modes.models import CourseMode
from common.djangoapps.student.models import CourseEnrollment
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils.timezone import now
from rest_framework import status as http_status
from rest_framework.test import APITestCase
from waffle.testutils import override_switch

from test_utils.dummy_processor import DummyProcessor
from zeitlabs_payments.constants import HANDOFF_GRACE_PERIOD, UNRECORDED_GATEWAY_GRACE_PERIOD
from zeitlabs_payments.exceptions import CartStatusError
from zeitlabs_payments.models import (
    AuditLog,
    BundleCourseItem,
    Cart,
    CartItem,
    CatalogueItem,
    Invoice,
    InvoiceItem,
    Transaction,
)
from zeitlabs_payments.views import InitiatePaymentView

User = get_user_model()

PAYMENT_ORIGIN = 'https://learn.example.com'
STORED_LEARNER_INFO = {'name': 'Layla Haddad'}
STORED_ORGANIZATION_INFO = {'name': 'The Key Institute LLC', 'vat_no': '445566', 'cr_no': '7788'}


class BaseTestViewMixin(APITestCase):
    """Base test view mixin"""
    VIEW_NAME = 'view name is not set!'

    def setUp(self):
        """Setup"""
        self.view_name = self.VIEW_NAME
        self.url_args = []
        self.learner1_id = 3
        self.learner2_id = 4

    @property
    def url(self):
        """Get the URL"""
        return reverse(self.view_name, args=self.url_args)

    def login_user(self, user):
        """Helper to login user"""
        self.client.force_login(user)


@pytest.mark.usefixtures('base_data')
class CartViewTest(BaseTestViewMixin):
    """Tests for CartView"""
    VIEW_NAME = 'zeitlabs_payments:cart-add'

    def test_unauthorized(self):
        """Verify that the view returns 403 when the user is not authenticated"""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, http_status.HTTP_403_FORBIDDEN)

    def test_get_success(self):
        """
        Verify following cases
        - Returns None when cart exist for another user but not for login user
        - Returns cart when cart is there with pending state for login user
        - Returns None when cart exist for user but with paid state, no cart exist with pending state.

        """
        user = User.objects.get(id=self.learner1_id)
        course_item = CatalogueItem.objects.get(sku='custom-sku-1')

        # other user has pending cart.
        other_user = User.objects.get(id=self.learner2_id)
        other_user_cart = Cart.objects.create(user=other_user, status=Cart.Status.PENDING)
        other_user_cart.items.create(
            catalogue_item=course_item,
            original_price=course_item.price,
            final_price=course_item.price
        )

        self.login_user(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, http_status.HTTP_200_OK)

        assert response.data['details'] == f'No pending cart found for user: {user}'
        user_cart = Cart.objects.create(user=user, status=Cart.Status.PENDING)
        user_cart.items.create(
            catalogue_item=course_item,
            original_price=course_item.price,
            final_price=course_item.price
        )

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, http_status.HTTP_200_OK)
        assert response.data['user'] == user.id
        assert response.data['status'] == Cart.Status.PENDING
        assert len(response.data['items']) == 1
        assert response.data['items'][0]['sku'] == course_item.sku

        # update cart status to paid
        user_cart.status = Cart.Status.PAID
        user_cart.save(allow_status_change=True)

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, http_status.HTTP_200_OK)
        assert response.data['details'] == f'No pending cart found for user: {user}'

    def test_post_success(self):
        """
        Verify following cases
        - User added sku, new cart should be created.
        - User tries to add another sku, old pending cart should be cancelled and new cart
          with give sku should be created
        """
        user = User.objects.get(id=self.learner1_id)
        course_item = CatalogueItem.objects.get(sku='custom-sku-1')

        self.login_user(user)
        response = self.client.post(self.url, data={
            'sku': course_item.sku
        })
        self.assertEqual(response.status_code, http_status.HTTP_201_CREATED)
        assert response.data['user'] == user.id
        assert response.data['status'] == Cart.Status.PENDING
        assert len(response.data['items']) == 1
        assert response.data['items'][0]['sku'] == course_item.sku

        user_old_cart = Cart.objects.get(id=response.data['id'])

        # user tries to add same sku again
        response = self.client.post(self.url, data={
            'sku': course_item.sku
        })
        self.assertEqual(response.status_code, http_status.HTTP_201_CREATED)

        # assert that new cart has been created with same catalogue_item.
        assert response.data['id'] != user_old_cart.id
        assert response.data['user'] == user.id
        assert response.data['status'] == Cart.Status.PENDING
        assert len(response.data['items']) == 1
        assert response.data['items'][0]['sku'] == course_item.sku

        # assert that old cart is updated to cancel state
        user_old_cart.refresh_from_db()
        assert user_old_cart.status == Cart.Status.CANCELLED

    def test_post_failed(self):
        """
        Verify following cases
        - User does not send 'sku' in payload
        - User sends invalid sku, catalogue_item does not exist for given sku.
        """
        user = User.objects.get(id=self.learner1_id)
        self.login_user(user)

        response = self.client.post(self.url, data={
            'something-else-than-sku': 'invalid'
        })
        self.assertEqual(response.status_code, http_status.HTTP_400_BAD_REQUEST)
        assert response.data['error'] == 'SKU is required'

        response = self.client.post(self.url, data={'sku': 'invalid'})
        self.assertEqual(response.status_code, http_status.HTTP_400_BAD_REQUEST)
        assert response.data['error'] == 'Invalid SKU, unable to find catalogue item.'

    @patch.dict(
        'zeitlabs_payments.views.CART_HANDLER', {}, clear=True
    )
    def test_post_failed_for_unsupported_item_type(self):
        """Verify that """
        user = User.objects.get(id=self.learner1_id)
        course_item = CatalogueItem.objects.get(sku='custom-sku-1')

        self.login_user(user)
        response = self.client.post(self.url, data={
            'sku': course_item.sku
        })
        self.assertEqual(response.status_code, http_status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['error'], 'Item with given SKU has unsupported type: paid_course.')

    def test_post_multiple_skus_returns_json_error(self):
        """Verify that a multi-SKU cart-add request returns JSON 400, not HTML.

        Both SKUs must appear in the error log for audit visibility, and no cart may be created.
        The API keeps its existing `{'error': ...}` shape so third-party clients see no new field.
        """
        user = User.objects.get(id=self.learner1_id)
        self.login_user(user)
        with self.assertLogs('zeitlabs_payments.views', level='ERROR') as cm:
            response = self.client.post(
                self.url,
                data='sku=custom-sku-1&sku=custom-sku-2',
                content_type='application/x-www-form-urlencoded',
            )
        self.assertEqual(response.status_code, http_status.HTTP_400_BAD_REQUEST)
        self.assertIn('one item per purchase', response.data['error'])
        log_text = '\n'.join(cm.output)
        assert 'custom-sku-1' in log_text, 'Multi-SKU refusal must log the first SKU.'
        assert 'custom-sku-2' in log_text, 'Multi-SKU refusal must log the second SKU.'
        self.assertFalse(
            Cart.objects.filter(user=user).exists(),
            'A refused multi-SKU request must not create a cart.',
        )

    def test_post_single_sku_in_list_still_succeeds(self):
        """Verify that the single-element list path is unchanged after the multi-SKU refusal."""
        user = User.objects.get(id=self.learner1_id)
        course_item = CatalogueItem.objects.get(sku='custom-sku-1')
        self.login_user(user)
        response = self.client.post(self.url, data={'sku': course_item.sku})
        self.assertEqual(response.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(response.data['items'][0]['sku'], course_item.sku)

    def test_post_sku_as_json_list_refuses_multi_sku(self):
        """Verify that a JSON body with sku as a list is refused like a form-encoded multi-SKU body.

        A list value is the JSON equivalent of repeating the key, and the same refusal must apply.
        """
        user = User.objects.get(id=self.learner1_id)
        self.login_user(user)
        carts_before = Cart.objects.filter(user=user).count()
        response = self.client.post(
            self.url,
            data={'sku': ['custom-sku-1', 'custom-sku-2']},
            format='json',
        )
        self.assertEqual(response.status_code, http_status.HTTP_400_BAD_REQUEST)
        self.assertIn('one item per purchase', response.data['error'])
        self.assertFalse(
            Cart.objects.filter(user=user).exists(),
            'A refused multi-SKU request must not create a cart.',
        )
        self.assertEqual(Cart.objects.filter(user=user).count(), carts_before)

    def test_post_json_scalar_sku_still_succeeds(self):
        """Verify that a JSON body with a single sku string takes the dict branch unchanged."""
        user = User.objects.get(id=self.learner1_id)
        course_item = CatalogueItem.objects.get(sku='custom-sku-1')
        self.login_user(user)
        response = self.client.post(self.url, data={'sku': course_item.sku}, format='json')
        self.assertEqual(response.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(response.data['items'][0]['sku'], course_item.sku)

    def test_post_json_missing_sku_returns_required_error(self):
        """Verify that a JSON body without a sku hits the empty-dict branch and returns the existing error."""
        user = User.objects.get(id=self.learner1_id)
        self.login_user(user)
        response = self.client.post(self.url, data={'something-else': 'x'}, format='json')
        self.assertEqual(response.status_code, http_status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['error'], 'SKU is required')

    @patch(
        'zeitlabs_payments.helpers.CourseEnrollment.is_enrolled'
    )
    def test_post_failed_for_add_to_cart_validation(self, mock_is_enrolled):
        """Verify that """
        user = User.objects.get(id=self.learner1_id)
        course_item = CatalogueItem.objects.get(sku='custom-sku-1')
        self.login_user(user)
        mock_is_enrolled.return_value = True
        response = self.client.post(self.url, data={
            'sku': course_item.sku
        })
        self.assertEqual(response.status_code, http_status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['error'], 'Given SKU item does not match add to cart requirements')
        details = response.data['details']
        self.assertIn(f'User {user.username} does not fulfill enrollment conditions', details)
        self.assertIn(course_item.sku, details, 'the API detail does not name the SKU')
        self.assertIn(course_item.item_ref_id, details, 'the API detail does not name the course')
        self.assertIn('User is already enrolled in the course.', details, 'the API detail omits the error behind it')


@pytest.mark.usefixtures('base_data')
class InitiatePaymentViewTest(TestCase):
    """Innitiate Payment View Test."""

    def setUp(self):
        self.user = User.objects.get(id=3)
        self.other_user = User.objects.get(id=4)
        self.cart = Cart.objects.create(user=self.user, status=Cart.Status.PENDING)
        self.provider = 'dummy'
        self.url = reverse('zeitlabs_payments:initiate-payment', args=[self.provider, str(self.cart.id)])
        self.checkout_link = f'href="{reverse("zeitlabs_payments:checkout")}?sku=custom-sku-1"'

    def add_item(self):
        """Put a catalogue item in the cart, so that a refusal can lead back to checkout for it."""
        item = CatalogueItem.objects.get(sku='custom-sku-1')
        self.cart.items.create(catalogue_item=item, original_price=item.price, final_price=item.price)

    def assert_refused(self, response, message, status_code=400):
        """Assert that the response is the refusal page saying the message, with the status code given."""
        self.assertEqual(response.status_code, status_code, f'the refusal is not answered {status_code}')
        self.assertTemplateUsed(response, 'zeitlabs_payments/payment_refused.html', 'the refusal is not a page')
        self.assertContains(response, message, status_code=status_code, msg_prefix='the refusal does not say why')

    def test_redirects_if_not_logged_in(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_unknown_provider_is_refused_with_a_way_back_to_checkout(self):
        """Verify that an unknown payment method is refused with a page leading back to checkout for the cart's item."""
        self.add_item()
        self.client.force_login(self.user)

        response = self.client.get(reverse('zeitlabs_payments:initiate-payment', args=['invalid', str(self.cart.id)]))

        self.assert_refused(response, 'This payment method is not available.')
        self.assertContains(response, self.checkout_link, status_code=400, msg_prefix='no way back to checkout')
        self.assertNotContains(response, 'invalid', status_code=400, msg_prefix='the page names the provider asked for')

    def test_a_cart_not_the_learners_is_answered_alike_whatever_the_reason(self):
        """
        Verify that a malformed cart id, a cart that does not exist, and another learner's cart get the same page.

        Cart ids are sequential, so any answer telling them apart would let a learner walking the ids learn which
        carts exist and whose they are. The page names no one and leads to the courses, the cart being none of theirs.
        """
        self.client.force_login(self.other_user)
        urls = {
            'malformed': reverse('zeitlabs_payments:initiate-payment', args=[self.provider, 'abc']),
            'missing': reverse('zeitlabs_payments:initiate-payment', args=[self.provider, '1000']),
            "another learner's": self.url,
        }

        with self.assertLogs('zeitlabs_payments.views', level='WARNING') as logs:
            responses = {reason: self.client.get(url) for reason, url in urls.items()}

        for reason, response in responses.items():
            with self.subTest(reason=reason):
                self.assert_refused(response, 'This payment cannot be found.', status_code=404)
                self.assertContains(
                    response, f'href="{reverse("courses")}"', status_code=404, msg_prefix='no way to the courses',
                )
                for name in (self.user.username, self.other_user.username):
                    self.assertNotContains(response, name, status_code=404, msg_prefix='the page names a learner')
        self.assertEqual(
            len({response.content for response in responses.values()}), 1, 'the three refusals can be told apart',
        )
        self.assertIn(
            f'User 4 asked to pay for cart {self.cart.id}, which is user 3\'s.', '\n'.join(logs.output),
            'the log does not tell another learner\'s cart apart',
        )
        self.cart.refresh_from_db()
        self.assertEqual(self.cart.status, Cart.Status.PENDING, 'another learner\'s request moved the cart')

    def test_successful_payment_view_initiation(self):
        request = RequestFactory().get(self.url)
        request.user = self.cart.user
        request.site = Site.objects.create(name='test.com', domain='test.com')
        InitiatePaymentView.as_view()(request, self.provider, self.cart.id)
        self.cart.refresh_from_db()
        self.assertEqual(self.cart.status, Cart.Status.PROCESSING)

    def test_returns_400_if_provider_hidden_for_user(self):
        """
        Verify that a processor hidden for the user is refused as an unknown one is, leaving the cart untouched.

        Hiding is how a processor limits who may pay with it. A processor that fakes payments for testers is the
        sharpest case: anyone who could start a payment with it by typing its address would enrol for free.
        """
        self.client.force_login(self.user)
        with patch.object(DummyProcessor, 'is_hidden_for', return_value=True):
            with self.assertLogs('zeitlabs_payments.views', level='ERROR') as logs:
                response = self.client.get(self.url)

        self.assert_refused(response, 'This payment method is not available.')
        self.assertIn(
            'Payment provider dummy is not offered to user user3', '\n'.join(logs.output),
            'The refusal must be logged with the provider and the user',
        )
        self.cart.refresh_from_db()
        self.assertEqual(self.cart.status, Cart.Status.PENDING, 'A refused payment must leave the cart pending')
        self.assertFalse(
            AuditLog.objects.filter(cart=self.cart).exists(),
            'A refused payment must record nothing against the cart',
        )

    def test_returns_400_if_provider_fails_to_decide_whether_it_is_hidden(self):
        """
        Verify that a processor whose hide check raises is refused as a hidden one is, and the failure is logged.

        The checkout page already leaves such a processor out, so a broken check must not leave its address open.
        """
        self.client.force_login(self.user)
        with patch.object(DummyProcessor, 'is_hidden_for', side_effect=RuntimeError('flag lookup failed')):
            with self.assertLogs('zeitlabs_payments.views', level='ERROR') as logs:
                response = self.client.get(self.url)

        self.assert_refused(response, 'This payment method is not available.')
        self.assertIn(
            'RuntimeError: flag lookup failed', '\n'.join(logs.output),
            'The failure must be logged with its traceback',
        )
        self.cart.refresh_from_db()
        self.assertEqual(self.cart.status, Cart.Status.PENDING, 'A refused payment must leave the cart pending')
        self.assertFalse(
            AuditLog.objects.filter(cart=self.cart).exists(),
            'A refused payment must record nothing against the cart',
        )

    def test_successful_payment_view_initiation_if_provider_visible_for_user(self):
        """
        Verify that a processor offered to the user still initiates the payment.

        The processor is asked about this very request, since who may pay with it is decided per learner.
        """
        request = RequestFactory().get(self.url)
        request.user = self.user
        request.site = Site.objects.get_current()
        with patch.object(DummyProcessor, 'is_hidden_for', return_value=False) as is_hidden_for:
            InitiatePaymentView.as_view()(request, self.provider, self.cart.id)

        self.assertEqual(
            is_hidden_for.call_args_list, [call(request)],
            'The processor must be asked whether it is hidden for this request',
        )
        self.cart.refresh_from_db()
        self.assertEqual(
            self.cart.status, Cart.Status.PROCESSING,
            'A processor offered to the user must initiate the payment',
        )

    def test_hand_off_records_the_gateway_and_the_payment_origin(self):
        """
        Verify that a cart handed off is processing, and records the gateway it was handed to and the origin it is paid
        on: the scheme of the request and the domain of its site.
        """
        request = RequestFactory().get(self.url, secure=True)
        request.user = self.user
        request.site = Site.objects.create(name='learn', domain='learn.example.com')

        InitiatePaymentView.as_view()(request, self.provider, self.cart.id)

        self.cart.refresh_from_db()
        self.assertEqual(
            (self.cart.status, self.cart.gateway, self.cart.payment_origin),
            (Cart.Status.PROCESSING, 'dummy', 'https://learn.example.com'),
            'the handed-off cart does not know its gateway or the origin it is paid on',
        )

    def test_refuses_a_site_that_gives_no_usable_origin(self):
        """
        Verify that a site whose domain does not make a bare origin is refused before the gateway is called, and the
        cart left pending.

        Its invoice could not be issued on that origin, so the learner would be charged for a payment that ends without
        an invoice.
        """
        request = RequestFactory().get(self.url, secure=True)
        request.user = self.user
        request.site = Site.objects.create(name='learn', domain='learn.example.com/lms')

        with patch('test_utils.dummy_processor.DummyProcessor.payment_view') as payment_view:
            with self.assertLogs('zeitlabs_payments.views', level='ERROR') as logs:
                response = InitiatePaymentView.as_view()(request, self.provider, self.cart.id)

        self.assertEqual(response.status_code, 400, 'a site with no usable origin was not refused')
        self.assertIn(
            'Payments cannot be made on this site right now.', response.content.decode(),
            'the refusal page does not say why',
        )
        self.assertIn(
            "Payment origin 'https://learn.example.com/lms' must be a scheme and a host", '\n'.join(logs.output),
            'the refusal does not say which origin was refused',
        )
        self.assertFalse(payment_view.called, 'the gateway was called for a payment no invoice could be issued for')
        self.cart.refresh_from_db()
        self.assertEqual(
            (self.cart.status, self.cart.payment_origin), (Cart.Status.PENDING, None),
            'the refused cart was moved',
        )

    def test_refuses_a_cart_that_is_not_pending(self):
        """
        Verify that a cart in any status but pending is refused before the gateway is called, and left as it is.

        A processing cart is one a first request has already handed off -- a double click, say -- and a paid one is
        what replaying this URL from the browser history after paying reaches. Either way, a second gateway order would
        be opened for a cart that has one.
        """
        self.add_item()
        self.client.force_login(self.user)
        for cart_status in (
            Cart.Status.PROCESSING, Cart.Status.PAYMENT_PENDING, Cart.Status.PAID, Cart.Status.CANCELLED,
        ):
            with self.subTest(cart_status=cart_status):
                Cart.objects.filter(pk=self.cart.pk).update(status=cart_status)

                with patch('test_utils.dummy_processor.DummyProcessor.payment_view') as payment_view:
                    response = self.client.get(self.url)

                self.assertEqual(response.status_code, 400, f'a {cart_status} cart was not refused')
                self.assertTemplateUsed(response, 'zeitlabs_payments/payment_refused.html', 'the refusal is not a page')
                self.assertNotContains(
                    response, f'>{cart_status}', status_code=400, msg_prefix='the page shows the cart\'s status',
                )
                self.assertFalse(payment_view.called, f'the gateway was called for a {cart_status} cart')
                self.cart.refresh_from_db()
                self.assertEqual(self.cart.status, cart_status, f'a {cart_status} cart was moved')

    def test_a_cart_no_longer_pending_leads_back_to_checkout(self):
        """
        Verify that a cart that is no longer pending, and not paid, is refused with a way back to checkout for its item.

        A replay of an address already handed off, or an older tab whose cart a newer checkout cancelled, reaches this.
        Checkout decides from there: it resumes a payment still at the gateway, or starts a new cart.
        """
        self.add_item()
        self.client.force_login(self.user)
        Cart.objects.filter(pk=self.cart.pk).update(status=Cart.Status.CANCELLED)

        response = self.client.get(self.url)

        self.assert_refused(response, 'This payment link is no longer valid.')
        self.assertContains(response, self.checkout_link, status_code=400, msg_prefix='no way back to checkout')

    def test_a_paid_cart_leads_to_its_invoice(self):
        """Verify that a cart already paid is refused as paid, with a way to its invoice rather than to checkout."""
        self.add_item()
        self.client.force_login(self.user)
        Cart.objects.filter(pk=self.cart.pk).update(status=Cart.Status.PAID)
        transaction = Transaction.objects.create(
            cart=self.cart, amount=100, gateway='dummy', gateway_transaction_id='TX-1',
        )
        Invoice.objects.create(
            status=Invoice.InvoiceStatus.PAID, cart=self.cart, invoice_number='INV-1', total=100, gross_total=100,
            currency='SAR', related_transaction=transaction,
        )

        response = self.client.get(self.url)

        self.assert_refused(response, 'This purchase is already paid.')
        self.assertContains(
            response, f'href="{reverse("zeitlabs_payments:invoice", args=["INV-1"])}"', status_code=400,
            msg_prefix='no way to the invoice',
        )
        self.assertNotContains(response, self.checkout_link, status_code=400, msg_prefix='a paid cart offers checkout')

    def test_a_paid_cart_without_an_invoice_leads_to_the_courses(self):
        """Verify that a cart paid but never invoiced is refused as paid, with a way to the courses only."""
        self.add_item()
        self.client.force_login(self.user)
        Cart.objects.filter(pk=self.cart.pk).update(status=Cart.Status.PAID)

        response = self.client.get(self.url)

        self.assert_refused(response, 'This purchase is already paid.')
        self.assertContains(
            response, f'href="{reverse("courses")}"', status_code=400, msg_prefix='no way to the courses',
        )

    def test_failed_hand_off_closes_the_cart(self):
        """
        Verify that a hand-off that raises closes the cart, records the failure, and records no redirect.

        The learner was never sent to the gateway, so the cart cannot be paid and must not look like a payment in
        flight. It is closed rather than reopened, so it is never handed off again; the learner starts a new cart.
        """
        self.client.force_login(self.user)

        with patch(
            'test_utils.dummy_processor.DummyProcessor.payment_view', side_effect=TimeoutError('no answer'),
        ):
            response = self.client.get(self.url)

        self.assertTemplateUsed(response, 'zeitlabs_payments/payment_error.html')
        self.cart.refresh_from_db()
        self.assertEqual(self.cart.status, Cart.Status.CLOSED, 'the cart of a failed hand-off was not closed')
        self.assertEqual(
            list(AuditLog.objects.filter(cart=self.cart).order_by('id').values_list('action', flat=True)),
            [AuditLog.AuditActions.CART_STATUS_UPDATED, AuditLog.AuditActions.PAYMENT_HANDOFF_FAILED],
            'the trail does not show the hand-off claimed and then failed, without a redirect',
        )

    def test_failed_hand_off_on_a_cart_moved_meanwhile_still_reaches_the_error_page(self):
        """
        Verify that a hand-off failing after another request moved the cart leaves the cart as that request left it,
        and still answers with the error page rather than a server error.

        The gateway is called after the cart is claimed, so a request running alongside -- the learner buying the course
        again in another tab, say -- can move the cart before the failure is known. Closing it then is refused.
        """
        self.client.force_login(self.user)

        def move_then_fail(**kwargs):
            Cart.objects.filter(pk=self.cart.pk).update(status=Cart.Status.PAYMENT_PENDING)
            raise TimeoutError('no answer')

        with patch('test_utils.dummy_processor.DummyProcessor.payment_view', side_effect=move_then_fail):
            with self.assertLogs('zeitlabs_payments.views', level='WARNING') as logs:
                response = self.client.get(self.url)

        self.assertTemplateUsed(response, 'zeitlabs_payments/payment_error.html')
        self.assertIn(
            f'Cart {self.cart.id} was not closed after its failed hand-off', '\n'.join(logs.output),
            'the refused close is not logged',
        )
        self.cart.refresh_from_db()
        self.assertEqual(
            self.cart.status, Cart.Status.PAYMENT_PENDING, 'the cart was not left as the other request left it',
        )
        self.assertFalse(
            AuditLog.objects.filter(cart=self.cart, action=AuditLog.AuditActions.PAYMENT_HANDOFF_FAILED).exists(),
            'a close that did not happen is on the trail',
        )

    def test_failed_hand_off_offers_a_new_cart_for_the_same_item(self):
        """
        Verify that the error page after a failed hand-off retries with a new cart for the item, not the closed cart.
        """
        item = CatalogueItem.objects.get(sku='custom-sku-1')
        self.cart.items.create(catalogue_item=item, original_price=item.price, final_price=item.price)
        self.client.force_login(self.user)

        with patch('test_utils.dummy_processor.DummyProcessor.payment_view', side_effect=TimeoutError('no answer')):
            response = self.client.get(self.url)

        self.assertContains(
            response,
            f'href="{reverse("zeitlabs_payments:checkout")}?sku=custom-sku-1"',
            msg_prefix='the error page does not retry with a new cart for the item',
        )
        self.assertContains(response, 'Try Again', msg_prefix='the retry is not offered')


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
class DeclinePageTest(TestCase):
    """The page a declined or cancelled payment ends on."""

    def setUp(self):
        self.user = User.objects.get(id=3)
        item = CatalogueItem.objects.get(sku='custom-sku-1')
        cart = Cart.objects.create(
            user=self.user, status=Cart.Status.CANCELLED, gateway='dummy', gateway_order_id='ORDER-1',
        )
        cart.items.create(catalogue_item=item, original_price=item.price, final_price=item.price)
        self.url = reverse('zeitlabs_payments:payment-decline', args=['ORDER-1'])
        self.retry = f'href="{reverse("zeitlabs_payments:checkout")}?sku=custom-sku-1"'

    def test_a_declined_payment_is_told_so_and_offered_a_new_attempt(self):
        """Verify that a declined payment is worded as declined, with nothing charged, and retried with a new cart."""
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        self.assertContains(response, 'Payment Declined', msg_prefix='the decline is not worded as one')
        self.assertContains(response, 'Nothing was charged', msg_prefix='the learner is not told nothing was charged')
        self.assertContains(response, self.retry, msg_prefix='no new attempt is offered for the item')
        self.assertNotContains(response, 'history.back()', msg_prefix='the retry still goes back to the waiting page')

    def test_a_cancelled_payment_is_not_called_declined(self):
        """Verify that a payment the learner cancelled is worded as cancelled, and offered a new attempt too."""
        self.client.force_login(self.user)

        response = self.client.get(self.url, {'cancelled': '1'})

        self.assertContains(response, 'Payment Cancelled', msg_prefix='the cancellation is not worded as one')
        self.assertNotContains(response, 'Payment Declined', msg_prefix='a cancellation is called a decline')
        self.assertContains(response, self.retry, msg_prefix='no new attempt is offered for the item')

    def test_a_reference_that_is_not_the_learners_offers_no_retry(self):
        """
        Verify that only the learner's own cart is retried from its reference, and anyone else is sent to the courses.

        The reference is in the address, so it can be read or guessed: it must not reveal what another learner bought.
        """
        for who, log_in in (('another learner', lambda: self.client.force_login(User.objects.get(id=4))),
                            ('nobody logged in', self.client.logout)):
            with self.subTest(who=who):
                log_in()

                response = self.client.get(self.url)

                self.assertNotContains(response, 'sku=custom-sku-1', msg_prefix=f'{who} was offered the cart\'s item')
                self.assertContains(
                    response, f'href="{reverse("courses")}"', msg_prefix=f'{who} was not sent to the courses',
                )


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
class ProcessorRoutesTest(TestCase):
    """The routes the plugin mounts every processor's return and status views on."""

    def test_a_slug_naming_no_processor_is_not_found(self):
        """Verify that a return or status route for a slug no processor has answers 404."""
        for name in ('zeitlabs_payments:processor-return', 'zeitlabs_payments:processor-status'):
            with self.subTest(route=name):
                response = self.client.get(reverse(name, args=['no-such-processor']))

                self.assertEqual(response.status_code, 404, f'{name} served a processor that does not exist')

    def test_a_processor_naming_no_view_has_none_mounted(self):
        """Verify that a processor naming no return view has nothing served at its return route."""
        with patch.object(DummyProcessor, 'RETURN_VIEW', None):
            response = self.client.get(reverse('zeitlabs_payments:processor-return', args=['dummy']))

        self.assertEqual(response.status_code, 404, 'a return view was served for a processor naming none')


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
class InFlightPaymentOnBuyingAgainTest(TestCase):
    """Buying a course again, from the checkout page and the cart API, while an earlier payment for it is open."""

    def setUp(self):
        self.user = User.objects.get(id=3)
        self.item = CatalogueItem.objects.get(sku='custom-sku-1')
        self.checkout_url = f"{reverse('zeitlabs_payments:checkout')}?sku={self.item.sku}"
        self.cart_url = reverse('zeitlabs_payments:cart-add')
        self.client.force_login(self.user)

    def inflight_cart(self, status=Cart.Status.PROCESSING, gateway_order_id=None, age=timedelta(0), gateway='dummy'):
        """Return the learner's cart for the course, handed to the given gateway the given time ago."""
        cart = Cart.objects.create(
            user=self.user, status=status, gateway=gateway, gateway_order_id=gateway_order_id,
        )
        cart.items.create(catalogue_item=self.item, original_price=self.item.price, final_price=self.item.price)
        Cart.objects.filter(pk=cart.pk).update(updated_at=now() - age)
        return cart

    def test_checkout_sends_the_learner_back_to_a_payment_that_can_be_resumed(self):
        """Verify that checkout sends the learner to the waiting page of the payment already open, making no cart."""
        self.inflight_cart(gateway_order_id='ORDER-1')

        response = self.client.get(self.checkout_url)

        self.assertRedirects(
            response, f'{DummyProcessor().get_return_url()}?orderId=ORDER-1', fetch_redirect_response=False,
        )
        self.assertEqual(Cart.objects.filter(user=self.user).count(), 1, 'a second cart was made')

    def test_checkout_asks_the_learner_to_wait_for_a_hand_off_in_progress(self):
        """
        Verify that checkout tells the learner a payment is in progress, and roughly when they may try again: the end
        of the grace a hand-off with no order recorded is given, worded in minutes below an hour.
        """
        self.inflight_cart(age=HANDOFF_GRACE_PERIOD - timedelta(minutes=5))

        response = self.client.get(self.checkout_url)

        self.assertTemplateUsed(response, 'zeitlabs_payments/payment_in_progress.html')
        self.assertContains(response, 'try again in about 5 minutes', msg_prefix='the time to wait is not given')

    def test_checkout_asks_the_learner_to_wait_for_a_hand_off_that_recorded_no_gateway(self):
        """
        Verify that checkout tells the learner whose cart was handed off before carts recorded their gateway to wait
        out a day from the hand-off, worded in hours from an hour up, rather than refusing them for good.
        """
        self.inflight_cart(gateway=None, age=UNRECORDED_GATEWAY_GRACE_PERIOD - timedelta(hours=3, minutes=30))

        response = self.client.get(self.checkout_url)

        self.assertTemplateUsed(response, 'zeitlabs_payments/payment_in_progress.html')
        self.assertContains(response, 'try again in about 4 hours', msg_prefix='the time to wait is not given')

    def test_checkout_makes_the_cart_once_a_failed_hand_off_is_closed(self):
        """Verify that checkout closes a hand-off with no order past its grace and goes on to the new cart."""
        failed = self.inflight_cart(age=HANDOFF_GRACE_PERIOD + timedelta(minutes=1))

        response = self.client.get(self.checkout_url)

        self.assertTemplateUsed(response, 'zeitlabs_payments/checkout.html')
        self.assertNotEqual(response.context['cart']['id'], failed.id, 'the failed hand-off was offered again')
        failed.refresh_from_db()
        self.assertEqual(failed.status, Cart.Status.CLOSED, 'the failed hand-off was not closed')

    def test_checkout_refuses_while_nothing_can_be_concluded_about_a_payment(self):
        """Verify that checkout refuses a learner whose earlier payment was answered about but cannot be asked again."""
        self.inflight_cart(status=Cart.Status.PAYMENT_PENDING, age=timedelta(hours=25))

        response = self.client.get(self.checkout_url)

        self.assertEqual(response.status_code, 400, 'a payment nothing can be concluded about was not refused')
        self.assertContains(
            response, 'still awaiting confirmation', status_code=400, msg_prefix='the refusal does not say why',
        )

    def test_api_names_the_waiting_page_of_a_payment_that_can_be_resumed(self):
        """Verify that the cart API answers 409 with the waiting page of the payment already open."""
        self.inflight_cart(gateway_order_id='ORDER-1')

        response = self.client.post(self.cart_url, data={'sku': self.item.sku})

        self.assertEqual(
            (response.status_code, response.json()),
            (
                409,
                {
                    'outcome': 'payment_in_progress',
                    'redirect_url': f'{DummyProcessor().get_return_url()}?orderId=ORDER-1',
                },
            ),
            'the in-flight payment was not named with its waiting page',
        )

    def test_api_names_when_to_retry_a_payment_in_progress(self):
        """Verify that the cart API answers 409 with the time after which buying again is allowed."""
        cart = self.inflight_cart(age=timedelta(minutes=1))
        cart.refresh_from_db()

        response = self.client.post(self.cart_url, data={'sku': self.item.sku})

        self.assertEqual(
            (response.status_code, response.json()),
            (409, {
                'outcome': 'payment_in_progress',
                'retry_after': (cart.updated_at + HANDOFF_GRACE_PERIOD).isoformat(),
            }),
            'the payment in progress was not named with the time to retry',
        )

    def test_api_names_no_retry_time_when_it_is_not_known(self):
        """Verify that the cart API answers 409 without a time when the payment's state changed as it was checked."""
        self.inflight_cart(age=timedelta(hours=25))

        with patch.object(Cart, 'change_status', side_effect=CartStatusError('moved by another request')):
            response = self.client.post(self.cart_url, data={'sku': self.item.sku})

        self.assertEqual(
            (response.status_code, response.json()),
            (409, {'outcome': 'payment_in_progress', 'retry_after': None}),
            'a payment moved meanwhile was not reported in progress',
        )

    def test_api_refuses_while_nothing_can_be_concluded_about_a_payment(self):
        """Verify that the cart API refuses, in its usual error shape, a payment nothing can be concluded about."""
        self.inflight_cart(status=Cart.Status.PAYMENT_PENDING, age=timedelta(hours=25))

        response = self.client.post(self.cart_url, data={'sku': self.item.sku})

        self.assertEqual(response.status_code, 400, 'a payment nothing can be concluded about was not refused')
        self.assertEqual(
            response.json()['details'], 'A payment for this course is still awaiting confirmation.',
            'the refusal does not say why',
        )


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
class BundleEveryCourseHeldTest(TestCase):
    """Checkout and the cart API refuse a bundle whose every course the learner already holds."""

    def setUp(self):
        self.user = User.objects.get(id=3)
        self.bundle = CatalogueItem.objects.get(sku='BUNDLE-PRO-CERT')
        self.checkout_url = f"{reverse('zeitlabs_payments:checkout')}?sku={self.bundle.sku}"
        self.cart_url = reverse('zeitlabs_payments:cart-add')
        self.client.force_login(self.user)
        for link in BundleCourseItem.objects.filter(bundle=self.bundle).select_related('course_item'):
            course_mode = CourseMode.objects.get(sku=link.course_item.sku)
            CourseEnrollment.objects.create(
                user=self.user, course=course_mode.course, mode=course_mode.mode_slug, is_active=True,
            )

    def test_checkout_refuses_the_bundle_and_says_every_course_is_held(self):
        """Verify that checkout refuses the bundle, saying the learner already has all its courses."""
        response = self.client.get(self.checkout_url)

        self.assertEqual(response.status_code, 400, 'the bundle whose every course is held was not refused')
        self.assertContains(response, 'You already have all the courses in this bundle.', status_code=400)
        self.assertFalse(
            Cart.objects.filter(user=self.user).exists(), 'a cart was made for a bundle the learner already holds',
        )

    def test_api_refuses_the_bundle_and_says_every_course_is_held(self):
        """Verify that the cart API refuses the bundle the same way, in its usual error shape."""
        response = self.client.post(self.cart_url, data={'sku': self.bundle.sku})

        self.assertEqual(response.status_code, 400, 'the bundle whose every course is held was not refused')
        self.assertIn(self.bundle.sku, response.json()['details'], 'the API detail does not name the bundle')
        self.assertIn('already holds all', response.json()['details'])
        self.assertFalse(
            Cart.objects.filter(user=self.user).exists(), 'a cart was made for a bundle the learner already holds',
        )


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
class EmptyBundleOnCheckoutTest(TestCase):
    """Checkout of a bundle with no courses linked: its setup error comes before the learner's carts."""

    def setUp(self):
        self.user = User.objects.get(id=3)
        self.bundle = CatalogueItem.objects.get(sku='BUNDLE-EMPTY')
        self.checkout_url = f"{reverse('zeitlabs_payments:checkout')}?sku={self.bundle.sku}"
        self.cart_url = reverse('zeitlabs_payments:cart-add')
        self.client.force_login(self.user)

    def inflight_cart(self, gateway_order_id='ORDER-EMPTY', age=timedelta(0)):
        """Return the learner's cart for the empty bundle, handed to the dummy gateway the given time ago."""
        cart = Cart.objects.create(
            user=self.user, status=Cart.Status.PROCESSING, gateway='dummy', gateway_order_id=gateway_order_id,
        )
        cart.items.create(catalogue_item=self.bundle, original_price=self.bundle.price, final_price=self.bundle.price)
        Cart.objects.filter(pk=cart.pk).update(updated_at=now() - age)
        cart.refresh_from_db()
        return cart

    def test_checkout_reports_an_empty_bundle_instead_of_resuming_its_payment(self):
        """
        Verify that checkout refuses an empty bundle with its setup error, leaving the resumable payment untouched.

        Its order is known and the dummy processor has a waiting page, so the cart checks would have sent the learner
        back to it; the bundle's configuration is reported instead.
        """
        inflight = self.inflight_cart()

        response = self.client.get(self.checkout_url)

        self.assertEqual(response.status_code, 400, 'the empty bundle was not refused')
        self.assertContains(response, 'This bundle is not available to buy right now.', status_code=400)
        inflight.refresh_from_db()
        self.assertEqual(inflight.status, Cart.Status.PROCESSING, 'the payment in flight was resolved')

    def test_checkout_reports_an_empty_bundle_instead_of_waiting_on_its_payment(self):
        """
        Verify that checkout refuses an empty bundle with its setup error, instead of asking the learner to wait.

        Its order is not recorded and its session has not lapsed, so the cart checks would have said a payment is in
        progress; the bundle's configuration is reported instead.
        """
        inflight = self.inflight_cart(gateway_order_id=None)

        response = self.client.get(self.checkout_url)

        self.assertEqual(response.status_code, 400, 'the empty bundle was not refused')
        self.assertContains(response, 'This bundle is not available to buy right now.', status_code=400)
        inflight.refresh_from_db()
        self.assertEqual(inflight.status, Cart.Status.PROCESSING, 'the payment in flight was resolved')

    def test_checkout_reports_an_empty_bundle_instead_of_expiring_its_payment(self):
        """
        Verify that checkout refuses an empty bundle even when its payment is past the session, leaving it processing.

        An abandoned payment would have been expired and a new cart made; the bundle's configuration is reported
        instead, and nothing of the learner's is moved or made.
        """
        inflight = self.inflight_cart(gateway_order_id=None, age=HANDOFF_GRACE_PERIOD * 2)

        response = self.client.get(self.checkout_url)

        self.assertEqual(response.status_code, 400, 'the empty bundle was not refused')
        self.assertContains(response, 'This bundle is not available to buy right now.', status_code=400)
        inflight.refresh_from_db()
        self.assertEqual(inflight.status, Cart.Status.PROCESSING, 'the abandoned payment was expired')
        self.assertEqual(
            Cart.objects.filter(user=self.user).count(), 1, 'a new cart was made for a bundle that cannot be bought',
        )

    def test_api_reports_an_empty_bundle_instead_of_resuming_its_payment(self):
        """Verify that the cart API refuses an empty bundle with its setup error, leaving its payment untouched."""
        inflight = self.inflight_cart()

        response = self.client.post(self.cart_url, data={'sku': self.bundle.sku})

        self.assertEqual(response.status_code, 400, 'the empty bundle was not refused')
        self.assertIn(self.bundle.sku, response.json()['details'], 'the API detail does not name the bundle')
        inflight.refresh_from_db()
        self.assertEqual(inflight.status, Cart.Status.PROCESSING, 'the payment in flight was resolved')
        self.assertEqual(
            Cart.objects.filter(user=self.user).count(), 1, 'a cart was made for a bundle that cannot be bought',
        )


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
class CheckoutViewTests(TestCase):
    """Checkout View Test."""
    VIEW_NAME = 'zeitlabs_payments:checkout'

    def setUp(self):
        self.user = User.objects.get(id=3)
        self.url = reverse(self.VIEW_NAME)

    def test_redirects_if_not_logged_in(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    @patch('zeitlabs_payments.views.available_processors', return_value=[])
    def test_checkout_view_context_without_cart(self, mock_available_processors):  # pylint: disable=unused-argument
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['cart'])
        self.assertEqual(response.context['methods'], [])

    def test_checkout_view_without_sku(self):
        user_last_cart = Cart.objects.create(user=self.user, status=Cart.Status.PENDING)
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['cart']['id'], user_last_cart.id)
        self.assertEqual(len(response.context['methods']), 1)

    def test_checkout_view_with_sku_success(self):
        user_existing_cart = Cart.objects.create(user=self.user, status=Cart.Status.PENDING)
        self.client.force_login(self.user)
        test_sku = 'custom-sku-1'
        response = self.client.get(f'{self.url}?sku={test_sku}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['cart']['items']), 1)
        self.assertEqual(response.context['cart']['items'][0]['sku'], test_sku)
        self.assertEqual(len(response.context['methods']), 1)
        user_existing_cart.refresh_from_db()
        self.assertEqual(user_existing_cart.status, Cart.Status.CANCELLED, 'Old pending cart should be cancelled.')

    def test_checkout_view_with_sku_for_invalid_sku(self):
        """Verify that an unknown SKU is refused with one plain sentence, with the SKU kept in the log."""
        self.client.force_login(self.user)
        test_sku = 'does-not-exist'
        with self.assertLogs('zeitlabs_payments.views', level='WARNING') as logs:
            response = self.client.get(f'{self.url}?sku={test_sku}')
        self.assertTemplateUsed(response, 'zeitlabs_payments/invalid_cart.html')
        self.assertEqual(response.context['error_message'], 'This item is not available.')
        self.assertNotIn(test_sku, response.context['error_message'], 'the learner sentence names the SKU')
        self.assertIn(test_sku, '\n'.join(logs.output), 'the refused SKU is not logged')
        self.assertEqual(response.status_code, 404)

    @patch.dict(
        'zeitlabs_payments.views.CART_HANDLER', {}, clear=True
    )
    def test_checkout_view_with_sku_for_item_sku_with_unsuppported_type(self):
        """Verify that an unsupported item is refused with one plain sentence, its type kept in the log."""
        self.client.force_login(self.user)
        with self.assertLogs('zeitlabs_payments.views', level='WARNING') as logs:
            response = self.client.get(f'{self.url}?sku=custom-sku-1')
        self.assertTemplateUsed(response, 'zeitlabs_payments/invalid_cart.html')
        self.assertEqual(response.context['error_message'], 'This item cannot be bought right now.')
        self.assertIn('paid_course', '\n'.join(logs.output), 'the refused type is not logged')
        self.assertEqual(response.status_code, 400)

    @patch(
        'zeitlabs_payments.helpers.CourseEnrollment.is_enrolled'
    )
    def test_checkout_view_with_sku_for_course_item_sku_already_enrolled(self, mock_enrolled):
        """Verify that an already-enrolled learner sees one plain sentence, with the detail kept in the log."""
        mock_enrolled.return_value = True
        self.client.force_login(self.user)
        with self.assertLogs('zeitlabs_payments.cart_handler', level='WARNING') as logs:
            response = self.client.get(f'{self.url}?sku=custom-sku-1')
        self.assertTemplateUsed(response, 'zeitlabs_payments/invalid_cart.html')
        self.assertEqual(response.context['error_message'], 'You are already enrolled in this course.')
        self.assertNotIn('user3', response.context['error_message'], 'the learner sentence names the user')
        self.assertNotIn('custom-sku-1', response.context['error_message'], 'the learner sentence names the SKU')
        self.assertEqual(response.status_code, 400)
        logged = '\n'.join(logs.output)
        self.assertIn('Checkout refused for user', logged)
        self.assertIn('custom-sku-1', logged, 'the refused SKU is not logged')

    def test_checkout_view_with_multiple_skus_renders_invalid_cart_page(self):
        """Verify that a multi-SKU checkout URL renders the invalid-cart page with status 400.

        Both SKUs must appear in the error log for audit visibility, and no cart may be created.
        The single-SKU path is left untouched by this test.
        """
        self.client.force_login(self.user)
        with self.assertLogs('zeitlabs_payments.views', level='ERROR') as cm:
            response = self.client.get(f'{self.url}?sku=custom-sku-1&sku=custom-sku-2')
        self.assertTemplateUsed(response, 'zeitlabs_payments/invalid_cart.html')
        self.assertEqual(response.status_code, 400)
        self.assertIn('one item per purchase', response.context['error_message'])
        log_text = '\n'.join(cm.output)
        assert 'custom-sku-1' in log_text, 'Multi-SKU refusal must log the first SKU.'
        assert 'custom-sku-2' in log_text, 'Multi-SKU refusal must log the second SKU.'
        self.assertFalse(
            Cart.objects.filter(user=self.user).exists(),
            'A refused multi-SKU request must not create a cart.',
        )

    def test_checkout_view_with_single_sku_in_list_still_succeeds(self):
        """Verify that the single-element list path is unchanged after the multi-SKU refusal."""
        self.client.force_login(self.user)
        response = self.client.get(f'{self.url}?sku=custom-sku-1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['cart']['items']), 1)
        self.assertEqual(response.context['cart']['items'][0]['sku'], 'custom-sku-1')

    def test_checkout_renders_money_through_format_price(self):
        """Verify checkout HTML uses currency-exponent precision, not the money-column storage scale."""
        self.client.force_login(self.user)
        catalogue_item = CatalogueItem.objects.get(sku='custom-sku-1')
        cart = Cart.objects.create(user=self.user, status=Cart.Status.PENDING)
        CartItem.objects.create(
            cart=cart,
            catalogue_item=catalogue_item,
            original_price=Decimal('45.000'),
            final_price=Decimal('45.000'),
        )

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '45.00')
        self.assertNotContains(response, '45.000')


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
class InvoiceViewTest(BaseTestViewMixin):
    """Tests for InvoiceView"""

    VIEW_NAME = 'zeitlabs_payments:invoice'

    def setUp(self):
        """Ask for invoices on the site they are issued on here, unless a test says otherwise."""
        super().setUp()
        self.client.defaults['HTTP_HOST'] = 'learn.example.com'

    def test_get_success(self):
        """
        Verify that the receipt carries the invoice, the gateway that took the payment, and the learner and
        organization info stored on it.
        """
        user = User.objects.get(id=3)
        self.login_user(user)
        cart = Cart.objects.create(user=user, status=Cart.Status.PAID, payment_origin=PAYMENT_ORIGIN)
        transaction = Transaction.objects.create(
            cart=cart, amount=100, gateway='payfort', gateway_transaction_id='TX-111'
        )
        invoice = Invoice.objects.create(
            status=Invoice.InvoiceStatus.PAID,
            cart=cart,
            invoice_number='TEST-111111',
            total=100,
            gross_total=100,
            currency='SAR',
            related_transaction=transaction,
            learner_info=STORED_LEARNER_INFO,
            organization_info=STORED_ORGANIZATION_INFO,
        )
        self.url_args = [invoice.invoice_number]
        response = self.client.get(self.url)

        assert response.status_code == 200
        assert response.context['invoice'].invoice_number == 'TEST-111111'
        assert response.context['payment_method'] == 'payfort', \
            'The receipt names the gateway that took the payment'
        assert response.context['learner_info'] == STORED_LEARNER_INFO, 'The receipt names someone else'
        assert response.context['organization_info'] == STORED_ORGANIZATION_INFO, 'The receipt names another issuer'
        assert response.context['currency'] == invoice.currency, \
            'The receipt shows what the invoice recorded, not what the deployment is configured for now'

    def test_receipt_formats_money_and_omits_vat_amount_ui(self):
        """
        Verify the receipt shows currency-exponent amounts and no VAT amount UI.

        Storage may hold three decimals after the money-column widening; the receipt must still print
        two for SAR. Tax amount fields may be non-zero on the model, but v1.0 does not name them on
        the page — only seller identity (tax number) stays when configured.
        """
        user = User.objects.get(id=3)
        self.login_user(user)
        catalogue_item = CatalogueItem.objects.get(sku='custom-sku-1')
        cart = Cart.objects.create(user=user, status=Cart.Status.PAID, payment_origin=PAYMENT_ORIGIN)
        cart_item = CartItem.objects.create(
            cart=cart,
            catalogue_item=catalogue_item,
            original_price=Decimal('99.000'),
            final_price=Decimal('99.000'),
        )
        transaction = Transaction.objects.create(
            cart=cart, amount=Decimal('99.000'), gateway='payfort', gateway_transaction_id='TX-FORMAT'
        )
        invoice = Invoice.objects.create(
            status=Invoice.InvoiceStatus.PAID,
            cart=cart,
            invoice_number='TEST-FORMAT-99',
            total=Decimal('99.000'),
            gross_total=Decimal('99.000'),
            tax_total=Decimal('5.00'),
            currency='SAR',
            related_transaction=transaction,
        )
        InvoiceItem.objects.create(
            invoice=invoice,
            cart_item=cart_item,
            original_price=Decimal('99.000'),
            price=Decimal('99.000'),
            tax_amount=Decimal('5.00'),
        )
        self.url_args = [invoice.invoice_number]

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '99.00')
        self.assertNotContains(response, '99.000')
        self.assertNotContains(response, '>VAT<')

    def _issue_invoice(self, invoice_number, payment_origin=PAYMENT_ORIGIN, **extra):
        """Log a learner in and issue them one paid invoice, on a cart paid on the given origin, returning it."""
        user = User.objects.get(id=3)
        self.login_user(user)
        cart = Cart.objects.create(user=user, status=Cart.Status.PAID, payment_origin=payment_origin)
        transaction = Transaction.objects.create(
            cart=cart, amount=100, gateway='payfort', gateway_transaction_id=f'TX-{invoice_number}',
        )
        return Invoice.objects.create(
            status=Invoice.InvoiceStatus.PAID,
            cart=cart,
            invoice_number=invoice_number,
            total=100,
            gross_total=100,
            currency='SAR',
            related_transaction=transaction,
            **extra,
        )

    def test_receipt_prints_the_parties_stored_at_issue(self):
        """
        Verify that the receipt prints the learner and organization info stored on the invoice: the learner's name,
        and the organization's name, VAT number and CR number.
        """
        invoice = self._issue_invoice(
            'TEST-555551', learner_info=STORED_LEARNER_INFO, organization_info=STORED_ORGANIZATION_INFO,
        )
        self.url_args = [invoice.invoice_number]

        response = self.client.get(self.url)

        for printed in (
            'Layla Haddad', 'The Key Institute LLC', 'VAT No.: 445566', 'CR No.: 7788',
        ):
            self.assertContains(response, printed, msg_prefix=f'the receipt does not print {printed!r}')

    def test_receipt_leaves_out_parties_it_has_nothing_stored_for(self):
        """
        Verify that an invoice with no info stored -- issued before it was, or whose function failed -- prints no
        learner name and no organization block, rather than empty ones.
        """
        invoice = self._issue_invoice('TEST-555552')
        self.url_args = [invoice.invoice_number]

        response = self.client.get(self.url)

        self.assertNotContains(response, 'Customer Name', msg_prefix='an empty learner name was printed')
        self.assertNotContains(
            response, '<div class="receipt-company-info">', msg_prefix='an empty organization block was printed',
        )

    def test_receipt_shows_live_info_while_the_switch_is_on(self):
        """
        Verify that while the switch is on, the receipt shows what the info functions answer now and ignores what the
        invoice stored, so invoices issued before the info was stored still show their parties.
        """
        invoice = self._issue_invoice(
            'TEST-555553', learner_info=STORED_LEARNER_INFO, organization_info=STORED_ORGANIZATION_INFO,
        )
        invoice.cart.user.first_name, invoice.cart.user.last_name = 'Omar', 'Saleh'
        invoice.cart.user.save()
        self.url_args = [invoice.invoice_number]

        with override_switch('zeitlabs_payments.invoice.show_live_info', active=True):
            response = self.client.get(self.url)

        self.assertEqual(
            (response.context['learner_info'], response.context['organization_info']),
            (
                {'name': 'Omar Saleh'},
                {'name': 'test_org', 'vat_no': '112233'},
            ),
            'the receipt does not show the live info',
        )

    def test_invoice_asked_for_on_another_site_is_sent_to_its_own(self):
        """
        Verify that an invoice asked for on another site than the one it was issued on is redirected to the same
        address on its own site, query included, rather than shown with the other site's organization.
        """
        invoice = self._issue_invoice('TEST-555554')
        self.url_args = [invoice.invoice_number]

        response = self.client.get(f'{self.url}?lang=ar', HTTP_HOST='other.example.com')

        self.assertRedirects(
            response, f'https://learn.example.com{self.url}?lang=ar', fetch_redirect_response=False,
            msg_prefix='the invoice was not sent to the site it was issued on',
        )

    def test_invoice_is_shown_on_its_own_host_however_its_origin_spells_it(self):
        """
        Verify that an invoice whose origin spells its host in capitals, or with the default port, is shown on that host
        rather than redirected to itself.

        The browser asks for the host in lower case and without the default port, so comparing the spellings as written
        would send every request for the invoice back to the address it came from.
        """
        for invoice_number, origin in (
            ('TEST-555561', 'https://Learn.Example.com'),
            ('TEST-555562', 'https://learn.example.com:443'),
        ):
            with self.subTest(origin=origin):
                invoice = self._issue_invoice(invoice_number, payment_origin=origin)
                self.url_args = [invoice.invoice_number]

                response = self.client.get(self.url)

                self.assertEqual(response.status_code, 200, f'the invoice issued on {origin} was not shown on its host')

    def test_invoice_asked_for_on_another_port_is_sent_to_its_own(self):
        """
        Verify that an invoice issued on a port other than the default is redirected when asked for on another port of
        the same host, since the two are different sites.
        """
        invoice = self._issue_invoice('TEST-555563', payment_origin='http://learn.example.com:18000')
        self.url_args = [invoice.invoice_number]

        response = self.client.get(self.url, HTTP_HOST='learn.example.com:8000')

        self.assertRedirects(
            response, f'http://learn.example.com:18000{self.url}', fetch_redirect_response=False,
            msg_prefix='the invoice was not sent to the port it was issued on',
        )

    def test_invoice_the_caller_may_not_see_is_not_sent_to_its_site(self):
        """
        Verify that someone who may not see an invoice gets the private page on any site, never a redirect, which
        would name the site the invoice was issued on and so confirm that its number is live.
        """
        invoice = self._issue_invoice('TEST-555555')
        self.client.force_login(User.objects.get(id=self.learner2_id))
        self.url_args = [invoice.invoice_number]

        response = self.client.get(self.url, HTTP_HOST='other.example.com')

        self.assertEqual(response.status_code, 410, 'another learner was not answered with the private page')
        self.assertTemplateUsed(response, 'zeitlabs_payments/invoice_private.html')

    def test_invoice_from_before_origins_were_recorded_is_shown_on_any_site(self):
        """Verify that an invoice whose cart has no origin, and so names no site, is shown where it is asked for."""
        invoice = self._issue_invoice('TEST-555556', payment_origin=None)
        self.url_args = [invoice.invoice_number]

        response = self.client.get(self.url, HTTP_HOST='other.example.com')

        self.assertEqual(response.status_code, 200, 'an invoice with no recorded site was not shown')

    def test_get_carries_no_receipt_url_while_the_cart_has_no_origin(self):
        """
        Verify that a receipt whose cart has no origin opens, but reaches the template with no receipt URL, so the QR
        tag renders nothing.

        A QR addressed on a guessed host would send a learner somewhere unrelated to their payment. Losing the QR is
        the whole cost of an origin nobody recorded, and it is not allowed to cost the page as well.
        """
        invoice = self._issue_invoice('TEST-111113', payment_origin=None)
        self.url_args = [invoice.invoice_number]
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(
            response.context['invoice'].receipt_url,
            'An invoice whose cart has no origin must reach the template with no URL to build a QR code from',
        )
        self.assertEqual(
            response.context['invoice'].invoice_number, 'TEST-111113',
            'The receipt must still render its invoice, only without the QR code',
        )

    def test_get_carries_a_receipt_url_on_the_recorded_origin_to_the_template(self):
        """
        Verify that the receipt URL reaching the template is built on the origin the invoice's cart was paid on.

        Rebuilding it from the rendering request would print a different address than the paper copy issued
        on the paying site.
        """
        invoice = self._issue_invoice('TEST-111112', payment_origin='https://paid-here.example.com')
        self.url_args = [invoice.invoice_number]

        response = self.client.get(self.url, HTTP_HOST='paid-here.example.com')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['invoice'].receipt_url,
            f'https://paid-here.example.com/payment/v2/invoice/{invoice.public_token}/',
            'The receipt must be addressed on the origin the cart was paid on',
        )

    def test_unauthorized(self):
        """
        Verify that an anonymous caller is answered with the explanation page, not redirected to a login form.

        This address is what older printed QR codes encode, and whoever scans one has no account to sign in
        with. A redirect would strand them on a form; the page tells them what to ask the owner for instead.
        """
        self.url_args = ['does-not-matter']

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 410, 'An anonymous caller must be answered, not redirected')
        self.assertTemplateUsed(response, 'zeitlabs_payments/invoice_private.html')

    def test_a_forbidden_invoice_is_indistinguishable_from_a_missing_one(self):
        """
        Verify that an invoice the caller may not see answers exactly as one that does not exist.

        Invoice numbers run as a sequence, so a reply that told the two apart could be walked through the
        series to learn which numbers are live and how many customers a deployment has.
        """
        invoice = self._issue_invoice('TEST-111114')
        self.login_user(User.objects.get(id=2))

        self.url_args = [invoice.invoice_number]
        forbidden = self.client.get(self.url)
        self.url_args = ['TEST-999999']
        missing = self.client.get(self.url)

        self.assertEqual(
            forbidden.status_code, missing.status_code,
            'An invoice withheld and an invoice absent must answer with the same status',
        )
        self.assertEqual(
            forbidden.content, missing.content,
            'A reply that differed would confirm which invoice numbers are live',
        )

    def test_invoices_access(self):
        """
        Ensure invoice access rules:
        - Admins can view all invoices
        - Normal users can only view their own
        """
        admin_user = User.objects.get(id=1)
        admin_user_cart = Cart.objects.create(user=admin_user, status=Cart.Status.PAID, payment_origin=PAYMENT_ORIGIN)
        admin_user_invoice = Invoice.objects.create(
            status=Invoice.InvoiceStatus.PAID,
            cart=admin_user_cart,
            invoice_number='TEST-111111',
            total=100,
            gross_total=100,
            currency='SAR',
            related_transaction=Transaction.objects.create(
                cart=admin_user_cart, amount=100, gateway='dummy', gateway_transaction_id='TX-1'
            )
        )

        normal_user1 = User.objects.get(id=2)
        normal_user1_cart = Cart.objects.create(
            user=normal_user1, status=Cart.Status.PAID, payment_origin=PAYMENT_ORIGIN,
        )
        normal_user1_invoice = Invoice.objects.create(
            status=Invoice.InvoiceStatus.PAID,
            cart=normal_user1_cart,
            invoice_number='TEST-22222',
            total=100,
            gross_total=100,
            currency='SAR',
            related_transaction=Transaction.objects.create(
                cart=normal_user1_cart, amount=100, gateway='dummy', gateway_transaction_id='TX-2'
            )
        )

        normal_user2 = User.objects.get(id=3)
        normal_user2_cart = Cart.objects.create(
            user=normal_user2, status=Cart.Status.PAID, payment_origin=PAYMENT_ORIGIN,
        )
        normal_user2_invoice = Invoice.objects.create(
            status=Invoice.InvoiceStatus.PAID,
            cart=normal_user2_cart,
            invoice_number='TEST-33333',
            total=200,
            gross_total=200,
            currency='SAR',
            related_transaction=Transaction.objects.create(
                cart=normal_user2_cart, amount=200, gateway='dummy', gateway_transaction_id='TX-3'
            )
        )

        self.url_args = [normal_user2_invoice.invoice_number]
        self.login_user(admin_user)
        response = self.client.get(self.url)
        assert response.status_code == 200, "Admin should be able to access other users' invoices"
        assert response.context['invoice'].invoice_number == normal_user2_invoice.invoice_number
        self.url_args = [admin_user_invoice.invoice_number]
        response = self.client.get(self.url)
        assert response.status_code == 200, 'Admin should be able to access their own invoice'
        assert response.context['invoice'].invoice_number == admin_user_invoice.invoice_number

        self.url_args = [normal_user1_invoice.invoice_number]
        self.login_user(normal_user1)
        response = self.client.get(self.url)
        assert response.status_code == 200, 'Normal user should be able to access their own invoice'
        assert response.context['invoice'].invoice_number == normal_user1_invoice.invoice_number
        self.url_args = [normal_user2_invoice.invoice_number]
        response = self.client.get(self.url)
        assert response.status_code == 410, "Normal user should not be able to access another user's invoice"

    @override_settings(ZEITLABS_PAYMENTS_SETTINGS={'valid_currency': 'IQD'})
    def test_get_uses_the_invoice_currency_not_the_configured_one(self):
        """
        Verify that a receipt keeps the currency it was issued with after the deployment changes currency.

        An issued invoice is a record of what was charged and is never recomputed from current configuration. A
        deployment that switches currency holds two in its history, and re-rendering an old receipt in the new one
        would restate a document already in a learner's hands -- silently, since every amount stays as it was.
        """
        user = User.objects.get(id=3)
        self.login_user(user)
        cart = Cart.objects.create(user=user, status=Cart.Status.PAID, payment_origin=PAYMENT_ORIGIN)
        invoice = Invoice.objects.create(
            status=Invoice.InvoiceStatus.PAID,
            cart=cart,
            invoice_number='TEST-333333',
            total=100,
            gross_total=100,
            currency='SAR',
            related_transaction=Transaction.objects.create(
                cart=cart, amount=100, gateway='dummy', gateway_transaction_id='TX-1'
            ),
        )
        self.url_args = [invoice.invoice_number]
        response = self.client.get(self.url)

        assert response.status_code == 200
        assert response.context['currency'] == 'SAR', \
            'The receipt must show the currency charged, not the one the deployment moved to'


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
class PublicInvoiceViewTest(BaseTestViewMixin):
    """Tests for PublicInvoiceView"""

    VIEW_NAME = 'zeitlabs_payments:public-invoice'

    def setUp(self):
        """Ask for receipts on the site they are issued on here, unless a test says otherwise."""
        super().setUp()
        self.client.defaults['HTTP_HOST'] = 'learn.example.com'

    def test_receipt_asked_for_on_another_site_is_sent_to_its_own(self):
        """
        Verify that a receipt opened by its token on another site than the one it was issued on is redirected to the
        same address on its own site.
        """
        invoice = self._issue_invoice('TEST-444446')
        self.url_args = [invoice.public_token]

        response = self.client.get(self.url, HTTP_HOST='other.example.com')

        self.assertRedirects(
            response, f'https://learn.example.com{self.url}', fetch_redirect_response=False,
            msg_prefix='the receipt was not sent to the site it was issued on',
        )

    def _issue_invoice(self, invoice_number):
        """Issue one paid invoice to a learner, returning it."""
        user = User.objects.get(id=self.learner1_id)
        cart = Cart.objects.create(user=user, status=Cart.Status.PAID, payment_origin=PAYMENT_ORIGIN)
        transaction = Transaction.objects.create(
            cart=cart, amount=100, gateway='payfort', gateway_transaction_id=f'TX-{invoice_number}',
        )
        return Invoice.objects.create(
            status=Invoice.InvoiceStatus.PAID,
            cart=cart,
            invoice_number=invoice_number,
            total=100,
            gross_total=100,
            currency='SAR',
            related_transaction=transaction,
        )

    def test_get_renders_the_invoice_without_an_account(self):
        """
        Verify that the invoice opens for a caller who is not logged in at all.

        Holding the token is the whole permission. Whoever was handed the printed copy checks it here, and an
        auditor has no account on the deployment that issued it.
        """
        invoice = self._issue_invoice('TEST-444444')
        self.url_args = [invoice.public_token]

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200, 'A token holder must reach the invoice without signing in')
        self.assertEqual(
            response.context['invoice'].invoice_number, 'TEST-444444',
            'The public page must render the invoice its token addresses',
        )

    def test_get_carries_the_same_details_as_the_owner_sees(self):
        """
        Verify that the public page is built from the same details as the owner's own view.

        A public copy that differed from the paper being checked against it would defeat the verification it
        exists for.
        """
        invoice = self._issue_invoice('TEST-444445')
        Invoice.objects.filter(pk=invoice.pk).update(
            learner_info=STORED_LEARNER_INFO, organization_info=STORED_ORGANIZATION_INFO,
        )
        self.url_args = [invoice.public_token]

        response = self.client.get(self.url)

        self.assertEqual(
            response.context['payment_method'], 'payfort',
            'The public page names the gateway that took the payment',
        )
        self.assertEqual(
            (response.context['learner_info'], response.context['organization_info']),
            (STORED_LEARNER_INFO, STORED_ORGANIZATION_INFO),
            'The public page names other parties than the invoice stored',
        )
        self.assertEqual(
            response.context['currency'], invoice.currency,
            'The public page shows what the invoice recorded',
        )

    def test_get_forbids_sending_the_token_in_a_referrer(self):
        """
        Verify that the response forbids sending a referrer.

        The token addresses the invoice, so letting it ride a Referer header out to whatever this page links
        to would hand the capability to whoever received the request.
        """
        invoice = self._issue_invoice('TEST-444446')
        self.url_args = [invoice.public_token]

        response = self.client.get(self.url)

        self.assertEqual(
            response['Referrer-Policy'], 'no-referrer',
            'The token must not leave the page through a referrer header',
        )

    def test_get_malformed_token_is_not_found(self):
        """
        Verify that a token outside the canonical UUID shape is a 404 rather than a server error.

        Such a token must not reach the lookup at all: the column parses its values as UUIDs and raises on
        anything else, which surfaces as a 500. These URLs are built by hand because the route cannot produce
        them -- refusing to reverse them is the same guarantee seen from the other side.
        """
        malformed = {
            'a' * 36: 'hex characters with no hyphens at all',
            '-' * 36: 'hyphens with no hex at all',
            'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa': 'one character short of a token',
        }

        for token, usecase in malformed.items():
            with self.subTest(usecase=usecase):
                response = self.client.get(f'/payment/v2/invoice/{token}/')
                self.assertEqual(response.status_code, 404, f'A malformed token must not resolve: {usecase}')

    def test_get_refuses_a_non_canonical_spelling_of_a_real_token(self):
        """
        Verify that an invoice answers only at its token's canonical spelling.

        ``uuid.UUID()`` drops hyphens wherever they fall, so many spellings parse to one token. Letting them all
        resolve would put a single invoice behind a family of URLs, multiplying what reaches logs and referrers.
        """
        invoice = self._issue_invoice('TEST-444447')
        aliased = str(invoice.public_token).replace('-', '') + '----'

        response = self.client.get(f'/payment/v2/invoice/{aliased}/')

        self.assertEqual(
            response.status_code, 404,
            'Only the canonical spelling of a token may reach its invoice',
        )

    def test_get_unknown_token_is_not_found(self):
        """
        Verify that a token addressing no invoice is an ordinary 404.

        A token is unguessable, so admitting that one matches nothing gives away nothing worth withholding --
        unlike the numbered address, whose series can be walked.
        """
        self.url_args = [uuid.uuid4()]

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 404, 'A token matching no invoice must not resolve')


@pytest.mark.usefixtures('base_data')
class PaymentErrorViewTest(BaseTestViewMixin):
    """Tests for PaymentErrorView"""
    VIEW_NAME = 'zeitlabs_payments:payment-error'

    def test_get_success(self):
        """
        Verify that the view renders correctly and includes merchant_reference in the context.
        """
        merchant_reference = 'ORDER-98765'
        self.url_args = [merchant_reference]
        response = self.client.get(self.url)

        assert response.status_code == 200
        assert 'merchant_reference' in response.context
        assert response.context['merchant_reference'] == merchant_reference


@pytest.mark.usefixtures('base_data')
class PaymentDeclineViewTest(BaseTestViewMixin):
    """Tests for PaymentDeclineView"""
    VIEW_NAME = 'zeitlabs_payments:payment-decline'

    def test_get_decline(self):
        """
        Verify that the view renders correctly and includes merchant_reference in the context.
        """
        merchant_reference = 'ORDER-98765'
        self.url_args = [merchant_reference]
        response = self.client.get(self.url)

        assert response.status_code == 200
        assert 'merchant_reference' in response.context
        assert response.context['merchant_reference'] == merchant_reference
        assert 'status-reference' in response.content.decode(), \
            'The shared layout must carry the merchant reference'


@pytest.mark.usefixtures('base_data')
class PaymentReconciliationViewTest(BaseTestViewMixin):
    """Tests for PaymentReconciliationView"""
    VIEW_NAME = 'zeitlabs_payments:payment-reconciliation'

    def test_get_reconciliation_carries_the_reference_and_no_retry(self):
        """
        Verify the reconciliation page carries the reference and offers no retry action.
        """
        merchant_reference = 'ORDER-98765'
        self.url_args = [merchant_reference]
        response = self.client.get(self.url)

        assert response.status_code == 200
        assert response.context['merchant_reference'] == merchant_reference
        markup = response.content.decode()
        assert 'ORDER-98765' in markup, 'The learner must be able to quote the reference'
        assert 'Try Again' not in markup, 'The reconciliation page must not offer a retry action'

    def test_a_charged_payment_is_told_it_was_received(self):
        """
        Verify that, by default, the page tells the learner their payment was received and not to pay again.

        It is reached when the gateway took the money but the enrolment could not be completed.
        """
        self.url_args = ['ORDER-98765']

        response = self.client.get(self.url)

        self.assertContains(response, 'Payment Received', msg_prefix='a charged learner is not told it was received')
        self.assertContains(response, 'Please do not pay again', msg_prefix='a charged learner is invited to pay again')
        self.assertNotContains(response, 'Not Yet Confirmed', msg_prefix='a charged learner is told it is unconfirmed')

    def test_an_unconfirmed_payment_is_not_told_it_was_received(self):
        """
        Verify that a payment not confirmed yet is not described as received, and the learner is told not to pay again.

        It is reached when the waiting page stops polling a payment still pending, which may yet fail.
        """
        self.url_args = ['ORDER-98765']

        response = self.client.get(self.url, {'unconfirmed': '1'})

        self.assertContains(response, 'Payment Not Yet Confirmed', msg_prefix='the unconfirmed wording is not shown')
        self.assertContains(response, 'Please do not pay again', msg_prefix='the learner is invited to pay again')
        self.assertNotContains(response, 'Payment Received', msg_prefix='an unconfirmed payment is called received')
