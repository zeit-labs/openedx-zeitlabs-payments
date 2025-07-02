"""Fullment related tests."""

from unittest.mock import MagicMock, patch

import pytest
from common.djangoapps.course_modes.models import CourseMode
from common.djangoapps.student.models import CourseEnrollmentException
from django.contrib.auth import get_user_model

from zeitlabs_payments.exceptions import CartFulfillmentError, InvalidCartError
from zeitlabs_payments.fulfillment import BaseFulfillmentStrategy, PaidCourseFulfillment
from zeitlabs_payments.models import AuditLog, Cart, CatalogueItem

User = get_user_model()


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
class TestBaseFulfillmentStrategy:
    """
    Tests for BaseFulfillmentStrategy.
    """
    strategy = BaseFulfillmentStrategy()
    cart_item = MagicMock()
    user = None
    catalogue_item = None

    def setup_method(self):
        self.strategy = BaseFulfillmentStrategy()
        self.cart_item = MagicMock()

    def test_validate_add_to_cart_does_nothing(self):
        """
        Should not raise anything: default implementation is empty.
        """
        # Should just run without exception
        self.strategy.validate_add_to_cart(self.user, self.catalogue_item)

    def test_fulfill_raises_not_implemented(self):
        """
        Should always raise NotImplementedError.
        """
        with pytest.raises(NotImplementedError, match='Subclasses must implement this.'):
            self.strategy.fulfill(self.cart_item, processor_slug='dummy')


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
class TestPaidCourseFulfillment:
    """
    Tests for PaidCourseFulfillment.
    """
    fulfillment = learner_user = valid_catalog_item = valid_cart = valid_cart_item = course_mode = None

    def setup_method(self):
        """setup."""
        self.fulfillment = PaidCourseFulfillment()
        self.learner_user = User.objects.get(id=3)
        self.valid_catalog_item = CatalogueItem.objects.get(sku='custom-sku-1')

        self.valid_cart = Cart.objects.create(user=self.learner_user, status=Cart.Status.PROCESSING)
        self.valid_cart_item = self.valid_cart.items.create(
            catalogue_item=self.valid_catalog_item,
            original_price=self.valid_catalog_item.price,
            final_price=self.valid_catalog_item.price
        )
        self.course_mode = CourseMode.objects.get(sku=self.valid_catalog_item.sku)

    def test_validate_add_to_cart_success(self):
        """
        Should not raise when CourseMode exists and all course conditions pass.
        """
        self.fulfillment.validate_add_to_cart(self.learner_user, self.valid_catalog_item)

    @pytest.mark.parametrize(
        'sku, expected_msg',
        [
            (
                'custom-sku-with_invlaid_ref_id',
                'Unable to add item to the cart as Course mode found with given sku but course_id'
                ' mismatch with catalogue item ref-id.'
            ),
            (
                'sku-does-not-match-to-any-course-mode',
                'Unable to add item to the cart as CourseMode not found'
            ),
        ],
    )
    def test_validate_add_to_cart_invalid_cases(self, sku, expected_msg):
        """
        Should raise InvalidCartError for invalid ref_id or invalid sku.
        """
        if 'does-not-match' in sku:
            # dynamically create the item with invalid sku
            course_item = CatalogueItem.objects.create(
                sku=sku,
                type=CatalogueItem.ItemType.PAID_COURSE,
                title='Invalid SKU course',
                item_ref_id='irrelevant',
                price=100,
                currency='SAR'
            )
        else:
            course_item = CatalogueItem.objects.get(sku=sku)

        with pytest.raises(InvalidCartError, match=expected_msg):
            self.fulfillment.validate_add_to_cart(self.learner_user, course_item)

    @pytest.mark.parametrize(
        'patch_target, return_value, expected_msg',
        [
            (
                'zeitlabs_payments.helpers.CourseEnrollment.is_enrolled',
                True,
                'Unable to add item to the cart as user: user3 does not fulfill enrollment conditions. '
                'User is already enrolled in the course.'
            ),
            (
                'zeitlabs_payments.helpers.CourseEnrollment.is_course_full',
                True,
                'Unable to add item to the cart as user: user3 does not fulfill enrollment conditions. '
                'Course is Full.'
            ),
            (
                'zeitlabs_payments.helpers.CourseEnrollment.is_enrollment_closed',
                True,
                'Unable to add item to the cart as user: user3 does not fulfill enrollment conditions. '
                'Enrollment is closed.'
            ),
        ],
    )
    def test_validate_add_to_cart_enrollment_failures(self, patch_target, return_value, expected_msg):
        """
        Should raise InvalidCartError when enrollment conditions fail (user enrolled, course full, enrollment closed).
        """
        # For course_full case, make sure course_overview.max_student_enrollments_allowed is set
        if 'is_course_full' in patch_target:
            course_overview = self.course_mode.course
            course_overview.max_student_enrollments_allowed = 2
            course_overview.save()

        with patch(patch_target, return_value=return_value):
            with pytest.raises(InvalidCartError, match=expected_msg):
                self.fulfillment.validate_add_to_cart(self.learner_user, self.valid_catalog_item)

    @patch('zeitlabs_payments.fulfillment.CourseEnrollment.enroll')
    def test_fulfill_success(self, mock_enroll):
        """
        Should enroll user successfully and log events.
        """
        assert not AuditLog.objects.filter(
            action=AuditLog.AuditActions.USER_ENROLLED,
            cart=self.valid_cart,
            details=(
                'User enrolled to the course: course-v1:org1+1+1 with mode: no-id-professional '
                'during cart fulfillment for catalogue_item: 1.'
            )
        ).exists()
        self.fulfillment.fulfill(self.valid_cart_item, 'processor')
        mock_enroll.assert_called_once_with(
            self.learner_user,
            self.course_mode.course.id,
            mode='no-id-professional',
            check_access=True
        )
        assert AuditLog.objects.filter(
            action=AuditLog.AuditActions.USER_ENROLLED,
            cart=self.valid_cart,
            details=(
                'User enrolled to the course: course-v1:org1+1+1 with mode: no-id-professional '
                'during cart fulfillment for catalogue_item: 1.'
            )
        ).exists()

    def test_fulfill_course_mode_not_found(self):
        """
        Should log and raise CartFulfillmentError when CourseMode is missing.
        """
        self.course_mode.delete()
        with pytest.raises(CartFulfillmentError, match='CourseMode not found'):
            self.fulfillment.fulfill(self.valid_cart_item, 'processor')

        assert AuditLog.objects.filter(
            action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR,
            cart=self.valid_cart,
            details=(
                f'Error during cart fulfillment for item: {self.valid_cart_item.id}, catalogue_item:'
                f' {self.valid_catalog_item.id} due to invalid SKU: {self.valid_catalog_item.sku} or unsupported type.'
            )
        ).exists()

    def test_fulfill_course_mode_found_but_invlaid_ref_id(self):
        """
        Should log and raise CartFulfillmentError when ref_id does not match with course mode.
        """
        self.valid_catalog_item.item_ref_id = 'invalid-does-not-match-with-course-mode'
        with pytest.raises(CartFulfillmentError, match='Course Mode found but item ref id mismatched.'):
            self.fulfillment.fulfill(self.valid_cart_item, 'processor')

        assert AuditLog.objects.filter(
            action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR,
            cart=self.valid_cart,
            details=(
                f'Error during cart fulfillment for item: {self.valid_cart_item.id}, catalogue_item:'
                f' {self.valid_catalog_item.id} due to invalid SKU: {self.valid_catalog_item.sku} or unsupported type.'
            )
        ).exists()

    @patch('zeitlabs_payments.fulfillment.CourseEnrollment.enroll')
    def test_fulfill_enrollment_fails(self, mock_enroll):
        """
        Should log and raise CartFulfillmentError when enrollment fails.
        """
        mock_enroll.side_effect = CourseEnrollmentException('Enrollment failed')

        with pytest.raises(CartFulfillmentError, match='Unexpected enrollment error'):
            self.fulfillment.fulfill(self.valid_cart_item, 'processor')

        assert AuditLog.objects.filter(
            action=AuditLog.AuditActions.USER_ENROLLED_ERROR,
            cart=self.valid_cart,
            details=(
                'Unable to complete user enrollment to course: course-v1:org1+1+1 with mode: no-id-professional '
                'during cart fulfillment for catalogue_item: 1.'
            )
        ).exists()
