"""URL patterns for Stripe payment processor."""
from django.urls import path

from .views import StripeCancelView, StripeCheckoutView, StripeSuccessView, StripeWebhookView

urlpatterns = [
    path(
        'checkout/<int:cart_id>/',
        StripeCheckoutView.as_view(),
        name='stripe-checkout'
    ),
    path(
        'success/',
        StripeSuccessView.as_view(),
        name='stripe-success'
    ),
    path(
        'cancel/',
        StripeCancelView.as_view(),
        name='stripe-cancel'
    ),
    path(
        'webhook/',
        StripeWebhookView.as_view(),
        name='stripe-webhook'
    ),
]
