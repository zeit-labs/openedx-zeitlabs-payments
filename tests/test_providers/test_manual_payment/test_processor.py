"""Manual payment related tests."""

import pytest
from django.contrib.auth import get_user_model
from django.http import HttpRequest

from zeitlabs_payments.models import AuditLog, Cart, CatalogueItem, Invoice, Transaction
from zeitlabs_payments.providers.manual_payment.processor import ManualPaymentProcessor

User = get_user_model()


@pytest.mark.django_db
class TestManualPaymentProcessor:
    """
    Tests for ManualPaymentProcessor.process_payment.
    """
    processor = None
    user = None
    catalog_item = None
    cart = None

    def setup_method(self):
        """setup method."""
        self.processor = ManualPaymentProcessor()
        self.user = User.objects.get(id=3)
        self.catalog_item = CatalogueItem.objects.get(sku='custom-sku-1')
        self.cart = Cart.objects.create(user=self.user, status=Cart.Status.PENDING)
        self.cart.items.create(
            catalogue_item=self.catalog_item,
            original_price=self.catalog_item.price,
            final_price=self.catalog_item.price
        )

    def test_process_payment_creates_cart_invoice_fulfillment_and_auditlog(self):
        """
        Should:
        - create a paid cart cartItem
        - create Invoice
        - create AuditLog entry
        """
        transaction_id = '12345'
        transaction_status = 'success'
        reason = 'manual payment recieved from someone.'
        request = HttpRequest()
        request.user = self.user

        result = self.processor.process_payment(
            cart=self.cart,
            request=request,
            transaction_id=transaction_id,
            transaction_status=transaction_status,
            reason=reason,
        )

        # verify cart updated to paid
        self.cart.refresh_from_db()
        assert self.cart.status == Cart.Status.PAID

        # Verify transaction exists
        transaction = Transaction.objects.get(
            gateway='manual',
            gateway_transaction_id=transaction_id,
        )
        assert transaction.cart == self.cart
        assert transaction.status == transaction_status
        assert transaction.reason == reason

        # Verify invoice exists
        invoice = Invoice.objects.get(invoice_number=result['created_invoice'])
        assert invoice.cart == self.cart

        # Verify AuditLog entry
        assert AuditLog.objects.filter(
            action=AuditLog.AuditActions.CART_FULFILLED,
            cart=self.cart,
            gateway='manual'
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
