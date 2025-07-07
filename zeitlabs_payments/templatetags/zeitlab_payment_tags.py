"""
This module provides custom Django template tags.

These tags can be used to extend template functionality, such as generating dynamic
content like QR codes or handling other custom template logic within Django views.
Additional tags can be added to further enhance template capabilities.
"""
import qrcode
import qrcode.image.svg
from crum import get_current_request
from django import template
from django.urls import reverse
from django.utils.safestring import mark_safe

register = template.Library()


@register.simple_tag
def generate_invoice_qr_code(invoice_number: str) -> str:
    """
    Generate a QR code in SVG format for the checkout receipt page.

    The function creates a checkout receipt URL that includes the given order number
    as a query parameter. The QR code is generated for this URL and returned as an SVG image.

    Args:
        order_number (int): The order number to include in the checkout receipt URL.

    Returns:
        str: A QR code in SVG format, rendered as a string.
    """
    receipt_url = reverse(
        'zeitlabs_payments:invoice',
        args=[invoice_number]
    )
    request = get_current_request()
    url = request.build_absolute_uri(receipt_url)

    qr = qrcode.QRCode(image_factory=qrcode.image.svg.SvgPathImage)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image()

    return mark_safe(img.to_string(encoding='unicode'))
