"""Zeitlabs payments models."""
import re

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import models

User = get_user_model()


class TimeStampedModel(models.Model):
    """TimeStamped model."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Cart(TimeStampedModel):
    """Cart model."""

    class Status(models.TextChoices):
        """Cart states."""

        PENDING = 'pending'
        PROCESSING = 'processing'
        PAID = 'paid'
        CANCELLED = 'cancelled'
        REFUND_REQUESTED = 'refund_requested'
        REFUNDED = 'refunded'

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='carts')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    @property
    def total(self) -> int:
        """Calculate total."""
        return sum(item.final_price for item in self.items.all())

    @property
    def discount_total(self) -> int:
        """Calculate discount total."""
        return sum(item.discount_amount for item in self.items.all())


class Transaction(TimeStampedModel):
    """Transaction model."""

    class TransactionType(models.TextChoices):
        """Transaction types."""

        PAYMENT = 'payment'
        REFUND = 'refund'

    cart = models.ForeignKey(Cart, on_delete=models.SET_NULL, related_name='transactions', null=True)
    type = models.CharField(max_length=20, choices=TransactionType.choices)
    status = models.CharField(max_length=50)
    gateway = models.CharField(max_length=50)
    gateway_transaction_id = models.CharField(max_length=255)
    method = models.CharField(max_length=50)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)
    response = models.JSONField(blank=True, null=True)
    reason = models.TextField(blank=True, null=True)
    initiator_user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True
    )


class WebhookEvent(TimeStampedModel):
    """WebhookEvent model."""

    gateway = models.CharField(max_length=50)
    event_type = models.CharField(max_length=100)
    payload = models.JSONField()
    related_transaction = models.ForeignKey(Transaction, on_delete=models.SET_NULL, null=True, blank=True)
    handled = models.BooleanField(default=False)


class AuditLog(TimeStampedModel):
    """AuditLog model."""

    class AuditActions:
        """Audit log actions."""

        CART_FULFILLMENT_ERROR = 'cart_fulfillment_error'
        USER_ENROLLED = 'user_enrolled'
        USER_ENROLLED_ERROR = 'user_enrolled_error'
        REDIRECT_TO_PAYMENT = 'redirect_to_payment_gateway'
        DUPLICATE_TRANSACTION = 'duplicate_transaction_detected'
        BAD_RESPONSE_SIGNATURE = 'bad_response_signature'
        RECEIVED_RESPONSE = 'received_gateway_response'
        RESPONSE_INVALID_CART = 'response_for_invalid_cart'
        TRANSACTION_ROLLED_BACK = 'transaction_rolled_back'
        CART_STATUS_UPDATED = 'cart_status_updated'
        CART_FULFILLED = 'cart_fulfilled'

    TEMPLATES = {
        AuditActions.CART_FULFILLMENT_ERROR: (
            'Error during cart fulfillment for item: {item_id}, '
            'catalogue_item: {catalogue_item_id} due to invalid SKU: {sku} or unsupported type.'
        ),
        AuditActions.USER_ENROLLED: (
            'User enrolled to the course: {course_id} with mode: {mode_slug} '
            'during cart fulfillment for catalogue_item: {catalogue_item_id}.'
        ),
        AuditActions.USER_ENROLLED_ERROR: (
            'Unable to complete user enrollment to course: {course_id} with mode: {mode_slug} '
            'during cart fulfillment for catalogue_item: {catalogue_item_id}.'
        ),
        AuditActions.REDIRECT_TO_PAYMENT: 'Redirecting to payment page.',
        AuditActions.DUPLICATE_TRANSACTION: (
            'Transaction with id: {transaction_id} already existed. Cart has status: {cart_status}.'
        ),
        AuditActions.BAD_RESPONSE_SIGNATURE: 'Bad response signature detected: {data}.',
        AuditActions.RECEIVED_RESPONSE: 'Received response from payment gateway: {data}.',
        AuditActions.RESPONSE_INVALID_CART: (
            'Invalid cart state found during success feedback processing. Cart'
            'is in state: {cart_status} instead of {required_cart_state}.'
        ),
        AuditActions.TRANSACTION_ROLLED_BACK: (
            'Transaction: {transaction_id} for cart: {cart_id} and site: {site_id} rolled back.'
        ),
        AuditActions.CART_STATUS_UPDATED: (
            'Status updated for cart from: {old_status} to: {new_status}.'
        ),
        AuditActions.CART_FULFILLED: (
            'Cart fulfilled successfully.'
        )
    }

    action = models.CharField(max_length=255)
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name='audits', null=True)
    gateway = models.CharField(max_length=50, blank=True, null=True)
    details = models.TextField(blank=True, null=True)

    @classmethod
    def log(cls, *, action: str, context: dict = None, cart: Cart = None, gateway: str = None) -> None:
        """
        Create a log entry for a given action, optionally using a template and additional context.
        This method:
        - Looks up a template string based on the provided action.
        - Validates that all required parameters for the template exist in the context.
        - Formats the log details using the context.
        - Creates and saves a log entry associated with the cart and payment gateway.

        :param action: The name of the action to log (used to find the template).
        :param context: A dictionary of context values to fill into the template.
        :param cart: Optional cart instance related to the log entry.
        :param gateway: Optional payment gateway name related to the log entry.
        :raises ValidationError: If the context is missing required parameters for the template.
        :return: None
        """
        context = context or {}
        template = cls.TEMPLATES.get(action, '')

        # Validate required template parameters
        if template:
            required_keys = set(re.findall(r'{(\w+)}', template))
            missing_keys = required_keys - context.keys()
            if missing_keys:
                raise ValidationError(
                    f"Missing template parameters for action '{action}': {', '.join(missing_keys)}"
                )

        details = template.format(**context) if template else str(context)
        return cls.objects.create(
            action=action,
            cart=cart,
            gateway=gateway,
            details=details,
        )


class Coupon(TimeStampedModel):
    """Coupon model."""

    class DiscountType(models.TextChoices):
        """Discount Types."""

        FIXED = 'fixed'
        PERCENTAGE = 'percentage'

    code = models.CharField(max_length=50, primary_key=True)
    discount_type = models.CharField(max_length=20, choices=DiscountType.choices)
    discount_value = models.DecimalField(max_digits=10, decimal_places=2)
    max_usage = models.PositiveIntegerField()
    usage_count = models.PositiveIntegerField(default=0)  # TODO: move to usage table
    expires_at = models.DateTimeField(blank=True, null=True)


class CouponUsage(TimeStampedModel):
    """CouponUsage model."""

    coupon = models.ForeignKey(Coupon, on_delete=models.CASCADE, related_name='usages')
    count = models.PositiveIntegerField(default=1)
    user = models.ForeignKey(User, on_delete=models.CASCADE)


class CatalogueItem(TimeStampedModel):
    """CatalogueItem model."""

    class ItemType(models.TextChoices):
        """Catalogue Item Types."""

        PAID_COURSE = 'paid_course'
        # TODO add other types here like 'section_of_course', 'fremium_course', etc.

    sku = models.CharField(max_length=255)
    type = models.CharField(max_length=20, choices=ItemType.choices)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    item_ref_id = models.CharField(max_length=255)
    price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    currency = models.CharField(max_length=3, blank=True, null=True)


class CartItem(TimeStampedModel):
    """CartItem model."""

    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name='items')
    catalogue_item = models.ForeignKey(CatalogueItem, on_delete=models.PROTECT)
    original_price = models.DecimalField(max_digits=10, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    coupon = models.ForeignKey(Coupon, on_delete=models.SET_NULL, null=True, blank=True)
    final_price = models.DecimalField(max_digits=10, decimal_places=2)


class Invoice(TimeStampedModel):
    """Invoice model."""

    class InvoiceStatus(models.TextChoices):
        """Invoice statuses."""

        DRAFT = 'draft'
        PAID = 'paid'
        CANCELLED = 'cancelled'

    invoice_number = models.CharField(max_length=255, unique=True)
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name='invoices')
    status = models.CharField(max_length=20, choices=InvoiceStatus.choices, default=InvoiceStatus.DRAFT)
    total = models.DecimalField(max_digits=10, decimal_places=2)
    discount_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    currency = models.CharField(max_length=3)
    paid_at = models.DateTimeField(blank=True, null=True)
    related_transaction = models.ForeignKey(Transaction, on_delete=models.SET_NULL, null=True, blank=True)


class InvoiceItem(TimeStampedModel):
    """InvoiceItem model."""

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='items')
    cart_item = models.ForeignKey(CartItem, on_delete=models.SET_NULL, null=True)
    original_price = models.DecimalField(max_digits=10, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.PositiveIntegerField(default=1)


class CreditMemo(TimeStampedModel):
    """CreditMemo model."""

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='credit_memos')
    total = models.DecimalField(max_digits=10, decimal_places=2)
    reason = models.TextField()
    gateway_refund_transaction_id = models.CharField(max_length=255)
    transaction = models.ForeignKey(Transaction, on_delete=models.SET_NULL, null=True, blank=True)
