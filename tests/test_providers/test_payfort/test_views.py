"""Test views for the zeitlabs_payment payfort provider"""
import hashlib
from unittest.mock import patch

import pytest
from common.djangoapps.course_modes.models import CourseMode
from common.djangoapps.student.models import CourseEnrollment
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from zeitlabs_payments.models import AuditLog, Cart, CatalogueItem, Invoice, Transaction, WebhookEvent
from zeitlabs_payments.providers.payfort.views import PayFortBaseView, PayfortFeedbackView

User = get_user_model()


@pytest.mark.django_db
class TestPayFortBaseViewNoMocks:
    """PayfortBaseView tests."""

    factory = RequestFactory()
    view = PayFortBaseView()
    site = None
    cart = None

    def setup_method(self):
        """setup"""
        self.site = Site.objects.create(name='TestSite', domain='testsite.com')
        self.cart = Cart.objects.create(
            user=User.objects.get(id=3),
            status=Cart.Status.PROCESSING
        )

    def test_cart_and_site_return_real_objects(self):
        merchant_ref = f'{self.site.id}-{self.cart.id}'
        request = self.factory.post('/fake-url/', data={'merchant_reference': merchant_ref})
        self.view.request = request

        cart = self.view.cart
        site = self.view.site

        assert cart is not None
        assert site is not None
        assert cart.id == self.cart.id
        assert site.id == self.site.id

    def test_cart_returns_none_on_bad_reference(self):
        request = self.factory.post('/fake-url/', data={'merchant_reference': 'bad-format'})
        self.view.request = request
        assert self.view.cart is None

    def test_site_returns_none_on_bad_reference(self):
        request = self.factory.post('/fake-url/', data={'merchant_reference': 'bad-format'})
        self.view.request = request
        assert self.view.site is None

    def test_cart_returns_none_if_merchant_reference_missing(self):
        request = self.factory.post('/fake-url/', data={})
        self.view.request = request
        assert self.view.cart is None

    def test_site_returns_none_if_merchant_reference_missing(self):
        request = self.factory.post('/fake-url/', data={})
        self.view.request = request
        assert self.view.site is None

    def test_cart_returns_none_if_request_missing(self):
        self.view.request = None
        assert self.view.cart is None

    def test_site_returns_none_if_request_missing(self):
        self.view.request = None
        assert self.view.site is None


@pytest.mark.usefixtures('base_data')
class PayfortFeedbackTestView(TestCase):
    """Payfort feedback test case."""

    def setUp(self) -> None:
        """
        Set up test data for the Payfort feedback tests.

        :return: None
        """
        self.user = User.objects.get(id=3)
        self.cart = Cart.objects.create(user=self.user, status=Cart.Status.PROCESSING)
        self.course_mode = CourseMode.objects.get(sku='custom-sku-1')
        self.course_item = CatalogueItem.objects.get(sku='custom-sku-1')
        self.cart.items.create(
            catalogue_item=self.course_item,
            original_price=self.course_item.price,
            final_price=self.course_item.price,
        )
        self.site = Site.objects.create(name='test.com', domain='test.com')
        self.provider = 'payfort'
        self.url = reverse('zeitlabs_payments:payfort:return')

        self.valid_response = {
            'amount': '150',
            'response_code': '14000',
            'acquirer_response_message': 'Success',
            'card_number': '411111******1111',
            'card_holder_name': 'Tehreem',
            'signature': '141ae3d36be4f7f50cefbb966b26c8ee073a84dd32d99eec3084d19efb247895',
            'merchant_identifier': 'abcdi',
            'access_code': 'm6ScifP9737ykbx31Z7i',
            'order_description': 'some order description',
            'payment_option': 'VISA',
            'expiry_date': '2511',
            'customer_ip': '101.53.219.17',
            'language': 'en',
            'eci': 'ECOMMERCE',
            'fort_id': '169996200024611493',
            'command': 'PURCHASE',
            'response_message': 'Success',
            'merchant_reference': f'{self.site.id}-{self.cart.id}',
            'authorization_code': '742138',
            'customer_email': 'tehreemsadat19@gmail.com',
            'currency': 'SAR',
            'acquirer_response_code': '00',
            'status': '14',
        }
        self.request_factory = RequestFactory()

    def test_post_for_invalid_cart_in_merchant_ref(self) -> None:
        """
        Test that posting with an invalid cart ID in merchant_reference raises PayFortException.

        :return: None
        """
        data = self.valid_response.copy()
        data.update({'merchant_reference': '1-10000'})
        request = self.request_factory.post(self.url, data)
        request.user = self.user
        response = PayfortFeedbackView.as_view()(request)
        assert response.status_code == 400

    def test_post_for_invalid_site_in_merchant_ref(self) -> None:
        """
        Test that posting with an invalid site ID in merchant_reference raises PayFortException.

        :return: None
        """
        data = self.valid_response.copy()
        data.update({'merchant_reference': f'10000-{self.cart.id}'})
        request = self.request_factory.post(self.url, data)
        request.user = self.user
        response = PayfortFeedbackView.as_view()(request)
        assert response.status_code == 400

    def test_post_for_invalid_signature(self) -> None:
        """
        Test post request with an invalid signature.

        :return: None
        """
        data = self.valid_response.copy()
        request = self.request_factory.post(self.url, data)
        request.user = self.user
        assert not AuditLog.objects.filter(
            action=AuditLog.AuditActions.BAD_RESPONSE_SIGNATURE,
            cart=self.cart,
            gateway='payfort'
        ).exists(), 'AuditLog for BAD_RESPONSE_SIGNATURE with PayFort gateway should not existbefore fucntion call.'

        response = PayfortFeedbackView.as_view()(request)
        assert response.status_code == 400
        assert AuditLog.objects.filter(
            action=AuditLog.AuditActions.BAD_RESPONSE_SIGNATURE,
            cart=self.cart,
            gateway='payfort'
        ).exists(), 'AuditLog for BAD_RESPONSE_SIGNATURE with PayFort gateway and the given cart should exist'

    @pytest.mark.django_db
    @patch('zeitlabs_payments.providers.payfort.views.verify_signature')
    def test_post_for_cart_not_in_processing_state(
        self, mock_verify_signature  # pylint: disable=unused-argument
    ) -> None:
        """
        Test that posting with a cart not in PROCESSING state raises PayFortException.

        :return: None
        """
        self.cart.status = Cart.Status.PENDING
        self.cart.save()
        request = self.request_factory.post(self.url, self.valid_response)
        request.user = self.user
        assert not AuditLog.objects.filter(gateway='payfort', action='ResponseForInvalidCart').exists()
        response = PayfortFeedbackView.as_view()(request)
        assert AuditLog.objects.filter(gateway='payfort', action='ResponseForInvalidCart').exists()
        assert response.status_code == 200

    @patch('zeitlabs_payments.providers.payfort.views.logger')
    @patch('zeitlabs_payments.providers.payfort.views.verify_signature')
    def test_post_for_unsuccessful_payment(
        self, mock_verify_signature, mock_logger  # pylint: disable=unused-argument
    ) -> None:
        """
        Test handling of unsuccessful payment status.

        :param mock_render: mocked render function
        :return: None
        """
        data = self.valid_response.copy()
        data.update({'status': '20'})
        request = self.request_factory.post(self.url, data)
        request.user = self.user
        response = PayfortFeedbackView.as_view()(request)
        mock_logger.warning.assert_called_with(
            f'PayFort payment unsuccessful. Status: 20, Data: {data}'
        )
        assert response.status_code == 200

    @patch('zeitlabs_payments.providers.payfort.views.logger.error')
    @patch('zeitlabs_payments.providers.payfort.views.verify_signature')
    def test_post_for_success_payment_enroll_error_no_course_mode(
        self, mock_verify_signature, mock_logger  # pylint: disable=unused-argument
    ) -> None:
        """
        Test successful payment but course mode missing, triggers error logging and error page.

        :param mock_logger: mocked logger.error function
        :param mock_render: mocked render function
        :return: None
        """
        assert not Transaction.objects.filter(gateway='payfort', cart=self.cart).exists(), \
            'Transaction should not exist before test'
        assert self.cart.status == Cart.Status.PROCESSING, \
            'Cart should be in PROCESSING state'

        self.course_mode.delete()
        request = self.request_factory.post(self.url, self.valid_response)
        request.user = self.user
        response = PayfortFeedbackView.as_view()(request)

        assert Transaction.objects.filter(gateway='payfort', cart=self.cart).exists(), \
            'Transaction should exist after payment'
        self.cart.refresh_from_db()
        assert self.cart.status == Cart.Status.PAID, \
            'Cart status should be PAID after successful payment'

        mock_logger.assert_called_with(
            f'Failed to fulfill cart {self.cart.id} or to create invoice: CourseMode not found'
        )
        assert response.status_code == 200

    @patch('zeitlabs_payments.providers.payfort.views.verify_signature')
    def test_post_success_for_rolled_back_of_tables_on_handle_payment_error(
        self, mock_verify_signature  # pylint: disable=unused-argument
    ) -> None:
        """
        Test successful payment but course mode missing, triggers error logging and error page.

        :param mock_logger: mocked logger.error function
        :param mock_render: mocked render function
        :return: None
        """
        assert not Transaction.objects.filter(gateway='payfort', cart=self.cart).exists(), \
            'Transaction should not exist before test'
        assert not WebhookEvent.objects.filter(
            gateway='payfort',
            event_type='direct-feedback'
        ).exists(), \
            'WebhookEvent should not exist before test'
        assert self.cart.status == Cart.Status.PROCESSING, \
            'Cart should be in PROCESSING state'

        request = self.request_factory.post(self.url, self.valid_response)
        request.user = self.user

        with patch(
            'zeitlabs_payments.providers.base.WebhookEvent.objects.create',
            side_effect=Exception('Unknown exception')
        ):

            response = PayfortFeedbackView.as_view()(request)

            assert not Transaction.objects.filter(gateway='payfort', cart=self.cart).exists(), \
                'Transaction should not exist after test'
            assert not WebhookEvent.objects.filter(
                gateway='payfort',
                event_type='direct-feedback'
            ).exists(), \
                'WebhookEVent should not exist after test'
            assert self.cart.status == Cart.Status.PROCESSING, \
                'Cart should not be changed and should be in PROCESSING state'
            assert response.status_code == 200

    @patch('zeitlabs_payments.providers.payfort.views.verify_signature')
    def test_post_success_for_duplicate_transaction(
        self, mock_verify_signature  # pylint: disable=unused-argument
    ) -> None:
        """
        Test successful payment but transaction already there with transaction_id received in response.
        """
        Transaction.objects.create(
            gateway='payfort',
            cart=self.cart,
            gateway_transaction_id=self.valid_response['fort_id'],
            amount=100
        )

        assert not AuditLog.objects.filter(
            action=AuditLog.AuditActions.DUPLICATE_TRANSACTION,
            cart=self.cart,
            gateway='payfort'
        ).exists()
        assert self.cart.status == Cart.Status.PROCESSING, \
            'Cart should be in PROCESSING state'

        request = self.request_factory.post(self.url, self.valid_response)
        request.user = self.user
        response = PayfortFeedbackView.as_view()(request)

        assert AuditLog.objects.filter(
            action=AuditLog.AuditActions.DUPLICATE_TRANSACTION,
            cart=self.cart,
            gateway='payfort'
        ).exists()
        assert self.cart.status == Cart.Status.PROCESSING, \
            'Cart status should not be changed.'
        assert response.status_code == 200

    @patch('zeitlabs_payments.providers.payfort.views.logger.error')
    @patch('zeitlabs_payments.cart_handler.CourseEnrollment.enroll')
    @patch('zeitlabs_payments.providers.payfort.views.verify_signature')
    def test_post_for_success_payment_paid_course_with_unsuccessful_enrollment(
        self, mock_verify_signature, mock_enroll, mock_logger  # pylint: disable=unused-argument
    ) -> None:
        """
        Test payment success but enrollment fails, logs exception and shows error page.

        :param mock_enroll: mocked CourseEnrollment.enroll method
        :param mock_logger: mocked logger.exception function
        :param mock_render: mocked render function
        :return: None
        """
        mock_enroll.side_effect = Exception('Unexpected error during enrollment')
        assert not Transaction.objects.filter(gateway='payfort', cart=self.cart).exists(), \
            'Transaction should not exist before test'
        assert self.cart.status == Cart.Status.PROCESSING, \
            'Cart should be in PROCESSING state'

        request = self.request_factory.post(self.url, self.valid_response)
        request.user = self.user
        response = PayfortFeedbackView.as_view()(request)

        assert Transaction.objects.filter(gateway='payfort', cart=self.cart).exists(), \
            'Transaction should exist after payment'
        self.cart.refresh_from_db()
        assert self.cart.status == Cart.Status.PAID, \
            'Cart status should be PAID after successful payment'

        mock_logger.assert_called_with(
            f'Failed to fulfill cart {self.cart.id} or to create invoice: Unexpected error during enrollment'
        )
        assert response.status_code == 200

    @pytest.mark.django_db
    @patch('zeitlabs_payments.providers.payfort.views.verify_signature')
    def test_post_for_successful_payment(
        self, mock_verify_signature  # pylint: disable=unused-argument
    ) -> None:
        """
        Test the full successful payment flow and enrollment.

        :param mock_render: mocked render function
        :return: None
        """
        assert not Transaction.objects.filter(gateway='payfort', cart=self.cart).exists(), \
            'Transaction should not exist before test'
        assert self.cart.status == Cart.Status.PROCESSING, \
            'Cart should be in PROCESSING state'
        assert not CourseEnrollment.objects.filter(
            user=self.cart.user, course=self.course_mode.course
        ).exists(), 'User should not be enrolled before test'

        request = self.request_factory.post(self.url, self.valid_response)
        request.user = self.user
        response = PayfortFeedbackView.as_view()(request)

        assert Transaction.objects.filter(gateway='payfort', cart=self.cart).exists(), \
            'Transaction should exist after payment'
        self.cart.refresh_from_db()
        assert self.cart.status == Cart.Status.PAID, \
            'Cart status should be PAID after payment'
        assert CourseEnrollment.objects.filter(
            user=self.cart.user, course=self.course_mode.course
        ).exists(), 'User should be enrolled after payment'

        assert response.status_code == 200

    @pytest.mark.django_db
    @patch('zeitlabs_payments.providers.payfort.views.verify_signature')
    @patch('zeitlabs_payments.providers.payfort.views.logger.error')
    def test_post_for_success_payment_cart_with_unsupported_item(
        self, mock_logger, mock_verify_signature  # pylint: disable=unused-argument
    ) -> None:
        """
        Test successful payment but cart contains unsupported item, triggers error logging.

        :param mock_logger: mocked logger.exception function
        :param mock_render: mocked render function
        :return: None
        """
        assert not Transaction.objects.filter(gateway='payfort', cart=self.cart).exists(), \
            'Transaction should not exist before test'
        assert self.cart.status == Cart.Status.PROCESSING, \
            'Cart should be in PROCESSING state'
        unsupported_item = CatalogueItem.objects.create(sku='abcd', type='unsupported', price=50)
        self.cart.items.all().delete()
        self.cart.items.create(
            catalogue_item=unsupported_item,
            original_price=unsupported_item.price,
            final_price=unsupported_item.price,
        )

        request = self.request_factory.post(self.url, self.valid_response)
        request.user = self.user
        response = PayfortFeedbackView.as_view()(request)

        assert Transaction.objects.filter(gateway='payfort', cart=self.cart).exists(), \
            'Transaction should exist after payment'
        self.cart.refresh_from_db()
        assert self.cart.status == Cart.Status.PAID, \
            'Cart status should be PAID after payment'

        mock_logger.assert_called_with(
            f'Failed to fulfill cart {self.cart.id} or to create invoice: Unsupported catalogue item type: unsupported'
        )
        assert response.status_code == 200


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
class PayFortStatusViewTest(APITestCase):
    """Tests for PayFortStatusView"""

    def setUp(self):
        """Setup"""
        self.user = User.objects.get(id=3)
        self.cart = Cart.objects.create(user=self.user, status=Cart.Status.PROCESSING)
        self.course_mode = CourseMode.objects.get(sku='custom-sku-1')
        self.course_item = CatalogueItem.objects.get(sku='custom-sku-1')
        self.cart.items.create(
            catalogue_item=self.course_item,
            original_price=self.course_item.price,
            final_price=self.course_item.price,
        )
        self.site = Site.objects.create(name='test.com', domain='test.com')
        self.url = reverse('zeitlabs_payments:payfort:status')

    def login_user(self, user):
        """Helper to login user"""
        self.client.force_login(user)

    def test_unauthorized(self):
        """Verify that the view returns 404 when the user is not authenticated"""
        response = self.client.post(self.url, data={})
        self.assertEqual(response.status_code, 404)

    def test_post_failed_for_invalid_merchant_ref(self):
        """Cart could not be found"""
        self.login_user(self.user)
        response = self.client.post(self.url, data={
            'merchant_reference': '1111-2222',
            'transaction_id': '1234'
        })
        assert response.status_code == 404
        assert response.json()['error'] == 'Unable to retrieve cart.'

    def test_post_failed_for_missing_merchant_ref(self):
        """Missing merchant reference"""
        self.login_user(self.user)
        response = self.client.post(self.url, data={'transaction_id': '1234'})
        assert response.status_code == 404
        assert response.json()['error'] == 'Unable to retrieve cart.'

    def test_post_failed_for_missing_transaction_id(self):
        """Missing transaction_id"""
        self.login_user(self.user)
        response = self.client.post(self.url, data={'merchant_reference': '1-1'})
        assert response.status_code == 400
        assert response.json()['error'] == 'Transaction id is required to verify payment status.'

    def test_paid_cart_with_invoice(self):
        """Cart is PAID and invoice exists"""
        self.login_user(self.user)
        self.cart.status = Cart.Status.PAID
        self.cart.save()

        transaction = Transaction.objects.create(
            gateway_transaction_id='tx123',
            gateway='payfort',
            amount=self.course_item.price,
        )

        Invoice.objects.create(
            cart=self.cart,
            status=Invoice.InvoiceStatus.PAID,
            invoice_number='DEV-100',
            related_transaction=transaction,
            total=self.course_item.price
        )

        response = self.client.post(self.url, data={
            'merchant_reference': f'{self.site.id}-{self.cart.id}',
            'transaction_id': 'tx123'
        })
        assert response.status_code == 200
        data = response.json()
        assert data['invoice'] == 'DEV-100'
        assert data['invoice_url'] == reverse(
            'zeitlabs_payments:invoice',
            args=['DEV-100']
        )

    def test_paid_cart_without_invoice(self):
        """Cart is PAID but no invoice found"""
        self.login_user(self.user)
        self.cart.status = Cart.Status.PAID
        self.cart.save()

        response = self.client.post(self.url, data={
            'merchant_reference': f'{self.site.id}-{self.cart.id}',
            'transaction_id': 'does-npt-matter'
        })
        assert response.status_code == 204

    def test_processing_cart(self):
        """Cart in PROCESSING status"""
        self.login_user(self.user)
        response = self.client.post(self.url, data={
            'merchant_reference': f'{self.site.id}-{self.cart.id}',
            'transaction_id': 'does-npt-matter'
        })
        assert response.status_code == 204

    def test_unknown_cart_status(self):
        """Cart in unknown status"""
        self.login_user(self.user)
        self.cart.status = 'UNKNOWN'
        self.cart.save()
        response = self.client.post(self.url, data={
            'merchant_reference': f'{self.site.id}-{self.cart.id}',
            'transaction_id': 'does-not-matter'
        })
        assert response.status_code == 404
        data = response.json()
        assert data['error'] == f'cart is in status: {self.cart.status}.'


class PayFortReturnViewTest(TestCase):
    """PayFortReturnView Tests."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.get(id=3)
        self.cart = Cart.objects.create(user=self.user, status=Cart.Status.PROCESSING)
        self.course_mode = CourseMode.objects.get(sku='custom-sku-1')
        self.course_item = CatalogueItem.objects.get(sku='custom-sku-1')
        self.cart.items.create(
            catalogue_item=self.course_item,
            original_price=self.course_item.price,
            final_price=self.course_item.price,
        )
        self.site = Site.objects.create(name='test.com', domain='test.com')
        self.client = Client()

    def test_missing_signature(self):
        data = {
            'other': '1234',
            'merchant_reference': f'{self.site.id}-{self.cart.id}',
        }

        assert not AuditLog.objects.filter(
            action=AuditLog.AuditActions.BAD_RESPONSE_SIGNATURE,
            cart=self.cart,
            gateway='payfort',
        ).exists()

        response = self.client.post(reverse('zeitlabs_payments:payfort:return'), data)

        assert response.status_code == 200
        assert AuditLog.objects.filter(
            action=AuditLog.AuditActions.BAD_RESPONSE_SIGNATURE,
            cart=self.cart,
            gateway='payfort',
            details=(
                "Bad response signature detected: {'other': '1234', 'merchant_reference': '2-1'}."
            )
        ).exists()
        self.assertTemplateUsed(response, 'zeitlabs_payments/payment_error.html')

    def test_bad_signature_renders_error_page(self):
        data = {
            'other': '1234',
            'merchant_reference': f'{self.site.id}-{self.cart.id}',
            'signature': 'invalid'
        }

        assert not AuditLog.objects.filter(
            action=AuditLog.AuditActions.BAD_RESPONSE_SIGNATURE,
            cart=self.cart,
            gateway='payfort',
        ).exists()

        response = self.client.post(reverse('zeitlabs_payments:payfort:return'), data)

        assert response.status_code == 200
        assert AuditLog.objects.filter(
            action=AuditLog.AuditActions.BAD_RESPONSE_SIGNATURE,
            cart=self.cart,
            gateway='payfort',
            details=(
                "Bad response signature detected: {'other': '1234', 'merchant_reference': '2-1', "
                "'signature': 'invalid'}."
            )
        ).exists()
        self.assertTemplateUsed(response, 'zeitlabs_payments/payment_error.html')

    @patch('zeitlabs_payments.providers.payfort.views.logger.error')
    def test_missing_merchant_identifier(self, mock_error_log):
        data = {'other': '1234'}

        params_copy = data.copy()
        sorted_keys = sorted(params_copy, key=lambda arg: arg.lower())
        sorted_dict = {key: params_copy[key] for key in sorted_keys}
        result_string = (
            f"test-response-phrase{''.join(f'{key}={value}' for key, value in sorted_dict.items())}"
            'test-response-phrase'
        )
        expected_signature = hashlib.sha256(result_string.encode()).hexdigest()

        data.update({'signature': expected_signature})
        response = self.client.post(reverse('zeitlabs_payments:payfort:return'), data)
        mock_error_log.assert_called_with(
            'Payfort payment failed! with merchant_reference: None, status: None and response_code: None'
        )
        self.assertTemplateUsed(response, 'zeitlabs_payments/payment_error.html')

    @patch('zeitlabs_payments.providers.payfort.views.logger.error')
    def test_post_for_failed_status(self, mock_error_log):
        data = {
            'other': '1234',
            'status': 'not-success',
            'response_code': 11
        }

        params_copy = data.copy()
        sorted_keys = sorted(params_copy, key=lambda arg: arg.lower())
        sorted_dict = {key: params_copy[key] for key in sorted_keys}
        result_string = (
            f"test-response-phrase{''.join(f'{key}={value}' for key, value in sorted_dict.items())}"
            'test-response-phrase'
        )
        expected_signature = hashlib.sha256(result_string.encode()).hexdigest()

        data.update({'signature': expected_signature})
        response = self.client.post(reverse('zeitlabs_payments:payfort:return'), data)
        mock_error_log.assert_called_with(
            'Payfort payment failed! with merchant_reference: None, status: not-success and response_code: 11'
        )
        self.assertTemplateUsed(response, 'zeitlabs_payments/payment_error.html')

    def test_post_for_verify_format_failiure(self):
        data = {
            'other': '1234',
            'status': '14',
            'response_code': 14
        }

        params_copy = data.copy()
        sorted_keys = sorted(params_copy, key=lambda arg: arg.lower())
        sorted_dict = {key: params_copy[key] for key in sorted_keys}
        result_string = (
            f"test-response-phrase{''.join(f'{key}={value}' for key, value in sorted_dict.items())}"
            'test-response-phrase'
        )
        expected_signature = hashlib.sha256(result_string.encode()).hexdigest()

        data.update({'signature': expected_signature})
        response = self.client.post(reverse('zeitlabs_payments:payfort:return'), data)
        self.assertTemplateUsed(response, 'zeitlabs_payments/payment_error.html')

    def test_post_success(self):
        data = {
            'amount': '150',
            'response_code': '14000',
            'merchant_identifier': 'abcdi',
            'fort_id': '123456',
            'command': 'PURCHASE',
            'response_message': 'Success',
            'merchant_reference': f'{self.site.id}-{self.cart.id}',
            'currency': 'SAR',
            'status': '14',
            'eci': 'test'
        }

        params_copy = data.copy()
        sorted_keys = sorted(params_copy, key=lambda arg: arg.lower())
        sorted_dict = {key: params_copy[key] for key in sorted_keys}
        result_string = (
            f"test-response-phrase{''.join(f'{key}={value}' for key, value in sorted_dict.items())}"
            'test-response-phrase'
        )
        expected_signature = hashlib.sha256(result_string.encode()).hexdigest()

        data.update({'signature': expected_signature})

        response = self.client.post(reverse('zeitlabs_payments:payfort:return'), data)
        self.assertTemplateUsed(response, 'zeitlabs_payments/wait_feedback.html')
        assert response.context['ecommerce_transaction_id'] == '123456'
        assert response.context['ecommerce_status_url'] == reverse('zeitlabs_payments:payfort:status')
        assert response.context['ecommerce_error_url'] == reverse(
            'zeitlabs_payments:payment-error',
            args=[data['fort_id']]
        )
        assert response.context['ecommerce_success_url'] == reverse(
            'zeitlabs_payments:payment-success',
            args=[data['fort_id']]
        )
        assert response.context['ecommerce_max_attempts'] == 24
        assert response.context['ecommerce_wait_time'] == 5000
        for key, value in data.items():
            assert response.context[key] == value
