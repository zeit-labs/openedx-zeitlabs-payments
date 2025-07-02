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
    settings.ECOMMERCE_PUBLIC_URL_ROOT = getattr(
        settings,
        'ECOMMERCE_PUBLIC_URL_ROOT',
        '',
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
