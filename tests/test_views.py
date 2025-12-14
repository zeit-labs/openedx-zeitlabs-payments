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
        self.assertEqual(response.data['details'], (
            'Unable to add item to the cart as user: user3 does not fulfill enrollment conditions. '
            'User is already enrolled in the course.'
        ))


@pytest.mark.usefixtures('base_data')
class InitiatePaymentViewTest(TestCase):
    """Innitiate Payment View Test."""

    def setUp(self):
        self.user = User.objects.get(id=3)
        self.other_user = User.objects.get(id=4)
        self.cart = Cart.objects.create(user=self.user, status=Cart.Status.PENDING)
        self.provider = 'dummy'
        self.url = reverse('zeitlabs_payments:initiate-payment', args=[self.provider, str(self.cart.id)])

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
        # Should have manual and stripe processors registered
        self.assertGreaterEqual(len(response.context['methods']), 1)

    def test_checkout_view_with_sku_success(self):
        user_existing_cart = Cart.objects.create(user=self.user, status=Cart.Status.PENDING)
        self.client.force_login(self.user)
        test_sku = 'custom-sku-1'
        response = self.client.get(f'{self.url}?sku={test_sku}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['cart']['items']), 1)
        self.assertEqual(response.context['cart']['items'][0]['sku'], test_sku)
        # Should have manual and stripe processors registered
        self.assertGreaterEqual(len(response.context['methods']), 1)
        user_existing_cart.refresh_from_db()
        self.assertEqual(user_existing_cart.status, Cart.Status.CANCELLED, 'Old pending cart should be cancelled.')

    def test_checkout_view_with_sku_for_invalid_sku(self):
        self.client.force_login(self.user)
        test_sku = 'does-not-exist'
        response = self.client.get(f'{self.url}?sku={test_sku}')
        self.assertTemplateUsed(response, 'zeitlabs_payments/invalid_cart.html')
        self.assertEqual(response.context['error_message'], 'Item with sku: does-not-exist does not exist.')
        self.assertEqual(response.status_code, 404)

    @patch.dict(
        'zeitlabs_payments.views.CART_HANDLER', {}, clear=True
    )
    def test_checkout_view_with_sku_for_item_sku_with_unsuppported_type(self):
        self.client.force_login(self.user)
        response = self.client.get(f'{self.url}?sku=custom-sku-1')
        self.assertTemplateUsed(response, 'zeitlabs_payments/invalid_cart.html')
        self.assertEqual(
            response.context['error_message'],
            'Item has unsupported type: paid_course.'
        )
        self.assertEqual(response.status_code, 400)

    @patch(
        'zeitlabs_payments.helpers.CourseEnrollment.is_enrolled'
    )
    def test_checkout_view_with_sku_for_course_item_sku_already_enrolled(self, mock_enrolled):
        mock_enrolled.return_value = True
        self.client.force_login(self.user)
        response = self.client.get(f'{self.url}?sku=custom-sku-1')
        self.assertTemplateUsed(response, 'zeitlabs_payments/invalid_cart.html')
        self.assertEqual(
            response.context['error_message'],
            'Unable to add item to the cart as user: user3 does not fulfill enrollment '
            'conditions. User is already enrolled in the course.'
        )
        self.assertEqual(response.status_code, 400)


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
            gross_total=100
        )

        normal_user1 = User.objects.get(id=2)
        normal_user1_cart = Cart.objects.create(user=normal_user1, status=Cart.Status.PAID)
        normal_user1_invoice = Invoice.objects.create(
            cart=normal_user1_cart,
            invoice_number='TEST-22222',
            total=100,
            gross_total=100
        )

        normal_user2 = User.objects.get(id=3)
        normal_user2_cart = Cart.objects.create(user=normal_user2, status=Cart.Status.PAID)
        normal_user2_invoice = Invoice.objects.create(
            cart=normal_user2_cart,
            invoice_number='TEST-33333',
            total=200,
            gross_total=200
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
        invoice = Invoice.objects.create(
            cart=cart,
            invoice_number='INV-222222',
            total=100,
            gross_total=100
        )

        transaction = Transaction.objects.create(
            cart=cart,
            amount=cart.total,
            gateway='payfort',
            gateway_transaction_id='TX-222'
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
