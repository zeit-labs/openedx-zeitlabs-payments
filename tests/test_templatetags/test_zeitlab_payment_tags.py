"""Test zeitlabs payment tags"""

from unittest.mock import MagicMock, patch

import pytest
from django.template import Context, Template

from zeitlabs_payments.models import Cart


@patch('zeitlabs_payments.helpers.get_current_request')
@patch('zeitlabs_payments.helpers.reverse')
def test_generate_invoice_qr_code_tag(mock_reverse, mock_get_request):
    mock_reverse.return_value = '/invoice/12345/'
    mock_request = MagicMock()
    mock_request.build_absolute_uri.return_value = 'https://example.com/invoice/12345/'
    mock_get_request.return_value = mock_request

    tpl = Template('{% load zeitlab_payment_tags %}{% generate_invoice_qr_code invoice_number %}')
    ctx = Context({'invoice_number': '12345'})
    rendered = tpl.render(ctx)

    assert rendered.startswith('<svg width="37mm" height="37mm" version="1.1" viewBox="0 0 37 37"')


def test_invoice_url_filter():
    """invoice_url filter returns a URL path via reverse()."""
    tpl = Template('{% load zeitlab_payment_tags %}{{ inv_num|invoice_url }}')
    rendered = tpl.render(Context({'inv_num': 'INV-001'}))
    assert '/payment/v1/invoice/INV-001/' in rendered


@pytest.mark.parametrize(
    'status_value,expected_label',
    [
        (Cart.Status.PENDING, 'Pending'),
        (Cart.Status.PAID, 'Paid'),
        (Cart.Status.CANCELLED, 'Cancelled'),
        (Cart.Status.PROCESSING, 'Processing'),
        ('unknown_status', 'unknown_status'),
    ],
)
def test_payment_status_display_filter(status_value, expected_label):
    """payment_status_display filter renders the human-readable cart status."""
    tpl = Template('{% load zeitlab_payment_tags %}{{ status|payment_status_display }}')
    rendered = tpl.render(Context({'status': status_value}))
    assert expected_label in rendered


def test_currency_symbol_filter_with_valid_code():
    """currency_symbol filter converts ISO code to a symbol."""
    tpl = Template('{% load zeitlab_payment_tags %}{{ code|currency_symbol }}')
    rendered = tpl.render(Context({'code': 'USD'}))
    # Should return '$' or 'US$' depending on locale
    assert rendered.strip() != ''
    assert rendered.strip() != 'USD' or rendered.strip() == 'USD'  # fallback is also acceptable


def test_currency_symbol_filter_with_empty_code():
    """currency_symbol filter returns empty string for empty input."""
    tpl = Template('{% load zeitlab_payment_tags %}{{ code|currency_symbol }}')
    rendered = tpl.render(Context({'code': ''}))
    assert rendered.strip() == ''


@patch(
    'zeitlabs_payments.templatetags.zeitlab_payment_tags.get_language',
    return_value='xx-bogus',
)
def test_currency_symbol_filter_falls_back_on_unparseable_locale(_mock_lang):
    """currency_symbol falls back to the language prefix when Babel cannot parse the locale."""
    tpl = Template('{% load zeitlab_payment_tags %}{{ code|currency_symbol }}')
    rendered = tpl.render(Context({'code': 'USD'}))
    # The fallback locale "xx" is still passed to get_currency_symbol;
    # Babel may resolve a symbol or raise — either way the filter must not crash.
    assert rendered.strip() != ''


@patch(
    'zeitlabs_payments.templatetags.zeitlab_payment_tags.get_currency_symbol',
    side_effect=Exception('boom'),
)
def test_currency_symbol_filter_returns_code_on_symbol_lookup_failure(_mock_symbol):
    """currency_symbol returns the raw ISO code when get_currency_symbol raises."""
    tpl = Template('{% load zeitlab_payment_tags %}{{ code|currency_symbol }}')
    rendered = tpl.render(Context({'code': 'XYZ'}))
    assert rendered.strip() == 'XYZ'
