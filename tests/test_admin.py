"""Test for admin"""

from ddt import data, ddt, unpack
from django.contrib import admin
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse

from zeitlabs_payments.admin import AuditLogAdmin, CatalogueItemAdmin, PaymentProcessorAdminPage
from zeitlabs_payments.models import AuditLog, CatalogueItem

User = get_user_model()


class CatalogueItemAdminTest(TestCase):
    """Tests for CatalogueItemAdmin.get_inline_instances."""

    def setUp(self):
        """Set up admin instance and request factory."""
        self.factory = RequestFactory()
        self.admin_site = AdminSite()
        self.admin_instance = CatalogueItemAdmin(CatalogueItem, self.admin_site)
        self.superuser = User.objects.create_superuser(
            username='inline_admin',
            email='inline_admin@example.com',
            password='pass',
        )

    def _make_request(self):
        """Create a request with a superuser attached."""
        request = self.factory.get('/admin/')
        request.user = self.superuser
        return request

    def test_get_inline_instances_no_obj(self):
        """get_inline_instances returns empty list when obj is None (add view)."""
        result = self.admin_instance.get_inline_instances(self._make_request(), obj=None)
        self.assertEqual(result, [])

    def test_get_inline_instances_paid_course(self):
        """get_inline_instances returns empty list for paid_course type."""
        obj = CatalogueItem(
            sku='TEST-SKU',
            type=CatalogueItem.ItemType.PAID_COURSE,
            price=100,
            currency='IQD',
        )
        result = self.admin_instance.get_inline_instances(self._make_request(), obj=obj)
        self.assertEqual(result, [])

    def test_get_inline_instances_program_bundle(self):
        """get_inline_instances returns inlines for program_bundle type."""
        obj = CatalogueItem(
            sku='TEST-BUNDLE',
            type=CatalogueItem.ItemType.PROGRAM_BUNDLE,
            price=150,
            currency='IQD',
        )
        result = self.admin_instance.get_inline_instances(self._make_request(), obj=obj)
        self.assertGreater(len(result), 0)


class AuditLogAdminTest(TestCase):
    """AuditLog model test."""

    def setUp(self):
        """setup."""
        self.factory = RequestFactory()
        self.admin_site = AdminSite()
        self.audit_log_admin = AuditLogAdmin(AuditLog, self.admin_site)

    def test_has_add_permission(self):
        """test has_add_permission"""
        request = self.factory.get('/admin/myapp/auditlog/add/')
        self.assertFalse(self.audit_log_admin.has_add_permission(request))


@ddt
class PaymentProcessorAdminPageTest(TestCase):
    """PaymentProcessorAdminPage test."""

    def setUp(self):
        self.admin_user = User.objects.create_superuser(username='admin', email='admin@example.com', password='pass')
        self.client.login(username='admin', password='pass')

        self.rf = RequestFactory()
        self.superuser_request = self.rf.get('/')
        self.superuser_request.user = self.admin_user

    def test_processors_page_loads(self):
        url = reverse('admin:zeitlabs_payments_processors')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Payment Processors')

    @data(
        ('no zeitlabs app', [], 0),
        ('empty models list', [{'app_label': 'zeitlabs_payments', 'models': []}], 1),
        ('missing models key', [{'app_label': 'zeitlabs_payments'}], 1),
        (
            'other model only',
            [
                {
                    'app_label': 'zeitlabs_payments',
                    'models': [{'object_name': 'AuditLog'}],
                }
            ],
            1,
        ),
        (
            'already has PaymentProcessors',
            [
                {
                    'app_label': 'zeitlabs_payments',
                    'models': [{'object_name': 'PaymentProcessors'}],
                }
            ],
            1,
        ),
        (
            'duplicate zeitlabs apps',
            [
                {'app_label': 'zeitlabs_payments', 'models': []},
                {'app_label': 'zeitlabs_payments', 'models': []},
            ],
            1,
        ),
    )
    @unpack
    def test_app_list_injection_cases(self, usecase, fake_app_list, expected_count):
        """
        Test applist injection for all cases.
        """
        original = admin.site.get_app_list
        try:
            admin.site.get_app_list = lambda r, app_label=None: fake_app_list.copy()
            PaymentProcessorAdminPage()
            app_list = admin.site.get_app_list(self.superuser_request)

            zp_apps = [a for a in app_list if a.get('app_label') == 'zeitlabs_payments']
            if not zp_apps:
                self.assertEqual(
                    expected_count,
                    0,
                    f'[{usecase}] expected no PaymentProcessors entry',
                )
            else:
                zp_models = zp_apps[0].get('models', [])
                count = sum(1 for m in zp_models if m.get('object_name') == 'PaymentProcessors')
                self.assertEqual(
                    count,
                    expected_count,
                    f'[{usecase}] expected {expected_count} entries, got {count}',
                )
        finally:
            admin.site.get_app_list = original
