"""test for models."""

import pytest
from django.core.exceptions import ValidationError

from zeitlabs_payments.models import AuditLog, Cart


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

        with pytest.raises(ValidationError, match="Missing template parameters for action 'CartFulfillmentError'"):
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
