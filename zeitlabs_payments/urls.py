"""
URLs for zeitlabs_payments.
"""
from django.urls import include, re_path

from zeitlabs_payments import views
from zeitlabs_payments.providers.manual_payment import views as manual_payment_views

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
        r'^payment/v1/success/(.+)/$',
        views.PaymentSuccessView.as_view(),
        name='payment-success'
    ),

    re_path(
        r'^payment/v1/invoice/(.+)/$',
        views.InvoiceView.as_view(),
        name='invoice'
    ),

    re_path(r'^api/cart/v1/cart/$', views.CartView.as_view(), name='cart-add'),
    re_path(r'^api/payment/v1/manual/$', manual_payment_views.ManualPaymentView.as_view(), name='manual-payment'),

    # Stripe payment provider URLs
    re_path(r'^payments/stripe/', include('zeitlabs_payments.providers.stripe_payment.urls')),
]
