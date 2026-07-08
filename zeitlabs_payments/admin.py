"""Django admin view for the models."""
from typing import Any

from django.contrib import admin
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import path, reverse

from .helpers import get_course_id
from .models import AuditLog, Cart, CartItem, CatalogueItem, Invoice, InvoiceItem, TaxRule, Transaction, WebhookEvent, ManualManagement
from .providers.registry import PROCESSORS
from .providers.manual_payment.processor import ManualPaymentProcessor


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    """
    Admin configuration for the Cart model.
    """

    list_display = ('id', 'user', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('user__email', 'id')


@admin.register(CartItem)
class CartItemAdmin(admin.ModelAdmin):
    """
    Admin configuration for the CartItem model.
    """

    list_display = ('id', 'cart', 'catalogue_item', 'final_price', 'discount_amount')
    list_filter = ('cart__status',)
    search_fields = ('cart__id', 'catalogue_item__sku')


@admin.register(CatalogueItem)
class CatalogueItemAdmin(admin.ModelAdmin):
    """
    Admin configuration for the CatalogueItem model.
    """

    list_display = ('id', 'sku', 'type', 'price', 'currency')
    list_filter = ('type',)
    search_fields = ('sku',)


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    """
    Admin configuration for the Transaction model.
    """

    list_display = (
        'id',
        'cart',
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
    readonly_fields = ('id', 'created_at')
    raw_id_fields = ('cart', 'initiator_user')


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    """
    Admin configuration for the WebhookEvent model.
    """

    list_display = (
        'id',
        'gateway',
        'event_type',
        'related_transaction',
        'created_at',
        'handled',
    )
    list_filter = ('gateway', 'event_type', 'handled', 'created_at')
    search_fields = ('id', 'gateway', 'event_type', 'related_transaction__gateway_transaction_id')
    readonly_fields = ('id', 'created_at')
    raw_id_fields = ('related_transaction',)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """
    Admin for AuditLog model.
    """

    list_display = ('id', 'cart', 'action', 'gateway', 'created_at', 'details')
    list_filter = ('action', 'gateway', 'created_at')
    search_fields = ('cart__user__username', 'cart__user__email', 'action', 'details', 'gateway')
    readonly_fields = ('cart', 'action', 'gateway', 'details', 'created_at')
    ordering = ('-created_at',)

    def has_add_permission(self, request: Any) -> bool:
        """Disallow adding logs manually."""
        return False


class InvoiceItemInline(admin.TabularInline):
    model = InvoiceItem
    extra = 0
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    """
    Admin for Invoice model.
    """

    list_display = (
        'invoice_number',
        'cart',
        'status',
        'total',
        'discount_total',
        'currency',
        'paid_at',
        'created_at',
        'updated_at',
    )
    list_filter = ('status', 'currency')
    search_fields = ('invoice_number', 'cart__id')
    inlines = [InvoiceItemInline]
    readonly_fields = ('created_at', 'updated_at')


@admin.register(InvoiceItem)
class InvoiceItemAdmin(admin.ModelAdmin):
    """
    Admin for InvoiceItem model.
    """

    list_display = (
        'invoice',
        'cart_item',
        'original_price',
        'discount_amount',
        'price',
        'quantity',
        'created_at',
        'updated_at',
    )
    search_fields = ('invoice__invoice_number',)
    readonly_fields = ('created_at', 'updated_at')


@admin.register(TaxRule)
class TaxRuleAdmin(admin.ModelAdmin):
    """
    Admin for TaxRule model.
    """

    list_display = ('name', 'tax_type', 'tax_value', 'is_active', 'created_at', 'updated_at')
    list_filter = ('tax_type', 'is_active')
    search_fields = ('name',)
    ordering = ('-is_active', '-id')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(ManualManagement)
class ManualManagementAdmin(admin.ModelAdmin):
    """
    Admin configuration for the ManualManagement model.
    """

    list_display = ('id', 'user', 'status', 'approver', 'course_id', 'invoice', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('user__email', 'cart__id', 'id', 'cart__items__catalogue_item__item_ref_id')
    autocomplete_fields = ('cart', 'user', 'approver')

    def get_readonly_fields(self, request, obj=None):
        readonly = ['cart', 'user', 'approver', 'invoice']
        if obj and obj.status == ManualManagement.ManualManagementType.PAID:
            readonly.append('status')
        return readonly

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.prefetch_related('cart__items__catalogue_item')

    @admin.display(description='Course ID')
    def course_id(self, obj):
        items = list(obj.cart.items.all())
        if not items:
            return '-'

        try:
            return get_course_id(items[0])
        except GatewayError:
            return '-'

    def has_add_permission(self, request):
        return False

    def save_model(self, request, obj, form, change):
        # Detect whether status is being changed to PAYMENT in this save.
        is_becoming_payment = (
            obj.status == ManualManagement.ManualManagementType.PAID
            and (not change or 'status' in form.changed_data)
        )

        if is_becoming_payment:
            obj.approver = request.user
            result = self._handle_payment(request, obj)
            obj.invoice = result['created_invoice']

        super().save_model(request, obj, form, change)

    def _handle_payment(self, request, obj):
        """
        Runs whenever this ManualManagement record is set to PAID.
        Directly invokes ManualPaymentProcessor.process_payment on the existing cart.

        :return: dict with 'created_cart' and 'created_invoice' keys.
        """
        processor = ManualPaymentProcessor()
        try:
             return processor.process_payment(
                request=request,
                cart=obj.cart,
                transaction_id=str(obj.id),
                transaction_status='success',
                reason=f'Manual payment created via admin by {request.user}',
            )
        except Exception as exc:
            raise ValidationError(f'Manual payment failed: {exc}') from exc


class PaymentProcessorAdminPage:
    """
    Registers a custom admin page to display registered Payment Processors.

    It will injects a new fake/dummy model 'Payment Processors' under 'zeitlabs_payments' app
    to render custom admin page that will show all registered processors from PROCESSORS dict.

    All logic is self-contained and does NOT require a database model.
    """

    URL_NAME = 'zeitlabs_payments_processors'

    def __init__(self) -> None:
        """Initilaize patch admin urls and applist."""
        self._patch_admin_urls()
        self._patch_app_list()

    def view(self, request: Any) -> HttpResponse:
        """
        Render custom admin page and display processors table.
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
            title='Payment Processors',
        )
        return render(request, 'zeitlabs_payments/admin/processors_list.html', context)

    def _patch_admin_urls(self) -> None:
        """Patch admin urls and add append custom url to it."""
        original_get_urls = admin.site.get_urls

        def get_urls() -> list:
            urls = original_get_urls()
            custom_urls = [
                path(
                    'processors/',
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
            zp_app = next((app for app in app_list if app.get('app_label') == 'zeitlabs_payments'), None)
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
