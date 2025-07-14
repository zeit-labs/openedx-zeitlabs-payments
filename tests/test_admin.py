"""Test for admin"""
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory, TestCase

from zeitlabs_payments.admin import AuditLogAdmin
from zeitlabs_payments.models import AuditLog


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
