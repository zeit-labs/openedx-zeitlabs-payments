"""Manual payment related tests."""

import pytest
from django.contrib.auth import get_user_model

from zeitlabs_payments.models import AuditLog, Cart, CartItem, CatalogueItem, Invoice
from zeitlabs_payments.providers.manual_payment import ManualPaymentProcessor

User = get_user_model()


@pytest.mark.django_db
class TestManualPaymentProcessor:
    """
    Tests for ManualPaymentProcessor.process_payment.
    """
    processor = None
    user = None
    catalog_item = None

    def setup_method(self):
        """setup method."""
        self.processor = ManualPaymentProcessor()
        self.user = User.objects.get(id=3)
        self.catalog_item = CatalogueItem.objects.get(sku='custom-sku-1')

    def test_process_payment_creates_cart_invoice_fulfillment_and_auditlog(self):
        """
        Should:
        - create a paid cart cartItem
        - create Invoice
        - create AuditLog entry
        """
        request = None

        result = self.processor.process_payment(
            user=self.user,
            course_catalog_item=self.catalog_item,
            request=request
        )

        # Verify cart exists and is paid
        created_cart = Cart.objects.get(id=result['created_cart'])
        assert created_cart.status == Cart.Status.PAID
        assert created_cart.user == self.user
        assert CartItem.objects.filter(cart=created_cart, catalogue_item=self.catalog_item).exists()

        # Verify invoice exists
        invoice = Invoice.objects.get(invoice_number=result['created_invoice'])
        assert invoice.cart == created_cart

        # Verify AuditLog entry
        assert AuditLog.objects.filter(
            action=AuditLog.AuditActions.CART_FULFIlED,
            cart=created_cart,
            gateway='manual_payment'
        ).exists()

    def test_get_transaction_parameters_raises_not_implemented(self):
        """
        Should raise NotImplementedError.
        """
        with pytest.raises(NotImplementedError):
            self.processor.get_transaction_parameters(
                cart=Cart.objects.create(user=self.user),
                request=None,
                use_client_side_checkout=False
            )
