"""Common Settings"""
from typing import Any

_PAYMENTS_THEME_DEFAULTS = {
    'primary': '#0B7A4A',
    'primary_rgb': '11, 122, 74',
    'secondary': '#054D2E',
    'success': '#0B7A4A',
    'success_light': '#E8F5E9',
    'error': '#D32F2F',
    'error_light': '#FFEBEE',
    'warning': '#F57C00',
    'warning_light': '#FFF3E0',
    'info': '#1976D2',
    'info_light': '#E3F2FD',
    'white': '#FFFFFF',
    'gray_50': '#FAFAFA',
    'gray_100': '#F5F5F5',
    'gray_200': '#EEEEEE',
    'gray_300': '#E0E0E0',
    'gray_400': '#BDBDBD',
    'gray_500': '#9E9E9E',
    'gray_600': '#757575',
    'gray_700': '#616161',
    'gray_800': '#424242',
    'gray_900': '#212121',
    'font_family': "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
}


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
    settings.ZEITLABS_PAYMENTS_THEME = getattr(
        settings,
        'ZEITLABS_PAYMENTS_THEME',
        _PAYMENTS_THEME_DEFAULTS,
    )
    settings.OVERRIDE_ECOMMERCE_SERVICE_CHECKOUT_PAGE = (
        'zeitlabs_payments.pluggable_overrides.override_ecommerce_checkout_page'
    )
