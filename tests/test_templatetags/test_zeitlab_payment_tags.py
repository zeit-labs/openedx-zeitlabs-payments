"""Test zeitlabs payment tags"""

from unittest.mock import MagicMock, patch

from django.template import Context, Template


@patch('zeitlabs_payments.helpers.get_current_request')
@patch('zeitlabs_payments.helpers.reverse')
def test_generate_invoice_qr_code_tag(mock_reverse, mock_get_request):
    mock_reverse.return_value = '/invoice/12345/'
    mock_request = MagicMock()
    mock_request.build_absolute_uri.return_value = 'https://example.com/invoice/12345/'
    mock_get_request.return_value = mock_request

    tpl = Template(
        '{% load zeitlab_payment_tags %}'
        '{% generate_invoice_qr_code invoice_number %}'
    )
    ctx = Context({'invoice_number': '12345'})
    rendered = tpl.render(ctx)

    assert rendered.startswith('<svg width="37mm" height="37mm" version="1.1" viewBox="0 0 37 37"')


def test_currency_symbol_filter_with_valid_code():
    """Should render a currency symbol for a known code."""
    tpl = Template('{% load zeitlab_payment_tags %}{{ code|currency_symbol }}')
    rendered = tpl.render(Context({'code': 'USD'}))
    assert '$' in rendered


def test_currency_symbol_filter_with_empty_code():
    """Should return empty string for falsy code."""
    tpl = Template('{% load zeitlab_payment_tags %}{{ code|currency_symbol }}')
    rendered = tpl.render(Context({'code': ''}))
    assert rendered.strip() == ''


@patch(
    'zeitlabs_payments.templatetags.zeitlab_payment_tags.Locale.parse',
    side_effect=Exception('bad locale'),
)
def test_currency_symbol_filter_with_bad_locale(mock_parse):  # pylint: disable=unused-argument
    """Should fall back to language prefix when Locale.parse fails."""
    tpl = Template('{% load zeitlab_payment_tags %}{{ code|currency_symbol }}')
    rendered = tpl.render(Context({'code': 'USD'}))
    assert rendered.strip() != ''


@patch(
    'zeitlabs_payments.templatetags.zeitlab_payment_tags.get_currency_symbol',
    side_effect=Exception('unknown currency'),
)
def test_currency_symbol_filter_returns_code_on_symbol_error(mock_get_symbol):  # pylint: disable=unused-argument
    """Should return raw code when get_currency_symbol raises."""
    tpl = Template('{% load zeitlab_payment_tags %}{{ code|currency_symbol }}')
    rendered = tpl.render(Context({'code': 'XYZ'}))
    assert 'XYZ' in rendered
