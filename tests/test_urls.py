"""Tests for the plugin's URL routes."""

import pytest
from django.urls import Resolver404, get_resolver, resolve, reverse

from zeitlabs_payments import views

CART_ID = '3f1b8d0c-4e2a-4c5b-9a7d-1e2f3a4b5c6d'
INVOICE_TOKEN = '7c9e6679-7425-40de-944b-e07fc1f90ae7'

ROUTES = [
    ('checkout', {}, (), '/checkout/v1/checkout/', views.CheckoutView),
    (
        'initiate-payment', {'provider': 'dummy', 'cart_id': CART_ID}, (),
        f'/payment/v1/initiate/dummy/{CART_ID}/', views.InitiatePaymentView,
    ),
    ('payment-error', {}, ('TEST-100001',), '/payment/v1/error/TEST-100001/', views.PaymentErrorView),
    ('payment-decline', {}, ('TEST-100001',), '/payment/v1/decline/TEST-100001/', views.PaymentDeclineView),
    (
        'payment-reconciliation', {}, ('TEST-100001',), '/payment/v1/reconciliation/TEST-100001/',
        views.PaymentReconciliationView,
    ),
    ('invoice', {}, ('TEST-100001',), '/payment/v1/invoice/TEST-100001/', views.InvoiceView),
    (
        'public-invoice', {'token': INVOICE_TOKEN}, (),
        f'/payment/v2/invoice/{INVOICE_TOKEN}/', views.PublicInvoiceView,
    ),
    ('order-history', {}, (), '/payment/v1/history/', views.OrderHistoryView),
    ('order-history-api', {}, ('3',), '/api/payment/v1/history/3/', views.OrderHistoryApiView),
    ('cart-add', {}, (), '/api/cart/v1/cart/', views.CartView),
    (
        'processor-return', {}, ('dummy',), '/payment/v1/processor/dummy/return/', views.processor_return_view,
    ),
    (
        'processor-status', {}, ('dummy',), '/payment/v1/processor/dummy/status/', views.processor_status_view,
    ),
]


def route_name_for(path):
    """Return the name of the route that serves ``path``, or None when no route matches it."""
    try:
        return resolve(path).url_name
    except Resolver404:
        return None


def registered_route_names():
    """Return every named route registered under the plugin's namespace."""
    names = set()

    def walk(patterns, namespace=''):
        for pattern in patterns:
            if hasattr(pattern, 'url_patterns'):
                walk(pattern.url_patterns, ':'.join(filter(None, [namespace, pattern.namespace])))
            elif pattern.name:
                names.add(':'.join(filter(None, [namespace, pattern.name])))

    walk(get_resolver().url_patterns)
    return {name for name in names if name.startswith('zeitlabs_payments:')}


@pytest.mark.parametrize('name, kwargs, args, expected_path, expected_view', ROUTES)
def test_every_route_reverses_and_resolves_to_its_view(name, kwargs, args, expected_path, expected_view):
    """
    Verify that each named route reverses to the path it advertises and resolves back to the view that serves it.

    Templates and processors address these by name, so a renamed route or a rewritten pattern breaks callers
    that never mention the path.
    """
    path = reverse(f'zeitlabs_payments:{name}', args=args, kwargs=kwargs)

    assert path == expected_path, f'{name} should reverse to {expected_path}'
    func = resolve(path).func
    assert getattr(func, 'view_class', func) is expected_view, f'{name} should be served by {expected_view.__name__}'


def test_every_registered_route_is_covered_here():
    """
    Verify that ROUTES lists every route the plugin registers, so a new route cannot skip the test above.

    Without this, a route added to urls.py is never pinned to its path or its view, and nothing fails to say so.
    """
    covered = {f'zeitlabs_payments:{case[0]}' for case in ROUTES}

    assert registered_route_names() == covered, \
        'A route was added or removed; update ROUTES so it keeps its path and view pinned'


@pytest.mark.parametrize(
    'token, usecase',
    [
        ('3f1b8d0c4e2a4c5b9a7d1e2f3a4b5c6d', 'a UUID with its hyphens dropped'),
        ('3f1b8d0c-4e2a-4c5b-9a7d-1e2f3a4b5c6', 'one hex digit short'),
        ('3f1b8d0c-4e2a-4c5b-9a7d-1e2f3a4b5c6dd', 'one hex digit too many'),
        ('3F1B8D0C-4E2A-4C5B-9A7D-1E2F3A4B5C6D', 'uppercase hex, a second spelling of one token'),
        ('3f1b8d0c-4e2a-4c5b-9a7d1e2f-3a4b5c6d', 'hyphens in the wrong places'),
        ('not-a-token', 'a value that is not hexadecimal at all'),
    ],
)
def test_public_invoice_route_matches_only_a_canonical_token(token, usecase):
    """
    Verify that anything other than a canonically spelled UUID fails to route rather than reaching the view.

    A looser pattern would reach a UUID column that raises on what it cannot parse, turning a 404 into a 500,
    and would let one invoice answer at several spellings because uuid.UUID() drops hyphens wherever they fall.
    """
    assert route_name_for(f'/payment/v2/invoice/{token}/') is None, f'Failed usecase: {usecase}'


def test_v1_invoice_route_matches_anything_nested_below_it():
    """
    Verify that the v1 invoice pattern really does capture a nested path, which is why v2 sits beside it.

    The pattern ends in '(.+)', which matches slashes too. A token route declared under that path rather than
    as its own version would be read as an invoice number.
    """
    assert route_name_for('/payment/v1/invoice/TEST-100001/nested/deeper/') == 'invoice', \
        'The v1 pattern should swallow a nested path as an invoice number'
