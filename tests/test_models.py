"""test for models."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from zeitlabs_payments.models import AuditLog, Cart, CatalogueItem, Coupon, CouponUsage, InvoiceSequence, TaxRule

User = get_user_model()


@pytest.mark.django_db
class TestAuditLogModel:
    """
    Tests for AuditLog.log method.
    """
    cart = None

    def setup_method(self):
        self.cart = Cart.objects.create(user_id=3)

    def test_log_with_valid_context(self):
        """
        Should create AuditLog entry with formatted details when context is complete.
        """
        context = {
            'item_id': 1,
            'catalogue_item_id': 2,
            'sku': 'SKU-123',
        }

        log = AuditLog.log(
            action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR,
            context=context,
            cart=self.cart,
            gateway='payfort'
        )

        assert log.action == AuditLog.AuditActions.CART_FULFILLMENT_ERROR
        assert log.cart == self.cart
        assert log.gateway == 'payfort'
        assert 'Error during cart fulfillment for item: 1, catalogue_item: 2' in log.details

    def test_log_missing_context_key_raises_validation_error(self):
        """
        Should raise ValidationError when required context keys are missing.
        """
        incomplete_context = {
            'item_id': 1,
            # 'catalogue_item_id' is missing
            'sku': 'SKU-123',
        }

        with pytest.raises(ValidationError, match="Missing template parameters for action 'cart_fulfillment_error'"):
            AuditLog.log(
                action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR,
                context=incomplete_context,
                cart=self.cart,
                gateway='payfort'
            )

    def test_log_unknown_action_stores_context_as_string(self):
        """
        Should save context as string in details if action is unknown (template missing).
        """
        context = {'foo': 'bar'}

        log = AuditLog.log(
            action='UnknownAction',
            context=context,
            cart=self.cart,
            gateway='payfort'
        )

        assert log.action == 'UnknownAction'
        assert log.details == str(context)
        assert log.cart == self.cart
        assert log.gateway == 'payfort'


@pytest.mark.django_db
class TestTaxRule:
    """Test for TaxRule class methods"""

    def test_calculate_tax_percentage(self):
        rule = TaxRule.objects.create(
            name='VAT',
            tax_type=TaxRule.TaxType.PERCENT,
            tax_value=Decimal('15.00'),
            is_active=True
        )
        base_price = Decimal('100.00')
        tax = TaxRule.calculate_tax(base_price, rule)
        assert tax == Decimal('15.00'), f'Expected 15% of {base_price} to be 15.00 but got {tax}'

    def test_calculate_tax_fixed(self):
        rule = TaxRule.objects.create(
            name='Fixed Tax',
            tax_type=TaxRule.TaxType.FIXED,
            tax_value=Decimal('5.00'),
            is_active=True
        )
        base_price = Decimal('100.00')
        tax = TaxRule.calculate_tax(base_price, rule)
        assert tax == Decimal('5.00'), f'Expected 5.00 as fixed_price disocunt but got {tax}'

    def test_calculate_tax_no_rule(self):
        tax = TaxRule.calculate_tax(Decimal('100.00'), None)
        assert tax == Decimal('0.00')

    def test_calculate_tax_no_base_price(self):
        rule = TaxRule.objects.create(
            name='Fixed Tax',
            tax_type=TaxRule.TaxType.FIXED,
            tax_value=Decimal('5.00'),
            is_active=True
        )
        tax = TaxRule.calculate_tax(None, rule)
        assert tax == Decimal('0.00')

    def test_get_applicable_tax_returns_last_active(self):
        TaxRule.objects.create(
            name='Old Tax',
            tax_type=TaxRule.TaxType.PERCENT,
            tax_value=Decimal('5.00'),
            is_active=True
        )
        latest_rule = TaxRule.objects.create(
            name='New Tax',
            tax_type=TaxRule.TaxType.FIXED,
            tax_value=Decimal('10.00'),
            is_active=True
        )

        base_price = Decimal('200.00')
        tax_rule, tax_amount = TaxRule.get_applicable_tax(base_price)

        assert tax_rule == latest_rule
        assert tax_amount == Decimal('10.00')

    def test_get_applicable_tax_no_active_rule(self):
        base_price = Decimal('100.00')
        tax_rule, tax_amount = TaxRule.get_applicable_tax(base_price)
        assert tax_rule is None
        assert tax_amount == Decimal('0.00')

    def test_get_applicable_tax_no_base_price(self):
        tax_rule, tax_amount = TaxRule.get_applicable_tax(None)
        assert tax_rule is None
        assert tax_amount == Decimal('0.00')

    def test_str_percentage(self):
        rule = TaxRule.objects.create(
            name='Service Tax',
            tax_type=TaxRule.TaxType.PERCENT,
            tax_value=Decimal('12.50'),
            is_active=True
        )
        assert str(rule) == 'Service Tax - 12.50%'

    def test_str_fixed(self):
        rule = TaxRule.objects.create(
            name='Processing Fee',
            tax_type=TaxRule.TaxType.FIXED,
            tax_value=Decimal('3.00'),
            is_active=True
        )
        assert str(rule) == 'Processing Fee - 3.00'


@pytest.mark.django_db
def test_usage_count_sums_correctly():
    coupon = Coupon.objects.create(
        code='SUMMER2025',
        discount_type=Coupon.DiscountType.PERCENTAGE,
        discount_value=Decimal('15.00'),
        max_usage=10,
        expires_at=timezone.now() + timezone.timedelta(days=10)
    )
    user1 = User.objects.get(id=1)
    user2 = User.objects.get(id=2)
    CouponUsage.objects.create(coupon=coupon, user=user1, count=3)
    CouponUsage.objects.create(coupon=coupon, user=user2, count=2)
    assert coupon.usage_count == 5


@pytest.mark.django_db
def test_valid_statuses_returns_all_choices():
    """Test that Cart.valid_statuses() returns all defined status values."""
    expected_statuses = {
        Cart.Status.PENDING,
        Cart.Status.PROCESSING,
        Cart.Status.PAYMENT_PENDING,
        Cart.Status.PAID,
        Cart.Status.CANCELLED,
        Cart.Status.REFUND_REQUESTED,
        Cart.Status.REFUNDED,
    }
    result = set(Cart.valid_statuses())
    assert result == expected_statuses


@pytest.mark.django_db
def test_valid_item_types_returns_all_choices():
    """Test that CatalogueItem.valid_item_types() returns all defined item type values."""
    expected_item_types = [
        CatalogueItem.ItemType.PAID_COURSE,
    ]
    result = CatalogueItem.valid_item_types()
    assert result == expected_item_types
    assert len(result) == len(set(result))
    assert all(item_type in dict(CatalogueItem.ItemType.choices) for item_type in result)


@pytest.mark.django_db
class TestInvoiceSequence:
    """
    Tests for the InvoiceSequence counter.
    """

    def test_next_number_creates_sequence_and_starts_at_first_number(self):
        """
        Should create the counter row on first use and start at FIRST_NUMBER.
        """
        assert InvoiceSequence.next_number('ABC') == InvoiceSequence.FIRST_NUMBER
        sequence = InvoiceSequence.objects.get(prefix='ABC')
        assert sequence.last_number == InvoiceSequence.FIRST_NUMBER
        assert str(sequence) == f'ABC: {InvoiceSequence.FIRST_NUMBER}'

    def test_next_number_increments_existing_sequence(self):
        """
        Should increment the stored counter by one on each call.
        """
        InvoiceSequence.objects.create(prefix='ABC', last_number=100010)
        assert InvoiceSequence.next_number('ABC') == 100011
        assert InvoiceSequence.next_number('ABC') == 100012
        assert InvoiceSequence.objects.get(prefix='ABC').last_number == 100012

    def test_next_number_is_independent_per_prefix(self):
        """
        Each prefix keeps its own counter.
        """
        InvoiceSequence.objects.create(prefix='ABC', last_number=100500)
        assert InvoiceSequence.next_number('XYZ') == InvoiceSequence.FIRST_NUMBER
        assert InvoiceSequence.next_number('ABC') == 100501

    def test_next_number_survives_gaining_a_digit(self):
        """
        Numbers are compared numerically, so 999999 is followed by 1000000.
        """
        InvoiceSequence.objects.create(prefix='ABC', last_number=999999)
        assert InvoiceSequence.next_number('ABC') == 1000000

    def test_next_number_locks_the_sequence_row(self):
        """
        The counter must be read with a row lock (select_for_update) inside a transaction.
        """
        InvoiceSequence.objects.create(prefix='ABC', last_number=100010)
        with patch.object(
            InvoiceSequence.objects, 'select_for_update', wraps=InvoiceSequence.objects.select_for_update
        ) as mock_lock:
            with patch('zeitlabs_payments.models.transaction.atomic', wraps=transaction.atomic) as mock_atomic:
                assert InvoiceSequence.next_number('ABC') == 100011
        mock_lock.assert_called_once_with()
        mock_atomic.assert_called_once_with()

    def test_next_number_concurrent_allocations_are_unique_and_gap_free(self):
        """
        Simulate two callers interleaving: the second caller only reads the row after the
        first one has incremented and saved it, which is what the row lock guarantees.
        """
        InvoiceSequence.objects.create(prefix='ABC', last_number=100010)
        allocated = []
        original_save = InvoiceSequence.save

        def save_then_let_other_caller_in(sequence, *args, **kwargs):
            original_save(sequence, *args, **kwargs)
            if not allocated:
                allocated.append(sequence.last_number)
                allocated.append(InvoiceSequence.next_number('ABC'))

        with patch.object(InvoiceSequence, 'save', save_then_let_other_caller_in):
            first = InvoiceSequence.next_number('ABC')

        assert first == 100011
        assert allocated == [100011, 100012]
        assert len(set(allocated)) == 2
        assert InvoiceSequence.objects.get(prefix='ABC').last_number == 100012
