"""Dummy Payment Processor for Tests."""

from django.views import View

from zeitlabs_payments.providers.base import BaseProcessor
from zeitlabs_payments.providers.mixins import PaymentReturnViewMixin, PaymentStatusViewMixin


class DummyProcessor(BaseProcessor):
    """Dummy Processor for testing purposes."""

    SLUG = 'dummy'
    NAME = 'Dummy'
    CHECKOUT_TEXT = 'Pay with Dummy'
    RETURN_VIEW = 'test_utils.dummy_processor.DummyReturnView'
    STATUS_VIEW = 'test_utils.dummy_processor.DummyStatusView'
    GATEWAY_ORDER_ID_PARAM = 'orderId'

    def get_transaction_parameters_base(self, cart, request):  # pylint: disable=useless-parent-delegation
        """Fake."""
        return super().get_transaction_parameters_base(cart, request)

    def get_transaction_parameters(self, cart, request=None, use_client_side_checkout=False, **kwargs):
        """Fake."""
        return {}

    def payment_view(self, cart, request=None, use_client_side_checkout=False, **kwargs):
        """Fake."""
        return None

    def reconcile_payment(self, response, cart):
        """Fake: every payment is for the money the cart expected."""
        return None

    def get_gateway_order_id(self, transaction_parameters):
        """Fake: the order the hand-off parameters name."""
        return transaction_parameters.get('gateway_order_id')


class DummyReturnView(PaymentReturnViewMixin, View):
    """Return view of the dummy processor, configured the way a processor package configures its own."""

    processor_class = DummyProcessor


class DummyStatusView(PaymentStatusViewMixin, View):
    """Status view of the dummy processor, leaving every gateway method to the mixin's default."""

    processor_class = DummyProcessor
