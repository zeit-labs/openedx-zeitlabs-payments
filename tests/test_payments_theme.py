"""Tests for PaymentsTheme model and theme system."""

from unittest.mock import patch

from django.contrib.sites.models import Site
from django.test import TestCase, override_settings

from zeitlabs_payments.models import PaymentsTheme


class PaymentsThemeModelTest(TestCase):
    """Tests for PaymentsTheme model."""

    def test_default_values(self):
        """Verify all defaults match production settings."""
        theme = PaymentsTheme.objects.create(label='Test')
        data = theme.to_dict()
        assert data['primary'] == '#0B7A4A'
        assert data['primary_rgb'] == '11, 122, 74'
        assert data['secondary'] == '#054D2E'
        assert data['font_family'] == "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
        assert data['white'] == '#FFFFFF'
        assert data['gray_50'] == '#FAFAFA'
        assert data['gray_900'] == '#212121'

    def test_to_dict_has_all_stylesheet_keys(self):
        """Every key in to_dict() must have a matching CSS variable in styles.html."""
        theme = PaymentsTheme.objects.create(label='Test')
        data = theme.to_dict()
        required_keys = {
            'primary', 'primary_rgb', 'secondary',
            'success', 'success_light', 'error', 'error_light',
            'warning', 'warning_light', 'info', 'info_light',
            'white',
            'gray_50', 'gray_100', 'gray_200', 'gray_300', 'gray_400',
            'gray_500', 'gray_600', 'gray_700', 'gray_800', 'gray_900',
            'font_family',
        }
        assert set(data.keys()) == required_keys

    def test_custom_values_persisted(self):
        """Custom hex values are stored and returned correctly."""
        theme = PaymentsTheme.objects.create(
            label='Custom',
            primary='#FF0000',
            primary_rgb='255, 0, 0',
            secondary='#0000FF',
            font_family='monospace',
        )
        data = theme.to_dict()
        assert data['primary'] == '#FF0000'
        assert data['primary_rgb'] == '255, 0, 0'
        assert data['secondary'] == '#0000FF'
        assert data['font_family'] == 'monospace'

    def test_str_returns_label(self):
        """__str__ returns the label field."""
        theme = PaymentsTheme.objects.create(label='My Brand')
        assert str(theme) == 'My Brand'

    def test_save_keeps_one_per_site(self):
        """Saving a second theme for the same site replaces the first."""
        site = Site.objects.get_current()
        t1 = PaymentsTheme.objects.create(site=site, label='First')
        t2 = PaymentsTheme.objects.create(site=site, label='Second')
        assert not PaymentsTheme.objects.filter(pk=t1.pk).exists()
        assert PaymentsTheme.objects.filter(pk=t2.pk).exists()

    def test_get_active_returns_db_record(self):
        """When a DB record exists for the site, get_active returns it."""
        site = Site.objects.get_current()
        PaymentsTheme.objects.create(site=site, primary='#ABCDEF', label='From DB')
        theme = PaymentsTheme.get_active()
        assert theme['primary'] == '#ABCDEF'

    @override_settings(ZEITLABS_PAYMENTS_THEME={'primary': '#FROM_SETTINGS'})
    def test_get_active_returns_settings_when_no_db_record(self):
        """When no DB record exists, get_active falls back to Django settings."""
        PaymentsTheme.objects.all().delete()
        theme = PaymentsTheme.get_active()
        assert theme['primary'] == '#FROM_SETTINGS'

    def test_get_active_returns_empty_dict_on_failure(self):
        """When DB and settings both fail, return empty dict."""
        PaymentsTheme.objects.all().delete()
        with patch.object(Site.objects, 'get_current', side_effect=Exception('DB down')):
            theme = PaymentsTheme.get_active()
            assert theme == {}
