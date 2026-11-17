"""
URLs for zeitlabs_payments.
"""
from django.urls import re_path

from zeitlabs_payments import views

app_name = 'zeitlabs_payments'


urlpatterns: list = [
    re_path(
        r'^checkout/v1/checkout/$',
        views.CheckoutView.as_view(),
        name='checkout'
    ),
    re_path(
        r'^payment/v1/initiate/(?P<provider>[\w-]+)/(?P<cart_id>[0-9a-f-]+)/$',
        views.InitiatePaymentView.as_view(),
        name='initiate-payment'
    ),
    re_path(
        r'^payment/v1/error/(.+)/$',
        views.PaymentErrorView.as_view(),
        name='payment-error'
    ),
    re_path(
        r'^payment/v1/decline/(.+)/$',
        views.PaymentDeclineView.as_view(),
        name='payment-decline'
    ),
    # Charged but not reconciled: the gateway took the money but we could not confirm fulfillment.
    # The reference is not optional here -- it is the only thing the learner can quote to support.
    re_path(
        r'^payment/v1/reconciliation/(.+)/$',
        views.PaymentReconciliationView.as_view(),
        name='payment-reconciliation'
    ),

    re_path(
        r'^payment/v1/invoice/(.+)/$',
        views.InvoiceView.as_view(),
        name='invoice'
    ),

    # A new version rather than a path under the v1 invoice route above, whose '(.+)' is greedy and would
    # match anything nested below it, reading a token as though it were an invoice number.
    #
    # The token is matched in its canonical shape rather than as loose hex-and-hyphens. Anything looser reaches
    # a UUID column that raises on what it cannot parse -- a 500 where a 404 belongs -- and lets one invoice
    # answer at several spellings, since uuid.UUID() drops hyphens wherever they fall.
    re_path(
        r'^payment/v2/invoice/(?P<token>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/$',
        views.PublicInvoiceView.as_view(),
        name='public-invoice',
    ),

    re_path(r'^payment/v1/history/$', views.OrderHistoryView.as_view(), name='order-history'),
    re_path(
        r'^api/payment/v1/history/(?P<user_id>[0-9]+)/$',
        views.OrderHistoryApiView.as_view(),
        name='order-history-api',
    ),

    re_path(r'^api/cart/v1/cart/$', views.CartView.as_view(), name='cart-add'),

    # Every processor's return and status views, mounted on one pattern by its slug, so no processor registers routes
    # of its own and the plugin can always reach them.
    re_path(
        r'^payment/v1/processor/(?P<slug>[\w-]+)/return/$',
        views.processor_return_view,
        name='processor-return',
    ),
    re_path(
        r'^payment/v1/processor/(?P<slug>[\w-]+)/status/$',
        views.processor_status_view,
        name='processor-status',
    ),
]
