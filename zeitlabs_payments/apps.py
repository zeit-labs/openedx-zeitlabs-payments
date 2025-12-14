"""
zeitlabs_payments Django application initialization.
"""

from django.apps import AppConfig


class ZeitlabsPaymentsConfig(AppConfig):
    """
    Configuration for the zeitlabs_payments Django application.
    """

    name = 'zeitlabs_payments'

    # pylint: disable=duplicate-code
    plugin_app = {
        'settings_config': {
            'lms.djangoapp': {
                'production': {
                    'relative_path': 'settings.common_production',
                }
            }
        },

        'url_config': {
            'lms.djangoapp': {
                'namespace': 'zeitlabs_payments',
                'regex': r'^',
                'relative_path': 'urls',
            },
        },
    }

    def ready(self) -> None:
        """
        Apply dynamic runtime modifications.
        """
        from edx_django_utils.plugins import pluggable_override  # pylint: disable=import-outside-toplevel
        from lms.djangoapps.commerce.utils import EcommerceService  # pylint: disable=import-outside-toplevel

        from zeitlabs_payments.providers import registry  # pylint: disable=import-outside-toplevel

        # Monkeypatch `EcommerceService.get_checkout_page_url` to apply the @pluggable_override decorator.
        original_fn = EcommerceService.get_checkout_page_url
        decorated_fn = pluggable_override('OVERRIDE_ECOMMERCE_SERVICE_CHECKOUT_PAGE')(original_fn)
        EcommerceService.get_checkout_page_url = decorated_fn

        registry.load_entrypoint_processors()
