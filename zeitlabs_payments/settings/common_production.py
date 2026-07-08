"""Common Settings"""
from typing import Any


def plugin_settings(settings: Any) -> None:
    """
    plugin settings
    """
    settings.ZEITLABS_PAYMENTS_SETTINGS = getattr(
        settings,
        'ZEITLABS_PAYMENTS_SETTINGS',
        {},
    )
    settings.IS_ZEITLABS_PAYMENTS_ENABLED = getattr(
        settings,
        'IS_ZEITLABS_PAYMENTS_ENABLED',
        False,
    )
    settings.ZEITLABS_MANUAL_PAYMENT_ENABLED = getattr(
        settings,
        'ZEITLABS_MANUAL_PAYMENT_ENABLED',
        False,
    )
    settings.OVERRIDE_ECOMMERCE_SERVICE_CHECKOUT_PAGE = (
        'zeitlabs_payments.pluggable_overrides.override_ecommerce_checkout_page'
    )
