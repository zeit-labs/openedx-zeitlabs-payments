"""Zeilabs payments views."""
import logging
import re
from typing import Any, Optional

from common.djangoapps.course_modes.models import CourseMode
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import TemplateView
from rest_framework import status
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from zeitlabs_payments import models
from zeitlabs_payments.cart_handler import CART_HANDLER
from zeitlabs_payments.exceptions import InvalidCartError
from zeitlabs_payments.helpers import get_currency
from zeitlabs_payments.providers.manual_payment import ManualPaymentProcessor
from zeitlabs_payments.providers.registry import PROCESSORS, get_processor
from zeitlabs_payments.serializers import CartSerializer

logger = logging.getLogger(__name__)
User = get_user_model()

ID_PART = r'[a-zA-Z0-9_-]+'
COURSE_ID_REGX = \
    fr'(?P<course_id>course-v1:(?P<org>{ID_PART})\+(?P<course>{ID_PART})\+(?P<run>{ID_PART}))'
COURSE_ID_REGX_EXACT = rf'^{COURSE_ID_REGX}$'


class CheckoutView(LoginRequiredMixin, TemplateView):
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
            methods = [
                processor.get_payment_method_metadata(cart)
                for processor in PROCESSORS.values()
            ]
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
            catalog_item = get_object_or_404(models.CatalogueItem, sku=sku_code)
            handler = CART_HANDLER.get(catalog_item.type)
            if not handler:
                return HttpResponse(f'Item with given SKU has unsupported type: {catalog_item.type}.', status=400)

            try:
                cart = handler.validate_item_and_create_cart(request.user, catalog_item)
            except InvalidCartError as exc:
                return HttpResponse(
                    f'Given SKU item does not match add to cart requirements: {str(exc)}',
                    status=400
                )
        else:
            cart = (
                models.Cart.objects.filter(user=request.user, status='pending')
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
            }
        )
        logger.info(f'Cart {cart.id} status updated to {models.Cart.Status.PROCESSING}')

        models.AuditLog.log(
            action=models.AuditLog.AuditActions.REDIRECT_TO_PAYMENT,
            cart=cart,
            gateway=processor.SLUG,
            context={}
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
        last_pending_cart = models.Cart.objects.filter(
            user=request.user, status=models.Cart.Status.PENDING
        ).order_by('-created_at').first()

        serializer = CartSerializer(last_pending_cart, context={'request': request})
        logger.debug(f'Retrieved last pending cart for user {request.user}: {last_pending_cart}')
        return Response(serializer.data, status=status.HTTP_200_OK)

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
                    'details': f'{str(exc)}'
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = CartSerializer(cart, context={'request': request})
        logger.info(f'Cart created for user {request.user} with SKU {sku_code}')
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class PaymentErrorView(TemplateView):
    """Render the template that shows the error message to the user when the payment handling is failed."""

    template_name = 'zeitlabs_payments/payment_error.html'

    def get(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        """Handle the GET request."""
        context = {
            'merchant_reference': args[0],
        }
        return render(request, self.template_name, context)


class PaymentSuccessView(TemplateView):
    """Render the template that shows the error message to the user when the payment handling is failed."""

    template_name = 'zeitlabs_payments/payment_successful.html'

    def get(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        """Handle the GET request."""
        context = {
            'merchant_reference': args[0],
        }
        return render(request, self.template_name, context)


class InvoiceView(TemplateView):
    """Render Invoice with given invoice number."""

    template_name = 'zeitlabs_payments/invoice.html'

    def get(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        """Handle the GET request."""
        invoice = models.Invoice.objects.get(invoice_number=args[0])
        payment_method = 'manual'
        if getattr(invoice, 'related_transaction', None):
            payment_method = invoice.related_transaction.gateway

        context = {
            'invoice': invoice,
            'payment_method': payment_method,
            'organization': settings.ORGANIZATION,
            'tax_number': settings.CUSTOMER_NUMBER,
            'currency': get_currency(invoice.cart)
        }
        return render(request, self.template_name, context)


class ManualPaymentView(APIView):
    """Manual Payment view."""

    permission_classes = [IsAdminUser]

    def _validate_required_fields(self, payload: dict) -> tuple:
        """
        Check if either 'user_id' or 'username' is present and all other required_fields exist.

        :Returns
        (True, None) if valid
        (False, 'field_name') if missing
        (False, 'user_id or username') if both missing
        """
        if not (payload.get('user_id') or payload.get('username')):
            return False, 'user_id or username'

        required_fields = ['course_key', 'mode']
        for field in required_fields:
            if not payload.get(field):
                return False, field

        return True, None

    def _get_user(self, payload: dict) -> Optional[get_user_model]:
        """
        Get user by user_id or username.

        :Returns
        User instance if found
        None if not found
        """
        try:
            if payload.get('user_id'):
                return User.objects.get(id=payload['user_id'])
            else:
                return User.objects.get(username=payload['username'])
        except User.DoesNotExist:
            return None

    def _get_course_item(self, mode: str, course_id: str) -> Optional[models.CatalogueItem]:
        """
        Get the CourseMode and related CatalogueItem by mode and course_id.

        :param mode: The mode string to search (e.g., 'verified', 'professional').
        :param course_id: The course ID (e.g., 'course-v1:TestX+Test100+2019_T1').

        :return: tuple
            - CatalogueItem instance if found, else None
            - None if successful, else error message string describing what failed
        """
        try:
            course_mode = CourseMode.objects.get(mode_slug=mode, course_id=course_id)
            catalogue_item = models.CatalogueItem.objects.get(sku=course_mode.sku)
            return catalogue_item
        except (CourseMode.DoesNotExist, models.CatalogueItem.DoesNotExist):
            return None

    def post(self, request: Any) -> Response:
        """
        Create order and invoice for manual payment.
        Expected payload example:
            {
                "user_id": 111,              # Optional if "username" is provided
                "username": "me",            # Optional if "user_id" is provided
                "course_run_key": "course-v1:TestX+Test100+2019_T1",   # Required
                "mode": "verified"           # Required, e.g., 'verified' or 'professional'
            }
        Notes:
            - Either "user_id" or "username" must be present.
            - "course_run_key" and "mode" must always be present and not empty.

        :param request: HTTP request with above described payload
        :return: create invoice and cart number response
        """
        is_valid, missing = self._validate_required_fields(request.data)
        if not is_valid:
            return Response(
                {'error': f'Missing required param: {missing}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        user = self._get_user(request.data)
        if not user:
            return Response(
                {'error': 'Unable to retrieve user with given user info.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not re.search(COURSE_ID_REGX_EXACT, request.data['course_key']):
            return Response(
                {'error': f"Invalid course id provided: {request.data['course_key']}."},
                status=status.HTTP_400_BAD_REQUEST
            )

        course_catalog_item = self._get_course_item(request.data['mode'], request.data['course_key'])
        if not course_catalog_item:
            return Response(
                {
                    'error': (
                        f'Unable to retrieve course mode or catalogue item for course_id ='
                        f" '{request.data['course_key']}' and mode='{request.data['mode']}'."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        handler = CART_HANDLER.get(course_catalog_item.type)
        if not handler:
            return Response(
                {'error': (
                    f'Catalog Item with given course_id and mode has unsupported'
                    f' type: {course_catalog_item.type}.'
                )},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            handler.validate_add_to_cart(user, course_catalog_item)
        except InvalidCartError as exc:
            return Response(
                {
                    'error': 'Given course does not match add to cart requirements',
                    'details': f'{str(exc)}'
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        processor = ManualPaymentProcessor()
        try:
            result = processor.process_payment(user, course_catalog_item, request)
            return Response(result, status=status.HTTP_201_CREATED)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(f'Failed to process manual payment: {str(e)}')
            return Response(
                {
                    'error': 'Failed to process manual payment',
                    'details': str(e)
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
