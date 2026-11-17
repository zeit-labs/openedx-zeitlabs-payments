"""Django admin view for the models."""
import re
from typing import Any, Optional
from urllib.parse import urlencode

from django import forms
from django.apps import apps as django_apps
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.db.models.query import QuerySet
from django.http import HttpResponse
from django.shortcuts import render
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from . import resolution
from .exceptions import ResolutionError
from .models import (
    AuditLog,
    BundleCourseItem,
    Cart,
    CartItem,
    CatalogueItem,
    Invoice,
    InvoiceItem,
    PaymentSitePermission,
    Transaction,
)
from .providers.registry import PROCESSORS


class ViewOnlyMixin:
    """
    Refuse every write an admin page can offer: adding, saving and deleting alike.

    List it among the bases of a ModelAdmin or an inline, ahead of the Django class. It governs permissions
    rather than fields, and permissions are what take the save and delete buttons off the page; declaring
    ``readonly_fields`` only changes how a field is drawn and leaves the buttons sitting there with nothing to
    write. Reading is untouched, because Django answers a view request from the ``view`` or ``change``
    permission on the model rather than from these hooks, so a team that already had access keeps it.

    The models written by checkout and by the payment gateway are records of what already happened. Editing one
    of those rows does not correct history, it falsifies it.
    """

    def has_add_permission(self, request: Any, obj: Any = None) -> bool:  # pylint: disable=unused-argument
        """Refuse the add page, which also drops the "Add" button and the "Save and add another" button."""
        return False

    def has_change_permission(self, request: Any, obj: Any = None) -> bool:  # pylint: disable=unused-argument
        """Refuse saving, which is what removes the save buttons from an otherwise readable page."""
        return False

    def has_delete_permission(self, request: Any, obj: Any = None) -> bool:  # pylint: disable=unused-argument
        """Refuse deleting, from the object's own page and from the changelist's bulk action alike."""
        return False


class CartLinkMixin(admin.ModelAdmin):
    """
    Give an admin a ``cart_link`` field showing the cart id as a link to that cart's own admin page.

    Name it in ``list_display``, ``fields`` or ``readonly_fields``. Django otherwise renders a foreign key
    through the model's ``__str__``, which reads "Cart object (9)" and goes nowhere. The id is read off the row
    itself, so the column adds no join, and a row with no cart falls back to the ordinary empty marker.
    """

    @admin.display(description='Cart', ordering='cart')
    def cart_link(self, obj: Any) -> str:
        """Return the cart id linked to its admin page, or the empty marker when the row has no cart."""
        if obj.cart_id is None:
            return self.get_empty_value_display()
        url = reverse('admin:zeitlabs_payments_cart_change', args=(obj.cart_id,))
        return format_html('<a href="{}">{}</a>', url, obj.cart_id)


class TransactionLinkMixin(admin.ModelAdmin):
    """
    Give an admin a ``related_transaction_link`` field showing the transaction id as a link to its own page.

    Name it in ``list_display``, ``fields`` or ``readonly_fields``. Django otherwise renders a foreign key
    through the model's ``__str__``, which reads "Transaction object (4)" and names no payment. The id is read
    off the row itself, so the field adds no join, and a row not yet tied to a payment shows the empty marker.
    """

    @admin.display(description='Related transaction', ordering='related_transaction')
    def related_transaction_link(self, obj: Any) -> str:
        """Return the transaction id linked to its admin page, or the empty marker when there is none."""
        if obj.related_transaction_id is None:
            return self.get_empty_value_display()
        url = reverse('admin:zeitlabs_payments_transaction_change', args=(obj.related_transaction_id,))
        return format_html('<a href="{}">{}</a>', url, obj.related_transaction_id)


class AuditLogLinkMixin(admin.ModelAdmin):
    """
    Give an admin an ``audit_log_link`` field: a button opening the audit trail of this row's cart.

    Name it in ``fields`` and ``readonly_fields``. The cart id is read off the row through
    ``audit_log_cart_field``, which is the plain foreign key on most models and the primary key on the cart's
    own page, so the field adds no join and a row with no cart shows the ordinary empty marker.

    The target is built as the search term ``cart=<id>`` rather than a column filter, because that term is
    what ``CartSearchMixin`` reads on the audit log. The page it opens is therefore one a reader can reach by
    typing the same thing into the search box, rather than a URL shape only this button knows how to make.
    """

    LINK_TEXT = 'View related audit logs'

    audit_log_cart_field = 'cart_id'

    @admin.display(description='Audit log')
    def audit_log_link(self, obj: Any) -> str:
        """Return a button opening this row's cart in the audit log, or the empty marker when it has no cart."""
        cart_id = getattr(obj, self.audit_log_cart_field, None)
        if cart_id is None:
            return self.get_empty_value_display()
        url = reverse('admin:zeitlabs_payments_auditlog_changelist')
        query = urlencode({'q': f'cart={cart_id}'})
        return format_html('<a class="button" href="{}?{}">{}</a>', url, query, self.LINK_TEXT)


class CartSearchMixin(admin.ModelAdmin):
    """
    Give an admin's search box a ``cart=<number>`` token that filters the page to that one cart.

    List it before ``admin.ModelAdmin`` among an admin's bases; it is never registered on its own. Models
    holding a plain ``cart`` foreign key need no more than that, while models that reach the cart by another
    route set ``cart_search_lookup`` to the path that gets there, such as ``'related_transaction__cart_id'``.

    The box is otherwise a text search across ``search_fields``, and these models carry transaction and course
    ids in their text columns, so a bare number matches far more than the cart it names. Only the whole term in
    this one form is read as a cart; anything else, ``cart=abc`` and ``cart= 6`` alike, goes to the ordinary
    search rather than quietly answering a question the reader did not ask.
    """

    CART_SEARCH = re.compile(r'cart=(\d+)', re.IGNORECASE)

    cart_search_lookup = 'cart_id'
    search_help_text = 'To filter on a specific cart, type cart=<the cart id>. eg. `cart=6`'

    def get_search_results(self, request: Any, queryset: QuerySet, search_term: str) -> tuple[QuerySet, bool]:
        """Filter to the named cart when the box holds exactly the token, and search normally otherwise."""
        cart_number = self.CART_SEARCH.fullmatch(search_term.strip())
        if cart_number is None:
            return super().get_search_results(request, queryset, search_term)
        return queryset.filter(**{self.cart_search_lookup: cart_number[1]}), False


class PaidWithoutInvoiceFilter(admin.SimpleListFilter):
    """Narrow the cart list to the paid carts left without an invoice, which wait for staff to retry or refund them."""

    PAID_WITHOUT_INVOICE = 'paid_without_invoice'

    title = 'fulfillment'
    parameter_name = 'fulfillment'

    def lookups(self, request: Any, model_admin: Any) -> list[tuple[str, str]]:
        """Offer the one choice this filter makes."""
        return [(self.PAID_WITHOUT_INVOICE, 'Paid without invoice')]

    def queryset(self, request: Any, queryset: QuerySet) -> QuerySet:
        """Keep only the paid carts without an invoice when chosen, and every cart otherwise."""
        if self.value() == self.PAID_WITHOUT_INVOICE:
            return queryset.filter(status=Cart.Status.PAID, invoices__isnull=True)
        return queryset


class RefundForm(forms.Form):
    """What a refund made in the gateway's dashboard is recorded under."""

    refund_reference = forms.CharField(
        label='Refund reference',
        max_length=Transaction._meta.get_field('gateway_transaction_id').max_length,
        help_text="The reference the gateway's dashboard gives the refund.",
    )


class UnreconciledRefundForm(RefundForm):
    """The refund of an unreconciled cart, which has no settled payment to take the amount refunded from."""

    amount = forms.DecimalField(
        label='Amount refunded',
        max_digits=Transaction._meta.get_field('amount').max_digits,
        decimal_places=Transaction._meta.get_field('amount').decimal_places,
        help_text="The amount the gateway's dashboard shows refunded, in the cart's currency.",
    )


class AcceptPaymentForm(forms.Form):
    """What staff copy from the gateway's dashboard to accept the payment of an unreconciled cart."""

    gateway_transaction_id = forms.CharField(
        label='Transaction id',
        max_length=Transaction._meta.get_field('gateway_transaction_id').max_length,
        help_text="The id the gateway's dashboard gives the payment.",
    )
    amount = forms.DecimalField(
        label='Amount taken',
        max_digits=Transaction._meta.get_field('amount').max_digits,
        decimal_places=Transaction._meta.get_field('amount').decimal_places,
        help_text="The amount the gateway's dashboard shows taken, in the cart's currency.",
    )


@admin.register(Cart)
class CartAdmin(AuditLogLinkMixin, ViewOnlyMixin, admin.ModelAdmin):
    """
    Admin configuration for the Cart model.

    The cart itself is never edited here. A cart whose payment did not end in an invoice is resolved through three
    actions, each behind its own permission: retrying the invoice and enrolment of a paid cart left without one,
    accepting the payment of an unreconciled cart, and recording the refund of either.
    """

    INVOICED_STATUSES = (Cart.Status.PAID, Cart.Status.REFUNDED)

    list_display = ('id', 'user', 'status', 'created_at')
    list_filter = ('status', PaidWithoutInvoiceFilter, 'created_at')
    actions = ('retry_fulfillment', 'accept_payment', 'record_refund')
    search_fields = ('user__username', 'user__email', 'id', 'gateway_order_id')
    fields = ('user', 'status', 'gateway', 'gateway_order_id', 'payment_origin', 'invoice_link', 'audit_log_link')
    readonly_fields = fields

    # A cart is the audited row itself rather than a row pointing at one, so its own key is the cart id.
    audit_log_cart_field = 'id'

    @admin.display(description='Invoice')
    def invoice_link(self, obj: Cart) -> str:
        """
        Return a link to the cart's invoice, and only once the cart has been paid or refunded.

        An invoice is written at fulfilment, so a cart in any earlier state has nothing to point at. Gating on
        the status rather than on whether a row happens to exist keeps a half-finished checkout from surfacing
        a document the buyer was never given. The number is what identifies the invoice on paper.
        """
        invoice = obj.invoices.order_by('id').first() if obj.status in self.INVOICED_STATUSES else None
        if invoice is None:
            return self.get_empty_value_display()
        url = reverse('admin:zeitlabs_payments_invoice_change', args=(invoice.id,))
        return format_html('<a href="{}">{}</a>', url, invoice.invoice_number)

    @admin.action(
        description='Retry the invoice and enrollment of the selected carts', permissions=['retry_fulfillment'],
    )
    def retry_fulfillment(self, request: Any, queryset: QuerySet) -> None:
        """
        Retry the invoice and enrolment of each selected cart, and report each on its own.

        Each cart is retried in a transaction of its own, so one refused or failing again holds none of the others back.
        """
        for cart_id in queryset.order_by('id').values_list('id', flat=True):
            try:
                invoice = resolution.retry_fulfillment(cart_id, request)
            except ResolutionError as exc:
                self.message_user(request, str(exc), messages.ERROR)
            else:
                self.message_user(
                    request, f'Cart {cart_id} is fulfilled, and invoiced as {invoice.invoice_number}.',
                    messages.SUCCESS,
                )

    @admin.action(
        description='Accept the payment of the selected unreconciled cart', permissions=['accept_payment'],
    )
    def accept_payment(self, request: Any, queryset: QuerySet) -> Optional[TemplateResponse]:
        """
        Accept the payment of one selected unreconciled cart, once its transaction id and amount are confirmed.

        The action first answers with a page showing the cart, its total, and what was recorded of the gateway's
        answers -- the payment rows, and why each answer was refused -- and asking for the transaction id and the amount
        taken as the gateway's dashboard shows them, which posts back to it.
        """
        if queryset.count() != 1:
            self.message_user(request, 'Select exactly one cart to accept the payment of.', messages.ERROR)
            return None
        cart = queryset.get()
        form = AcceptPaymentForm(request.POST if 'confirm' in request.POST else None)
        if form.is_valid():
            try:
                invoice = resolution.accept_payment(
                    cart.id, request, form.cleaned_data['gateway_transaction_id'], form.cleaned_data['amount'],
                )
            except ResolutionError as exc:
                self.message_user(request, str(exc), messages.ERROR)
            else:
                self.message_user(
                    request, f'The payment of cart {cart.id} is accepted, and the cart invoiced as '
                    f'{invoice.invoice_number}.',
                    messages.SUCCESS,
                )
            return None
        refusals = cart.audits.filter(action__in=(
            AuditLog.AuditActions.GATEWAY_RESPONSE_REJECTED,
            AuditLog.AuditActions.TRANSACTION_NOT_RECONCILED,
        )).order_by('id')
        return TemplateResponse(request, 'zeitlabs_payments/admin/accept_payment.html', {
            **self.admin_site.each_context(request),
            'title': f'Accept the payment of cart {cart.id}',
            'opts': self.model._meta,
            'cart': cart,
            'payments': cart.transactions.filter(type=Transaction.TransactionType.PAYMENT).order_by('id'),
            'refusals': refusals,
            'form': form,
            'action_checkbox_name': helpers.ACTION_CHECKBOX_NAME,
        })

    @admin.action(description='Record the refund of the selected cart', permissions=['record_refund'])
    def record_refund(self, request: Any, queryset: QuerySet) -> Optional[TemplateResponse]:
        """
        Record the refund of one selected cart, made in the gateway's dashboard, once confirmed with its reference.

        The action first answers with a page confirming the cart and asking for the refund's reference, which posts
        back to it; for an unreconciled cart, which has no settled payment to take it from, the amount refunded too.
        The page also lists any bundle of the cart that failed part-way, since the learner may have been told they are
        enrolled in the courses that went through before the failure.
        """
        if queryset.count() != 1:
            self.message_user(request, 'Select exactly one cart to record a refund for.', messages.ERROR)
            return None
        cart = queryset.get()
        form_class = UnreconciledRefundForm if cart.status == Cart.Status.UNRECONCILED else RefundForm
        form = form_class(request.POST if 'confirm' in request.POST else None)
        if form.is_valid():
            try:
                refund = resolution.record_refund(
                    cart.id, request.user, form.cleaned_data['refund_reference'], form.cleaned_data.get('amount'),
                )
            except ResolutionError as exc:
                self.message_user(request, str(exc), messages.ERROR)
            else:
                self.message_user(
                    request, f'Cart {cart.id} is refunded under reference {refund.gateway_transaction_id}.',
                    messages.SUCCESS,
                )
            return None
        bundle_failures = cart.audits.filter(action=AuditLog.AuditActions.BUNDLE_FULFILLMENT_FAILED).order_by('id')
        return TemplateResponse(request, 'zeitlabs_payments/admin/record_refund.html', {
            **self.admin_site.each_context(request),
            'title': f'Record the refund of cart {cart.id}',
            'opts': self.model._meta,
            'cart': cart,
            'payments': cart.transactions.filter(type=Transaction.TransactionType.PAYMENT).order_by('id'),
            'bundle_failures': bundle_failures,
            'form': form,
            'action_checkbox_name': helpers.ACTION_CHECKBOX_NAME,
        })

    def has_retry_fulfillment_permission(self, request: Any) -> bool:
        """Offer the retry only to staff holding its own permission."""
        return request.user.has_perm('zeitlabs_payments.retry_cart_fulfillment')

    def has_accept_payment_permission(self, request: Any) -> bool:
        """Offer accepting a payment only to staff holding its own permission."""
        return request.user.has_perm('zeitlabs_payments.accept_cart_payment')

    def has_record_refund_permission(self, request: Any) -> bool:
        """Offer the refund only to staff holding its own permission."""
        return request.user.has_perm('zeitlabs_payments.record_cart_refund')


@admin.register(CartItem)
class CartItemAdmin(CartSearchMixin, CartLinkMixin, ViewOnlyMixin, admin.ModelAdmin):
    """
    Admin configuration for the CartItem model.
    """

    list_display = ('id', 'cart_link', 'catalogue_item_link', 'final_price')
    list_filter = ('cart__status',)
    list_select_related = ('catalogue_item',)
    search_fields = ('cart__id', 'catalogue_item__sku')
    fields = ('cart_link', 'catalogue_item_link', 'original_price', 'final_price')
    readonly_fields = fields

    @admin.display(description='Catalogue item', ordering='catalogue_item__sku')
    def catalogue_item_link(self, obj: CartItem) -> str:
        """
        Return the catalogue item's SKU, linked to its own admin page.

        The SKU is what names the thing being sold, where the default ``__str__`` reads "CatalogueItem object
        (3)" and identifies nothing. The item is never null on a cart row, so there is no empty case to answer.
        """
        url = reverse('admin:zeitlabs_payments_catalogueitem_change', args=(obj.catalogue_item_id,))
        return format_html('<a href="{}">{}</a>', url, obj.catalogue_item.sku)


class BundleCourseItemInline(admin.TabularInline):
    """Inline for BundleCourseItem on the CatalogueItem admin, used only when the item type is program_bundle."""

    model = BundleCourseItem
    fk_name = 'bundle'
    extra = 1
    autocomplete_fields = ('course_item',)


@admin.register(CatalogueItem)
class CatalogueItemAdmin(admin.ModelAdmin):
    """
    Admin configuration for the CatalogueItem model.
    """

    list_display = ('id', 'sku', 'type', 'price', 'currency')
    list_filter = ('type',)
    search_fields = ('sku',)
    inlines = [BundleCourseItemInline]

    def get_inline_instances(self, request: Any, obj: Any = None) -> list:
        """Show the bundle-course inline only when the item type is program_bundle."""
        if obj and obj.type == CatalogueItem.ItemType.PROGRAM_BUNDLE:
            return super().get_inline_instances(request, obj)
        return []


@admin.register(BundleCourseItem)
class BundleCourseItemAdmin(admin.ModelAdmin):
    """
    Admin configuration for the BundleCourseItem model.
    """

    list_display = ('id', 'bundle', 'course_item')
    list_filter = ('bundle',)
    search_fields = ('bundle__sku', 'course_item__sku')
    autocomplete_fields = ('bundle', 'course_item')


@admin.register(PaymentSitePermission)
class PaymentSitePermissionAdmin(admin.ModelAdmin):
    """
    Where the platform's own staff grant a site's employees access to the site's payments, one row per user and site.
    """

    list_display = ('user', 'site', 'users_order_history', 'updated_at')
    list_filter = ('site', 'users_order_history')
    search_fields = ('user__username', 'user__email')
    # The user table holds every learner of every site, too many to list in a select.
    raw_id_fields = ('user',)


@admin.register(Transaction)
class TransactionAdmin(AuditLogLinkMixin, CartSearchMixin, CartLinkMixin, ViewOnlyMixin, admin.ModelAdmin):
    """
    Admin configuration for the Transaction model.
    """

    list_display = (
        'id',
        'cart_link',
        'type',
        'status',
        'gateway',
        'gateway_transaction_id',
        'method',
        'amount',
        'currency',
        'initiator_user',
        'created_at',
    )
    list_filter = ('type', 'status', 'gateway', 'method', 'currency', 'created_at')
    search_fields = ('gateway_transaction_id', 'cart__id', 'initiator_user__username', 'initiator_user__email')
    fields = (
        'id',
        'cart_link',
        'type',
        'status',
        'gateway',
        'gateway_transaction_id',
        'method',
        'amount',
        'currency',
        'initiator_user',
        'response',
        'reason',
        'created_at',
        'updated_at',
        'audit_log_link',
    )
    readonly_fields = fields


@admin.register(AuditLog)
class AuditLogAdmin(CartSearchMixin, CartLinkMixin, ViewOnlyMixin, admin.ModelAdmin):
    """
    Admin for AuditLog model.
    """

    list_display = ('id', 'cart_link', 'action', 'gateway', 'created_at', 'details')
    list_filter = ('action', 'gateway', 'created_at')
    search_fields = ('cart__user__username', 'cart__user__email', 'action', 'details', 'gateway')
    fields = ('cart_link', 'action', 'gateway', 'details', 'created_at')
    readonly_fields = fields
    ordering = ('-created_at',)


class InvoiceItemInline(admin.TabularInline):
    model = InvoiceItem
    extra = 0
    exclude = ('discount_amount', 'tax_amount')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Invoice)
class InvoiceAdmin(
    AuditLogLinkMixin, CartSearchMixin, CartLinkMixin, TransactionLinkMixin, ViewOnlyMixin, admin.ModelAdmin,
):
    """
    Admin for Invoice model.
    """

    list_display = (
        'invoice_number',
        'cart_link',
        'status',
        'total',
        'currency',
        'related_transaction_link',
        'paid_at',
        'created_at',
        'updated_at',
    )
    list_filter = ('status', 'currency')
    search_fields = ('invoice_number', 'cart__id')
    inlines = [InvoiceItemInline]
    fields = (
        'invoice_number',
        'cart_link',
        'status',
        'gross_total',
        'total',
        'currency',
        'related_transaction_link',
        'paid_at',
        'learner_info',
        'organization_info',
        'created_at',
        'updated_at',
        'audit_log_link',
    )
    readonly_fields = fields


@admin.register(InvoiceItem)
class InvoiceItemAdmin(ViewOnlyMixin, admin.ModelAdmin):
    """
    Admin for InvoiceItem model.
    """

    list_display = (
        'id',
        'invoice_link',
        'cart_item_link',
        'original_price',
        'price',
        'quantity',
        'created_at',
        'updated_at',
    )
    search_fields = ('invoice__invoice_number',)
    list_select_related = ('invoice',)
    fields = (
        'invoice_link',
        'cart_item_link',
        'original_price',
        'price',
        'quantity',
        'created_at',
        'updated_at',
    )
    readonly_fields = fields

    @admin.display(description='Invoice', ordering='invoice__invoice_number')
    def invoice_link(self, obj: InvoiceItem) -> str:
        """
        Return the invoice number, linked to that invoice's own admin page.

        The number is what names the bill this line belongs to, where the default ``__str__`` reads "Invoice
        object (2)" and identifies nothing. A line always belongs to an invoice, so there is no empty case.
        """
        url = reverse('admin:zeitlabs_payments_invoice_change', args=(obj.invoice_id,))
        return format_html('<a href="{}">{}</a>', url, obj.invoice.invoice_number)

    @admin.display(description='Cart item', ordering='cart_item')
    def cart_item_link(self, obj: InvoiceItem) -> str:
        """
        Return the cart item id, linked to that cart item's own admin page.

        The id is read off this row itself, so the column adds no join. A cart item is dropped to null when it
        is deleted out from under the line it was billed as, and such a line shows the empty marker.
        """
        if obj.cart_item_id is None:
            return self.get_empty_value_display()
        url = reverse('admin:zeitlabs_payments_cartitem_change', args=(obj.cart_item_id,))
        return format_html('<a href="{}">{}</a>', url, obj.cart_item_id)


class PaymentProcessorAdminPage:
    """
    Registers a custom admin page to display registered Payment Processors.

    It will injects a new fake/dummy model 'Payment Processors' under 'zeitlabs_payments' app
    to render custom admin page that will show all registered processors from PROCESSORS dict.

    All logic is self-contained and does NOT require a database model.
    """

    # The page is filed under this app in the admin index, so it is served from under the app's path too.
    # Django gives every registered model '<app_label>/<model>/'; a page reached at the admin root instead
    # sits outside the app it is listed in, and any per-app URL rule written for the app misses it.
    APP_LABEL = 'zeitlabs_payments'
    URL_PATH = f'{APP_LABEL}/processors/'
    URL_NAME = f'{APP_LABEL}_processors'

    def __init__(self) -> None:
        """Initialize the admin URL and app-list patches."""
        self._patch_admin_urls()
        self._patch_app_list()

    def view(self, request: Any) -> HttpResponse:
        """
        Render custom admin page and display processors table.

        The app label and its verbose name go into the context because the breadcrumb needs them and there is
        no model here to read them off. Django builds a model page's trail from ``opts``; this page has no
        ``opts``, so without them the trail skips the app and reads as though the page sat at the admin root.
        """
        processors_with_path = {
            slug: {
                'cls': cls,
                'name': cls.NAME,
                'path': f'{cls.__module__}.{cls.__name__}'
            }
            for slug, cls in PROCESSORS.items()
        }
        context = dict(
            admin.site.each_context(request),
            processors=processors_with_path,
            title=_('Payment Processors'),
            app_label=self.APP_LABEL,
            app_verbose_name=django_apps.get_app_config(self.APP_LABEL).verbose_name,
        )
        return render(request, 'zeitlabs_payments/admin/processors_list.html', context)

    def _patch_admin_urls(self) -> None:
        """Patch admin urls and add append custom url to it."""
        original_get_urls = admin.site.get_urls

        def get_urls() -> list:
            urls = original_get_urls()
            custom_urls = [
                path(
                    self.URL_PATH,
                    admin.site.admin_view(self.view),
                    name=self.URL_NAME,
                ),
            ]
            return custom_urls + urls

        admin.site.get_urls = get_urls

    def _patch_app_list(self) -> None:
        """Patch admin apps list and add append custom model 'Payment Processors' under zeitlabs_payments app."""
        original_get_app_list = admin.site.get_app_list

        def custom_get_app_list(request: Any, app_label: str | None = None) -> list:
            app_list = list(original_get_app_list(request, app_label))

            # find the app dict for zeitlabs_payments, if it exists and add custom Processor Link/app
            zp_app = next((app for app in app_list if app.get('app_label') == self.APP_LABEL), None)
            if zp_app is not None:
                models = zp_app.setdefault('models', [])
                if 'PaymentProcessors' not in [m.get('object_name') for m in models]:
                    models.append({
                        'name': 'Payment Processors',
                        'object_name': 'PaymentProcessors',
                        'admin_url': reverse(f'admin:{self.URL_NAME}'),
                        'add_url': None,
                        'view_only': True,
                        'perms': {'add': False, 'change': False, 'delete': False, 'view': True},
                    })
            return app_list

        admin.site.get_app_list = custom_get_app_list


# Instantiate once to register everything
PaymentProcessorAdminPage()
