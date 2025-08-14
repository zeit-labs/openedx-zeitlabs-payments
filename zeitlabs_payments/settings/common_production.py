"""Common Settings"""
from typing import Any


def plugin_settings(settings: Any) -> None:
    """
    plugin settings
    """
    settings.PAYFORT_SETTINGS = getattr(
        settings,
        'PAYFORT_SETTINGS',
        {},
    )
    settings.INVOICE_PREFIX = getattr(
        settings,
        'INVOICE_PREFIX',
        '',
    )
    settings.ORGANIZATION = getattr(
        settings,
        'ORGANIZATION',
        '',
    )
    settings.CUSTOMER_NUMBER = getattr(
        settings,
        'CUSTOMER_NUMBER',
        '',
    )
    settings.OVERRIDE_ECOMMERCE_SERVICE_CHECKOUT_PAGE = (
        'zeitlabs_payments.pluggable_override_func.override_ecommerce_checkout_page'
    )
