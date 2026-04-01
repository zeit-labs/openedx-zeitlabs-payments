"""
Custom Django template tags and filters for zeitlabs-payments.

Provides QR-code generation, currency formatting, invoice URL
building, and payment-status display helpers used across payment
templates.
"""

from babel.core import Locale
from babel.numbers import get_currency_symbol
from django import template
from django.urls import reverse
from django.utils.translation import get_language

from zeitlabs_payments.helpers import generate_invoice_qr_code as generate_qr_code
from zeitlabs_payments.models import Cart

register = template.Library()


@register.simple_tag
def generate_invoice_qr_code(invoice_number: str) -> str:
    """
    Generate a QR code in SVG format for the checkout receipt page.

    The function creates a checkout receipt URL that includes the given order number
    as a query parameter. The QR code is generated for this URL and returned as an SVG image.

    :params order_number (int): The order number to include in the checkout receipt URL.
    :returns: str: A QR code in SVG format, rendered as a string.
    """
    return generate_qr_code(invoice_number)


@register.filter
def currency_symbol(code: str) -> str:
    """
    Render a currency symbol based on the current active Django language.

    Example: ``"sar"`` -> ``"ر.س."`` when the current language is Arabic.
    """
    if not code:
        return ''

    code = str(code).upper()

    lang = get_language() or 'en'
    babel_locale = lang.replace('-', '_')

    try:
        Locale.parse(babel_locale)
    except Exception:  # pylint: disable=broad-except
        babel_locale = lang.split('-')[0] or 'en'

    try:
        return get_currency_symbol(code, locale=babel_locale)
    except Exception:  # pylint: disable=broad-except
        return code


@register.filter
def invoice_url(invoice_number: str) -> str:
    """Return the URL path for the given invoice number."""
    return reverse('zeitlabs_payments:invoice', args=[invoice_number])


@register.filter
def payment_status_display(status_value: str) -> str:
    """Return the human-readable, translatable label for a cart payment status."""
    return Cart.get_status_display(status_value)
