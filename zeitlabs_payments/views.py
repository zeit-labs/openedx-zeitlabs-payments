"""Zeilabs payments views."""

import logging
from typing import Any

from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import TemplateView
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from zeitlabs_payments import models
from zeitlabs_payments.cart_handler import CART_HANDLER
from zeitlabs_payments.exceptions import InvalidCartError
from zeitlabs_payments.helpers import get_currency, get_settings
from zeitlabs_payments.providers.registry import PROCESSORS, get_processor
from zeitlabs_payments.querysets import get_orders_queryset
from zeitlabs_payments.serializers import CartSerializer

logger = logging.getLogger(__name__)
User = get_user_model()


class ContextMixing(TemplateView):
    """
    Mixin to add common context data to views.
    """

    def get_context_data(self, **kwargs: Any) -> dict:
        """
        Return Context dictionary including common settings.
        """
        context = super().get_context_data(**kwargs)
        context.update(
            {
                'support_url': get_settings().support_url,
                'support_email': get_settings().support_email,
                'logo_url': get_settings().logo_url,
            }
        )
        return context


class CheckoutView(LoginRequiredMixin, ContextMixing):
    """
    View responsible for rendering the checkout page.

    This page is only accessible by authenticated users and provides a last pending cart
    along with available payment methods derived from registered processors.
    """

    template_name = 'zeitlabs_payments/checkout.html'

    def get_context_data(self, **kwargs: Any) -> dict:
        """
        Return Context dictionary including cart and payment methods.
        """
        context = super().get_context_data(**kwargs)
        methods = []
        cart = kwargs.get('cart')
        if cart:
            methods = [processor.get_payment_method_metadata(cart) for processor in PROCESSORS.values()]
            cart = CartSerializer(cart, context={'request': self.request}).data

        context.update(
            {
                'cart': cart,
                'methods': methods,
            }
        )
        return context

    def get(self, request: Any, *args: Any, **kwargs: Any) -> None:
        """
        Checkout View. If SKU provided create a new cart otherwise render last pending cart of user.
        """
        sku_code = request.GET.get('sku')
        if sku_code:
            try:
                catalog_item = models.CatalogueItem.objects.get(sku=sku_code)
            except models.CatalogueItem.DoesNotExist:
                return render(
                    request,
                    'zeitlabs_payments/invalid_cart.html',
                    {'error_message': f'Item with sku: {sku_code} does not exist.'},
                    status=404,
                )

            handler = CART_HANDLER.get(catalog_item.type)
            if not handler:
                return render(
                    request,
                    'zeitlabs_payments/invalid_cart.html',
                    {'error_message': f'Item has unsupported type: {catalog_item.type}.'},
                    status=400,
                )
            try:
                cart = handler.validate_item_and_create_cart(request.user, catalog_item)
            except InvalidCartError as exc:
                return render(
                    request,
                    'zeitlabs_payments/invalid_cart.html',
                    {'error_message': str(exc)},
                    status=400,
                )
        else:
            cart = (
                models.Cart.objects.filter(user=request.user, status=models.Cart.Status.PENDING)
                .order_by('-created_at')
                .first()
            )

        context = self.get_context_data(cart=cart)
        return self.render_to_response(context)


class InitiatePaymentView(LoginRequiredMixin, View):
    """
    View that initiates the payment process for a given provider.

    Only accessible by authenticated users.
    """

    def get(self, request: Any, provider: str, cart_id: str) -> Any:
        """
        Initiate the payment by calling the appropriate processor.

        :param request: Django request object
        :param provider: The payment provider slug
        :param cart_id: UUID of the cart
        :return: Rendered payment page or error response
        """
        try:
            processor = get_processor(provider)
            logger.debug(f'Processor found for provider: {provider}')
        except ValueError as exc:
            logger.error(f'Invalid payment provider: {provider} - {exc}')
            return HttpResponseBadRequest(f'Error: {str(exc)}')

        try:
            cart = processor.get_cart(cart_id)
        except InvalidCartError as exc:
            logger.error(f'Cart not found with id: {cart_id} - {exc}')
            return HttpResponseBadRequest(f'Error: {str(exc)}')

        if request.user != cart.user:
            logger.warning(f'User {request.user} attempted to access cart {cart.id} belonging to {cart.user}')
            return HttpResponseBadRequest(
                f'Error: User {request.user} attempted to access cart belonging to {cart.user}.'
            )

        payment_view = processor.payment_view(
            cart=cart,
            request=request,
            use_client_side_checkout=False,
        )

        cart.status = models.Cart.Status.PROCESSING
        cart.save(update_fields=['status'])

        models.AuditLog.log(
            action=models.AuditLog.AuditActions.CART_STATUS_UPDATED,
            cart=cart,
            context={
                'old_status': models.Cart.Status.PENDING,
                'new_status': models.Cart.Status.PROCESSING,
            },
        )
        logger.info(f'Cart {cart.id} status updated to {models.Cart.Status.PROCESSING}')

        models.AuditLog.log(
            action=models.AuditLog.AuditActions.REDIRECT_TO_PAYMENT,
            cart=cart,
            gateway=processor.SLUG,
            context={},
        )
        return payment_view


@method_decorator(csrf_exempt, name='dispatch')
class CartView(APIView):
    """
    ViewSet for managing user's shopping cart.

    Supports retrieving current cart and adding a SKU to the cart.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Any) -> Response:
        """
        Retrieve last cart with pending state.

        :param request: HTTP request
        :return: Serialized cart data with HTTP 200 status
        """
        last_pending_cart = (
            models.Cart.objects.filter(user=request.user, status=models.Cart.Status.PENDING)
            .order_by('-created_at')
            .first()
        )
        if last_pending_cart:
            serializer = CartSerializer(last_pending_cart, context={'request': request})
            data = serializer.data
        else:
            data = {'details': f'No pending cart found for user: {request.user}'}
        logger.debug(f'Retrieved last pending cart for user {request.user}: {last_pending_cart}')
        return Response(data, status=status.HTTP_200_OK)

    def post(self, request: Any) -> Response:
        """
        Create a new cart and add the requested SKU item.

        Expected payload:
        {
            "sku": "courseSS101",
        }

        :param request: HTTP request with SKU in data
        :return: Serialized cart data with HTTP 201 status or error response
        """
        sku_code = request.data.get('sku')

        if not sku_code:
            logger.warning('POST to CartView missing SKU in request data')
            return Response(
                {'error': 'SKU is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            catalog_item = models.CatalogueItem.objects.get(sku=sku_code)
            logger.debug(f'Catalog item found for SKU {sku_code}')
        except models.CatalogueItem.DoesNotExist:
            return Response(
                {'error': 'Invalid SKU, unable to find catalogue item.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        handler = CART_HANDLER.get(catalog_item.type)
        if not handler:
            return Response(
                {'error': f'Item with given SKU has unsupported type: {catalog_item.type}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            cart = handler.validate_item_and_create_cart(request.user, catalog_item)
        except InvalidCartError as exc:
            return Response(
                {
                    'error': 'Given SKU item does not match add to cart requirements',
                    'details': f'{str(exc)}',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = CartSerializer(cart, context={'request': request})
        logger.info(f'Cart created for user {request.user} with SKU {sku_code}')
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class PaymentErrorView(ContextMixing):
    """Render the template that shows the error message to the user when the payment handling is failed."""

    template_name = 'zeitlabs_payments/payment_error.html'

    def get(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        """Handle the GET request."""
        context = self.get_context_data()
        context.update(
            {
                'merchant_reference': args[0],
            }
        )
        return render(request, self.template_name, context)


class PaymentDeclineView(ContextMixing):
    """Render the template that shows the error message to the user when the payment handling is failed."""

    template_name = 'zeitlabs_payments/payment_decline.html'

    def get(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        """Handle the GET request."""
        context = self.get_context_data()
        context.update({'merchant_reference': args[0], 'test': 'abcd hello'})
        return render(request, self.template_name, context)


class PaymentSuccessView(ContextMixing):
    """Render the template that shows the error message to the user when the payment handling is failed."""

    template_name = 'zeitlabs_payments/payment_successful.html'

    def get(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        """Handle the GET request."""
        context = self.get_context_data()
        context.update(
            {
                'merchant_reference': args[0],
            }
        )
        return render(request, self.template_name, context)


class InvoiceView(LoginRequiredMixin, ContextMixing):
    """Render Invoice with given invoice number."""

    template_name = 'zeitlabs_payments/invoice.html'

    def get(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        """Handle the GET request."""
        invoice_number = args[0]
        filters = {'invoice_number': invoice_number}
        if not request.user.is_superuser:
            filters['cart__user'] = request.user
        invoice = get_object_or_404(models.Invoice, **filters)

        payment_method = 'manual'
        if getattr(invoice, 'related_transaction', None):
            payment_method = invoice.related_transaction.gateway

        context = self.get_context_data()
        context.update(
            {
                'invoice': invoice,
                'payment_method': payment_method,
                'organization': get_settings().organization,
                'tax_number': get_settings().customer_number,
                'currency': get_currency(invoice.cart),
            }
        )
        return render(request, self.template_name, context)


class OrderHistoryView(LoginRequiredMixin, ContextMixing):
    """
    Display the authenticated user's order (payment) history.

    Lists all carts that contain at least one catalogue item,
    together with their related invoices. The queryset is built by
    :func:`zeitlabs_payments.querysets.get_orders_queryset` which
    handles prefetching of items and invoices for performance.
    """

    template_name = 'zeitlabs_payments/order_history.html'

    def get_context_data(self, **kwargs: Any) -> dict:
        """Return context with the current user's order records."""
        context = super().get_context_data(**kwargs)
        context['records'] = get_orders_queryset(
            filtered_users_qs=User.objects.filter(pk=self.request.user.pk),
            include_invoice=True,
        )
        return context
