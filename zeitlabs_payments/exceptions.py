"""Zeitlasb payments generic exceptions."""


class GatewayError(Exception):
    """Custom exception for payment gateway related errors."""


class CartFulfillmentError(Exception):
    """Custom exception raised when cart fulfillment fails."""


class InvalidCartError(Exception):
    """Custom exception raised when cart is invalid."""


class InvoiceError(Exception):
    """Custom exception raised for invoice errors."""
