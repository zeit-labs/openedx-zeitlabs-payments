"""Common Settings"""
import os
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
    settings.OVERRIDE_ECOMMERCE_SERVICE_CHECKOUT_PAGE = (
        'zeitlabs_payments.pluggable_overrides.override_ecommerce_checkout_page'
    )

    import zeitlabs_payments  # pylint: disable=import-outside-toplevel
    zeitlabs_payments_locale_path = os.path.join(
        os.path.dirname(zeitlabs_payments.__file__),
        'locale'
    )
    if zeitlabs_payments_locale_path not in settings.LOCALE_PATHS:
        settings.LOCALE_PATHS = list(settings.LOCALE_PATHS) + [zeitlabs_payments_locale_path]
