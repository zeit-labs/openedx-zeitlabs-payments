"""Tests for data migrations."""

import importlib

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model

from zeitlabs_payments.models import Cart, Invoice, InvoiceSequence

User = get_user_model()

migration_0003 = importlib.import_module('zeitlabs_payments.migrations.0003_invoicesequence')


def create_invoice(invoice_number):
    """Create a paid invoice with the given number."""
    return Invoice.objects.create(
        invoice_number=invoice_number,
        total=100,
        gross_total=100,
        cart=Cart.objects.create(user=User.objects.get(id=3), status=Cart.Status.PAID),
    )


@pytest.mark.parametrize(
    'invoice_number, expected',
    [
        ('TEST-100005', ('TEST', 100005)),
        ('MY-PREFIX-100005', ('MY-PREFIX', 100005)),
        ('-100005', ('', 100005)),
        ('TEST-INVALID', None),
        ('100005', None),
        ('TEST-', None),
    ]
)
def test_parse_invoice_number(invoice_number, expected):
    """Should split prefix and number, or return None for numbers outside the generated format."""
    assert migration_0003.parse_invoice_number(invoice_number) == expected


@pytest.mark.django_db
def test_seed_invoice_sequences_from_existing_invoices():
    """
    Should create one counter per prefix at the highest number issued, comparing numerically,
    and skip invoice numbers that do not follow the generated format.
    """
    for invoice_number in ('TEST-100005', 'TEST-999999', 'TEST-1000000', 'OTHER-100001', 'TEST-INVALID', 'FREE'):
        create_invoice(invoice_number)
    InvoiceSequence.objects.create(prefix='OTHER', last_number=3)

    migration_0003.seed_invoice_sequences(apps, None)

    assert dict(InvoiceSequence.objects.values_list('prefix', 'last_number')) == {
        'TEST': 1000000,
        'OTHER': 100001,
    }
    assert InvoiceSequence.next_number('TEST') == 1000001


@pytest.mark.django_db
def test_seed_invoice_sequences_without_invoices():
    """Should create nothing on a fresh install; the first invoice then starts at FIRST_NUMBER."""
    migration_0003.seed_invoice_sequences(apps, None)

    assert not InvoiceSequence.objects.exists()
    assert InvoiceSequence.next_number('TEST') == InvoiceSequence.FIRST_NUMBER
