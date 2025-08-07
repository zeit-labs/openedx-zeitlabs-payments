"""Test zeitlabs payment tags"""

from unittest.mock import MagicMock, patch

from django.template import Context, Template


@patch('zeitlabs_payments.templatetags.zeitlab_payment_tags.get_current_request')
@patch('zeitlabs_payments.templatetags.zeitlab_payment_tags.reverse')
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
