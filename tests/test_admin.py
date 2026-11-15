"""Test for admin"""
import re
from decimal import Decimal

import pytest
from ddt import data, ddt, unpack
from django.contrib import admin
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.messages import get_messages
from django.contrib.sites.models import Site
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from zeitlabs_payments.admin import (
    AuditLogAdmin,
    AuditLogLinkMixin,
    CartAdmin,
    CartLinkMixin,
    CartSearchMixin,
    CatalogueItemAdmin,
    PaymentProcessorAdminPage,
    TransactionLinkMixin,
    ViewOnlyMixin,
)
from zeitlabs_payments.models import (
    AuditLog,
    Cart,
    CartItem,
    CatalogueItem,
    Invoice,
    InvoiceItem,
    PaymentSitePermission,
    Transaction,
    WebhookEvent,
)

User = get_user_model()


class AuditLogAdminTest(TestCase):
    """The audit log admin: what it wires in from the shared mixins, and what is its own."""

    def setUp(self):
        """setup."""
        self.factory = RequestFactory()
        self.admin_site = AdminSite()
        self.audit_log_admin = AuditLogAdmin(AuditLog, self.admin_site)

    def _staffed_log(self):
        """Create one audit log and sign a superuser in, so every refusal below is the admin class's own."""
        staff = User.objects.create_superuser(username='remover', email='remover@example.com', password='pass')
        learner = User.objects.create(username='buyer', email='buyer@example.com')
        cart = Cart.objects.create(user=learner)
        log = AuditLog.objects.create(cart=cart, action=AuditLog.AuditActions.CART_FULFILLED)
        self.client.force_login(staff)
        return staff, log

    def test_the_log_pages_are_view_only_end_to_end(self):
        """
        Verify that the wired-up admin offers no button and refuses every write route, while staying readable.

        Each refusal belongs to the mixin and is covered there. What this settles is the wiring: that the mixin
        sits among the bases, and that nothing this admin declares for itself reopens a route. The bulk delete
        is checked by the surviving row rather than by a status, because Django ignores an action it no longer
        recognises instead of refusing it.
        """
        _, log = self._staffed_log()
        change_url = reverse('admin:zeitlabs_payments_auditlog_change', args=(log.id,))

        response = self.client.get(change_url)

        self.assertNotContains(
            response, '<input type="submit"', msg_prefix='A view-only record should offer nothing to submit'
        )
        self.assertEqual(
            self.client.post(change_url, {'_save': 'Save'}).status_code,
            403,
            'A save posted straight at the change url should still be refused',
        )
        self.assertEqual(
            self.client.post(
                reverse('admin:zeitlabs_payments_auditlog_delete', args=(log.id,)), {'post': 'yes'}
            ).status_code,
            403,
            'The delete route should be refused even for a superuser',
        )
        self.client.post(
            reverse('admin:zeitlabs_payments_auditlog_changelist'),
            {'action': 'delete_selected', '_selected_action': [str(log.id)], 'post': 'yes'},
        )
        self.assertTrue(
            AuditLog.objects.filter(id=log.id).exists(), 'Neither delete route may take the log away'
        )
        self.assertContains(
            response, log.action, msg_prefix='The log itself must still be readable on the page'
        )

    def test_staff_holding_only_the_change_permission_can_still_read_logs(self):
        """
        Verify that refusing to save does not shut out the staff who used to hold the change permission.

        Django answers a view request from the ``view`` or the ``change`` permission on the model, neither of
        which this admin overrides, so an existing team keeps its access and simply loses the buttons.
        """
        reader = User.objects.create_user(
            username='reader', email='reader@example.com', password='pass', is_staff=True
        )
        reader.user_permissions.add(Permission.objects.get(codename='change_auditlog'))
        _, log = self._staffed_log()
        self.client.force_login(reader)

        self.assertEqual(
            self.client.get(reverse('admin:zeitlabs_payments_auditlog_change', args=(log.id,))).status_code,
            200,
            'Staff with the change permission should still be able to read a log',
        )
        self.assertEqual(
            self.client.get(reverse('admin:zeitlabs_payments_auditlog_changelist')).status_code,
            200,
            'Staff with the change permission should still be able to list the logs',
        )

    def test_the_cart_token_reaches_this_search_box(self):
        """
        Verify that the cart token filters the audit log list, resolving against this model's own cart column.

        The token's grammar belongs to the mixin and is covered there. What is under test here is the wiring
        only: that the mixin is among the bases and that its lookup path names a column this model actually
        has. No amount of testing on the mixin can settle that second part for an adopter.
        """
        staff = User.objects.create_superuser(username='searcher', email='searcher@example.com', password='pass')
        learner = User.objects.create(username='buyer', email='buyer@example.com')
        for cart_id in (6, 66):
            cart = Cart.objects.create(id=cart_id, user=learner)
            AuditLog.objects.create(cart=cart, action=AuditLog.AuditActions.CART_FULFILLED)
        self.client.force_login(staff)

        response = self.client.get(reverse('admin:zeitlabs_payments_auditlog_changelist'), {'q': 'cart=6'})

        self.assertEqual(
            [log.cart_id for log in response.context['cl'].queryset],
            [6],
            'The cart token should reach this box and resolve against the audit log cart column',
        )

    def test_the_cart_field_reaches_the_change_page_read_only(self):
        """
        Verify that the mixin's cart field arrives on the change page as read-only HTML.

        How that field renders is the mixin's business. Whether it reaches the page at all is this admin's:
        naming ``cart_link`` in ``fields`` without also naming it in ``readonly_fields`` would put the raw
        foreign key back on the form as a select box.
        """
        staff = User.objects.create_superuser(username='staff', email='staff@example.com', password='pass')
        learner = User.objects.create(username='shopper', email='shopper@example.com')
        cart = Cart.objects.create(user=learner)
        log = AuditLog.objects.create(cart=cart, action=AuditLog.AuditActions.CART_FULFILLED)
        self.client.force_login(staff)

        response = self.client.get(reverse('admin:zeitlabs_payments_auditlog_change', args=(log.id,)))

        cart_url = reverse('admin:zeitlabs_payments_cart_change', args=(cart.id,))
        self.assertContains(
            response, f'<a href="{cart_url}">{cart.id}</a>', html=True,
            msg_prefix='The cart row should reach the page as the mixin renders it',
        )
        self.assertNotContains(
            response, 'name="cart"', msg_prefix='The cart must stay read-only on an audit log'
        )


@ddt
class InvoiceAdminTest(TestCase):
    """The invoice pages: read-only throughout, and linked to the cart and the payment behind them."""

    def setUp(self):
        """setup."""
        self.staff = User.objects.create_superuser(username='staff', email='staff@example.com', password='pass')
        learner = User.objects.create(username='buyer', email='b@example.com')
        self.cart = Cart.objects.create(id=9, user=learner)
        catalogue_item = CatalogueItem.objects.create(
            sku='SKU-1',
            type=CatalogueItem.ItemType.PAID_COURSE,
            title='Demo',
            item_ref_id='course-v1:edX+Demo+2024',
            price=Decimal('99.99'),
            currency='SAR',
        )
        cart_item = CartItem.objects.create(
            cart=self.cart,
            catalogue_item=catalogue_item,
            original_price=Decimal('99.99'),
            final_price=Decimal('99.99'),
        )
        self.transaction = Transaction.objects.create(
            cart=self.cart,
            type=Transaction.TransactionType.PAYMENT,
            status='paid',
            gateway='dummy',
            gateway_transaction_id='TX-77',
            method='card',
            amount=Decimal('99.99'),
            currency='SAR',
        )
        self.invoice = Invoice.objects.create(
            status=Invoice.InvoiceStatus.PAID,
            invoice_number='INV-1',
            cart=self.cart,
            gross_total=Decimal('99.99'),
            total=Decimal('99.99'),
            currency='SAR',
            related_transaction=self.transaction,
        )
        InvoiceItem.objects.create(
            invoice=self.invoice, cart_item=cart_item, original_price=Decimal('99.99'), price=Decimal('99.99')
        )
        unpaid_transaction = Transaction.objects.create(
            cart=self.cart, amount=Decimal('1.00'), gateway='dummy', gateway_transaction_id='TX-78'
        )
        self.unpaid = Invoice.objects.create(
            status=Invoice.InvoiceStatus.PAID,
            invoice_number='INV-2',
            cart=self.cart,
            gross_total=Decimal('1.00'),
            total=Decimal('1.00'),
            currency='SAR',
            related_transaction=unpaid_transaction,
        )
        self.client.force_login(self.staff)

    def _change_page(self, invoice):
        """Fetch the change page for the given invoice."""
        return self.client.get(reverse('admin:zeitlabs_payments_invoice_change', args=(invoice.id,)))

    def test_the_invoice_items_name_the_sku_they_bill(self):
        """Verify that the items inline names each line by the SKU it bills, not by the model's default string."""
        response = self._change_page(self.invoice)

        self.assertContains(
            response, 'SKU-1', msg_prefix='The inline should name the line by the SKU it bills'
        )
        self.assertNotContains(
            response, 'CartItem object', msg_prefix='The default __str__ should not reach the page'
        )

    def test_the_cart_field_reaches_both_of_this_admins_pages(self):
        """
        Verify that the mixin's cart field is wired into the list and the details view alike.

        How the field renders belongs to the mixin. Whether it is named in ``list_display`` and in ``fields``,
        and whether its lookup resolves against an invoice, is this admin's to get right.
        """
        cart_url = reverse('admin:zeitlabs_payments_cart_change', args=(self.cart.id,))
        expected = f'<a href="{cart_url}">{self.cart.id}</a>'

        self.assertContains(
            self.client.get(reverse('admin:zeitlabs_payments_invoice_changelist')),
            expected, html=True, msg_prefix='The list should carry the cart as a link',
        )
        self.assertContains(
            self._change_page(self.invoice), expected, html=True,
            msg_prefix='The details view should carry the cart as a link',
        )

    def test_the_cart_token_reaches_this_search_box(self):
        """
        Verify that the cart token filters the invoice list, resolving against this model's own cart column.

        The token's grammar belongs to the mixin and is covered there. What is under test here is the wiring
        only: that the mixin is among the bases and that its lookup path names a column an invoice has.
        """
        look_alike = Cart.objects.create(id=99, user=self.cart.user)
        look_alike_transaction = Transaction.objects.create(
            cart=look_alike, amount=Decimal('5.00'), gateway='dummy', gateway_transaction_id='TX-79'
        )
        Invoice.objects.create(
            status=Invoice.InvoiceStatus.PAID,
            invoice_number='INV-3',
            cart=look_alike,
            gross_total=Decimal('5.00'),
            total=Decimal('5.00'),
            currency='SAR',
            related_transaction=look_alike_transaction,
        )

        response = self.client.get(reverse('admin:zeitlabs_payments_invoice_changelist'), {'q': 'cart=9'})

        self.assertEqual(
            sorted(invoice.invoice_number for invoice in response.context['cl'].queryset),
            ['INV-1', 'INV-2'],
            'cart=9 should return cart 9 invoices alone, not those of every cart containing a 9',
        )

    def test_the_transaction_field_reaches_both_of_this_admins_pages(self):
        """
        Verify that the mixin's transaction field is wired into the list and the details view alike.

        How the field renders belongs to the mixin. Whether it is named in ``list_display`` and in ``fields``
        is this admin's to get right.
        """
        transaction_url = reverse('admin:zeitlabs_payments_transaction_change', args=(self.transaction.id,))
        expected = f'<a href="{transaction_url}">{self.transaction.id}</a>'

        self.assertContains(
            self.client.get(reverse('admin:zeitlabs_payments_invoice_changelist')),
            expected, html=True, msg_prefix='The list should carry the transaction as a link',
        )
        self.assertContains(
            self._change_page(self.invoice), expected, html=True,
            msg_prefix='The details view should carry the transaction as a link',
        )

    @data('discount_total', 'tax_total', 'discount_amount', 'tax_amount')
    def test_the_money_fields_version_one_does_not_use_are_absent(self, field_name):
        """Verify that the discount and tax fields are gone from the invoice and from its lines alike."""
        self.assertNotContains(
            self._change_page(self.invoice),
            field_name,
            msg_prefix=f'{field_name} should not appear anywhere on the invoice page',
        )

    def test_the_invoice_pages_are_view_only_end_to_end(self):
        """
        Verify that the wired-up admin offers no button and refuses every write route, while staying readable.

        Each refusal belongs to the mixin and is covered there. What this settles is the wiring: that the mixin
        sits among the bases of the invoice and of its lines alike, and that nothing either declares for itself
        reopens a route. The bulk delete is checked by the surviving row rather than by a status, because
        Django ignores an action it no longer recognises instead of refusing it.
        """
        change_url = reverse('admin:zeitlabs_payments_invoice_change', args=(self.invoice.id,))

        response = self.client.get(change_url)

        self.assertNotContains(
            response, '<input type="submit"', msg_prefix='A view-only record should offer nothing to submit'
        )
        self.assertEqual(
            self.client.get(reverse('admin:zeitlabs_payments_invoice_add')).status_code,
            403,
            'Raising an invoice by hand should be refused outright',
        )
        self.assertEqual(
            self.client.post(change_url, {'_save': 'Save'}).status_code,
            403,
            'A save posted straight at the change url should still be refused',
        )
        self.assertEqual(
            self.client.post(
                reverse('admin:zeitlabs_payments_invoice_delete', args=(self.invoice.id,)), {'post': 'yes'}
            ).status_code,
            403,
            'The delete route should be refused even for a superuser',
        )
        self.client.post(
            reverse('admin:zeitlabs_payments_invoice_changelist'),
            {'action': 'delete_selected', '_selected_action': [str(self.invoice.id)], 'post': 'yes'},
        )
        self.assertTrue(
            Invoice.objects.filter(id=self.invoice.id).exists(), 'Neither delete route may take the invoice away'
        )
        self.assertContains(
            response, self.invoice.invoice_number, msg_prefix='The invoice itself must still be readable'
        )


@ddt
class InvoiceItemAdminTest(TestCase):
    """The invoice line pages: named by id, linked to the bill and the cart row behind them, and view only."""

    def setUp(self):
        """setup."""
        staff = User.objects.create_superuser(username='staff', email='staff@example.com', password='pass')
        learner = User.objects.create(username='buyer', email='b@example.com')
        cart = Cart.objects.create(id=9, user=learner)
        catalogue_item = CatalogueItem.objects.create(
            sku='SKU-1',
            type=CatalogueItem.ItemType.PAID_COURSE,
            title='Demo',
            item_ref_id='course-v1:edX+Demo+2024',
            price=Decimal('99.99'),
            currency='SAR',
        )
        self.cart_item = CartItem.objects.create(
            cart=cart,
            catalogue_item=catalogue_item,
            original_price=Decimal('99.99'),
            final_price=Decimal('99.99'),
        )
        transaction = Transaction.objects.create(
            cart=cart, amount=Decimal('99.99'), gateway='dummy', gateway_transaction_id='TX-1'
        )
        self.invoice = Invoice.objects.create(
            status=Invoice.InvoiceStatus.PAID,
            invoice_number='INV-001',
            cart=cart,
            gross_total=Decimal('99.99'),
            total=Decimal('99.99'),
            currency='SAR',
            related_transaction=transaction,
        )
        self.line = InvoiceItem.objects.create(
            invoice=self.invoice,
            cart_item=self.cart_item,
            original_price=Decimal('99.99'),
            price=Decimal('99.99'),
        )
        self.client.force_login(staff)

    def _list_page(self):
        """Fetch the invoice line list."""
        return self.client.get(reverse('admin:zeitlabs_payments_invoiceitem_changelist'))

    def _change_page(self):
        """Fetch the change page for the line under test."""
        return self.client.get(reverse('admin:zeitlabs_payments_invoiceitem_change', args=(self.line.id,)))

    def test_the_list_carries_the_line_id(self):
        """Verify that the list names each line by its own id, which is the only thing that identifies one."""
        line_url = reverse('admin:zeitlabs_payments_invoiceitem_change', args=(self.line.id,))

        self.assertContains(
            self._list_page(),
            f'<a href="{line_url}">{self.line.id}</a>',
            html=True,
            msg_prefix='The list should carry the line id',
        )

    def test_the_invoice_is_a_link_named_by_its_number(self):
        """Verify that the invoice column names the bill by its number and reaches it in one click."""
        invoice_url = reverse('admin:zeitlabs_payments_invoice_change', args=(self.invoice.id,))
        expected = f'<a href="{invoice_url}">{self.invoice.invoice_number}</a>'

        self.assertContains(
            self._list_page(), expected, html=True, msg_prefix='The list should name the invoice by its number'
        )
        self.assertContains(
            self._change_page(), expected, html=True, msg_prefix='The details view should name it the same way'
        )
        self.assertNotContains(
            self._list_page(), 'Invoice object', msg_prefix='The default __str__ should not reach the page'
        )

    def test_the_cart_item_is_a_link_named_by_its_id(self):
        """Verify that the cart item column is the bare id as a link, in place of the model's default string."""
        cart_item_url = reverse('admin:zeitlabs_payments_cartitem_change', args=(self.cart_item.id,))
        expected = f'<a href="{cart_item_url}">{self.cart_item.id}</a>'

        self.assertContains(
            self._list_page(), expected, html=True, msg_prefix='The list should carry the cart item as a link'
        )
        self.assertContains(
            self._change_page(), expected, html=True, msg_prefix='The details view should carry it too'
        )
        self.assertNotContains(
            self._list_page(), 'CartItem object', msg_prefix='The default __str__ should not reach the page'
        )

    def test_a_line_whose_cart_item_is_gone_shows_the_empty_marker(self):
        """
        Verify that a line with no cart item reads as empty rather than as the word None.

        The cart item is dropped to null when the row it was billed as is deleted, so a paid invoice outlives
        the cart it came from and its lines have to render without one.
        """
        orphan = InvoiceItem.objects.create(
            invoice=self.invoice, cart_item=None, original_price=Decimal('1.00'), price=Decimal('1.00')
        )

        response = self.client.get(reverse('admin:zeitlabs_payments_invoiceitem_change', args=(orphan.id,)))

        self.assertNotContains(
            response, '>None<', msg_prefix='An absent cart item should not reach the page as the word None'
        )
        self.assertContains(response, 'Cart item:', msg_prefix='The cart item row should still be present')

    @data('discount_amount', 'tax_amount')
    def test_the_money_fields_version_one_does_not_use_are_absent(self, field_name):
        """Verify that the discount and tax amounts are gone from a details view that prices neither."""
        self.assertNotContains(
            self._change_page(),
            field_name,
            msg_prefix=f'{field_name} should not appear anywhere on the invoice line page',
        )

    def test_the_invoice_line_pages_are_view_only_end_to_end(self):
        """
        Verify that the wired-up admin offers no button and refuses every write route, while staying readable.

        Each refusal belongs to the mixin and is covered there. What this settles is the wiring: that the mixin
        sits among the bases, and that nothing this admin declares for itself reopens a route. The bulk delete
        is checked by the surviving row rather than by a status, because Django ignores an action it no longer
        recognises instead of refusing it.
        """
        change_url = reverse('admin:zeitlabs_payments_invoiceitem_change', args=(self.line.id,))

        response = self.client.get(change_url)

        self.assertNotContains(
            response, '<input type="submit"', msg_prefix='A view-only record should offer nothing to submit'
        )
        self.assertEqual(
            self.client.get(reverse('admin:zeitlabs_payments_invoiceitem_add')).status_code,
            403,
            'Adding an invoice line by hand should be refused outright',
        )
        self.assertEqual(
            self.client.post(change_url, {'_save': 'Save'}).status_code,
            403,
            'A save posted straight at the change url should still be refused',
        )
        self.assertEqual(
            self.client.post(
                reverse('admin:zeitlabs_payments_invoiceitem_delete', args=(self.line.id,)), {'post': 'yes'}
            ).status_code,
            403,
            'The delete route should be refused even for a superuser',
        )
        self.client.post(
            reverse('admin:zeitlabs_payments_invoiceitem_changelist'),
            {'action': 'delete_selected', '_selected_action': [str(self.line.id)], 'post': 'yes'},
        )
        self.assertTrue(
            InvoiceItem.objects.filter(id=self.line.id).exists(), 'Neither delete route may take the line away'
        )
        self.assertContains(
            response, self.invoice.invoice_number, msg_prefix='The line itself must still be readable'
        )


class TransactionAdminTest(TestCase):
    """The transaction admin: what it wires in from the shared mixins, and nothing it may write."""

    def setUp(self):
        """setup."""
        staff = User.objects.create_superuser(username='staff', email='staff@example.com', password='pass')
        learner = User.objects.create(username='buyer', email='b@example.com')
        self.cart = Cart.objects.create(id=6, user=learner)
        look_alike = Cart.objects.create(id=66, user=learner)
        self.transaction = self._transaction(self.cart, learner)
        self._transaction(look_alike, learner)
        self.client.force_login(staff)

    @staticmethod
    def _transaction(cart, learner):
        """Create a settled payment against the given cart."""
        return Transaction.objects.create(
            cart=cart,
            type=Transaction.TransactionType.PAYMENT,
            status='paid',
            gateway='dummy',
            gateway_transaction_id=f'TX-{cart.id}',
            method='card',
            amount=Decimal('99.99'),
            currency='SAR',
            initiator_user=learner,
        )

    def test_the_cart_field_reaches_both_of_this_admins_pages(self):
        """
        Verify that the mixin's cart field is wired into the list and the details view alike.

        How the field renders belongs to the mixin. Whether it is named in ``list_display`` and in ``fields``,
        and whether its lookup resolves against a transaction, is this admin's to get right.
        """
        cart_url = reverse('admin:zeitlabs_payments_cart_change', args=(self.cart.id,))
        expected = f'<a href="{cart_url}">{self.cart.id}</a>'

        self.assertContains(
            self.client.get(reverse('admin:zeitlabs_payments_transaction_changelist')),
            expected, html=True, msg_prefix='The list should carry the cart as a link',
        )
        self.assertContains(
            self.client.get(reverse('admin:zeitlabs_payments_transaction_change', args=(self.transaction.id,))),
            expected, html=True, msg_prefix='The details view should carry the cart as a link',
        )

    def test_the_cart_token_reaches_this_search_box(self):
        """
        Verify that the cart token filters the transaction list, resolving against this model's cart column.

        The token's grammar belongs to the mixin and is covered there. What is under test here is the wiring
        only: that the mixin is among the bases and that its lookup path names a column a transaction has.
        """
        response = self.client.get(
            reverse('admin:zeitlabs_payments_transaction_changelist'), {'q': 'cart=6'}
        )

        self.assertEqual(
            [transaction.cart_id for transaction in response.context['cl'].queryset],
            [self.cart.id],
            'cart=6 should return cart 6 alone, not every cart whose id contains a 6',
        )

    def test_the_transaction_pages_are_view_only_end_to_end(self):
        """
        Verify that the wired-up admin offers no button and refuses every write route, while staying readable.

        Each refusal belongs to the mixin and is covered there. What this settles is the wiring: that the mixin
        sits among the bases, and that nothing this admin declares for itself reopens a route. The bulk delete
        is checked by the surviving row rather than by a status, because Django ignores an action it no longer
        recognises instead of refusing it.
        """
        change_url = reverse('admin:zeitlabs_payments_transaction_change', args=(self.transaction.id,))

        response = self.client.get(change_url)

        self.assertNotContains(
            response, '<input type="submit"', msg_prefix='A view-only record should offer nothing to submit'
        )
        self.assertEqual(
            self.client.get(reverse('admin:zeitlabs_payments_transaction_add')).status_code,
            403,
            'Recording a transaction by hand should be refused outright',
        )
        self.assertEqual(
            self.client.post(change_url, {'_save': 'Save'}).status_code,
            403,
            'A save posted straight at the change url should still be refused',
        )
        self.assertEqual(
            self.client.post(
                reverse('admin:zeitlabs_payments_transaction_delete', args=(self.transaction.id,)),
                {'post': 'yes'},
            ).status_code,
            403,
            'The delete route should be refused even for a superuser',
        )
        self.client.post(
            reverse('admin:zeitlabs_payments_transaction_changelist'),
            {'action': 'delete_selected', '_selected_action': [str(self.transaction.id)], 'post': 'yes'},
        )
        self.assertTrue(
            Transaction.objects.filter(id=self.transaction.id).exists(),
            'Neither delete route may take the transaction away',
        )
        self.assertContains(
            response,
            self.transaction.gateway_transaction_id,
            msg_prefix='The transaction itself must still be readable on the page',
        )


class TransactionLinkMixinTest(TestCase):
    """
    The linked transaction field, owned by the mixin rather than by whichever admin happens to show it.

    Adopted here by a webhook event, whose ``related_transaction`` is the only nullable one in the app and so
    the only row that can reach the field's empty-marker branch at all.
    """

    def setUp(self):
        """setup."""
        class AdoptingAdmin(TransactionLinkMixin, admin.ModelAdmin):
            """A stand-in for any admin that names related_transaction_link among its fields."""

        self.model_admin = AdoptingAdmin(WebhookEvent, AdminSite())
        learner = User.objects.create(username='buyer', email='b@example.com')
        cart = Cart.objects.create(user=learner)
        self.transaction = Transaction.objects.create(
            cart=cart,
            type=Transaction.TransactionType.PAYMENT,
            status='paid',
            gateway='dummy',
            gateway_transaction_id='TX-1',
            method='card',
            amount=Decimal('99.99'),
            currency='SAR',
        )

    def _event(self, transaction):
        """Create a webhook event tied to the given transaction, or to none."""
        return WebhookEvent.objects.create(
            gateway='dummy', event_type='payment.settled', payload={}, related_transaction=transaction
        )

    def test_the_field_is_the_transaction_id_hyperlinked_to_that_transaction(self):
        """Verify that the field renders the bare transaction id wrapped in a link to its own admin page."""
        event = self._event(self.transaction)

        expected_url = reverse('admin:zeitlabs_payments_transaction_change', args=(self.transaction.id,))
        self.assertEqual(
            self.model_admin.related_transaction_link(event),
            f'<a href="{expected_url}">{self.transaction.id}</a>',
            'The field should be the id alone, linked to the payment it names',
        )

    def test_the_field_is_the_empty_marker_when_the_row_has_no_transaction(self):
        """
        Verify that a row with no transaction yields the admin's empty marker rather than None.

        Django pipes a read-only callable through ``linebreaksbr``, which has no empty-value handling of its
        own, so returning None would reach a change page as the literal word.
        """
        event = self._event(None)

        self.assertEqual(
            self.model_admin.related_transaction_link(event),
            self.model_admin.get_empty_value_display(),
            'A row with no transaction should read as an empty field, not as a link to nothing',
        )

    def test_the_field_costs_no_query(self):
        """
        Verify that rendering the field touches the database not at all.

        The id is read off the row's own column rather than by following the foreign key, which is what lets
        an admin carry this field without a join and without one query per row.
        """
        self._event(self.transaction)
        event = WebhookEvent.objects.get(related_transaction=self.transaction)

        with self.assertNumQueries(0):
            self.model_admin.related_transaction_link(event)


@ddt
class PaymentProcessorAdminPageTest(TestCase):
    """PaymentProcessorAdminPage test."""

    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            username='admin', email='admin@example.com', password='pass'
        )
        self.client.login(username='admin', password='pass')

        self.rf = RequestFactory()
        self.superuser_request = self.rf.get('/')
        self.superuser_request.user = self.admin_user

    def test_processors_page_loads(self):
        url = reverse('admin:zeitlabs_payments_processors')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Payment Processors')

    def test_processors_page_is_served_from_under_the_app_path(self):
        """
        Verify that the page sits under the app it is listed in, where Django puts every registered model.

        The admin index files this entry under Zeitlabs_Payments, so a visitor following it should stay inside
        '/admin/zeitlabs_payments/'. A page answering at the admin root instead is outside the app it claims
        to belong to, and any rule written against the app's path misses it.
        """
        url = reverse('admin:zeitlabs_payments_processors')

        self.assertEqual(
            url, '/admin/zeitlabs_payments/processors/',
            'The processors page must be served from under the app path, as every registered model is',
        )

    def test_processors_page_breadcrumb_names_the_app_it_is_filed_under(self):
        """
        Verify that the trail reads Home, then the app, then the page, as a model changelist's trail does.

        Django builds that trail from a model's ``opts``. This page has no model, so the default trail skips
        straight from Home to the title and reads as though the page sat at the admin root, next to nothing.
        """
        response = self.client.get(reverse('admin:zeitlabs_payments_processors'))
        trail = re.search(
            r'<div class="breadcrumbs">(.*?)</div>', response.content.decode(), re.DOTALL,
        )

        self.assertIsNotNone(trail, 'The page must render a breadcrumb trail')
        crumbs = [crumb.strip() for crumb in trail.group(1).split('&rsaquo;')]
        self.assertEqual(len(crumbs), 3, f'The trail should have three steps, got {crumbs}')
        self.assertIn('>Home<', crumbs[0], 'The trail should start at the admin index')
        self.assertIn(
            reverse('admin:app_list', kwargs={'app_label': 'zeitlabs_payments'}), crumbs[1],
            'The middle step should link to the app index, as it does on every model page',
        )
        self.assertIn('Zeitlabs_Payments', crumbs[1], 'The middle step should name the app')
        self.assertEqual(crumbs[2], 'Payment Processors', 'The trail should end at this page')

    def test_processors_page_is_reachable_at_the_path_the_app_list_advertises(self):
        """
        Verify that the link the admin index renders is the link that answers.

        The entry is injected into the app list by hand rather than derived from a registered model, so the
        advertised URL and the served route are two separate statements that can drift apart.
        """
        zp_app = next(
            app for app in admin.site.get_app_list(self.superuser_request)
            if app.get('app_label') == 'zeitlabs_payments'
        )
        entry = next(m for m in zp_app['models'] if m.get('object_name') == 'PaymentProcessors')

        response = self.client.get(entry['admin_url'])

        self.assertEqual(
            response.status_code, 200,
            f"The app list advertises {entry['admin_url']}, which must be the URL that answers",
        )
        self.assertContains(response, 'Payment Processors')

    @data(
        ('no zeitlabs app', [], 0),
        ('empty models list', [{'app_label': 'zeitlabs_payments', 'models': []}], 1),
        ('missing models key', [{'app_label': 'zeitlabs_payments'}], 1),
        (
            'other model only',
            [{'app_label': 'zeitlabs_payments', 'models': [{'object_name': 'AuditLog'}]}],
            1,
        ),
        (
            'already has PaymentProcessors',
            [{'app_label': 'zeitlabs_payments', 'models': [{'object_name': 'PaymentProcessors'}]}],
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
                    expected_count, 0, f'[{usecase}] expected no PaymentProcessors entry'
                )
            else:
                zp_models = zp_apps[0].get('models', [])
                count = sum(
                    1 for m in zp_models if m.get('object_name') == 'PaymentProcessors'
                )
                self.assertEqual(
                    count,
                    expected_count,
                    f'[{usecase}] expected {expected_count} entries, got {count}',
                )
        finally:
            admin.site.get_app_list = original


@ddt
class ViewOnlyMixinTest(TestCase):
    """Refusing every write, owned by the mixin rather than by the admins that adopt it."""

    def setUp(self):
        """setup."""
        self.request = RequestFactory().get('/')

    @staticmethod
    def _adopting_inline():
        """Return a throwaway inline that has done nothing but adopt the mixin."""
        class AdoptingInline(ViewOnlyMixin, admin.TabularInline):
            """A stand-in for any inline over rows that must not be rewritten."""

            model = InvoiceItem

        return AdoptingInline(Invoice, AdminSite())

    @data('has_add_permission', 'has_change_permission', 'has_delete_permission')
    def test_an_admin_using_the_mixin_refuses_every_write(self, hook):
        """Verify that each of the three write permissions is refused on an admin that adopts the mixin."""
        class AdoptingAdmin(ViewOnlyMixin, admin.ModelAdmin):
            """A stand-in for any admin that is a window onto rows it must not rewrite."""

        model_admin = AdoptingAdmin(AuditLog, AdminSite())

        self.assertFalse(
            getattr(model_admin, hook)(self.request), f'{hook} should refuse on an admin using the mixin'
        )

    @data('has_add_permission', 'has_change_permission', 'has_delete_permission')
    def test_an_inline_using_the_mixin_refuses_every_write(self, hook):
        """
        Verify that the same three permissions are refused on an inline, which Django calls with two arguments.

        A ModelAdmin is asked ``has_add_permission(request)`` while an inline is asked
        ``has_add_permission(request, obj)``. The mixin takes the object as optional so that one definition
        serves both, and an inline matters here because it is a second door into rows whose own page is shut.
        """
        inline = self._adopting_inline()

        self.assertFalse(
            getattr(inline, hook)(self.request, None), f'{hook} should refuse on an inline using the mixin'
        )

    def test_an_inline_using_the_mixin_offers_no_delete_column(self):
        """Verify that the refusal reaches Django's formset, which is what takes the delete checkbox away."""
        inline = self._adopting_inline()

        self.assertFalse(
            inline.get_formset(self.request).can_delete,
            'An inline that refuses deletion should not draw a delete checkbox beside each row',
        )


@ddt
class CartAdminTest(TestCase):
    """The cart pages: finding a cart by its buyer, reading it without changing it, and reaching its invoice."""

    def setUp(self):
        """setup."""
        staff = User.objects.create_superuser(username='staff', email='staff@example.com', password='pass')
        alice = User.objects.create(username='alice', email='a.smith@example.com')
        bob = User.objects.create(username='bob', email='b.jones@example.com')
        self.buyer = alice
        self.alice_cart = Cart.objects.create(user=alice)
        Cart.objects.create(user=bob)
        self.empty_marker = CartAdmin(Cart, AdminSite()).get_empty_value_display()
        self.client.force_login(staff)

    def _cart_with_invoice(self, status):
        """Create a cart in the given status, together with an invoice raised against it."""
        cart = Cart.objects.create(user=self.buyer, status=status)
        transaction = Transaction.objects.create(
            cart=cart, amount=Decimal('10.00'), gateway='dummy', gateway_transaction_id=f'TX-{status.upper()}'
        )
        invoice = Invoice.objects.create(
            status=Invoice.InvoiceStatus.PAID,
            invoice_number=f'INV-{status.upper()}',
            cart=cart,
            gross_total=Decimal('10.00'),
            total=Decimal('10.00'),
            currency='SAR',
            related_transaction=transaction,
        )
        return cart, invoice

    def _invoice_row(self, cart):
        """Return what the invoice row renders as on the given cart's change page."""
        response = self.client.get(reverse('admin:zeitlabs_payments_cart_change', args=(cart.id,)))
        self.assertEqual(response.status_code, 200, 'The cart change page should render')
        row = re.search(
            r'field-invoice_link.*?<div class="readonly">(.*?)</div>', response.content.decode(), re.S
        )
        return row.group(1).strip() if row else None

    @data('alice', 'a.smith@example.com')
    def test_the_search_box_finds_a_cart_by_its_buyer(self, term):
        """
        Verify that a cart is reachable by its buyer's username, and still by their email as before.

        The username is deliberately not a substring of the email, so a hit on 'alice' can only have come from
        the username field and cannot be the email field answering by accident.
        """
        response = self.client.get(reverse('admin:zeitlabs_payments_cart_changelist'), {'q': term})

        self.assertEqual(
            [cart.id for cart in response.context['cl'].queryset],
            [self.alice_cart.id],
            f'Searching {term!r} should return that buyer\'s carts and nobody else\'s',
        )

    def test_the_search_box_finds_a_cart_by_its_gateway_order_id(self):
        """
        Verify that a cart is reachable by the order id its gateway gave it, which is what a learner quotes to support.
        """
        cart = Cart.objects.create(
            user=self.buyer, status=Cart.Status.PROCESSING, gateway='dummy', gateway_order_id='GW-ORDER-77',
        )

        response = self.client.get(reverse('admin:zeitlabs_payments_cart_changelist'), {'q': 'GW-ORDER-77'})

        self.assertEqual(
            [found.id for found in response.context['cl'].queryset],
            [cart.id],
            'Searching a gateway order id should return the cart it was opened for, and no other',
        )

    @data(Cart.Status.PAID, Cart.Status.REFUNDED)
    def test_a_settled_cart_links_to_its_invoice(self, status):
        """Verify that a paid or refunded cart reaches its invoice in one click, named by its invoice number."""
        cart, invoice = self._cart_with_invoice(status)

        invoice_url = reverse('admin:zeitlabs_payments_invoice_change', args=(invoice.id,))
        self.assertEqual(
            self._invoice_row(cart),
            f'<a href="{invoice_url}">{invoice.invoice_number}</a>',
            f'A {status} cart should link the invoice it settled against',
        )

    @data(
        Cart.Status.PENDING,
        Cart.Status.PROCESSING,
        Cart.Status.CANCELLED,
        Cart.Status.PAYMENT_PENDING,
        Cart.Status.REFUND_REQUESTED,
    )
    def test_an_unsettled_cart_links_to_no_invoice_even_when_one_exists(self, status):
        """
        Verify that a cart which is neither paid nor refunded shows no invoice link, invoice on file or not.

        The fixture deliberately gives every one of these carts an invoice. The link is gated on the cart's
        status rather than on whether a row happens to exist, so a half-finished checkout cannot surface a
        document the buyer was never given.
        """
        cart, invoice = self._cart_with_invoice(status)

        self.assertEqual(
            self._invoice_row(cart),
            self.empty_marker,
            f'A {status} cart must not surface {invoice.invoice_number}, even though it is on file',
        )

    def test_a_settled_cart_whose_invoice_is_missing_reads_as_empty(self):
        """Verify that a paid cart with no invoice row shows the empty marker rather than breaking the page."""
        cart = Cart.objects.create(user=self.buyer, status=Cart.Status.PAID)

        self.assertEqual(
            self._invoice_row(cart),
            self.empty_marker,
            'A missing invoice should read as an empty field, not break the cart page',
        )

    def test_the_cart_pages_are_view_only_end_to_end(self):
        """
        Verify that the wired-up admin offers no button and refuses every write route, while staying readable.

        Each refusal belongs to the mixin and is covered there. What this settles is the wiring: that the mixin
        sits among the bases, and that nothing this admin declares for itself reopens a route. A cart is opened
        by a learner at checkout, and deleting one would take its items, invoices and audit log with it. The
        bulk delete is judged by the surviving row, because Django ignores an action it no longer recognises.
        """
        change_url = reverse('admin:zeitlabs_payments_cart_change', args=(self.alice_cart.id,))

        response = self.client.get(change_url)

        self.assertNotContains(
            response, '<input type="submit"', msg_prefix='A view-only record should offer nothing to submit'
        )
        self.assertEqual(
            self.client.get(reverse('admin:zeitlabs_payments_cart_add')).status_code,
            403,
            'Opening a cart by hand should be refused outright',
        )
        self.assertEqual(
            self.client.post(change_url, {'_save': 'Save'}).status_code,
            403,
            'A save posted straight at the change url should still be refused',
        )
        self.assertEqual(
            self.client.post(
                reverse('admin:zeitlabs_payments_cart_delete', args=(self.alice_cart.id,)), {'post': 'yes'}
            ).status_code,
            403,
            'The delete route should be refused even for a superuser',
        )
        self.client.post(
            reverse('admin:zeitlabs_payments_cart_changelist'),
            {'action': 'delete_selected', '_selected_action': [str(self.alice_cart.id)], 'post': 'yes'},
        )
        self.assertTrue(
            Cart.objects.filter(id=self.alice_cart.id).exists(), 'Neither delete route may take the cart away'
        )
        self.assertContains(
            response, self.buyer.username, msg_prefix='The cart must still be readable on the page'
        )


@pytest.mark.usefixtures('base_data')
class CartResolutionAdminTest(TestCase):
    """The cart list's two ways of resolving a paid cart left without an invoice: retry it, or record its refund."""

    def setUp(self):
        """setup."""
        self.client.force_login(User.objects.get(id=1))
        self.changelist = reverse('admin:zeitlabs_payments_cart_changelist')

    def _paid_cart(self, payment_id='TX-PAID'):
        """Create a cart paid on the current site through the dummy processor, whose invoice and enrolment failed."""
        item = CatalogueItem.objects.get(sku='custom-sku-1')
        cart = Cart.objects.create(
            user=User.objects.get(id=3),
            status=Cart.Status.PAID,
            gateway='dummy',
            payment_origin=f'https://{Site.objects.get_current().domain}',
        )
        cart.items.create(catalogue_item=item, original_price=item.price, final_price=item.price)
        cart.transactions.create(
            type=Transaction.TransactionType.PAYMENT, status='success', gateway='dummy',
            gateway_transaction_id=payment_id, method='card', amount=item.price, currency='SAR',
        )
        return cart

    def _act(self, action, carts, **fields):
        """Post the action on the given carts from the cart list, with any further form fields."""
        return self.client.post(
            self.changelist, {'action': action, '_selected_action': [str(cart.pk) for cart in carts], **fields},
        )

    def _offered_actions(self):
        """Return the actions the cart list offers the signed-in user."""
        action_form = self.client.get(self.changelist).context['action_form']
        if action_form is None:
            return set()
        return {name for name, _ in action_form.fields['action'].choices if name}

    @staticmethod
    def _messages(response):
        """Return the messages the response left for the user."""
        return [str(message) for message in get_messages(response.wsgi_request)]

    def test_each_resolution_is_offered_only_with_its_own_permission(self):
        """
        Verify that a staff member who may view carts is offered neither resolution, and each one only once given its
        own permission.

        One enrols a learner and the other records money given back, so holding one does not grant the other.
        """
        clerk = User.objects.create(username='clerk', is_staff=True)
        clerk.user_permissions.add(Permission.objects.get(codename='view_cart'))
        self.client.force_login(clerk)
        self.assertEqual(self._offered_actions(), set(), 'A clerk who may only view carts was offered a resolution')

        clerk.user_permissions.add(Permission.objects.get(codename='retry_cart_fulfillment'))
        self.assertEqual(
            self._offered_actions(), {'retry_fulfillment'}, 'The retry permission must offer the retry, and only it',
        )

        clerk.user_permissions.add(Permission.objects.get(codename='record_cart_refund'))
        self.assertEqual(
            self._offered_actions(), {'retry_fulfillment', 'record_refund'}, 'The refund permission must offer refunds',
        )

        clerk.user_permissions.add(Permission.objects.get(codename='accept_cart_payment'))
        self.assertEqual(
            self._offered_actions(), {'retry_fulfillment', 'record_refund', 'accept_payment'},
            'The accept permission must offer accepting a payment',
        )

    def test_the_filter_lists_only_paid_carts_without_an_invoice(self):
        """Verify that the filter lists the paid carts left without an invoice, and no completed sale or open cart."""
        unfulfilled = self._paid_cart()
        invoiced = self._paid_cart('TX-INVOICED')
        Invoice.objects.create(
            status=Invoice.InvoiceStatus.PAID, invoice_number='TEST-900002', cart=invoiced, total=50, gross_total=50,
            currency='SAR', related_transaction=invoiced.transactions.get(),
        )
        Cart.objects.create(user=User.objects.get(id=3))

        response = self.client.get(self.changelist, {'fulfillment': 'paid_without_invoice'})

        self.assertEqual(
            [cart.id for cart in response.context['cl'].queryset], [unfulfilled.id],
            'The filter must list the paid cart without an invoice and nothing else',
        )

    def test_retrying_reports_each_selected_cart_on_its_own(self):
        """
        Verify that retrying several carts invoices each one it can and reports why it refused each other one.
        """
        retriable = self._paid_cart()
        unpaid = Cart.objects.create(user=User.objects.get(id=3), status=Cart.Status.PAYMENT_PENDING)

        response = self._act('retry_fulfillment', [retriable, unpaid])

        invoice = Invoice.objects.get(cart=retriable)
        self.assertEqual(
            self._messages(response),
            [
                f'Cart {retriable.id} is fulfilled, and invoiced as {invoice.invoice_number}.',
                f'Cart {unpaid.id} is payment_pending; only a paid cart without an invoice is resolved here.',
            ],
            'Each cart must be reported on its own',
        )

    def test_a_refund_is_recorded_for_one_cart_at_a_time(self):
        """Verify that a refund asked for several carts at once is refused, and none of them refunded."""
        response = self._act('record_refund', [self._paid_cart(), self._paid_cart('TX-OTHER')])

        self.assertEqual(
            self._messages(response), ['Select exactly one cart to record a refund for.'], 'The refusal must say why',
        )
        self.assertFalse(Cart.objects.filter(status=Cart.Status.REFUNDED).exists(), 'A cart was refunded')

    def test_the_refund_page_shows_the_payment_and_what_the_learner_may_have_been_told(self):
        """
        Verify that asking for a refund first shows the cart's payment and the courses of a bundle that failed after
        their enrolment went through, and refunds nothing yet.
        """
        cart = self._paid_cart()
        AuditLog.objects.create(
            cart=cart, action=AuditLog.AuditActions.BUNDLE_FULFILLMENT_FAILED, gateway='dummy',
            details='Courses whose enrollment completed before the failure: course-v1:org+landed+1.',
        )

        response = self._act('record_refund', [cart])

        self.assertTemplateUsed(response, 'zeitlabs_payments/admin/record_refund.html')
        self.assertContains(response, 'Payment TX-PAID through dummy', msg_prefix='The payment is not shown')
        self.assertContains(response, 'course-v1:org+landed+1', msg_prefix='The landed course is not shown')
        self.assertEqual(Cart.objects.get(pk=cart.pk).status, Cart.Status.PAID, 'Opening the page refunded the cart')

    def test_a_confirmed_refund_is_recorded(self):
        """Verify that the refund confirmed with its reference is recorded, and the cart refunded."""
        cart = self._paid_cart()

        response = self._act('record_refund', [cart], confirm='yes', refund_reference='RF-1')

        self.assertEqual(
            self._messages(response), [f'Cart {cart.id} is refunded under reference RF-1.'],
            'The refund is not reported',
        )
        self.assertEqual(Cart.objects.get(pk=cart.pk).status, Cart.Status.REFUNDED, 'The cart was not refunded')

    def test_a_refund_without_a_reference_is_asked_for_again(self):
        """Verify that a refund confirmed without a reference shows the page again, and refunds nothing."""
        cart = self._paid_cart()

        response = self._act('record_refund', [cart], confirm='yes', refund_reference='  ')

        self.assertTemplateUsed(response, 'zeitlabs_payments/admin/record_refund.html')
        self.assertContains(response, 'This field is required.', msg_prefix='The missing reference is not pointed out')
        self.assertEqual(
            Cart.objects.get(pk=cart.pk).status, Cart.Status.PAID, 'A refund with no reference went through',
        )

    def _unreconciled_cart(self, transaction_id='TX-HELD'):
        """Create a cart handed to the dummy processor on the current site, whose payment was taken and refused."""
        item = CatalogueItem.objects.get(sku='custom-sku-1')
        cart = Cart.objects.create(
            user=User.objects.get(id=3),
            status=Cart.Status.UNRECONCILED,
            gateway='dummy',
            payment_origin=f'https://{Site.objects.get_current().domain}',
        )
        cart.items.create(catalogue_item=item, original_price=item.price, final_price=item.price)
        cart.transactions.create(
            type=Transaction.TransactionType.PAYMENT, status='failed', gateway='dummy',
            gateway_transaction_id=transaction_id, method='card', amount=item.price, currency='682',
            reason='Payment completed | not reconciled: wrong amount',
        )
        AuditLog.objects.create(
            cart=cart, action=AuditLog.AuditActions.TRANSACTION_NOT_RECONCILED, gateway='dummy',
            details='Transaction: TX-HELD for cart: 1 was recorded but is not reconciled: wrong amount.',
        )
        return cart

    def test_the_accept_page_shows_the_payment_and_why_it_was_refused(self):
        """
        Verify that asking to accept a payment first shows the cart's total, its refused payment and why it was refused,
        and settles nothing yet.
        """
        cart = self._unreconciled_cart()

        response = self._act('accept_payment', [cart])

        self.assertTemplateUsed(response, 'zeitlabs_payments/admin/accept_payment.html')
        self.assertContains(response, f'Total: {cart.total}', msg_prefix='The cart total is not shown')
        self.assertContains(response, 'Payment TX-HELD', msg_prefix='The refused payment is not shown')
        self.assertContains(response, 'is not reconciled: wrong amount', msg_prefix='Why it was refused is not shown')
        self.assertEqual(
            Cart.objects.get(pk=cart.pk).status, Cart.Status.UNRECONCILED, 'Opening the page accepted the payment',
        )

    def test_a_confirmed_payment_is_accepted(self):
        """Verify that the payment confirmed with its transaction id and amount is accepted, and the cart invoiced."""
        cart = self._unreconciled_cart()

        response = self._act(
            'accept_payment', [cart], confirm='yes', gateway_transaction_id='TX-HELD', amount=str(cart.total),
        )

        invoice = Invoice.objects.get(cart=cart)
        self.assertEqual(
            self._messages(response),
            [f'The payment of cart {cart.id} is accepted, and the cart invoiced as {invoice.invoice_number}.'],
            'The acceptance is not reported',
        )
        self.assertEqual(Cart.objects.get(pk=cart.pk).status, Cart.Status.PAID, 'The cart was not moved to paid')

    def test_a_refused_acceptance_is_reported(self):
        """Verify that accepting a payment of another amount than the cart total is refused, and reported."""
        cart = self._unreconciled_cart()

        response = self._act(
            'accept_payment', [cart], confirm='yes', gateway_transaction_id='TX-HELD', amount=str(cart.total + 1),
        )

        self.assertIn('but the gateway took', self._messages(response)[0], 'The refusal must say why')
        self.assertEqual(
            Cart.objects.get(pk=cart.pk).status, Cart.Status.UNRECONCILED, 'A refused acceptance moved the cart',
        )

    def test_a_payment_is_accepted_for_one_cart_at_a_time(self):
        """Verify that accepting payments for several carts at once is refused, and none of them settled."""
        carts = [self._unreconciled_cart(), self._unreconciled_cart(transaction_id='TX-HELD-2')]

        response = self._act('accept_payment', carts)

        self.assertEqual(
            self._messages(response), ['Select exactly one cart to accept the payment of.'], 'The refusal must say why',
        )

    def test_the_refund_of_an_unreconciled_cart_asks_for_the_amount_and_records_it(self):
        """
        Verify that the refund page of an unreconciled cart asks for the amount refunded too, and that the refund
        confirmed with it is recorded.
        """
        cart = self._unreconciled_cart()

        page = self._act('record_refund', [cart])
        response = self._act('record_refund', [cart], confirm='yes', refund_reference='RF-9', amount='50')

        self.assertContains(page, 'name="amount"', msg_prefix='The amount refunded is not asked for')
        self.assertEqual(
            self._messages(response), [f'Cart {cart.id} is refunded under reference RF-9.'],
            'The refund is not reported',
        )
        self.assertEqual(
            Transaction.objects.get(gateway_transaction_id='RF-9').amount, Decimal('50'),
            'The refund is not recorded for the amount typed',
        )

    def test_the_refund_of_a_paid_cart_does_not_ask_for_an_amount(self):
        """Verify that the refund page of a paid cart, whose refund is its payment, asks for no amount."""
        page = self._act('record_refund', [self._paid_cart()])

        self.assertNotContains(page, 'name="amount"', msg_prefix='A paid cart was asked for an amount refunded')

    def test_a_refused_refund_is_reported(self):
        """Verify that a refund refused, as one under the payment's own id, is reported and refunds nothing."""
        cart = self._paid_cart()

        response = self._act('record_refund', [cart], confirm='yes', refund_reference='TX-PAID')

        self.assertEqual(
            self._messages(response), ['dummy reference TX-PAID is already recorded on another transaction.'],
            'The refusal must say why',
        )
        self.assertEqual(Cart.objects.get(pk=cart.pk).status, Cart.Status.PAID, 'A refused refund moved the cart')


@ddt
class CartItemAdminTest(TestCase):
    """The cart item pages name what they show and link where a reader would go next."""

    def setUp(self):
        """setup."""
        self.staff = User.objects.create_superuser(username='staff', email='staff@example.com', password='pass')
        learner = User.objects.create(username='buyer', email='buyer@example.com')
        self.cart = Cart.objects.create(id=9, user=learner)
        self.catalogue_item = CatalogueItem.objects.create(
            sku='COURSE-DEMO-2024',
            type=CatalogueItem.ItemType.PAID_COURSE,
            title='Demo',
            item_ref_id='course-v1:edX+Demo+2024',
            price=Decimal('99.99'),
            currency='SAR',
        )
        self.item = CartItem.objects.create(
            cart=self.cart,
            catalogue_item=self.catalogue_item,
            original_price=Decimal('99.99'),
            final_price=Decimal('99.99'),
        )
        self.client.force_login(self.staff)

    def test_list_page_names_the_catalogue_item_by_sku_and_links_to_it(self):
        """Verify that the catalogue item column carries the SKU as a link, not the model's default string."""
        response = self.client.get(reverse('admin:zeitlabs_payments_cartitem_changelist'))

        item_url = reverse('admin:zeitlabs_payments_catalogueitem_change', args=(self.catalogue_item.id,))
        self.assertContains(
            response,
            f'<a href="{item_url}">{self.catalogue_item.sku}</a>',
            html=True,
            msg_prefix='The catalogue item column should name the SKU and link to the item',
        )
        self.assertNotContains(
            response, 'CatalogueItem object', msg_prefix='The default __str__ should not reach the page'
        )

    def test_list_page_shows_the_cart_as_a_linked_id(self):
        """Verify that the cart column carries the bare id as a link, in place of the model's default string."""
        response = self.client.get(reverse('admin:zeitlabs_payments_cartitem_changelist'))

        cart_url = reverse('admin:zeitlabs_payments_cart_change', args=(self.cart.id,))
        self.assertContains(
            response,
            f'<a href="{cart_url}">{self.cart.id}</a>',
            html=True,
            msg_prefix='The cart column should be the id, linked to that cart',
        )
        self.assertNotContains(
            response, 'Cart object', msg_prefix='No cart string should be left anywhere on the list page'
        )

    def test_change_page_shows_both_relations_as_links(self):
        """Verify that the change page reaches the cart and the catalogue item in one click apiece."""
        response = self.client.get(reverse('admin:zeitlabs_payments_cartitem_change', args=(self.item.id,)))

        cart_url = reverse('admin:zeitlabs_payments_cart_change', args=(self.cart.id,))
        item_url = reverse('admin:zeitlabs_payments_catalogueitem_change', args=(self.catalogue_item.id,))
        self.assertContains(
            response, f'<a href="{cart_url}">{self.cart.id}</a>', html=True,
            msg_prefix='The cart row should be the id, linked to the cart',
        )
        self.assertContains(
            response, f'<a href="{item_url}">{self.catalogue_item.sku}</a>', html=True,
            msg_prefix='The catalogue item row should be the SKU, linked to the item',
        )

    def test_change_page_offers_nothing_to_edit(self):
        """Verify that the change page carries no editable widget, so a cart item cannot be rewritten by hand."""
        response = self.client.get(reverse('admin:zeitlabs_payments_cartitem_change', args=(self.item.id,)))

        editable = re.findall(r'<(?:input|select|textarea)[^>]*name="(\w+)"', response.content.decode())
        self.assertEqual(
            [name for name in editable if not name.startswith('_') and name != 'csrfmiddlewaretoken'],
            [],
            'Every field on the cart item form should be read-only',
        )

    @data('discount_amount', 'tax_amount', 'coupon')
    def test_change_page_leaves_out_the_money_fields_version_one_does_not_use(self, field_name):
        """Verify that the discount, tax and coupon fields are absent, since this version prices neither."""
        response = self.client.get(reverse('admin:zeitlabs_payments_cartitem_change', args=(self.item.id,)))

        self.assertNotContains(
            response, field_name, msg_prefix=f'{field_name} should not appear on the cart item page'
        )

    def test_the_cart_token_reaches_this_search_box(self):
        """
        Verify that the cart token filters the cart item list, resolving against this model's own cart column.

        The token's grammar belongs to the mixin and is covered there. What is under test here is the wiring
        only: that the mixin is among the bases and that its lookup path names a column this model has.
        """
        look_alike = Cart.objects.create(id=99, user=self.cart.user)
        CartItem.objects.create(
            cart=look_alike,
            catalogue_item=self.catalogue_item,
            original_price=Decimal('99.99'),
            final_price=Decimal('99.99'),
        )

        response = self.client.get(reverse('admin:zeitlabs_payments_cartitem_changelist'), {'q': 'cart=9'})

        self.assertEqual(
            [item.cart_id for item in response.context['cl'].queryset],
            [self.cart.id],
            'cart=9 should return cart 9 alone, not every cart whose id contains a 9',
        )

    def test_list_page_still_searches_by_sku_as_it_did(self):
        """Verify that the ordinary SKU search is untouched, since the token must not shadow the rest of the box."""
        response = self.client.get(
            reverse('admin:zeitlabs_payments_cartitem_changelist'), {'q': self.catalogue_item.sku}
        )

        self.assertEqual(
            [item.id for item in response.context['cl'].queryset],
            [self.item.id],
            'Searching the SKU should still find the cart items holding it',
        )

    def test_the_cart_item_pages_are_view_only_end_to_end(self):
        """
        Verify that the wired-up admin offers no button and refuses every write route, while staying readable.

        Each refusal belongs to the mixin and is covered there. What this settles is the wiring: that the mixin
        sits among the bases, and that nothing this admin declares for itself reopens a route. Cart items are
        written by checkout, so there is nothing a staff member should be typing in here. The bulk delete is
        judged by the surviving row, because Django ignores an action it no longer recognises.
        """
        change_url = reverse('admin:zeitlabs_payments_cartitem_change', args=(self.item.id,))

        response = self.client.get(change_url)

        self.assertNotContains(
            response, '<input type="submit"', msg_prefix='A view-only record should offer nothing to submit'
        )
        self.assertEqual(
            self.client.get(reverse('admin:zeitlabs_payments_cartitem_add')).status_code,
            403,
            'Adding a cart item by hand should be refused outright',
        )
        self.assertEqual(
            self.client.post(change_url, {'_save': 'Save'}).status_code,
            403,
            'A save posted straight at the change url should still be refused',
        )
        self.assertEqual(
            self.client.post(
                reverse('admin:zeitlabs_payments_cartitem_delete', args=(self.item.id,)), {'post': 'yes'}
            ).status_code,
            403,
            'The delete route should be refused even for a superuser',
        )
        self.client.post(
            reverse('admin:zeitlabs_payments_cartitem_changelist'),
            {'action': 'delete_selected', '_selected_action': [str(self.item.id)], 'post': 'yes'},
        )
        self.assertTrue(
            CartItem.objects.filter(id=self.item.id).exists(), 'Neither delete route may take the cart item away'
        )
        self.assertContains(
            response, self.catalogue_item.sku, msg_prefix='The cart item must still be readable on the page'
        )


class CartLinkMixinTest(TestCase):
    """The linked cart field, owned by the mixin rather than by whichever admin happens to show it."""

    def setUp(self):
        """setup."""
        class AdoptingAdmin(CartLinkMixin, admin.ModelAdmin):
            """A stand-in for any admin that names cart_link among its fields."""

        self.model_admin = AdoptingAdmin(AuditLog, AdminSite())
        self.learner = User.objects.create(username='buyer', email='buyer@example.com')

    def test_the_field_is_the_cart_id_hyperlinked_to_that_cart(self):
        """Verify that the field renders the bare cart id wrapped in a link to that cart's own admin page."""
        cart = Cart.objects.create(user=self.learner)
        log = AuditLog.objects.create(cart=cart, action=AuditLog.AuditActions.CART_FULFILLED)

        expected_url = reverse('admin:zeitlabs_payments_cart_change', args=(cart.id,))
        self.assertEqual(
            self.model_admin.cart_link(log),
            f'<a href="{expected_url}">{cart.id}</a>',
            'The field should be the id alone, linked to the cart it names',
        )

    def test_the_field_is_the_empty_marker_when_the_row_has_no_cart(self):
        """
        Verify that a row without a cart yields the admin's empty marker rather than None.

        Django pipes a read-only callable through ``linebreaksbr``, which has no empty-value handling of its
        own, so returning None would reach a change page as the literal word.
        """
        log = AuditLog.objects.create(cart=None, action=AuditLog.AuditActions.RESPONSE_INVALID_CART)

        self.assertEqual(
            self.model_admin.cart_link(log),
            self.model_admin.get_empty_value_display(),
            'A row with no cart should read as an empty field, not as a link to nothing',
        )

    def test_the_field_costs_no_query(self):
        """
        Verify that rendering the field touches the database not at all.

        The id is read off the row's own column rather than by following the foreign key, which is what lets an
        admin carry this column without a join and without one query per row.
        """
        cart = Cart.objects.create(user=self.learner)
        AuditLog.objects.create(cart=cart, action=AuditLog.AuditActions.CART_FULFILLED)
        log = AuditLog.objects.get(cart=cart)

        with self.assertNumQueries(0):
            self.model_admin.cart_link(log)


@ddt
class CartSearchMixinTest(TestCase):
    """The cart token's grammar and its adoption, owned by the mixin rather than by any admin that uses it."""

    def setUp(self):
        """setup."""
        class AdoptingAdmin(CartSearchMixin, admin.ModelAdmin):
            """A stand-in for any admin that adopts the cart token."""

            search_fields = ('action', 'details')

        self.admin_site = AdminSite()
        self.request = RequestFactory().get('/')
        self.learner = User.objects.create(username='buyer', email='buyer@example.com')
        self.model_admin = AdoptingAdmin(AuditLog, self.admin_site)

    def _rows_for_carts(self, *cart_ids):
        """Give each named cart one searchable row."""
        for cart_id in cart_ids:
            cart = Cart.objects.create(id=cart_id, user=self.learner)
            AuditLog.objects.create(
                cart=cart, action=AuditLog.AuditActions.CART_FULFILLED, details='Cart fulfilled successfully.'
            )

    def _search(self, term):
        """Run the mixin's search over every row and return the cart ids it matched."""
        results, _ = self.model_admin.get_search_results(self.request, AuditLog.objects.all(), term)
        return sorted(row.cart_id for row in results)

    def _transaction(self, cart_id):
        """Create a transaction against a cart with the given id."""
        cart = Cart.objects.create(id=cart_id, user=self.learner)
        return Transaction.objects.create(
            cart=cart,
            type=Transaction.TransactionType.PAYMENT,
            status='paid',
            gateway='dummy',
            gateway_transaction_id=f'TX-{cart_id}',
            method='card',
            amount=Decimal('10.00'),
            currency='SAR',
        )

    @data('cart=6', 'CART=6', ' cart=6 ')
    def test_the_token_returns_one_exact_cart(self, term):
        """
        Verify that the token returns that cart's rows alone, whatever its case or surrounding space.

        Carts 16 and 66 both carry a 6 in their id and must stay out; the box is otherwise blind to case, and a
        number pasted into it often arrives with a space attached.
        """
        self._rows_for_carts(6, 16, 66)

        self.assertEqual(
            self._search(term), [6], f'{term!r} should return cart 6 only, not every cart containing those digits'
        )

    def test_a_cart_with_no_rows_comes_back_empty(self):
        """Verify that a cart number nothing was recorded against returns nothing, not the unfiltered list."""
        self._rows_for_carts(6, 16, 66)

        self.assertEqual(
            self._search('cart=999'), [], 'A cart with no rows should return nothing rather than everything'
        )

    @data('cart= 6', 'cart =6', 'cart=6 fulfilled', 'cart=abc', 'cart=', 'cart=-6')
    def test_terms_that_are_not_the_token_go_to_the_ordinary_search(self, term):
        """
        Verify that a term which is not exactly cart=<number> is searched as plain text instead.

        Each of these would return cart 6 if the parser were loose about spacing, sign or trailing words. Coming
        back empty is what shows the term reached the ordinary search, which finds none of them in any field.
        """
        self._rows_for_carts(6, 16, 66)

        self.assertEqual(
            self._search(term), [], f'{term!r} is not the token and should be searched as text, not parsed'
        )

    def test_ordinary_words_still_reach_the_search_fields(self):
        """Verify that a normal search still works, since the token must not shadow the rest of the box."""
        self._rows_for_carts(6, 16, 66)

        self.assertEqual(
            self._search('fulfilled'), [6, 16, 66], 'A plain word should still match across the search fields'
        )

    def test_a_model_with_its_own_cart_column_adopts_the_token_unconfigured(self):
        """
        Verify that a model holding a plain cart foreign key needs no configuration beyond the mixin itself.

        This is the point of the default lookup: it already names the column that four of these models carry,
        so adoption is a change to the bases and nothing else.
        """
        class TransactionSearchAdmin(CartSearchMixin, admin.ModelAdmin):
            """A transaction admin that has done nothing but add the mixin."""

        wanted = self._transaction(6)
        self._transaction(66)
        model_admin = TransactionSearchAdmin(Transaction, self.admin_site)

        results, _ = model_admin.get_search_results(self.request, Transaction.objects.all(), 'cart=6')

        self.assertEqual(
            [transaction.id for transaction in results],
            [wanted.id],
            'The default lookup alone should filter a transaction admin to cart 6',
        )

    def test_a_model_reaching_the_cart_indirectly_sets_the_lookup_path(self):
        """
        Verify that a model without its own cart column filters correctly once given the path that reaches one.

        A webhook event knows its cart only through the transaction it belongs to, so it stands for every model
        that would otherwise be shut out of this search.
        """
        class WebhookSearchAdmin(CartSearchMixin, admin.ModelAdmin):
            """A webhook admin reaching the cart through its transaction."""

            cart_search_lookup = 'related_transaction__cart_id'

        wanted = WebhookEvent.objects.create(
            gateway='dummy', event_type='paid', payload={}, related_transaction=self._transaction(6)
        )
        WebhookEvent.objects.create(
            gateway='dummy', event_type='paid', payload={}, related_transaction=self._transaction(66)
        )
        model_admin = WebhookSearchAdmin(WebhookEvent, self.admin_site)

        results, _ = model_admin.get_search_results(self.request, WebhookEvent.objects.all(), 'cart=6')

        self.assertEqual(
            [event.id for event in results],
            [wanted.id],
            'An indirect lookup path should filter to the events belonging to cart 6',
        )

    def test_the_mixin_carries_its_own_help_text_to_whoever_adopts_it(self):
        """Verify that an adopting admin shows the hint without restating it, since a hidden syntax is no use."""
        self.assertIn(
            'cart=',
            self.model_admin.search_help_text,
            'An adopting admin should inherit the hint that tells staff the token exists',
        )


@ddt
class AuditLogLinkMixinTest(TestCase):
    """The audit-log button, owned by the mixin rather than by whichever admin happens to show it."""

    def setUp(self):
        """setup."""
        class AdoptingAdmin(AuditLogLinkMixin, admin.ModelAdmin):
            """A stand-in for any admin that names audit_log_link among its fields."""

        self.admin_site = AdminSite()
        self.model_admin = AdoptingAdmin(Transaction, self.admin_site)
        self.learner = User.objects.create(username='buyer', email='buyer@example.com')

    def _transaction(self, cart):
        """Create a transaction against the given cart, or against none at all."""
        return Transaction.objects.create(
            cart=cart,
            type=Transaction.TransactionType.PAYMENT,
            status='paid',
            gateway='dummy',
            gateway_transaction_id='TX-1',
            method='card',
            amount=Decimal('10.00'),
            currency='SAR',
        )

    def test_the_field_is_a_button_opening_the_audit_log_filtered_to_the_row_s_cart(self):
        """
        Verify that the field renders a button whose target is the audit log searched for this row's cart.

        The target is spelled as the search term rather than as a column filter so that the page it opens is
        one a reader can also reach by typing cart=<id> into the audit log's own search box.
        """
        cart = Cart.objects.create(user=self.learner)
        transaction = self._transaction(cart)

        expected_url = reverse('admin:zeitlabs_payments_auditlog_changelist')
        self.assertEqual(
            self.model_admin.audit_log_link(transaction),
            f'<a class="button" href="{expected_url}?q=cart%3D{cart.id}">View related audit logs</a>',
            "The field should be a button opening the audit log filtered to the row's cart",
        )

    def test_the_field_is_the_empty_marker_when_the_row_has_no_cart(self):
        """
        Verify that a row without a cart yields the admin's empty marker rather than a button going nowhere.

        A transaction keeps its record when its cart is deleted, so this is a state the page really reaches.
        """
        self.assertEqual(
            self.model_admin.audit_log_link(self._transaction(None)),
            self.model_admin.get_empty_value_display(),
            'A row with no cart should read as an empty field, not as a button filtering on nothing',
        )

    def test_the_field_costs_no_query(self):
        """
        Verify that rendering the field touches the database not at all.

        The id is read off the row's own column rather than by following the foreign key, which is what lets
        the button sit on a page without adding a join.
        """
        cart = Cart.objects.create(user=self.learner)
        self._transaction(cart)
        transaction = Transaction.objects.get(cart=cart)

        with self.assertNumQueries(0):
            self.model_admin.audit_log_link(transaction)

    def test_an_admin_whose_row_is_the_cart_names_its_own_key(self):
        """
        Verify that the cart's own page filters on its primary key, since it holds no cart foreign key.

        The cart is the audited row itself rather than a row pointing at one, which is the case the
        configurable field name exists to cover.
        """
        class CartAuditAdmin(AuditLogLinkMixin, admin.ModelAdmin):
            """A cart admin reading the cart id off its own primary key."""

            audit_log_cart_field = 'id'

        cart = Cart.objects.create(user=self.learner)
        model_admin = CartAuditAdmin(Cart, self.admin_site)

        self.assertIn(
            f'?q=cart%3D{cart.id}',
            model_admin.audit_log_link(cart),
            "A cart's own page should filter the audit log on the cart's primary key",
        )

    @data(
        ('cart', 'zeitlabs_payments_cart'),
        ('invoice', 'zeitlabs_payments_invoice'),
        ('transaction', 'zeitlabs_payments_transaction'),
    )
    @unpack
    def test_the_change_page_renders_a_working_button(self, usecase, url_name):
        """
        Verify that each change page carries the button and that following it shows that cart's entries only.

        The three pages are the ones a reader lands on when investigating a payment, and the whole point of
        the button is that the page it opens is already narrowed to the right cart.
        """
        staff = User.objects.create_superuser(username=f'{usecase}-staff', email='s@example.com', password='pass')
        self.client.login(username=f'{usecase}-staff', password='pass')

        cart = Cart.objects.create(user=staff, status=Cart.Status.PAID)
        other_cart = Cart.objects.create(user=staff, status=Cart.Status.PAID)
        transaction = self._transaction(cart)
        invoice = Invoice.objects.create(
            cart=cart,
            invoice_number='TEST-100001',
            status=Invoice.InvoiceStatus.PAID,
            gross_total=Decimal('10.00'),
            total=Decimal('10.00'),
            currency='SAR',
            related_transaction=transaction,
        )
        AuditLog.objects.create(cart=cart, action=AuditLog.AuditActions.CART_FULFILLED, details='belongs to this cart')
        AuditLog.objects.create(
            cart=other_cart, action=AuditLog.AuditActions.CART_FULFILLED, details='belongs to another cart',
        )
        row_id = {'cart': cart.id, 'invoice': invoice.id, 'transaction': transaction.id}[usecase]

        page = self.client.get(reverse(f'admin:{url_name}_change', args=(row_id,)))
        button = re.search(r'<a class="button" href="([^"]+)">View related audit logs</a>', page.content.decode())

        self.assertIsNotNone(button, f'The {usecase} change page should carry the audit-log button')
        audit_log = self.client.get(button.group(1).replace('&amp;', '&'))
        self.assertContains(
            audit_log, 'belongs to this cart',
            msg_prefix=f'The button on the {usecase} page should open the entries of its own cart',
        )
        self.assertNotContains(
            audit_log, 'belongs to another cart',
            msg_prefix=f'The button on the {usecase} page should not open another cart\'s entries',
        )


class CatalogueItemAdminCleanTest(TestCase):
    """CatalogueItemAdmin surfaces the precision rule as a form field error."""

    def setUp(self):
        """setup."""
        self.factory = RequestFactory()
        self.admin_site = AdminSite()
        self.catalogue_admin = CatalogueItemAdmin(CatalogueItem, self.admin_site)

    def test_clean_rejects_over_precision_price(self):
        """clean() raises ValidationError on a 3-decimal price for SAR."""
        item = CatalogueItem(
            sku='ADMIN-OVER-SAR',
            type=CatalogueItem.ItemType.PAID_COURSE,
            title='over-sar',
            item_ref_id='course-v1:admin+over+1',
            price=Decimal('10.123'),
            currency='SAR',
        )
        with self.assertRaises(ValidationError) as ctx:
            item.clean()
        self.assertIn('SAR', str(ctx.exception))
        self.assertIn('2', str(ctx.exception))

    def test_clean_accepts_valid_price(self):
        """clean() passes for a 2-decimal price in SAR."""
        item = CatalogueItem(
            sku='ADMIN-OK-SAR',
            type=CatalogueItem.ItemType.PAID_COURSE,
            title='ok-sar',
            item_ref_id='course-v1:admin+ok+1',
            price=Decimal('9.99'),
            currency='SAR',
        )
        item.clean()  # no exception

    @override_settings(ZEITLABS_PAYMENTS_SETTINGS={'valid_currency': 'IQD'})
    def test_clean_accepts_three_decimal_iqd(self):
        """Verify that clean() passes for a 3-decimal IQD price on an IQD deployment."""
        item = CatalogueItem(
            sku='ADMIN-OK-IQD',
            type=CatalogueItem.ItemType.PAID_COURSE,
            title='ok-iqd',
            item_ref_id='course-v1:admin+ok+1',
            price=Decimal('1.234'),
            currency='IQD',
        )
        item.clean()  # no exception

    def test_clean_rejects_missing_price(self):
        """Verify that a null price is refused as a form error, since None means not purchasable."""
        item = CatalogueItem(
            sku='ADMIN-NULL-PRICE',
            type=CatalogueItem.ItemType.PAID_COURSE,
            title='null-price',
            item_ref_id='course-v1:admin+null+1',
            price=None,
            currency='SAR',
        )
        with self.assertRaises(ValidationError) as ctx:
            item.clean()
        self.assertIn('price', str(ctx.exception), 'The form error should name the offending field')

    def test_clean_rejects_missing_currency(self):
        """Verify that a null currency is refused as a form error."""
        item = CatalogueItem(
            sku='ADMIN-NULL-CURRENCY',
            type=CatalogueItem.ItemType.PAID_COURSE,
            title='null-currency',
            item_ref_id='course-v1:admin+null+1',
            price=Decimal('9.999'),
            currency=None,
        )
        with self.assertRaises(ValidationError) as ctx:
            item.clean()
        self.assertIn(
            'not a supported ISO 4217 currency',
            str(ctx.exception),
            'The form error should say the currency is unusable',
        )

    def test_clean_rejects_currency_other_than_the_deployments(self):
        """
        Verify that clean() refuses a real ISO currency that is not the deployment's.

        The admin is where a staff member would otherwise create an item that only fails later, at checkout, in
        front of a learner. IQD is a valid code carrying a valid 3-decimal price here; what refuses it is that the
        deployment trades in SAR.
        """
        item = CatalogueItem(
            sku='ADMIN-WRONG-CURRENCY',
            type=CatalogueItem.ItemType.PAID_COURSE,
            title='wrong-currency',
            item_ref_id='course-v1:admin+wrong+1',
            price=Decimal('1.234'),
            currency='IQD',
        )
        with self.assertRaises(ValidationError) as ctx:
            item.clean()
        self.assertIn('IQD', str(ctx.exception), 'The form error should name the currency that was rejected')
        self.assertIn(
            'SAR', str(ctx.exception), 'The form error should name the currency the deployment expects'
        )


class BundleCourseItemAdminTest(TestCase):
    """The bundle-course inline: shown for a program bundle, hidden for every other item type."""

    def setUp(self):
        self.admin_site = AdminSite()
        self.admin = CatalogueItemAdmin(CatalogueItem, self.admin_site)
        self.bundle = CatalogueItem.objects.create(
            sku='ADMIN-BUNDLE-1',
            type=CatalogueItem.ItemType.PROGRAM_BUNDLE,
            title='Admin Bundle',
            item_ref_id='program-uuid-admin',
            price=90,
            currency='SAR',
        )
        self.paid_course = CatalogueItem.objects.create(
            sku='ADMIN-COURSE-1',
            type=CatalogueItem.ItemType.PAID_COURSE,
            title='Admin Course',
            item_ref_id='course-v1:admin+1+1',
            price=90,
            currency='SAR',
        )

    def test_the_autocomplete_names_items_by_sku(self):
        """
        Verify that the autocomplete behind the add page names each item by its SKU.

        The add page renders the bundle and course fields as autocompletes, whose options are fetched from the
        admin's own endpoint and labelled there with the model's string, so that endpoint is where the SKU has
        to surface.
        """
        self.client.force_login(
            User.objects.create_superuser(username='autocomplete-admin', email='ac@example.com', password='pass'),
        )

        response = self.client.get(
            reverse('admin:autocomplete'),
            {
                'app_label': 'zeitlabs_payments',
                'model_name': 'bundlecourseitem',
                'field_name': 'bundle',
                'term': 'ADMIN-BUNDLE-1',
            },
        )

        self.assertEqual(
            [result['text'] for result in response.json()['results']], ['ADMIN-BUNDLE-1'],
            'The autocomplete should name the bundle by its SKU',
        )

    def test_inline_is_shown_for_a_program_bundle(self):
        """Verify that the change page of a program bundle carries the BundleCourseItem inline."""
        request = RequestFactory().get('/admin/')
        request.user = User.objects.create(username='bundle-admin', is_superuser=True)
        inlines = self.admin.get_inline_instances(request, self.bundle)
        inline_classes = [type(inline).__name__ for inline in inlines]
        assert 'BundleCourseItemInline' in inline_classes

    def test_inline_is_hidden_for_other_item_types(self):
        """Verify that the change page of a paid course carries no BundleCourseItem inline."""
        request = RequestFactory().get('/admin/')
        request.user = User.objects.create(username='bundle-admin-paid', is_superuser=True)
        inlines = self.admin.get_inline_instances(request, self.paid_course)
        assert 'BundleCourseItemInline' not in [type(inline).__name__ for inline in inlines]

    def test_inline_is_hidden_on_the_add_page(self):
        """Verify that the add page (no object) carries no BundleCourseItem inline."""
        request = RequestFactory().get('/admin/')
        request.user = User.objects.create(username='bundle-admin-add', is_superuser=True)
        inlines = self.admin.get_inline_instances(request, None)
        assert 'BundleCourseItemInline' not in [type(inline).__name__ for inline in inlines]


class PaymentSitePermissionAdminTest(TestCase):
    """The admin page where the platform's own staff grant a site's employees access to its payments."""

    def setUp(self):
        self.client.force_login(User.objects.create(username='platform-admin', is_staff=True, is_superuser=True))
        self.site = Site.objects.get(pk=1)

    def test_grants_are_listed_and_one_with_its_flag_off_can_be_added(self):
        """
        Verify that the platform's staff see the grants listed, and can add one with its flag off, which is kept so
        access can be prepared, or switched off for a while, without deleting the row.
        """
        granted = User.objects.create(username='granted-employee')
        PaymentSitePermission.objects.create(user=granted, site=self.site, users_order_history=True)
        paused = User.objects.create(username='paused-employee')

        changelist = self.client.get(reverse('admin:zeitlabs_payments_paymentsitepermission_changelist'))
        added = self.client.post(
            reverse('admin:zeitlabs_payments_paymentsitepermission_add'), {'user': paused.pk, 'site': self.site.pk},
        )

        self.assertEqual(changelist.status_code, 200, 'the grants could not be listed')
        self.assertContains(changelist, 'granted-employee', msg_prefix='the grant is not listed')
        self.assertEqual(added.status_code, 302, 'the grant with its flag off was not saved')
        self.assertTrue(
            PaymentSitePermission.objects.filter(user=paused, site=self.site, users_order_history=False).exists(),
            'the grant with its flag off was not stored',
        )

    def test_a_second_grant_for_the_same_user_and_site_is_refused_on_the_form(self):
        """Verify that the add page refuses a second grant for a user on the same site, rather than failing."""
        employee = User.objects.create(username='site-employee')
        PaymentSitePermission.objects.create(user=employee, site=self.site)

        response = self.client.post(
            reverse('admin:zeitlabs_payments_paymentsitepermission_add'),
            {'user': employee.pk, 'site': self.site.pk, 'users_order_history': 'on'},
        )

        self.assertEqual(response.status_code, 200, 'the duplicate grant was not sent back to the form')
        self.assertEqual(
            PaymentSitePermission.objects.filter(user=employee).count(), 1, 'a second grant for the site was saved',
        )
