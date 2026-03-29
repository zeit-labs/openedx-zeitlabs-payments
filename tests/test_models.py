"""test for models."""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from zeitlabs_payments.models import (
    AuditLog,
    BundleCourseItem,
    Cart,
    CartItem,
    CatalogueItem,
    Coupon,
    CouponUsage,
    CreditMemo,
    Invoice,
    InvoiceItem,
    TaxRule,
    Transaction,
    WebhookEvent,
)

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
            gateway='payfort',
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

        with pytest.raises(
            ValidationError,
            match="Missing template parameters for action 'cart_fulfillment_error'",
        ):
            AuditLog.log(
                action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR,
                context=incomplete_context,
                cart=self.cart,
                gateway='payfort',
            )

    def test_log_unknown_action_stores_context_as_string(self):
        """
        Should save context as string in details if action is unknown (template missing).
        """
        context = {'foo': 'bar'}

        log = AuditLog.log(action='UnknownAction', context=context, cart=self.cart, gateway='payfort')

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
            is_active=True,
        )
        base_price = Decimal('100.00')
        tax = TaxRule.calculate_tax(base_price, rule)
        assert tax == Decimal('15.00'), f'Expected 15% of {base_price} to be 15.00 but got {tax}'

    def test_calculate_tax_fixed(self):
        rule = TaxRule.objects.create(
            name='Fixed Tax',
            tax_type=TaxRule.TaxType.FIXED,
            tax_value=Decimal('5.00'),
            is_active=True,
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
            is_active=True,
        )
        tax = TaxRule.calculate_tax(None, rule)
        assert tax == Decimal('0.00')

    def test_get_applicable_tax_returns_last_active(self):
        TaxRule.objects.create(
            name='Old Tax',
            tax_type=TaxRule.TaxType.PERCENT,
            tax_value=Decimal('5.00'),
            is_active=True,
        )
        latest_rule = TaxRule.objects.create(
            name='New Tax',
            tax_type=TaxRule.TaxType.FIXED,
            tax_value=Decimal('10.00'),
            is_active=True,
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
            is_active=True,
        )
        assert str(rule) == 'Service Tax - 12.50%'

    def test_str_fixed(self):
        rule = TaxRule.objects.create(
            name='Processing Fee',
            tax_type=TaxRule.TaxType.FIXED,
            tax_value=Decimal('3.00'),
            is_active=True,
        )
        assert str(rule) == 'Processing Fee - 3.00'


@pytest.mark.django_db
def test_usage_count_sums_correctly():
    coupon = Coupon.objects.create(
        code='SUMMER2025',
        discount_type=Coupon.DiscountType.PERCENTAGE,
        discount_value=Decimal('15.00'),
        max_usage=10,
        expires_at=timezone.now() + timezone.timedelta(days=10),
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
        CatalogueItem.ItemType.PROGRAM_BUNDLE,
    ]
    result = CatalogueItem.valid_item_types()
    assert result == expected_item_types
    assert len(result) == len(set(result))
    assert all(item_type in dict(CatalogueItem.ItemType.choices) for item_type in result)


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
class TestBundleCourseItem:
    """Tests for BundleCourseItem model."""

    def test_str_representation(self):
        """Should return 'bundle_sku -> course_sku' format."""
        link = BundleCourseItem.objects.filter(bundle__sku='BUNDLE-PRO-CERT').first()
        assert str(link) == f'{link.bundle.sku} -> {link.course_item.sku}'

    def test_bundle_has_expected_courses(self):
        """Bundle should have exactly 2 linked courses."""
        links = BundleCourseItem.objects.filter(bundle__sku='BUNDLE-PRO-CERT')
        assert links.count() == 2
        skus = set(links.values_list('course_item__sku', flat=True))
        assert skus == {'custom-sku-1', 'course1-org2-no-id-professional'}

    def test_unique_together_constraint(self):
        """Should prevent duplicate bundle-course links."""
        bundle = CatalogueItem.objects.get(sku='BUNDLE-PRO-CERT')
        course_item = CatalogueItem.objects.get(sku='custom-sku-1')

        with pytest.raises(IntegrityError):
            BundleCourseItem.objects.create(bundle=bundle, course_item=course_item)


@pytest.mark.usefixtures('base_data')
class TestCartGetStatusDisplay(TestCase):
    """Tests for Cart.get_status_display static method."""

    def test_known_statuses(self):
        """Should return translated display string for every known status."""
        for status_value, _ in Cart.Status.choices:
            result = Cart.get_status_display(status_value)
            assert result is not None
            assert len(str(result)) > 0

    def test_unknown_status_returns_raw_value(self):
        """Should return the raw value when the status is unknown."""
        assert Cart.get_status_display('nonexistent') == 'nonexistent'


@pytest.mark.django_db
@pytest.mark.usefixtures('base_data')
class TestModelStrRepresentations:
    """Tests for __str__ methods on all payment models."""

    def test_cart_str(self):
        """Cart.__str__ should return 'Cart #<id> - <username> (<status>)'."""
        user = User.objects.get(id=1)
        cart = Cart.objects.create(user=user, status=Cart.Status.PENDING)
        assert str(cart) == f'Cart #{cart.pk} - {user.username} (pending)'

    def test_transaction_str(self):
        """Transaction.__str__ should return 'Txn #<id> - <type> (<gateway>)'."""
        user = User.objects.get(id=1)
        cart = Cart.objects.create(user=user)
        txn = Transaction.objects.create(
            cart=cart,
            type=Transaction.TransactionType.PAYMENT,
            status='success',
            gateway='payfort',
            gateway_transaction_id='TXN-001',
            method='credit_card',
            amount=Decimal('100.00'),
            currency='IQD',
        )
        assert str(txn) == f'Txn #{txn.pk} - payment (payfort)'

    def test_webhook_event_str(self):
        """WebhookEvent.__str__ should return 'Webhook #<id> - <gateway> (<event_type>)'."""
        event = WebhookEvent.objects.create(
            gateway='payfort',
            event_type='payment.captured',
            payload={'test': True},
        )
        assert str(event) == f'Webhook #{event.pk} - payfort (payment.captured)'

    def test_audit_log_str(self):
        """AuditLog.__str__ should return 'Audit #<id> - <action>'."""
        user = User.objects.get(id=1)
        cart = Cart.objects.create(user=user)
        log = AuditLog.log(
            action=AuditLog.AuditActions.CART_FULFILLED,
            cart=cart,
        )
        assert str(log) == f'Audit #{log.pk} - cart_fulfilled'

    def test_coupon_str(self):
        """Coupon.__str__ should return '<code> (<type>: <value>)'."""
        coupon = Coupon.objects.create(
            code='TEST50',
            discount_type=Coupon.DiscountType.PERCENTAGE,
            discount_value=Decimal('50.00'),
            max_usage=100,
        )
        assert str(coupon) == 'TEST50 (percentage: 50.00)'

    def test_coupon_usage_str(self):
        """CouponUsage.__str__ should return '<code> - <username> (x<count>)'."""
        coupon = Coupon.objects.create(
            code='USAGE10',
            discount_type=Coupon.DiscountType.FIXED,
            discount_value=Decimal('10.00'),
            max_usage=50,
        )
        user = User.objects.get(id=1)
        usage = CouponUsage.objects.create(coupon=coupon, user=user, count=3)
        assert str(usage) == f'USAGE10 - {user.username} (x3)'

    def test_catalogue_item_str(self):
        """CatalogueItem.__str__ should return '<title> (<sku>)'."""
        item = CatalogueItem.objects.get(sku='custom-sku-1')
        assert str(item) == f'{item.title} (custom-sku-1)'

    def test_cart_item_str(self):
        """CartItem.__str__ should return 'CartItem #<id> - <catalogue_item_title>'."""
        user = User.objects.get(id=1)
        cart = Cart.objects.create(user=user)
        catalogue_item = CatalogueItem.objects.get(sku='custom-sku-1')
        cart_item = CartItem.objects.create(
            cart=cart,
            catalogue_item=catalogue_item,
            original_price=Decimal('50.00'),
            final_price=Decimal('50.00'),
        )
        assert str(cart_item) == f'CartItem #{cart_item.pk} - {catalogue_item.title}'

    def test_invoice_str(self):
        """Invoice.__str__ should return '<invoice_number> (<status>)'."""
        user = User.objects.get(id=1)
        cart = Cart.objects.create(user=user, status=Cart.Status.PAID)
        invoice = Invoice.objects.create(
            invoice_number='INV-TEST-001',
            cart=cart,
            status=Invoice.InvoiceStatus.PAID,
            gross_total=Decimal('100.00'),
            total=Decimal('100.00'),
            currency='IQD',
        )
        assert str(invoice) == 'INV-TEST-001 (paid)'

    def test_invoice_item_str(self):
        """InvoiceItem.__str__ should return 'InvoiceItem #<id> - <invoice_number>'."""
        user = User.objects.get(id=1)
        cart = Cart.objects.create(user=user, status=Cart.Status.PAID)
        invoice = Invoice.objects.create(
            invoice_number='INV-TEST-002',
            cart=cart,
            gross_total=Decimal('50.00'),
            total=Decimal('50.00'),
            currency='IQD',
        )
        invoice_item = InvoiceItem.objects.create(
            invoice=invoice,
            original_price=Decimal('50.00'),
            price=Decimal('50.00'),
        )
        assert str(invoice_item) == f'InvoiceItem #{invoice_item.pk} - INV-TEST-002'

    def test_credit_memo_str(self):
        """CreditMemo.__str__ should return 'CreditMemo #<id> - <invoice_number>'."""
        user = User.objects.get(id=1)
        cart = Cart.objects.create(user=user, status=Cart.Status.REFUNDED)
        invoice = Invoice.objects.create(
            invoice_number='INV-TEST-003',
            cart=cart,
            gross_total=Decimal('75.00'),
            total=Decimal('75.00'),
            currency='IQD',
        )
        memo = CreditMemo.objects.create(
            invoice=invoice,
            total=Decimal('75.00'),
            reason='Test refund',
            gateway_refund_transaction_id='REF-001',
        )
        assert str(memo) == f'CreditMemo #{memo.pk} - INV-TEST-003'
