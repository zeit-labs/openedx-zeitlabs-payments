"""Tests for the apps module"""
# pylint: disable=duplicate-code
from zeitlabs_payments.apps import ZeitlabsPaymentsConfig
from zeitlabs_payments.settings import common_production


def test_app_name():
    """Test that the app name is correct"""
    assert ZeitlabsPaymentsConfig.name == 'zeitlabs_payments'


def test_app_config():
    """Verify that the app is compatible with edx-platform plugins"""
    assert ZeitlabsPaymentsConfig.plugin_app == {
        'settings_config': {
            'lms.djangoapp': {
                'production': {
                    'relative_path': 'settings.common_production',
                },
            },
        },
        'url_config': {
            'lms.djangoapp': {
                'namespace': 'zeitlabs_payments',
                'regex': '^',
                'relative_path': 'urls',
            },
        },
    }, 'The app is not compatible with edx-platform plugins!'


def test_common_production_plugin_settings():
    """Verify that used settings contain the method plugin_settings"""
    assert hasattr(common_production, 'plugin_settings'), 'settings is missing the method plugin_settings!'
