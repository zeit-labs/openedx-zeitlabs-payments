"""Common Settings"""
from typing import Any

ENROLLMENT_FILTER_TYPE = 'org.openedx.learning.course.enrollment.started.v1'
ENROLLMENT_FILTER_STEP = 'zeitlabs_payments.filters.BlockUnpaidCourseEnrollment'


def plugin_settings(settings: Any) -> None:
    """
    plugin settings
    """
    settings.ZEITLABS_PAYMENTS_SETTINGS = getattr(
        settings,
        'ZEITLABS_PAYMENTS_SETTINGS',
        {},
    )
    settings.IS_ZEITLABS_PAYMENTS_ENABLED = getattr(
        settings,
        'IS_ZEITLABS_PAYMENTS_ENABLED',
        False,
    )
    settings.OVERRIDE_ECOMMERCE_SERVICE_CHECKOUT_PAGE = (
        'zeitlabs_payments.pluggable_overrides.override_ecommerce_checkout_page'
    )

    filters_config = getattr(settings, 'OPEN_EDX_FILTERS_CONFIG', {})
    enrollment_config = filters_config.setdefault(ENROLLMENT_FILTER_TYPE, {
        'fail_silently': False,
        'pipeline': [],
    })
    if ENROLLMENT_FILTER_STEP not in enrollment_config['pipeline']:
        enrollment_config['pipeline'].append(ENROLLMENT_FILTER_STEP)
    settings.OPEN_EDX_FILTERS_CONFIG = filters_config
