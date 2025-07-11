"""Test views for the zeitlabs_payments app"""

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.test import RequestFactory, TestCase
from django.urls import reverse
from rest_framework import status as http_status
from rest_framework.test import APITestCase

from zeitlabs_payments.models import Cart, CatalogueItem
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


# @pytest.mark.usefixtures('base_data')
# class InitiatePaymentViewTest(TestCase):
#     """Innitiate Payment View Test."""

#     def setUp(self):
#         self.user = User.objects.get(id=3)
#         self.other_user = User.objects.get(id=4)
#         self.cart = Cart.objects.create(user=self.user, status=Cart.Status.PENDING)
#         self.provider = 'payfort'
#         self.url = reverse('zeitlabs_payments:initiate-payment', args=[self.provider, str(self.cart.id)])

#     def test_redirects_if_not_logged_in(self):
#         response = self.client.get(self.url)
#         self.assertEqual(response.status_code, 302)

#     def test_returns_400_if_provider_invalid(self):
#         self.client.force_login(self.user)
#         bad_url = reverse('zeitlabs_payments:initiate-payment', args=['invalid', str(self.cart.id)])
#         response = self.client.get(bad_url)
#         self.assertEqual(response.status_code, 400)
#         self.assertIn(b'Unsupported payment provider', response.content)

#     def test_returns_400_if_cart_does_not_exist(self):
#         self.client.force_login(self.user)
#         bad_url = reverse('zeitlabs_payments:initiate-payment', args=[self.provider, str(1000)])
#         response = self.client.get(bad_url)
#         self.assertEqual(response.status_code, 400)
#         self.assertIn(b'Cart matching query does not exist.', response.content)

#     def test_returns_400_if_cart_does_not_belong_to_user(self):
#         self.client.force_login(self.other_user)
#         response = self.client.get(self.url)
#         self.assertEqual(response.status_code, 400)
#         self.assertIn(b'attempted to access cart', response.content)

#     def test_successful_payment_view_initiation(self):
#         request = RequestFactory().get(self.url)
#         request.user = self.cart.user
#         request.site = Site.objects.create(name='test.com', domain='test.com')
#         InitiatePaymentView.as_view()(request, self.provider, self.cart.id)
#         self.cart.refresh_from_db()
#         self.assertEqual(self.cart.status, Cart.Status.PROCESSING)


class CheckoutViewTests(TestCase):
    """Checkout View Test."""

    def setUp(self):
        self.user = User.objects.get(id=3)
        self.url = reverse('zeitlabs_payments:checkout')

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

    def test_checkout_view_context_with_cart_and_methods(self):
        user_cart = Cart.objects.create(user=self.user, status=Cart.Status.PENDING)
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['cart']['id'], user_cart.id)
        self.assertEqual(len(response.context['methods']), 1)
