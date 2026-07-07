"""Tests for enrollment gating filter."""
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase
from openedx_filters.learning.filters import CourseEnrollmentStarted

from zeitlabs_payments.filters import (
    BlockUnpaidCourseEnrollment,
    _course_has_paid_mode,
    _has_paid_cart_for_course,
    _payments_enabled,
)
from zeitlabs_payments.models import Cart, CartItem, CatalogueItem

User = get_user_model()


def _make_catalogue_item(**kwargs):
    """Create a CatalogueItem with minimal required defaults."""
    defaults = {
        'sku': 'TST-SKU',
        'type': CatalogueItem.ItemType.PAID_COURSE,
        'title': 'Test Item',
        'item_ref_id': 'course-v1:org+1+1',
    }
    defaults.update(kwargs)
    return CatalogueItem.objects.create(**defaults)


class TestHasPaidCartForCourse(TestCase):
    """Tests for _has_paid_cart_for_course()."""

    def setUp(self):
        self.user = User.objects.create(username='testuser', email='test@example.com')

    def test_no_cart(self):
        """Returns False when user has no cart at all."""
        result = _has_paid_cart_for_course(self.user, 'course-v1:org+1+1')
        assert result is False

    def test_pending_cart(self):
        """Returns False when cart is still pending (not paid)."""
        cart = Cart.objects.create(user=self.user, status=Cart.Status.PENDING)
        cat_item = _make_catalogue_item()
        CartItem.objects.create(
            cart=cart, catalogue_item=cat_item,
            original_price=100, final_price=100,
        )
        result = _has_paid_cart_for_course(self.user, 'course-v1:org+1+1')
        assert result is False

    def test_paid_cart_exists(self):
        """Returns True when user has a PAID cart for the course."""
        cart = Cart.objects.create(user=self.user, status=Cart.Status.PAID)
        cat_item = _make_catalogue_item()
        CartItem.objects.create(
            cart=cart, catalogue_item=cat_item,
            original_price=100, final_price=100,
        )
        result = _has_paid_cart_for_course(self.user, 'course-v1:org+1+1')
        assert result is True

    def test_paid_cart_different_course(self):
        """Returns False when PAID cart is for a different course."""
        cart = Cart.objects.create(user=self.user, status=Cart.Status.PAID)
        cat_item = _make_catalogue_item()
        CartItem.objects.create(
            cart=cart, catalogue_item=cat_item,
            original_price=100, final_price=100,
        )
        result = _has_paid_cart_for_course(self.user, 'course-v1:org+2+2')
        assert result is False

    def test_payment_pending_cart(self):
        """Returns False when cart is PAYMENT_PENDING (not yet paid)."""
        cart = Cart.objects.create(user=self.user, status=Cart.Status.PAYMENT_PENDING)
        cat_item = _make_catalogue_item()
        CartItem.objects.create(
            cart=cart, catalogue_item=cat_item,
            original_price=100, final_price=100,
        )
        result = _has_paid_cart_for_course(self.user, 'course-v1:org+1+1')
        assert result is False

    def test_cancelled_cart(self):
        """Returns False when cart is CANCELLED."""
        cart = Cart.objects.create(user=self.user, status=Cart.Status.CANCELLED)
        cat_item = _make_catalogue_item()
        CartItem.objects.create(
            cart=cart, catalogue_item=cat_item,
            original_price=100, final_price=100,
        )
        result = _has_paid_cart_for_course(self.user, 'course-v1:org+1+1')
        assert result is False


@pytest.mark.usefixtures('base_data')
class TestCourseHasPaidMode(TestCase):
    """Tests for _course_has_paid_mode()."""

    def test_course_with_paid_mode(self):
        """Returns True for a course that has a professional mode."""
        assert _course_has_paid_mode('course-v1:org1+1+1') is True

    def test_course_without_paid_mode(self):
        """Returns False for a course with no paid modes."""
        assert _course_has_paid_mode('course-v1:org1+3+3') is False

    def test_course_does_not_exist(self):
        """Returns False for a non-existent course."""
        assert _course_has_paid_mode('course-v1:nonexistent+1+1') is False


@pytest.mark.usefixtures('base_data')
class TestBlockUnpaidCourseEnrollment(TestCase):
    """Tests for BlockUnpaidCourseEnrollment."""

    def _make_step(self):
        """Create a pipeline step instance with default filter metadata."""
        return BlockUnpaidCourseEnrollment(
            filter_type='org.openedx.learning.course.enrollment.started.v1',
            running_pipeline=['zeitlabs_payments.filters.BlockUnpaidCourseEnrollment'],
        )

    def test_free_course_allows_enrollment(self):
        """Free courses (no paid mode) should never be blocked — no exception raised."""
        step = self._make_step()
        with patch(
            'zeitlabs_payments.filters._payments_enabled',
            return_value=True,
        ):
            result = step.run_filter(
                MagicMock(id=1, is_staff=False, is_superuser=False),
                MagicMock(__str__=lambda s: 'course-v1:org1+3+3'),
                'audit',
            )
        assert isinstance(result, dict)
        assert not result

    def test_paid_course_without_cart_blocks(self):
        """Paid course without a paid cart raises PreventEnrollment."""
        step = self._make_step()
        with pytest.raises(CourseEnrollmentStarted.PreventEnrollment):
            with patch(
                'zeitlabs_payments.filters._payments_enabled',
                return_value=True,
            ), patch(
                'zeitlabs_payments.filters._has_paid_cart_for_course',
                return_value=False,
            ):
                step.run_filter(
                    MagicMock(id=1, is_staff=False, is_superuser=False),
                    MagicMock(__str__=lambda s: 'course-v1:org1+1+1'),
                    'verified',
                )

    def test_paid_course_with_paid_cart_allows(self):
        """Paid course with a paid cart should not block enrollment."""
        step = self._make_step()
        with patch(
            'zeitlabs_payments.filters._payments_enabled',
            return_value=True,
        ), patch(
            'zeitlabs_payments.filters._has_paid_cart_for_course',
            return_value=True,
        ), patch(
            'zeitlabs_payments.filters._course_has_paid_mode',
            return_value=True,
        ):
            result = step.run_filter(
                MagicMock(id=1, is_staff=False, is_superuser=False),
                MagicMock(__str__=lambda s: 'course-v1:org1+1+1'),
                'verified',
            )
        assert isinstance(result, dict)
        assert not result

    def test_logs_warning_on_blocked_enrollment(self):
        """Verifies a warning is logged when enrollment is blocked."""
        step = self._make_step()
        mock_logger = MagicMock()
        with patch(
            'zeitlabs_payments.filters.logger',
            mock_logger,
        ), patch(
            'zeitlabs_payments.filters._payments_enabled',
            return_value=True,
        ), patch(
            'zeitlabs_payments.filters._has_paid_cart_for_course',
            return_value=False,
        ), pytest.raises(CourseEnrollmentStarted.PreventEnrollment):
            step.run_filter(
                MagicMock(id=42, is_staff=False, is_superuser=False),
                MagicMock(__str__=lambda s: 'course-v1:org1+1+1'),
                'verified',
            )
        mock_logger.warning.assert_called_once()
        args_str = str(mock_logger.warning.call_args[0])
        assert '42' in args_str
        assert 'course-v1:org1+1+1' in args_str

    def test_staff_user_always_allowed(self):
        """Staff users are never blocked, even without a paid cart."""
        step = self._make_step()
        with patch(
            'zeitlabs_payments.filters._payments_enabled',
            return_value=True,
        ), patch(
            'zeitlabs_payments.filters._has_paid_cart_for_course',
            return_value=False,
        ):
            result = step.run_filter(
                MagicMock(id=1, is_staff=True, is_superuser=False),
                MagicMock(__str__=lambda s: 'course-v1:org1+1+1'),
                'verified',
            )
        assert isinstance(result, dict)
        assert not result

    def test_superuser_always_allowed(self):
        """Superusers are never blocked, even without a paid cart."""
        step = self._make_step()
        with patch(
            'zeitlabs_payments.filters._payments_enabled',
            return_value=True,
        ), patch(
            'zeitlabs_payments.filters._has_paid_cart_for_course',
            return_value=False,
        ):
            result = step.run_filter(
                MagicMock(id=1, is_staff=False, is_superuser=True),
                MagicMock(__str__=lambda s: 'course-v1:org1+1+1'),
                'verified',
            )
        assert isinstance(result, dict)
        assert not result

    def test_payments_disabled_bypasses_filter(self):
        """When IS_ZEITLABS_PAYMENTS_ENABLED is False, enrollment is never blocked."""
        step = self._make_step()
        with patch(
            'zeitlabs_payments.filters._payments_enabled',
            return_value=False,
        ):
            result = step.run_filter(
                MagicMock(id=1),
                MagicMock(__str__=lambda s: 'course-v1:org1+1+1'),
                'verified',
            )
        assert isinstance(result, dict)
        assert not result


class TestPaymentsEnabled(TestCase):
    """Tests for _payments_enabled()."""

    def test_enabled_true(self):
        """Returns True when IS_ZEITLABS_PAYMENTS_ENABLED is True."""
        with patch(
            'django.conf.settings.IS_ZEITLABS_PAYMENTS_ENABLED',
            True,
        ):
            assert _payments_enabled() is True

    def test_enabled_false(self):
        """Returns False when IS_ZEITLABS_PAYMENTS_ENABLED is False."""
        with patch(
            'django.conf.settings.IS_ZEITLABS_PAYMENTS_ENABLED',
            False,
        ):
            assert _payments_enabled() is False

    def test_current_environment_value(self):
        """Returns whatever IS_ZEITLABS_PAYMENTS_ENABLED is in test settings."""
        assert _payments_enabled() is False
