"""Test views for the zeitlabs_payments app"""

from unittest.mock import patch

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.test import RequestFactory, TestCase
from django.urls import reverse
from rest_framework import status as http_status
from rest_framework.test import APITestCase

from zeitlabs_payments.helpers import get_currency
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
        assert response.data['user'] is None, 'Expected "user" to be None since there is no existing cart'
        assert response.data['status'] is None, 'Expected "status" to be None since there is no existing cart'
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
        assert response.data['user'] is None, (
            'Expected "user" to be None since there is no existing cart with pending state'
        )
        assert response.data['status'] is None, (
            'Expected "status" to be None since there is no existing cart with pending state'
        )

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
        self.provider = 'payfort'
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
        self.client.force_login(self.user)
        test_sku = 'does-not-exist'
        response = self.client.get(f'{self.url}?sku={test_sku}')
        self.assertEqual(response.status_code, 404)

    @patch.dict(
        'zeitlabs_payments.views.CART_HANDLER', {}, clear=True
    )
    def test_checkout_view_with_sku_for_item_sku_with_unsuppported_type(self):
        self.client.force_login(self.user)
        response = self.client.get(f'{self.url}?sku=custom-sku-1')
        self.assertEqual(response.status_code, 400)

    @patch(
        'zeitlabs_payments.helpers.CourseEnrollment.is_enrolled'
    )
    def test_checkout_view_with_sku_for_course_item_sku_already_enrolled(self, mock_enrolled):
        mock_enrolled.return_value = True
        self.client.force_login(self.user)
        response = self.client.get(f'{self.url}?sku=custom-sku-1')
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
        cart = Cart.objects.create(user_id=3, status=Cart.Status.PAID)
        invoice = Invoice.objects.create(cart=cart, invoice_number='TEST-111111', total=100)
        self.url_args = [invoice.invoice_number]
        response = self.client.get(self.url)

        assert response.status_code == 200
        assert response.context['invoice'].invoice_number == 'TEST-111111'
        assert response.context['payment_method'] == 'manual'
        assert response.context['organization'] == settings.ORGANIZATION
        assert response.context['tax_number'] == settings.CUSTOMER_NUMBER
        assert response.context['currency'] == get_currency(cart)

    def test_get_success_with_related_transaction(self):
        """
        Verify the invoice page shows correct payment_method when invoice has a related transaction.
        """
        cart = Cart.objects.create(user_id=3, status=Cart.Status.PAID)
        invoice = Invoice.objects.create(cart=cart, invoice_number='INV-222222', total=100)

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
        assert response.context['organization'] == settings.ORGANIZATION
        assert response.context['tax_number'] == settings.CUSTOMER_NUMBER
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


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
class TestManualPaymentView(BaseTestViewMixin):
    """Tests for ManualPaymentView"""

    VIEW_NAME = 'zeitlabs_payments:manual-payment'
    admin_user = None
    learner_user = None
    course_id = 'course-v1:org1+1+1'
    mode_slug = 'no-id-professional'

    @pytest.fixture(autouse=True)
    def setup(self, db):  # pylint: disable=unused-argument
        """Test setup"""
        self.admin_user = User.objects.get(id=1)
        self.learner_user = User.objects.get(id=3)

    def test_unauthorized(self):
        """Verify that the view returns 403 when the user is not authenticated"""
        response = self.client.post(self.url, data={
            'sku': 'does-not-matter'
        })
        self.assertEqual(response.status_code, http_status.HTTP_403_FORBIDDEN)

    def test_non_admin_user_access(self):
        """Verify that the view returns 403 when the non amdin suer try to access"""
        self.login_user(self.learner_user)
        response = self.client.post(self.url, data={
            'sku': 'does-not-matter'
        })
        self.assertEqual(response.status_code, http_status.HTTP_403_FORBIDDEN)

    def test_post_success(self):
        """
        Valid request → should create invoice & cart.
        """
        self.login_user(self.admin_user)
        payload = {
            'username': self.learner_user.username,
            'course_key': self.course_id,
            'mode': self.mode_slug
        }

        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 201
        data = response.data

        assert 'created_cart' in data
        assert 'created_invoice' in data

        cart = Cart.objects.get(id=data['created_cart'])
        invoice = Invoice.objects.get(invoice_number=data['created_invoice'])

        assert cart.user == self.learner_user
        assert cart.status == Cart.Status.PAID
        assert invoice.cart == cart

    def test_missing_required_fields(self):
        """
        Should return 400 when missing user_id/username or course_key/mode.
        """
        self.login_user(self.admin_user)
        payload = {'course_key': self.course_id, 'mode': self.mode_slug}
        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 400
        assert response.data['error'] == 'Missing required param: user_id or username'

        # missing mode
        payload = {'username': self.learner_user.username, 'course_key': self.course_id}
        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 400
        assert response.data['error'] == 'Missing required param: mode'

        # missing course_key
        payload = {'user_id': self.learner_user.id, 'mode': 'verified'}
        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 400
        assert response.data['error'] == 'Missing required param: course_key'

    def test_invalid_user(self):
        """
        Should return 400 when user not found.
        """
        self.login_user(self.admin_user)
        payload = {
            'username': 'nonexistentuser',
            'course_key': self.course_id,
            'mode': self.mode_slug
        }
        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 400
        assert response.data['error'] == 'Unable to retrieve user with given user info.'

        payload = {
            'user_id': 99999,
            'course_key': self.course_id,
            'mode': self.mode_slug
        }
        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 400
        assert response.data['error'] == 'Unable to retrieve user with given user info.'

    def test_invalid_course_id(self):
        """
        Should return 400 when course mode or catalogue item not found.
        """
        self.login_user(self.admin_user)
        payload = {
            'username': self.learner_user.username,
            'course_key': 'invlaid-course-id',
            'mode': self.mode_slug
        }
        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 400
        assert response.data['error'] == 'Invalid course id provided: invlaid-course-id.'

    def test_non_exist_course_id(self):
        """
        Should return 400 when course mode or catalogue item not found.
        """
        self.login_user(self.admin_user)
        payload = {
            'username': self.learner_user.username,
            'course_key': 'course-v1:notexist+1+1',
            'mode': self.mode_slug
        }
        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 400
        assert response.data['error'] == (
            "Unable to retrieve course mode or catalogue item for course_id = 'course-v1:notexist+1+1' "
            "and mode='no-id-professional'."
        )

    @patch.dict(
        'zeitlabs_payments.views.CART_HANDLER', {}, clear=True
    )
    def test_catalogue_item_with_unsupported_type(self):
        """
        Should return 400 when course mode or catalogue item not found.
        """
        self.login_user(self.admin_user)
        payload = {
            'username': self.learner_user.username,
            'course_key': 'course-v1:org1+1+1',
            'mode': self.mode_slug
        }
        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 400
        assert response.data['error'] == (
            'Catalog Item with given course_id and mode has unsupported type: paid_course.'
        )

    def test_post_for_validate_add_to_cart_exception(self):
        """
        Valid request → should create invoice & cart.
        """
        self.login_user(self.admin_user)
        payload = {
            'username': self.learner_user.username,
            'course_key': self.course_id,
            'mode': self.mode_slug
        }

        related_course_item = CatalogueItem.objects.get(sku='custom-sku-1')
        related_course_item.item_ref_id = 'invalid-does-not-match-with-course-id'
        related_course_item.save()
        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 400
        assert response.data['error'] == 'Given course does not match add to cart requirements'
        assert response.data['details'] == (
            'Unable to add item to the cart as Course mode found with given sku but course_id '
            'mismatch with catalogue item ref-id.'
        )

    @patch('zeitlabs_payments.views.ManualPaymentProcessor.process_payment')
    def test_process_payment_raises_exception(self, mock_process_payment):
        """
        Should return 400 when processor.process_payment raises an exception.
        """
        self.client.force_login(self.admin_user)
        mock_process_payment.side_effect = Exception('some error')

        payload = {
            'user_id': self.learner_user.id,
            'course_key': self.course_id,
            'mode': self.mode_slug
        }
        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 400
        assert response.data['error'] == 'Failed to process manual payment'
        assert response.data['details'] == 'some error'
