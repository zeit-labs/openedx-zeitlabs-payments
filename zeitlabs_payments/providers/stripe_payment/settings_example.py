"""
Example Django settings configuration for Stripe payment processor.

Add these settings to your Django settings file (e.g., settings.py or a production settings file).
"""
import os

# =============================================================================
# STRIPE PAYMENT PROCESSOR SETTINGS
# =============================================================================

# Required: Your Stripe Secret Key (starts with sk_test_ for test mode or sk_live_ for live mode)
# Get this from: https://dashboard.stripe.com/apikeys
STRIPE_SECRET_KEY = 'sk_test_51234567890abcdef...'

# Required: Your Stripe Publishable Key (starts with pk_test_ for test mode or pk_live_ for live mode)
# Get this from: https://dashboard.stripe.com/apikeys
STRIPE_PUBLISHABLE_KEY = 'pk_test_51234567890abcdef...'

# Optional but Recommended: Webhook Signing Secret (starts with whsec_)
# Get this from: https://dashboard.stripe.com/webhooks
# This is used to verify webhook authenticity and prevent replay attacks
STRIPE_WEBHOOK_SECRET = 'whsec_1234567890abcdef...'

# =============================================================================
# ENVIRONMENT-SPECIFIC CONFIGURATION
# =============================================================================

# For production, use environment variables instead of hardcoding:

STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY', '')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')

# =============================================================================
# ZEITLABS PAYMENTS CONFIGURATION
# =============================================================================

# Make sure Stripe URLs are included in your URL configuration
# Add to your main urls.py:
#
# from django.urls import include, path
#
# urlpatterns = [
#     path('payments/stripe/', include('zeitlabs_payments.providers.stripe_payment.urls')),
#     # ... other patterns
# ]

# =============================================================================
# STRIPE WEBHOOK CONFIGURATION
# =============================================================================

# Configure your webhook endpoint in the Stripe Dashboard:
# URL: https://yourdomain.com/payments/stripe/webhook/
#
# Select the following events:
# - checkout.session.completed (Required)
# - payment_intent.succeeded (Optional, for logging)
# - payment_intent.payment_failed (Optional, for error tracking)

# =============================================================================
# TESTING CONFIGURATION
# =============================================================================

# For local development and testing, use Stripe CLI:
# 1. Install: https://stripe.com/docs/stripe-cli
# 2. Login: stripe login
# 3. Forward webhooks: stripe listen --forward-to localhost:8000/payments/stripe/webhook/
# 4. Use test card numbers: https://stripe.com/docs/testing

# Common test cards:
# - Success: 4242 4242 4242 4242
# - Decline: 4000 0000 0000 0002
# - Authentication required: 4000 0025 0000 3155

# =============================================================================
# SECURITY BEST PRACTICES
# =============================================================================

# 1. Never commit API keys to version control
# 2. Always use HTTPS in production (required by Stripe)
# 3. Set STRIPE_WEBHOOK_SECRET to verify webhook signatures
# 4. Rotate keys regularly
# 5. Use separate keys for test and live modes
# 6. Monitor webhook events in Stripe Dashboard
# 7. Set up alerts for failed payments
