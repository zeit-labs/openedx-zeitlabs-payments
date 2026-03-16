"""Fullment related tests."""

from unittest.mock import MagicMock, patch

import pytest
from common.djangoapps.course_modes.models import CourseMode
from common.djangoapps.student.models import CourseEnrollmentException
from django.contrib.auth import get_user_model

from zeitlabs_payments.cart_handler import BaseCartHandler, PaidCourseCartHandler, validate_and_create_cart
from zeitlabs_payments.exceptions import CartFulfillmentError, InvalidCartError
from zeitlabs_payments.models import AuditLog, Cart, CatalogueItem, TaxRule

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
                'zeitlabs_payments.helpers.CourseEnrollment.objects.is_course_full',
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
                self.fulfillment.validate_add_to_cart(self.learner_user, self.catalog_item)

    def test_validate_add_to_cart_duplicate_cart_failure(self):
        """
        Should raise InvalidCartError when there is existing cart with smae course.
        """
        existing_cart = Cart.objects.create(user=self.learner_user, status=Cart.Status.PAYMENT_PENDING)
        existing_cart.items.create(
            catalogue_item=self.catalog_item,
            original_price=self.catalog_item.price,
            final_price=self.catalog_item.price
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
            final_price=self.catalog_item.price
        )
        assert not AuditLog.objects.filter(
            action=AuditLog.AuditActions.USER_ENROLLED,
            cart=cart,
            details=(
                'User enrolled to the course: course-v1:org1+1+1 with mode: no-id-professional '
                'during cart fulfillment for catalogue_item: 1.'
            )
        ).exists()
        self.fulfillment.fulfill(cart_item, 'processor')
        mock_enroll.assert_called_once_with(
            self.learner_user,
            self.course_mode.course.id,
            mode='no-id-professional',
            check_access=True
        )
        assert AuditLog.objects.filter(
            action=AuditLog.AuditActions.USER_ENROLLED,
            cart=cart,
            details=(
                'User enrolled to the course: course-v1:org1+1+1 with mode: no-id-professional '
                'during cart fulfillment for catalogue_item: 1.'
            )
        ).exists()

    def test_fulfill_course_mode_not_found(self):
        """
        Should log and raise CartFulfillmentError when CourseMode is missing.
        """
        cart = Cart.objects.create(user=self.learner_user, status=Cart.Status.PROCESSING)
        cart_item = cart.items.create(
            catalogue_item=self.catalog_item,
            original_price=self.catalog_item.price,
            final_price=self.catalog_item.price
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
            )
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
            final_price=self.catalog_item.price
        )
        with pytest.raises(CartFulfillmentError, match='Course Mode found but item ref id mismatched.'):
            self.fulfillment.fulfill(cart_item, 'processor')

        assert AuditLog.objects.filter(
            action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR,
            cart=cart,
            details=(
                f'Error during cart fulfillment for item: {cart_item.id}, catalogue_item:'
                f' {self.catalog_item.id} due to invalid SKU: {self.catalog_item.sku} or unsupported type.'
            )
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
            final_price=self.catalog_item.price
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
            )
        ).exists()


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
class TestValidateAndCreateCart:
    """
    Tests for the validate_and_create_cart() module-level function.
    """

    learner_user = None
    catalog_item_1 = None
    catalog_item_2 = None

    def setup_method(self):
        """Set up test data."""
        self.learner_user = User.objects.get(id=3)
        self.catalog_item_1 = CatalogueItem.objects.get(sku='custom-sku-1')
        self.catalog_item_2 = CatalogueItem.objects.get(
            sku='course1-org2-no-id-professional'
        )

    def test_single_item_creates_cart_with_one_item(self):
        """
        Should create a cart with a single CartItem when given one catalogue item.
        """
        cart = validate_and_create_cart(self.learner_user, [self.catalog_item_1])

        assert cart.status == Cart.Status.PENDING
        assert cart.user == self.learner_user
        assert cart.items.count() == 1

        cart_item = cart.items.first()
        assert cart_item.catalogue_item == self.catalog_item_1
        assert cart_item.original_price == self.catalog_item_1.price

    def test_multiple_items_creates_cart_with_all_items(self):
        """
        Should create a single cart containing all provided catalogue items.
        """
        cart = validate_and_create_cart(
            self.learner_user, [self.catalog_item_1, self.catalog_item_2]
        )

        assert cart.status == Cart.Status.PENDING
        assert cart.user == self.learner_user
        assert cart.items.count() == 2

        skus_in_cart = set(cart.items.values_list('catalogue_item__sku', flat=True))
        assert skus_in_cart == {'custom-sku-1', 'course1-org2-no-id-professional'}

    def test_empty_list_raises_error(self):
        """
        Should raise InvalidCartError when given an empty list of catalogue items.
        """
        with pytest.raises(
            InvalidCartError, match='At least one catalogue item is required'
        ):
            validate_and_create_cart(self.learner_user, [])

    def test_duplicate_skus_raises_error(self):
        """
        Should raise InvalidCartError when the same catalogue item appears twice.
        """
        with pytest.raises(InvalidCartError, match='Duplicate SKUs found in request'):
            validate_and_create_cart(
                self.learner_user, [self.catalog_item_1, self.catalog_item_1]
            )

    def test_unsupported_item_type_raises_error(self):
        """
        Should raise InvalidCartError when a catalogue item has an unregistered type.
        """
        unsupported_item = CatalogueItem.objects.create(
            sku='unsupported-type-sku',
            type='unsupported_type',
            title='Unsupported Item',
            item_ref_id='ref-1',
            price=10,
            currency='SAR',
        )
        with pytest.raises(InvalidCartError, match='unsupported type'):
            validate_and_create_cart(self.learner_user, [unsupported_item])

    def test_validation_failure_on_any_item_prevents_cart_creation(self):
        """
        Should raise InvalidCartError and create no cart if any item fails validation.
        """
        invalid_ref_item = CatalogueItem.objects.get(
            sku='custom-sku-with_invlaid_ref_id'
        )
        cart_count_before = Cart.objects.filter(user=self.learner_user).count()

        with pytest.raises(InvalidCartError):
            validate_and_create_cart(
                self.learner_user, [self.catalog_item_1, invalid_ref_item]
            )

        # No cart should have been created
        assert Cart.objects.filter(user=self.learner_user).count() == cart_count_before

    def test_cancels_old_pending_carts_by_default(self):
        """
        Should cancel existing pending carts before creating a new one.
        """
        old_cart = Cart.objects.create(
            user=self.learner_user, status=Cart.Status.PENDING
        )

        new_cart = validate_and_create_cart(self.learner_user, [self.catalog_item_1])

        old_cart.refresh_from_db()
        assert old_cart.status == Cart.Status.CANCELLED
        assert new_cart.status == Cart.Status.PENDING
        assert new_cart.id != old_cart.id

    def test_skip_cancel_old_carts_when_flag_is_false(self):
        """
        Should NOT cancel existing pending carts when cancel_old_carts=False.
        """
        old_cart = Cart.objects.create(
            user=self.learner_user, status=Cart.Status.PENDING
        )

        new_cart = validate_and_create_cart(
            self.learner_user, [self.catalog_item_1], cancel_old_carts=False
        )

        old_cart.refresh_from_db()
        assert old_cart.status == Cart.Status.PENDING
        assert new_cart.status == Cart.Status.PENDING

    def test_tax_is_applied_to_each_item(self):
        """
        Should compute tax_amount and final_price for each CartItem via TaxRule.
        """
        TaxRule.objects.create(
            name='VAT',
            tax_type=TaxRule.TaxType.PERCENT,
            tax_value=15,
            is_active=True,
        )

        cart = validate_and_create_cart(
            self.learner_user, [self.catalog_item_1, self.catalog_item_2]
        )

        for cart_item in cart.items.all():
            assert cart_item.tax_amount > 0
            assert (
                cart_item.final_price == cart_item.original_price + cart_item.tax_amount
            )

    def test_backward_compat_wrapper_calls_validate_and_create_cart(self):
        """
        BaseCartHandler.validate_item_and_create_cart() should delegate to
        validate_and_create_cart() with a single-item list.
        """
        handler = BaseCartHandler()

        with patch(
            'zeitlabs_payments.cart_handler.validate_and_create_cart'
        ) as mock_fn:
            mock_fn.return_value = MagicMock()
            handler.validate_item_and_create_cart(
                self.learner_user, self.catalog_item_1
            )

            mock_fn.assert_called_once_with(
                self.learner_user, [self.catalog_item_1], cancel_old_carts=True
            )

    def test_backward_compat_wrapper_passes_cancel_flag(self):
        """
        BaseCartHandler.validate_item_and_create_cart() should forward cancel_old_carts=False.
        """
        handler = BaseCartHandler()

        with patch(
            'zeitlabs_payments.cart_handler.validate_and_create_cart'
        ) as mock_fn:
            mock_fn.return_value = MagicMock()
            handler.validate_item_and_create_cart(
                self.learner_user, self.catalog_item_1, cancel_old_carts=False
            )

            mock_fn.assert_called_once_with(
                self.learner_user, [self.catalog_item_1], cancel_old_carts=False
            )
