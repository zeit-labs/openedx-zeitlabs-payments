"""Test views for the zeitlabs_payments app"""

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.test import RequestFactory, TestCase
from django.urls import reverse
from rest_framework import status as http_status
from rest_framework.test import APITestCase

from zeitlabs_payments.helpers import get_currency, get_settings
from zeitlabs_payments.models import Cart, CatalogueItem, Invoice, Transaction
from zeitlabs_payments.providers.registry import PROCESSORS
from zeitlabs_payments.views import InitiatePaymentView

User = get_user_model()


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
            final_price=course_item.price,
        )

        self.login_user(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, http_status.HTTP_200_OK)

        assert response.data['details'] == f'No pending cart found for user: {user}'
        user_cart = Cart.objects.create(user=user, status=Cart.Status.PENDING)
        user_cart.items.create(
            catalogue_item=course_item,
            original_price=course_item.price,
            final_price=course_item.price,
        )

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, http_status.HTTP_200_OK)
        assert response.data['user'] == user.id
        assert response.data['status'] == Cart.Status.PENDING
        assert len(response.data['items']) == 1
        assert response.data['items'][0]['sku'] == course_item.sku

        # update cart status to paid
        user_cart.status = Cart.Status.PAID
        user_cart.save()

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
        response = self.client.post(self.url, data={'sku': course_item.sku})
        self.assertEqual(response.status_code, http_status.HTTP_201_CREATED)
        assert response.data['user'] == user.id
        assert response.data['status'] == Cart.Status.PENDING
        assert len(response.data['items']) == 1
        assert response.data['items'][0]['sku'] == course_item.sku

        user_old_cart = Cart.objects.get(id=response.data['id'])

        # user tries to add same sku again
        response = self.client.post(self.url, data={'sku': course_item.sku})
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

        response = self.client.post(self.url, data={'something-else-than-sku': 'invalid'})
        self.assertEqual(response.status_code, http_status.HTTP_400_BAD_REQUEST)
        assert response.data['error'] == 'SKU is required'

        response = self.client.post(self.url, data={'sku': 'invalid'})
        self.assertEqual(response.status_code, http_status.HTTP_400_BAD_REQUEST)
        assert response.data['error'] == 'Invalid SKU, unable to find catalogue item.'

    @patch.dict('zeitlabs_payments.views.CART_HANDLER', {}, clear=True)
    def test_post_failed_for_unsupported_item_type(self):
        """Verify that"""
        user = User.objects.get(id=self.learner1_id)
        course_item = CatalogueItem.objects.get(sku='custom-sku-1')

        self.login_user(user)
        response = self.client.post(self.url, data={'sku': course_item.sku})
        self.assertEqual(response.status_code, http_status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data['error'],
            'Item with given SKU has unsupported type: paid_course.',
        )

    @patch('zeitlabs_payments.helpers.CourseEnrollment.is_enrolled')
    def test_post_failed_for_add_to_cart_validation(self, mock_is_enrolled):
        """Verify that"""
        user = User.objects.get(id=self.learner1_id)
        course_item = CatalogueItem.objects.get(sku='custom-sku-1')
        self.login_user(user)
        mock_is_enrolled.return_value = True
        response = self.client.post(self.url, data={'sku': course_item.sku})
        self.assertEqual(response.status_code, http_status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data['error'],
            'Given SKU item does not match add to cart requirements',
        )
        self.assertEqual(
            response.data['details'],
            (
                'Unable to add item to the cart as user: user3 does not fulfill enrollment conditions. '
                'User is already enrolled in the course.'
            ),
        )


@pytest.mark.usefixtures('base_data')
class InitiatePaymentViewTest(TestCase):
    """Innitiate Payment View Test."""

    def setUp(self):
        self.user = User.objects.get(id=3)
        self.other_user = User.objects.get(id=4)
        self.cart = Cart.objects.create(user=self.user, status=Cart.Status.PENDING)
        self.provider = 'dummy'
        self.url = reverse(
            'zeitlabs_payments:initiate-payment',
            args=[self.provider, str(self.cart.id)],
        )

    def test_redirects_if_not_logged_in(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_returns_400_if_provider_invalid(self):
        self.client.force_login(self.user)
        bad_url = reverse('zeitlabs_payments:initiate-payment', args=['invalid', str(self.cart.id)])
        response = self.client.get(bad_url)
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'Unsupported payment provider', response.content)

    def test_returns_400_if_cart_does_not_exist(self):
        self.client.force_login(self.user)
        bad_url = reverse('zeitlabs_payments:initiate-payment', args=[self.provider, str(1000)])
        response = self.client.get(bad_url)
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'Cart with ID 1000 does not exist.', response.content)

    def test_returns_400_if_cart_does_not_belong_to_user(self):
        self.client.force_login(self.other_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'attempted to access cart', response.content)

    def test_successful_payment_view_initiation(self):
        request = RequestFactory().get(self.url)
        request.user = self.cart.user
        request.site = Site.objects.create(name='test.com', domain='test.com')
        InitiatePaymentView.as_view()(request, self.provider, self.cart.id)
        self.cart.refresh_from_db()
        self.assertEqual(self.cart.status, Cart.Status.PROCESSING)


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

    @patch('zeitlabs_payments.views.PROCESSORS', {})
    def test_checkout_view_context_without_cart(self):
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
        self.assertEqual(len(response.context['methods']), len(PROCESSORS))

    def test_checkout_view_with_sku_success(self):
        user_existing_cart = Cart.objects.create(user=self.user, status=Cart.Status.PENDING)
        self.client.force_login(self.user)
        test_sku = 'custom-sku-1'
        response = self.client.get(f'{self.url}?sku={test_sku}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['cart']['items']), 1)
        self.assertEqual(response.context['cart']['items'][0]['sku'], test_sku)
        self.assertEqual(len(response.context['methods']), len(PROCESSORS))
        user_existing_cart.refresh_from_db()
        self.assertEqual(
            user_existing_cart.status,
            Cart.Status.CANCELLED,
            'Old pending cart should be cancelled.',
        )

    def test_checkout_view_with_sku_for_invalid_sku(self):
        self.client.force_login(self.user)
        test_sku = 'does-not-exist'
        response = self.client.get(f'{self.url}?sku={test_sku}')
        self.assertTemplateUsed(response, 'zeitlabs_payments/invalid_cart.html')
        self.assertEqual(
            response.context['error_message'],
            'Item with sku: does-not-exist does not exist.',
        )
        self.assertEqual(response.status_code, 404)

    @patch.dict('zeitlabs_payments.views.CART_HANDLER', {}, clear=True)
    def test_checkout_view_with_sku_for_item_sku_with_unsuppported_type(self):
        self.client.force_login(self.user)
        response = self.client.get(f'{self.url}?sku=custom-sku-1')
        self.assertTemplateUsed(response, 'zeitlabs_payments/invalid_cart.html')
        self.assertEqual(response.context['error_message'], 'Item has unsupported type: paid_course.')
        self.assertEqual(response.status_code, 400)

    @patch('zeitlabs_payments.helpers.CourseEnrollment.is_enrolled')
    def test_checkout_view_with_sku_for_course_item_sku_already_enrolled(self, mock_enrolled):
        mock_enrolled.return_value = True
        self.client.force_login(self.user)
        response = self.client.get(f'{self.url}?sku=custom-sku-1')
        self.assertTemplateUsed(response, 'zeitlabs_payments/invalid_cart.html')
        self.assertEqual(
            response.context['error_message'],
            'Unable to add item to the cart as user: user3 does not fulfill enrollment '
            'conditions. User is already enrolled in the course.',
        )
        self.assertEqual(response.status_code, 400)

    def test_get_context_data_will_not_include_disabled_processors(self):
        cart = Cart.objects.create(user=self.user, status=Cart.Status.PENDING)
        self.client.force_login(self.user)

        class EnabledProcessor:
            """fake processor"""
            SLUG = 'enabled'

            @classmethod
            def get_payment_method_metadata(cls, cart):  # pylint: disable=unused-argument
                """not disabled"""
                return {'slug': cls.SLUG, 'disabled': None}

        class DisabledProcessor:
            """fake processor"""
            SLUG = 'disabled'

            @classmethod
            def get_payment_method_metadata(cls, cart):  # pylint: disable=unused-argument
                """disabled"""
                return {'slug': cls.SLUG, 'disabled': True}

        mock_processors = {'enabled': EnabledProcessor, 'disabled': DisabledProcessor}
        with patch('zeitlabs_payments.views.PROCESSORS', mock_processors):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['cart']['id'], cart.id)
        method_slugs = [m['slug'] for m in response.context['methods']]
        self.assertIn('enabled', method_slugs)
        self.assertNotIn('disabled', method_slugs)


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
class InvoiceViewTest(BaseTestViewMixin):
    """Tests for InvoiceView"""

    VIEW_NAME = 'zeitlabs_payments:invoice'

    def test_get_success(self):
        """
        Verify the invoice page shows correct context when invoice has no related transaction.
        """
        user = User.objects.get(id=3)
        self.login_user(user)
        cart = Cart.objects.create(user=user, status=Cart.Status.PAID)
        invoice = Invoice.objects.create(
            cart=cart,
            invoice_number='TEST-111111',
            total=100,
            gross_total=100,
        )
        self.url_args = [invoice.invoice_number]
        response = self.client.get(self.url)

        assert response.status_code == 200
        assert response.context['invoice'].invoice_number == 'TEST-111111'
        assert response.context['payment_method'] == 'manual'
        assert response.context['organization'] == get_settings().organization
        assert response.context['tax_number'] == get_settings().customer_number
        assert response.context['currency'] == get_currency(cart)

    def test_unauthorized(self):
        """Verify that the view returns 302 when the user is not authenticated"""
        self.url_args = ['does-not-matter']
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_invoices_access(self):
        """
        Ensure invoice access rules:
        - Admins can view all invoices
        - Normal users can only view their own
        """
        admin_user = User.objects.get(id=1)
        admin_user_cart = Cart.objects.create(user=admin_user, status=Cart.Status.PAID)
        admin_user_invoice = Invoice.objects.create(
            cart=admin_user_cart,
            invoice_number='TEST-111111',
            total=100,
            gross_total=100,
        )

        normal_user1 = User.objects.get(id=2)
        normal_user1_cart = Cart.objects.create(user=normal_user1, status=Cart.Status.PAID)
        normal_user1_invoice = Invoice.objects.create(
            cart=normal_user1_cart,
            invoice_number='TEST-22222',
            total=100,
            gross_total=100,
        )

        normal_user2 = User.objects.get(id=3)
        normal_user2_cart = Cart.objects.create(user=normal_user2, status=Cart.Status.PAID)
        normal_user2_invoice = Invoice.objects.create(
            cart=normal_user2_cart,
            invoice_number='TEST-33333',
            total=200,
            gross_total=200,
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
        assert response.status_code == 404, "Normal user should not be able to access another user's invoice"

    def test_get_success_with_related_transaction(self):
        """
        Verify the invoice page shows correct payment_method when invoice has a related transaction.
        """
        user = User.objects.get(id=3)
        self.login_user(user)
        cart = Cart.objects.create(user=user, status=Cart.Status.PAID)
        invoice = Invoice.objects.create(cart=cart, invoice_number='INV-222222', total=100, gross_total=100)

        transaction = Transaction.objects.create(
            cart=cart,
            amount=cart.total,
            gateway='payfort',
            gateway_transaction_id='TX-222',
        )
        invoice.related_transaction = transaction
        invoice.save()

        self.url_args = [invoice.invoice_number]
        response = self.client.get(self.url)

        assert response.status_code == 200
        assert response.context['invoice'].invoice_number == 'INV-222222'
        assert response.context['payment_method'] == 'payfort'
        assert response.context['organization'] == get_settings().organization
        assert response.context['tax_number'] == get_settings().customer_number
        assert response.context['currency'] == get_currency(cart)

    def test_invoice_exposes_navigation_targets_for_paid_course(self):
        """navigation_targets contains a 'Go to Your Course' entry for a paid_course item."""
        user = User.objects.get(id=3)
        self.login_user(user)
        course_item = CatalogueItem.objects.get(sku='custom-sku-1')
        cart = Cart.objects.create(user=user, status=Cart.Status.PAID)
        cart_item = cart.items.create(
            catalogue_item=course_item,
            original_price=course_item.price,
            final_price=course_item.price,
        )
        invoice = Invoice.objects.create(
            cart=cart,
            invoice_number='INV-NAV-PAID',
            total=course_item.price,
            gross_total=course_item.price,
        )
        invoice.items.create(
            cart_item=cart_item,
            original_price=course_item.price,
            price=course_item.price,
        )

        self.url_args = [invoice.invoice_number]
        response = self.client.get(self.url)

        assert response.status_code == 200
        targets = response.context['navigation_targets']
        assert len(targets) == 1
        assert targets[0]['label'] == 'Go to Your Course'
        assert targets[0]['url'] == f'/courses/{course_item.item_ref_id}/course/'
        assert targets[0]['is_program'] is False

    def test_invoice_exposes_navigation_targets_for_bundle(self):
        """navigation_targets contains a 'Start Your First Course' entry for a bundle."""
        user = User.objects.get(id=3)
        self.login_user(user)
        bundle_item = CatalogueItem.objects.get(sku='BUNDLE-PRO-CERT')
        cart = Cart.objects.create(user=user, status=Cart.Status.PAID)
        cart_item = cart.items.create(
            catalogue_item=bundle_item,
            original_price=bundle_item.price,
            final_price=bundle_item.price,
        )
        invoice = Invoice.objects.create(
            cart=cart,
            invoice_number='INV-NAV-BUNDLE',
            total=bundle_item.price,
            gross_total=bundle_item.price,
        )
        invoice.items.create(
            cart_item=cart_item,
            original_price=bundle_item.price,
            price=bundle_item.price,
        )

        self.url_args = [invoice.invoice_number]
        response = self.client.get(self.url)

        assert response.status_code == 200
        targets = response.context['navigation_targets']
        assert len(targets) == 1
        assert targets[0]['label'] == 'Start Your First Course'
        # _create_program_bundles links custom-sku-1 first (course-v1:org1+1+1).
        assert targets[0]['url'] == '/courses/course-v1:org1+1+1/course/'
        assert targets[0]['is_program'] is True


@pytest.mark.usefixtures('base_data')
class PaymentSuccessViewTest(BaseTestViewMixin):
    """Tests for PaymentErrorView"""

    VIEW_NAME = 'zeitlabs_payments:payment-success'

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

    def test_first_course_url_for_paid_course_cart(self):
        """
        When the merchant_reference resolves to a cart containing a single
        paid_course, the context exposes a primary CTA pointing at that course.
        """
        user = User.objects.get(id=self.learner1_id)
        self.login_user(user)

        course_item = CatalogueItem.objects.get(sku='custom-sku-1')
        cart = Cart.objects.create(user=user, status=Cart.Status.PAID)
        cart.items.create(
            catalogue_item=course_item,
            original_price=course_item.price,
            final_price=course_item.price,
        )

        site = Site.objects.get_current()
        self.url_args = [f'{site.id}-{cart.id}']
        response = self.client.get(self.url)

        assert response.status_code == 200
        assert response.context['first_course_url'] == f'/courses/{course_item.item_ref_id}/course/'
        assert response.context['is_program'] is False
        assert response.context['first_course_name'] == course_item.title

    def test_first_course_url_for_program_bundle_cart(self):
        """
        When the merchant_reference resolves to a cart containing a program
        bundle, the context exposes a primary CTA pointing at the first
        course linked to that bundle.
        """
        user = User.objects.get(id=self.learner1_id)
        self.login_user(user)

        bundle_item = CatalogueItem.objects.get(sku='BUNDLE-PRO-CERT')
        cart = Cart.objects.create(user=user, status=Cart.Status.PAID)
        cart.items.create(
            catalogue_item=bundle_item,
            original_price=bundle_item.price,
            final_price=bundle_item.price,
        )

        site = Site.objects.get_current()
        self.url_args = [f'{site.id}-{cart.id}']
        response = self.client.get(self.url)

        assert response.status_code == 200
        # _create_program_bundles links custom-sku-1 (course-v1:org1+1+1) first
        assert response.context['first_course_url'] == '/courses/course-v1:org1+1+1/course/'
        assert response.context['is_program'] is True
        assert response.context['first_course_name'] is not None

    def test_no_first_course_url_for_other_users_cart(self):
        """
        A learner must not be shown a CTA pointing into another user's cart.
        """
        user = User.objects.get(id=self.learner1_id)
        other_user = User.objects.get(id=self.learner2_id)
        self.login_user(user)

        course_item = CatalogueItem.objects.get(sku='custom-sku-1')
        other_cart = Cart.objects.create(user=other_user, status=Cart.Status.PAID)
        other_cart.items.create(
            catalogue_item=course_item,
            original_price=course_item.price,
            final_price=course_item.price,
        )

        site = Site.objects.get_current()
        self.url_args = [f'{site.id}-{other_cart.id}']
        response = self.client.get(self.url)

        assert response.status_code == 200
        assert response.context['first_course_url'] is None

    def test_no_first_course_url_for_empty_bundle(self):
        """
        A program bundle without linked courses yields no CTA so the user
        still sees the dashboard fallback.
        """
        user = User.objects.get(id=self.learner1_id)
        self.login_user(user)

        empty_bundle = CatalogueItem.objects.get(sku='BUNDLE-EMPTY')
        cart = Cart.objects.create(user=user, status=Cart.Status.PAID)
        cart.items.create(
            catalogue_item=empty_bundle,
            original_price=empty_bundle.price,
            final_price=empty_bundle.price,
        )

        site = Site.objects.get_current()
        self.url_args = [f'{site.id}-{cart.id}']
        response = self.client.get(self.url)

        assert response.status_code == 200
        assert response.context['first_course_url'] is None

    def test_no_first_course_url_for_anonymous_user(self):
        """
        An anonymous visitor must not see a CTA pointing into any cart.
        """
        course_item = CatalogueItem.objects.get(sku='custom-sku-1')
        user = User.objects.get(id=self.learner1_id)
        cart = Cart.objects.create(user=user, status=Cart.Status.PAID)
        cart.items.create(
            catalogue_item=course_item,
            original_price=course_item.price,
            final_price=course_item.price,
        )

        site = Site.objects.get_current()
        self.url_args = [f'{site.id}-{cart.id}']
        response = self.client.get(self.url)

        assert response.status_code == 200
        assert response.context['first_course_url'] is None

    def test_malformed_merchant_reference_does_not_break_page(self):
        """
        A non-{site_id}-{cart_id} reference (e.g. test-only 'ORDER-98765')
        must still render the page with no first-course CTA.
        """
        self.url_args = ['ORDER-98765']
        response = self.client.get(self.url)

        assert response.status_code == 200
        assert response.context['merchant_reference'] == 'ORDER-98765'
        assert response.context['first_course_url'] is None

    def test_reference_without_dash_does_not_break_page(self):
        """
        A reference with no '-' separator must be treated as malformed: the
        page renders and no CTA is exposed.
        """
        self.url_args = ['no-dash-here']
        response = self.client.get(self.url)

        assert response.status_code == 200
        assert response.context['first_course_url'] is None

    def test_superuser_can_view_first_course_for_any_cart(self):
        """
        Superusers (e.g. support staff inspecting a learner's order) must
        still see the first-course CTA even for carts that aren't theirs.
        """
        superuser = User.objects.get(id=1)  # id=1 is in conftest super_users
        self.login_user(superuser)

        other_user = User.objects.get(id=self.learner2_id)
        course_item = CatalogueItem.objects.get(sku='custom-sku-1')
        cart = Cart.objects.create(user=other_user, status=Cart.Status.PAID)
        cart.items.create(
            catalogue_item=course_item,
            original_price=course_item.price,
            final_price=course_item.price,
        )

        site = Site.objects.get_current()
        self.url_args = [f'{site.id}-{cart.id}']
        response = self.client.get(self.url)

        assert response.status_code == 200
        assert response.context['first_course_url'] == f'/courses/{course_item.item_ref_id}/course/'


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


@pytest.mark.usefixtures('base_data')
class OrderHistoryViewTest(BaseTestViewMixin):
    """Tests for OrderHistoryView — payment history page."""

    VIEW_NAME = 'zeitlabs_payments:order-history'

    def test_redirect_when_anonymous(self):
        """Anonymous users should be redirected to the login page."""
        response = self.client.get(self.url)
        assert response.status_code == 302
        assert '/login' in response.url or '/accounts/login' in response.url

    def test_empty_history(self):
        """Authenticated user with no orders sees the empty-state message."""
        user = User.objects.get(id=self.learner1_id)
        self.login_user(user)

        response = self.client.get(self.url)
        assert response.status_code == 200
        assert 'records' in response.context
        assert response.context['records'].count() == 0

    def test_history_shows_user_orders(self):
        """Authenticated user sees their own orders with invoices."""
        user = User.objects.get(id=self.learner1_id)
        other_user = User.objects.get(id=self.learner2_id)
        course_item = CatalogueItem.objects.get(sku='custom-sku-1')

        # Create a paid cart with invoice for the logged-in user
        user_cart = Cart.objects.create(user=user, status=Cart.Status.PAID)
        user_cart.items.create(
            catalogue_item=course_item,
            original_price=course_item.price,
            final_price=course_item.price,
        )
        Invoice.objects.create(
            cart=user_cart,
            invoice_number='INV-USER',
            currency='SAR',
            status='paid',
            gross_total=50,
            total=50,
        )

        # Create a paid cart for *another* user — should NOT appear
        other_cart = Cart.objects.create(user=other_user, status=Cart.Status.PAID)
        other_cart.items.create(
            catalogue_item=course_item,
            original_price=course_item.price,
            final_price=course_item.price,
        )
        Invoice.objects.create(
            cart=other_cart,
            invoice_number='INV-OTHER',
            currency='SAR',
            status='paid',
            gross_total=50,
            total=50,
        )

        self.login_user(user)
        response = self.client.get(self.url)

        assert response.status_code == 200
        records = response.context['records']
        cart_ids = [c.id for c in records]
        assert user_cart.id in cart_ids
        assert other_cart.id not in cart_ids

    def test_history_template_used(self):
        """Verify the correct template is rendered."""
        user = User.objects.get(id=self.learner1_id)
        self.login_user(user)

        response = self.client.get(self.url)
        assert response.status_code == 200
        self.assertTemplateUsed(response, 'zeitlabs_payments/order_history.html')
