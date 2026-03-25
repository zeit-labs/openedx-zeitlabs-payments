"""Fullment related tests."""

from unittest.mock import MagicMock, patch

import pytest
from common.djangoapps.course_modes.models import CourseMode
from common.djangoapps.student.models import CourseEnrollmentException
from django.contrib.auth import get_user_model

from zeitlabs_payments.cart_handler import BaseCartHandler, PaidCourseCartHandler, ProgramBundleCartHandler
from zeitlabs_payments.exceptions import CartFulfillmentError, InvalidCartError
from zeitlabs_payments.models import AuditLog, BundleCourseItem, Cart, CatalogueItem

User = get_user_model()


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
class TestBaseCartHandler:
    """
    Tests for BaseCartHandler.
    """

    strategy = BaseCartHandler()
    cart_item = MagicMock()
    user = None
    catalogue_item = None

    def setup_method(self):
        self.strategy = BaseCartHandler()
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
class TestPaidCourseCartHandler:
    """
    Tests for PaidCourseCartHandler.
    """

    fulfillment = learner_user = catalog_item = course_mode = None

    def setup_method(self):
        """setup."""
        self.fulfillment = PaidCourseCartHandler()
        self.learner_user = User.objects.get(id=3)
        self.catalog_item = CatalogueItem.objects.get(sku='custom-sku-1')
        self.course_mode = CourseMode.objects.get(sku=self.catalog_item.sku)

    def test_validate_add_to_cart_success(self):
        """
        Should not raise when CourseMode exists and all course conditions pass.
        """
        self.fulfillment.validate_add_to_cart(self.learner_user, self.catalog_item)

    @pytest.mark.parametrize(
        'sku, expected_msg',
        [
            (
                'custom-sku-with_invlaid_ref_id',
                'Unable to add item to the cart as Course mode found with given sku but course_id'
                ' mismatch with catalogue item ref-id.',
            ),
            (
                'sku-does-not-match-to-any-course-mode',
                'Unable to add item to the cart as CourseMode not found',
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
                currency='SAR',
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
                'User is already enrolled in the course.',
            ),
            (
                'zeitlabs_payments.helpers.CourseEnrollment.objects.is_course_full',
                True,
                'Unable to add item to the cart as user: user3 does not fulfill enrollment conditions. Course is Full.',
            ),
            (
                'zeitlabs_payments.helpers.CourseEnrollment.is_enrollment_closed',
                True,
                'Unable to add item to the cart as user: user3 does not fulfill enrollment conditions. '
                'Enrollment is closed.',
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
                self.fulfillment.validate_add_to_cart(self.learner_user, self.catalog_item)

    def test_validate_add_to_cart_duplicate_cart_failure(self):
        """
        Should raise InvalidCartError when there is existing cart with smae course.
        """
        existing_cart = Cart.objects.create(user=self.learner_user, status=Cart.Status.PAYMENT_PENDING)
        existing_cart.items.create(
            catalogue_item=self.catalog_item,
            original_price=self.catalog_item.price,
            final_price=self.catalog_item.price,
        )
        assert existing_cart.status == Cart.Status.PAYMENT_PENDING
        with pytest.raises(InvalidCartError) as exc:
            self.fulfillment.validate_add_to_cart(self.learner_user, self.catalog_item)
        assert str(exc.value) == (
            'Unable to add item to the cart as user has existing cart with same course. '
            f'Duplicate cart found ID: {existing_cart.id}, state: {Cart.Status.PAYMENT_PENDING}.'
        )

        existing_cart.status = Cart.Status.PENDING
        existing_cart.save()

        # should not raise exception as there is no duplicate cart exist in processing state.
        self.fulfillment.validate_add_to_cart(self.learner_user, self.catalog_item)

    @patch('zeitlabs_payments.cart_handler.CourseEnrollment.enroll')
    def test_fulfill_success(self, mock_enroll):
        """
        Should enroll user successfully and log events.
        """
        cart = Cart.objects.create(user=self.learner_user, status=Cart.Status.PROCESSING)
        cart_item = cart.items.create(
            catalogue_item=self.catalog_item,
            original_price=self.catalog_item.price,
            final_price=self.catalog_item.price,
        )
        assert not AuditLog.objects.filter(
            action=AuditLog.AuditActions.USER_ENROLLED,
            cart=cart,
            details=(
                'User enrolled to the course: course-v1:org1+1+1 with mode: no-id-professional '
                'during cart fulfillment for catalogue_item: 1.'
            ),
        ).exists()
        self.fulfillment.fulfill(cart_item, 'processor')
        mock_enroll.assert_called_once_with(
            self.learner_user,
            self.course_mode.course.id,
            mode='no-id-professional',
            check_access=True,
        )
        assert AuditLog.objects.filter(
            action=AuditLog.AuditActions.USER_ENROLLED,
            cart=cart,
            details=(
                'User enrolled to the course: course-v1:org1+1+1 with mode: no-id-professional '
                'during cart fulfillment for catalogue_item: 1.'
            ),
        ).exists()

    def test_fulfill_course_mode_not_found(self):
        """
        Should log and raise CartFulfillmentError when CourseMode is missing.
        """
        cart = Cart.objects.create(user=self.learner_user, status=Cart.Status.PROCESSING)
        cart_item = cart.items.create(
            catalogue_item=self.catalog_item,
            original_price=self.catalog_item.price,
            final_price=self.catalog_item.price,
        )
        self.course_mode.delete()
        with pytest.raises(CartFulfillmentError, match='CourseMode not found'):
            self.fulfillment.fulfill(cart_item, 'processor')

        assert AuditLog.objects.filter(
            action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR,
            cart=cart,
            details=(
                f'Error during cart fulfillment for item: {cart_item.id}, catalogue_item:'
                f' {cart_item.id} due to invalid SKU: {self.catalog_item.sku} or unsupported type.'
            ),
        ).exists()

    def test_fulfill_course_mode_found_but_invlaid_ref_id(self):
        """
        Should log and raise CartFulfillmentError when ref_id does not match with course mode.
        """
        self.catalog_item.item_ref_id = 'invalid-does-not-match-with-course-mode'
        cart = Cart.objects.create(user=self.learner_user, status=Cart.Status.PROCESSING)
        cart_item = cart.items.create(
            catalogue_item=self.catalog_item,
            original_price=self.catalog_item.price,
            final_price=self.catalog_item.price,
        )
        with pytest.raises(CartFulfillmentError, match='Course Mode found but item ref id mismatched.'):
            self.fulfillment.fulfill(cart_item, 'processor')

        assert AuditLog.objects.filter(
            action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR,
            cart=cart,
            details=(
                f'Error during cart fulfillment for item: {cart_item.id}, catalogue_item:'
                f' {self.catalog_item.id} due to invalid SKU: {self.catalog_item.sku} or unsupported type.'
            ),
        ).exists()

    @patch('zeitlabs_payments.cart_handler.CourseEnrollment.enroll')
    def test_fulfill_enrollment_fails(self, mock_enroll):
        """
        Should log and raise CartFulfillmentError when enrollment fails.
        """
        cart = Cart.objects.create(user=self.learner_user, status=Cart.Status.PROCESSING)
        cart_item = cart.items.create(
            catalogue_item=self.catalog_item,
            original_price=self.catalog_item.price,
            final_price=self.catalog_item.price,
        )
        mock_enroll.side_effect = CourseEnrollmentException('Enrollment failed')

        with pytest.raises(CartFulfillmentError, match='Unexpected enrollment error'):
            self.fulfillment.fulfill(cart_item, 'processor')

        assert AuditLog.objects.filter(
            action=AuditLog.AuditActions.USER_ENROLLED_ERROR,
            cart=cart,
            details=(
                'Unable to complete user enrollment to course: course-v1:org1+1+1 with mode: no-id-professional '
                'during cart fulfillment for catalogue_item: 1.'
            ),
        ).exists()


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
class TestProgramBundleCartHandler:
    """
    Tests for ProgramBundleCartHandler.
    """

    handler = learner_user = bundle_item = empty_bundle_item = None

    def setup_method(self):
        """setup."""
        self.handler = ProgramBundleCartHandler()
        self.learner_user = User.objects.get(id=3)
        self.bundle_item = CatalogueItem.objects.get(sku='BUNDLE-PRO-CERT')
        self.empty_bundle_item = CatalogueItem.objects.get(sku='BUNDLE-EMPTY')

    # ── validate_add_to_cart ──

    def test_validate_add_to_cart_success(self):
        """Should not raise when bundle has valid courses and user passes all checks."""
        self.handler.validate_add_to_cart(self.learner_user, self.bundle_item)

    def test_validate_add_to_cart_empty_bundle(self):
        """Should raise InvalidCartError when bundle has no linked courses."""
        with pytest.raises(InvalidCartError, match='no courses linked to this bundle'):
            self.handler.validate_add_to_cart(self.learner_user, self.empty_bundle_item)

    def test_validate_add_to_cart_course_mode_not_found(self):
        """Should raise InvalidCartError when a constituent course has no matching CourseMode."""
        # Delete one course mode to simulate missing mode
        course_item = BundleCourseItem.objects.filter(bundle=self.bundle_item).first().course_item
        CourseMode.objects.filter(sku=course_item.sku).delete()

        with pytest.raises(
            InvalidCartError,
            match=f'Bundle course {course_item.sku}: CourseMode not found',
        ):
            self.handler.validate_add_to_cart(self.learner_user, self.bundle_item)

    def test_validate_add_to_cart_course_id_mismatch(self):
        """Should raise InvalidCartError when CourseMode course_id mismatches item_ref_id."""
        link = BundleCourseItem.objects.filter(bundle=self.bundle_item).first()
        course_item = link.course_item
        # Corrupt the item_ref_id
        original_ref = course_item.item_ref_id
        course_item.item_ref_id = 'course-v1:wrong+0+0'
        course_item.save()

        with pytest.raises(
            InvalidCartError,
            match=f'Bundle course {course_item.sku}: CourseMode course_id mismatch',
        ):
            self.handler.validate_add_to_cart(self.learner_user, self.bundle_item)

        # Restore
        course_item.item_ref_id = original_ref
        course_item.save()

    @pytest.mark.parametrize(
        'patch_target, return_value, expected_msg_fragment',
        [
            (
                'zeitlabs_payments.helpers.CourseEnrollment.is_enrolled',
                True,
                'does not fulfill enrollment conditions',
            ),
            (
                'zeitlabs_payments.helpers.CourseEnrollment.objects.is_course_full',
                True,
                'does not fulfill enrollment conditions',
            ),
            (
                'zeitlabs_payments.helpers.CourseEnrollment.is_enrollment_closed',
                True,
                'does not fulfill enrollment conditions',
            ),
        ],
    )
    def test_validate_add_to_cart_enrollment_failures(self, patch_target, return_value, expected_msg_fragment):
        """Should raise InvalidCartError when enrollment conditions fail for any constituent course."""
        # Ensure max_student_enrollments_allowed is set for course_full test
        if 'is_course_full' in patch_target:
            for link in BundleCourseItem.objects.filter(bundle=self.bundle_item).select_related('course_item'):
                course_mode = CourseMode.objects.get(sku=link.course_item.sku)
                course_mode.course.max_student_enrollments_allowed = 2
                course_mode.course.save()

        with patch(patch_target, return_value=return_value):
            with pytest.raises(InvalidCartError, match=expected_msg_fragment):
                self.handler.validate_add_to_cart(self.learner_user, self.bundle_item)

    def test_validate_add_to_cart_duplicate_cart_failure(self):
        """Should raise InvalidCartError when user has existing cart with a bundle course."""
        link = BundleCourseItem.objects.filter(bundle=self.bundle_item).first()
        course_item = link.course_item

        existing_cart = Cart.objects.create(user=self.learner_user, status=Cart.Status.PAYMENT_PENDING)
        existing_cart.items.create(
            catalogue_item=course_item,
            original_price=course_item.price,
            final_price=course_item.price,
        )
        with pytest.raises(InvalidCartError, match='user has existing cart with same course'):
            self.handler.validate_add_to_cart(self.learner_user, self.bundle_item)

    def test_validate_add_to_cart_overlapping_bundle_cart(self):
        """Should raise InvalidCartError when user has a pending cart with an overlapping bundle."""
        # Create another bundle that shares a course with self.bundle_item
        link = BundleCourseItem.objects.filter(bundle=self.bundle_item).first()
        shared_course_item = link.course_item

        other_bundle = CatalogueItem.objects.create(
            sku='BUNDLE-OTHER',
            type=CatalogueItem.ItemType.PROGRAM_BUNDLE,
            title='Other Bundle',
            item_ref_id='program-uuid-other',
            price=60,
            currency='SAR',
        )
        BundleCourseItem.objects.create(bundle=other_bundle, course_item=shared_course_item)

        # User has a pending cart with the other bundle
        existing_cart = Cart.objects.create(user=self.learner_user, status=Cart.Status.PAYMENT_PENDING)
        existing_cart.items.create(
            catalogue_item=other_bundle,
            original_price=other_bundle.price,
            final_price=other_bundle.price,
        )

        with pytest.raises(InvalidCartError, match='overlapping bundle purchase'):
            self.handler.validate_add_to_cart(self.learner_user, self.bundle_item)

        # Cleanup
        existing_cart.delete()
        BundleCourseItem.objects.filter(bundle=other_bundle).delete()
        other_bundle.delete()

    def test_validate_add_to_cart_bulk_sku_mismatch(self):
        """Should raise InvalidCartError when CourseMode bulk_sku does not match bundle SKU."""
        link = BundleCourseItem.objects.filter(bundle=self.bundle_item).first()
        course_mode = CourseMode.objects.get(sku=link.course_item.sku)

        # Set a mismatched bulk_sku
        course_mode.bulk_sku = 'WRONG-BUNDLE-SKU'
        course_mode.save()

        with pytest.raises(InvalidCartError, match='bulk_sku.*does not match bundle SKU'):
            self.handler.validate_add_to_cart(self.learner_user, self.bundle_item)

        # Restore
        course_mode.bulk_sku = ''
        course_mode.save()

    # ── fulfill ──

    @patch('zeitlabs_payments.cart_handler.CourseEnrollment.enroll')
    def test_fulfill_success(self, mock_enroll):
        """Should enroll user in all constituent courses and create audit logs."""
        cart = Cart.objects.create(user=self.learner_user, status=Cart.Status.PROCESSING)
        cart_item = cart.items.create(
            catalogue_item=self.bundle_item,
            original_price=self.bundle_item.price,
            final_price=self.bundle_item.price,
        )
        AuditLog.objects.all().delete()

        self.handler.fulfill(cart_item, 'processor')

        bundle_links = BundleCourseItem.objects.filter(bundle=self.bundle_item)
        assert mock_enroll.call_count == bundle_links.count()

        for link in bundle_links:
            course_mode = CourseMode.objects.get(sku=link.course_item.sku)
            mock_enroll.assert_any_call(
                self.learner_user,
                course_mode.course.id,
                mode=course_mode.mode_slug,
                check_access=True,
            )

        enrolled_logs = AuditLog.objects.filter(
            action=AuditLog.AuditActions.USER_ENROLLED,
            cart=cart,
        )
        assert enrolled_logs.count() == bundle_links.count()

    def test_fulfill_empty_bundle(self):
        """Should raise CartFulfillmentError when bundle has no linked courses."""
        cart = Cart.objects.create(user=self.learner_user, status=Cart.Status.PROCESSING)
        cart_item = cart.items.create(
            catalogue_item=self.empty_bundle_item,
            original_price=self.empty_bundle_item.price,
            final_price=self.empty_bundle_item.price,
        )

        with pytest.raises(CartFulfillmentError, match='No courses linked to this bundle'):
            self.handler.fulfill(cart_item, 'processor')

        assert AuditLog.objects.filter(
            action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR,
            cart=cart,
        ).exists()

    def test_fulfill_course_mode_not_found(self):
        """Should raise CartFulfillmentError when a constituent CourseMode is missing."""
        cart = Cart.objects.create(user=self.learner_user, status=Cart.Status.PROCESSING)
        cart_item = cart.items.create(
            catalogue_item=self.bundle_item,
            original_price=self.bundle_item.price,
            final_price=self.bundle_item.price,
        )
        # Delete all course modes for one course
        link = BundleCourseItem.objects.filter(bundle=self.bundle_item).first()
        CourseMode.objects.filter(sku=link.course_item.sku).delete()

        with pytest.raises(CartFulfillmentError, match='CourseMode not found for bundle course'):
            self.handler.fulfill(cart_item, 'processor')

        assert AuditLog.objects.filter(
            action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR,
            cart=cart,
        ).exists()

    def test_fulfill_course_id_mismatch(self):
        """Should raise CartFulfillmentError when CourseMode course_id mismatches item_ref_id."""
        cart = Cart.objects.create(user=self.learner_user, status=Cart.Status.PROCESSING)
        cart_item = cart.items.create(
            catalogue_item=self.bundle_item,
            original_price=self.bundle_item.price,
            final_price=self.bundle_item.price,
        )
        # Corrupt item_ref_id on first linked course
        link = BundleCourseItem.objects.filter(bundle=self.bundle_item).first()
        original_ref = link.course_item.item_ref_id
        link.course_item.item_ref_id = 'course-v1:wrong+0+0'
        link.course_item.save()

        with pytest.raises(CartFulfillmentError, match='item ref id mismatched'):
            self.handler.fulfill(cart_item, 'processor')

        assert AuditLog.objects.filter(
            action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR,
            cart=cart,
        ).exists()

        # Restore
        link.course_item.item_ref_id = original_ref
        link.course_item.save()

    @patch('zeitlabs_payments.cart_handler.CourseEnrollment.enroll')
    def test_fulfill_enrollment_error(self, mock_enroll):
        """Should raise CartFulfillmentError when enrollment raises CourseEnrollmentException."""
        cart = Cart.objects.create(user=self.learner_user, status=Cart.Status.PROCESSING)
        cart_item = cart.items.create(
            catalogue_item=self.bundle_item,
            original_price=self.bundle_item.price,
            final_price=self.bundle_item.price,
        )
        mock_enroll.side_effect = CourseEnrollmentException('Enrollment failed')

        with pytest.raises(CartFulfillmentError, match='Enrollment error for bundle course'):
            self.handler.fulfill(cart_item, 'processor')

        assert AuditLog.objects.filter(
            action=AuditLog.AuditActions.USER_ENROLLED_ERROR,
            cart=cart,
        ).exists()
