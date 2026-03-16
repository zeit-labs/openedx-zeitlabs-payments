"""Test views for the zeitlabs_payments app"""

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status as http_status
from rest_framework.test import APITestCase

from zeitlabs_payments.models import Cart, CatalogueItem, Invoice, Transaction

User = get_user_model()


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
class TestManualPaymentView(APITestCase):
    """Tests for ManualPaymentView"""

    VIEW_NAME = 'zeitlabs_payments:manual-payment'
    admin_user = None
    learner_user = None
    course_id = 'course-v1:org1+1+1'
    mode_slug = 'no-id-professional'

    def setUp(self):
        """Setup"""
        self.view_name = self.VIEW_NAME
        self.url_args = []
        self.admin_user = User.objects.get(id=1)
        self.learner_user = User.objects.get(id=3)

    @property
    def url(self):
        """Get the URL"""
        return reverse(self.view_name, args=self.url_args)

    def login_user(self, user):
        """Helper to login user"""
        self.client.force_login(user)

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
            'mode': self.mode_slug,
            'transaction_id': '1245',
            'transaction_status': 'success'
        }

        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 201
        data = response.data

        assert 'created_cart' in data
        assert 'created_invoice' in data

        cart = Cart.objects.get(id=data['created_cart'])
        invoice = Invoice.objects.get(invoice_number=data['created_invoice'])
        assert Transaction.objects.filter(
            gateway_transaction_id='1245', status='success', gateway='manual'
        ).exists()

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
        payload = {
            'username': 'nonexistentuser',
            'course_key': self.course_id,
            'mode': self.mode_slug,
            'transaction_id': 'does not matter',
            'transaction_status': 'does not maatter'
        }

        self.login_user(self.admin_user)
        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 400
        assert response.data['error'] == 'Unable to retrieve user with given user info.'

        payload.pop('username')
        payload.update({'user_id': 99999})
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
            'mode': self.mode_slug,
            'transaction_id': 'does not matter',
            'transaction_status': 'does not maatter'
        }
        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 400
        assert 'Invalid course id' in response.data['error']
        assert 'invlaid-course-id' in response.data['error']

    def test_non_exist_course_id(self):
        """
        Should return 400 when course mode or catalogue item not found.
        """
        self.login_user(self.admin_user)
        payload = {
            'username': self.learner_user.username,
            'course_key': 'course-v1:notexist+1+1',
            'mode': self.mode_slug,
            'transaction_id': 'does not matter',
            'transaction_status': 'does not maatter'
        }
        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 400
        assert (
            'Unable to retrieve course mode or catalogue item' in response.data['error']
        )
        assert 'course-v1:notexist+1+1' in response.data['error']

    @patch.dict('zeitlabs_payments.cart_handler.CART_HANDLER', {}, clear=True)
    def test_catalogue_item_with_unsupported_type(self):
        """
        Should return 400 when item type has no registered handler.
        """
        self.login_user(self.admin_user)
        payload = {
            'username': self.learner_user.username,
            'course_key': 'course-v1:org1+1+1',
            'mode': self.mode_slug,
            'transaction_id': 'does not matter',
            'transaction_status': 'does not maatter'
        }
        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 400
        assert 'unsupported type' in response.data['details']

    def test_post_for_validate_add_to_cart_exception(self):
        """
        Should return 400 when cart validation fails.
        """
        self.login_user(self.admin_user)
        payload = {
            'username': self.learner_user.username,
            'course_key': self.course_id,
            'mode': self.mode_slug,
            'transaction_id': 'does not matter',
            'transaction_status': 'does not maatter'
        }

        related_course_item = CatalogueItem.objects.get(sku='custom-sku-1')
        related_course_item.item_ref_id = 'invalid-does-not-match-with-course-id'
        related_course_item.save()
        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 400
        assert 'do not match add to cart requirements' in response.data['error']
        assert response.data['details'] == (
            'Unable to add item to the cart as Course mode found with given sku but course_id '
            'mismatch with catalogue item ref-id.'
        )

    @patch(
        'zeitlabs_payments.providers.manual_payment.processor.ManualPaymentProcessor.process_payment'
    )
    def test_process_payment_raises_exception(self, mock_process_payment):
        """
        Should return 400 when processor.process_payment raises an exception.
        """
        self.client.force_login(self.admin_user)
        mock_process_payment.side_effect = Exception('some error')

        payload = {
            'user_id': self.learner_user.id,
            'course_key': self.course_id,
            'mode': self.mode_slug,
            'transaction_id': 'does not matter',
            'transaction_status': 'does not maatter'
        }
        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 400
        assert response.data['error'] == 'Failed to process manual payment'
        assert response.data['details'] == 'some error'

    def test_post_bulk_items_success(self):
        """
        Should create a cart with multiple items when 'items' list is provided.
        """
        self.login_user(self.admin_user)
        payload = {
            'username': self.learner_user.username,
            'items': [
                {'course_key': 'course-v1:org1+1+1', 'mode': 'no-id-professional'},
                {'course_key': 'course-v1:org2+1+1', 'mode': 'no-id-professional'},
            ],
            'transaction_id': 'bulk-tx-001',
            'transaction_status': 'success',
        }
        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 201

        cart = Cart.objects.get(id=response.data['created_cart'])
        assert cart.items.count() == 2
        assert cart.user == self.learner_user
        assert cart.status == Cart.Status.PAID

    def test_post_bulk_items_missing_per_item_fields(self):
        """
        Should return 400 when an item in 'items' list is missing course_key or mode.
        """
        self.login_user(self.admin_user)
        payload = {
            'username': self.learner_user.username,
            'items': [
                {'course_key': 'course-v1:org1+1+1', 'mode': 'no-id-professional'},
                {'course_key': 'course-v1:org2+1+1'},  # missing 'mode'
            ],
            'transaction_id': 'bulk-tx-002',
            'transaction_status': 'success',
        }
        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 400
        assert 'index 1' in response.data['error']
        assert 'missing required field' in response.data['error'].lower()

    def test_post_bulk_items_invalid_course_id(self):
        """
        Should return 400 when a course_key in 'items' has invalid format.
        """
        self.login_user(self.admin_user)
        payload = {
            'username': self.learner_user.username,
            'items': [
                {'course_key': 'not-a-valid-course-id', 'mode': 'no-id-professional'},
            ],
            'transaction_id': 'bulk-tx-003',
            'transaction_status': 'success',
        }
        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 400
        assert 'Invalid course id' in response.data['error']

    def test_post_bulk_items_nonexistent_course(self):
        """
        Should return 400 when a course_key in 'items' does not have a matching
        CourseMode or CatalogueItem.
        """
        self.login_user(self.admin_user)
        payload = {
            'username': self.learner_user.username,
            'items': [
                {'course_key': 'course-v1:org1+1+1', 'mode': 'no-id-professional'},
                {'course_key': 'course-v1:noexist+1+1', 'mode': 'no-id-professional'},
            ],
            'transaction_id': 'bulk-tx-004',
            'transaction_status': 'success',
        }
        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 400
        assert (
            'Unable to retrieve course mode or catalogue item' in response.data['error']
        )
        assert 'index 1' in response.data['error']

    def test_post_bulk_does_not_require_top_level_course_key(self):
        """
        When 'items' key is present, top-level 'course_key' and 'mode' should NOT be required.
        """
        self.login_user(self.admin_user)
        # No top-level course_key / mode — should be fine because 'items' is present
        payload = {
            'username': self.learner_user.username,
            'items': [
                {'course_key': 'course-v1:org1+1+1', 'mode': 'no-id-professional'},
            ],
            'transaction_id': 'bulk-tx-005',
            'transaction_status': 'success',
        }
        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 201

    def test_post_bulk_items_validation_failure(self):
        """
        Should return 400 when validate_and_create_cart raises InvalidCartError for bulk items.
        """
        self.login_user(self.admin_user)
        # Use the catalogue item with invalid ref_id
        payload = {
            'username': self.learner_user.username,
            'items': [
                {'course_key': 'course-v1:org1+1+1', 'mode': 'verified'},
            ],
            'transaction_id': 'bulk-tx-006',
            'transaction_status': 'success',
        }
        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 400
        assert 'do not match add to cart requirements' in response.data['error']

    def test_post_bulk_items_must_be_list(self):
        """
        Should return 400 when 'items' is present but not a list (e.g. a string).
        """
        self.login_user(self.admin_user)
        payload = {
            'username': self.learner_user.username,
            'items': 'not-a-list',
            'transaction_id': 'bulk-tx-007',
            'transaction_status': 'success',
        }
        response = self.client.post(self.url, data=payload, format='json')
        assert response.status_code == 400
        assert 'must be a non-empty list' in response.data['error']
