"""Test base processsor"""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.http import HttpRequest

from zeitlabs_payments.exceptions import GatewayError, InvalidCartError, InvoiceError
from zeitlabs_payments.models import Cart, CatalogueItem, Invoice, InvoiceItem
from zeitlabs_payments.providers.base import BaseProcessor

User = get_user_model()


@pytest.fixture
def base_processor():
    """processor fixture"""
    return BaseProcessor()


@pytest.fixture
def cart():
    """cart fixture"""
    item = CatalogueItem.objects.get(sku='custom-sku-1')
    user_cart = Cart.objects.create(user=User.objects.get(id=3), status=Cart.Status.PROCESSING)
    user_cart.items.create(
        catalogue_item=item,
        original_price=item.price,
        final_price=item.price
    )
    return user_cart


def test_payment_view_raises_not_implemented(base_processor):  # pylint: disable=redefined-outer-name
    request = HttpRequest()
    with pytest.raises(NotImplementedError):
        base_processor.payment_view(cart={}, request=request)


def test_get_transaction_parameters_raises_not_implemented(base_processor):  # pylint: disable=redefined-outer-name
    request = HttpRequest()
    with pytest.raises(NotImplementedError):
        base_processor.get_transaction_parameters(cart={}, request=request)


@pytest.mark.django_db
def test_get_cart_valid_and_invalid(base_processor, cart):  # pylint: disable=redefined-outer-name
    assert base_processor.get_cart(cart.id).id == cart.id

    with pytest.raises(InvalidCartError):
        base_processor.get_cart('invalid-id')

    with pytest.raises(InvalidCartError):
        base_processor.get_cart(999999)


@pytest.mark.django_db
def test_get_site_valid_and_invalid(base_processor):  # pylint: disable=redefined-outer-name
    site = Site.objects.get_current()
    assert base_processor.get_site(site.id).id == site.id

    with pytest.raises(GatewayError):
        base_processor.get_site('invalid-id')

    with pytest.raises(GatewayError):
        base_processor.get_site(999999)


@pytest.mark.django_db
def test_create_invoice_raises_for_non_paid_cart(base_processor, cart):  # pylint: disable=redefined-outer-name
    request = HttpRequest()
    with pytest.raises(InvoiceError):
        base_processor.create_invoice(cart, request)


@pytest.mark.django_db
def test_create_invoice_success(base_processor, cart):  # pylint: disable=redefined-outer-name
    request = HttpRequest()
    cart.status = Cart.Status.PAID
    invoice = base_processor.create_invoice(cart, request)

    assert invoice.cart == cart
    assert invoice.total == cart.total
    assert invoice.status == Invoice.InvoiceStatus.PAID
    assert invoice.invoice_number is not None

    assert InvoiceItem.objects.filter(invoice=invoice).count() == cart.items.count()
