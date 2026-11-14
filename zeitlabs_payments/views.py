"""Zeilabs payments views."""
import logging
from decimal import Decimal
from functools import lru_cache
from typing import Any, NamedTuple, Optional
from urllib.parse import urlencode, urljoin, urlsplit

from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.sites.shortcuts import get_current_site
from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction
from django.db.models import Prefetch
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.module_loading import import_string
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import ListView, TemplateView
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from zeitlabs_payments import models
from zeitlabs_payments.cart_handler import CART_HANDLER
from zeitlabs_payments.exceptions import CartStatusError, InvalidCartError
from zeitlabs_payments.inflight_payment import (
    InFlightPayment,
    Resolution,
    create_cart_resolving_inflight_payments,
    waiting_time,
)
from zeitlabs_payments.invoice_info import get_invoice_info_context
from zeitlabs_payments.providers.registry import PROCESSORS, available_processors, get_processor
from zeitlabs_payments.serializers import CartSerializer, OrderHistorySerializer
from zeitlabs_payments.validators import validate_payment_origin

logger = logging.getLogger(__name__)
User = get_user_model()


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
                for processor in available_processors(self.request)
            ]
            cart = CartSerializer(cart, context={'request': self.request}).data

        context.update(
            {
                'cart': cart,
                'methods': methods,
            }
        )
        return context

    def inflight_payment_response(self, request: Any, inflight_payment: InFlightPayment) -> HttpResponse:
        """
        Answer a learner buying a course whose earlier payment is still open, as the payment was resolved.

        They go back to that payment's waiting page when it can be resumed, are told to wait while it may still go
        through, and are refused when nothing can be concluded about it -- never offered a second payment for it.
        """
        if inflight_payment.resolution == Resolution.RESUME:
            return redirect(inflight_payment.waiting_page_url)
        bundle = sells_a_bundle(inflight_payment.cart)
        if inflight_payment.resolution == Resolution.IN_PROGRESS:
            return render(
                request,
                'zeitlabs_payments/payment_in_progress.html',
                {
                    'merchant_reference': inflight_payment.cart.gateway_order_id,
                    'bundle': bundle,
                    **waiting_time(inflight_payment.retry_after),
                },
            )
        if bundle:
            message = _(
                'A payment for this bundle is still awaiting confirmation. Please contact support before paying again.'
            )
        else:
            message = _(
                'A payment for this course is still awaiting confirmation. Please contact support before paying again.'
            )
        return render(request, 'zeitlabs_payments/invalid_cart.html', {'error_message': message}, status=400)

    def get(self, request: Any, *args: Any, **kwargs: Any) -> None:
        """
        Checkout View. If SKU provided create a new cart otherwise render the last pending cart of the user.

        Only one SKU is accepted per checkout URL. A request that names more than one is
        refused and renders the invalid-cart page so the learner knows this is the design, not an error.
        """
        sku_codes = request.GET.getlist('sku')
        if len(sku_codes) > 1:
            logger.error(
                'Multi-SKU checkout URL refused: SKUs=%s user=%s',
                sku_codes,
                request.user,
            )
            return render(
                request,
                'zeitlabs_payments/invalid_cart.html',
                {
                    'error_message': _(
                        'This product sells one item per purchase; please complete each purchase separately.'
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        sku_code = sku_codes[0] if sku_codes else ''
        if sku_code:
            try:
                catalog_item = models.CatalogueItem.objects.get(sku=sku_code)
            except models.CatalogueItem.DoesNotExist:
                logger.warning('Checkout refused: no catalogue item with sku %s.', sku_code)
                return render(
                    request,
                    'zeitlabs_payments/invalid_cart.html',
                    {'error_message': _('This item is not available.')},
                    status=404
                )

            handler = CART_HANDLER.get(catalog_item.type)
            if not handler:
                logger.warning(
                    'Checkout refused: catalogue item %s has unsupported type %s.', sku_code, catalog_item.type,
                )
                return render(
                    request,
                    'zeitlabs_payments/invalid_cart.html',
                    {'error_message': _('This item cannot be bought right now.')},
                    status=400
                )
            try:
                cart, inflight_payment = create_cart_resolving_inflight_payments(handler, request.user, catalog_item)
            except InvalidCartError as exc:
                return render(
                    request,
                    'zeitlabs_payments/invalid_cart.html',
                    {'error_message': str(exc)},
                    status=400
                )
            if inflight_payment is not None:
                return self.inflight_payment_response(request, inflight_payment)
        else:
            cart = (
                models.Cart.objects.filter(user=request.user, status=models.Cart.Status.PENDING)
                .order_by('-created_at')
                .first()
            )

        context = self.get_context_data(cart=cart)
        return self.render_to_response(context)


@lru_cache(maxsize=None)
def _view_function(dotted_path: str) -> Any:
    """Return the view function of the view class at the dotted path, made once."""
    return import_string(dotted_path).as_view()


def _processor_view(slug: str, attribute: str) -> Any:
    """
    Return the view function of the processor's view the attribute names, or raise Http404.

    :param slug: The processor's SLUG, from the path.
    :param attribute: ``RETURN_VIEW`` or ``STATUS_VIEW``.
    :raises Http404: When no processor has the slug, or it names no such view.
    """
    dotted_path = getattr(PROCESSORS.get(slug), attribute, None)
    if not dotted_path:
        raise Http404(f'No {attribute.lower().replace("_", " ")} for payment provider {slug!r}.')
    return _view_function(dotted_path)


# The two routes every processor's views are mounted on. The exemptions are made here because Django reads them from
# the view the URL resolves to, not from the processor's view called inside it: a gateway posting the learner back
# cannot carry this site's CSRF token, and both views write records that must be committed whatever follows.
@csrf_exempt
@db_transaction.non_atomic_requests
def processor_return_view(request: Any, slug: str) -> HttpResponse:
    """Serve the processor's return view, where its gateway sends the learner back to wait for the payment."""
    return _processor_view(slug, 'RETURN_VIEW')(request)


@db_transaction.non_atomic_requests
def processor_status_view(request: Any, slug: str) -> HttpResponse:
    """Serve the processor's status view, which the waiting page polls."""
    return _processor_view(slug, 'STATUS_VIEW')(request)


class InitiatePaymentView(LoginRequiredMixin, View):
    """
    View that initiates the payment process for a given provider.

    Only accessible by authenticated users.
    """

    template_name = 'zeitlabs_payments/payment_refused.html'

    def refuse(self, request: Any, message: str, cart: Optional[models.Cart] = None) -> HttpResponse:
        """
        Answer a refused hand-off with a page saying why in one plain sentence, and where the learner goes next.

        The sentence names no cart, user, provider or status; the caller logs what actually went wrong. A paid cart
        leads to its invoice, any other cart of the learner's own back to checkout for its item, and a refusal naming
        no cart of theirs to the courses.

        :param request: Django request object
        :param message: The translated sentence the learner reads.
        :param cart: The learner's own cart the refusal is about, or None when the address names no cart of theirs.
        """
        invoice_url = checkout_url = None
        if cart is not None and cart.status == models.Cart.Status.PAID:
            invoice = cart.invoices.filter(status=models.Invoice.InvoiceStatus.PAID).first()
            if invoice is not None:
                invoice_url = reverse('zeitlabs_payments:invoice', args=[invoice.invoice_number])
        elif cart is not None:
            checkout_url = retry_url_for(cart)
        return render(
            request,
            self.template_name,
            {'error_message': message, 'invoice_url': invoice_url, 'checkout_url': checkout_url},
            status=404 if cart is None else 400,
        )

    def get_learner_cart(self, request: Any, cart_id: str) -> Optional[models.Cart]:
        """
        Return the requesting learner's cart with the id, or None when the id is malformed, missing or not theirs.

        The three are answered alike, so walking the sequential cart ids tells a learner nothing about whose each one
        is. The log tells them apart.

        :param request: Django request object
        :param cart_id: The cart id, as the address gives it.
        """
        try:
            cart = models.Cart.objects.filter(id=int(cart_id)).first()
        except ValueError:
            logger.warning(f'User {request.user.id} asked to pay for a malformed cart id {cart_id!r}.')
            return None
        if cart is None:
            logger.warning(f'User {request.user.id} asked to pay for cart {cart_id}, which does not exist.')
            return None
        if cart.user_id != request.user.id:
            logger.warning(f'User {request.user.id} asked to pay for cart {cart.id}, which is user {cart.user_id}\'s.')
            return None
        return cart

    def get(self, request: Any, provider: str, cart_id: str) -> Any:
        """
        Initiate the payment by calling the appropriate processor.

        Every refusal answers with a page, see ``refuse``. A cart id that is malformed, names no cart, or names another
        learner's cart is refused first, all three alike.

        A processor hidden for the request is refused as an unknown one is, before the cart is touched: hiding is how a
        processor limits who may pay with it, and typing its address must not get round that. One that raises while
        deciding counts as hidden, as in ``registry.available_processors``, so a broken check fails closed instead of
        offering the processor to everyone.

        Only a pending cart is handed off. It is moved to processing before the gateway is called, so of two requests
        for the same cart -- a double click, or this URL replayed from the browser history -- only the first reaches the
        gateway, and a cart already paid is never offered for payment again. A hand-off that raises closes the cart
        instead of leaving it processing, since the learner was never sent anywhere; they start a new cart to pay.

        The move records the origin the learner pays on, which the cart's invoice is issued on. An unusable one --
        a site whose domain is misconfigured -- refuses the hand-off, so the learner is not charged for a payment no
        invoice could be issued for.

        :param request: Django request object
        :param provider: The payment provider slug
        :param cart_id: UUID of the cart
        :return: Rendered payment page or error response
        """
        cart = self.get_learner_cart(request, cart_id)
        if cart is None:
            return self.refuse(request, _('This payment cannot be found.'))

        try:
            processor = get_processor(provider)
            logger.debug(f'Processor found for provider: {provider}')
        except ValueError as exc:
            logger.error(f'Invalid payment provider: {provider} - {exc}')
            return self.refuse(request, _('This payment method is not available.'), cart)

        try:
            hidden = processor.is_hidden_for(request)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception(f'Failed to decide whether payment provider {provider} is hidden; treating it as hidden.')
            hidden = True
        if hidden:
            logger.error(f'Payment provider {provider} is not offered to user {request.user}; refusing it.')
            return self.refuse(request, _('This payment method is not available.'), cart)

        payment_origin = f'{request.scheme}://{request.site.domain}'
        try:
            validate_payment_origin(payment_origin)
        except ValidationError as exc:
            logger.error(f'Cart {cart.id} not handed off to {provider}: {exc.message}')
            return self.refuse(request, _('Payments cannot be made on this site right now.'), cart)

        try:
            cart.change_status(
                models.Cart.Status.PROCESSING,
                processor=processor,
                fields={'gateway': processor.SLUG, 'payment_origin': payment_origin},
            )
        except CartStatusError as exc:
            logger.warning(f'Cart {cart.id} not handed off to {provider}: {exc}')
            if cart.status == models.Cart.Status.PAID:
                return self.refuse(request, _('This purchase is already paid.'), cart)
            return self.refuse(request, _('This payment link is no longer valid.'), cart)
        logger.info(f'Cart {cart.id} status updated to {models.Cart.Status.PROCESSING}')

        try:
            payment_view = processor.payment_view(
                cart=cart,
                request=request,
                use_client_side_checkout=False,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception(f'Cart {cart.id} could not be handed off to {provider}.')
            try:
                cart.change_status(
                    models.Cart.Status.CLOSED,
                    processor=processor,
                    action=models.AuditLog.AuditActions.PAYMENT_HANDOFF_FAILED,
                    context={'error': f'{type(exc).__name__}: {exc}'},
                )
            except CartStatusError as close_exc:
                # Another request moved the cart while the gateway was being called. Its status is that request's to
                # keep; the learner was still never sent to the gateway, so they get the error page all the same.
                logger.warning(f'Cart {cart.id} was not closed after its failed hand-off to {provider}: {close_exc}')
            return render(request, 'zeitlabs_payments/payment_error.html', {'retry_url': retry_url_for(cart)})

        models.AuditLog.log(
            action=models.AuditLog.AuditActions.REDIRECT_TO_PAYMENT,
            cart=cart,
            processor=processor,
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

        Only one SKU is accepted per request. A request that names more than one is refused in
        the existing JSON error shape so the API surface stays unchanged.

        :param request: HTTP request with SKU in data
        :return: Serialized cart data with HTTP 201 status or error response
        """
        if hasattr(request.data, 'getlist'):
            sku_codes = request.data.getlist('sku')
        else:
            sku_value = request.data.get('sku')
            if isinstance(sku_value, list):
                sku_codes = sku_value
            elif sku_value:
                sku_codes = [sku_value]
            else:
                sku_codes = []

        if len(sku_codes) > 1:
            logger.error(
                'Multi-SKU cart-add request refused: SKUs=%s user=%s',
                sku_codes,
                request.user,
            )
            return Response(
                {
                    'error': (
                        'This product sells one item per purchase; please complete each purchase separately.'
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        sku_code = sku_codes[0] if sku_codes else ''

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
            cart, inflight_payment = create_cart_resolving_inflight_payments(handler, request.user, catalog_item)
        except InvalidCartError as exc:
            return Response(
                {
                    'error': 'Given SKU item does not match add to cart requirements',
                    'details': exc.detail or str(exc),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if inflight_payment is not None:
            return inflight_payment_json_response(inflight_payment)
        serializer = CartSerializer(cart, context={'request': request})
        logger.info(f'Cart created for user {request.user} with SKU {sku_code}')
        return Response(serializer.data, status=status.HTTP_201_CREATED)


def inflight_payment_json_response(inflight_payment: InFlightPayment) -> Response:
    """
    Answer an API request to buy a course whose earlier payment is still open, as the payment was resolved.

    A payment that can be resumed or may still go through is answered 409, naming it: with the waiting page to send
    the learner to, or the time after which buying again is allowed. One nothing can be concluded about is refused in
    the error shape every other refusal uses.
    """
    if inflight_payment.resolution == Resolution.RESUME:
        return Response(
            {'outcome': 'payment_in_progress', 'redirect_url': inflight_payment.waiting_page_url},
            status=status.HTTP_409_CONFLICT,
        )
    if inflight_payment.resolution == Resolution.IN_PROGRESS:
        retry_after = inflight_payment.retry_after.isoformat() if inflight_payment.retry_after else None
        return Response(
            {'outcome': 'payment_in_progress', 'retry_after': retry_after},
            status=status.HTTP_409_CONFLICT,
        )
    item_type = 'bundle' if sells_a_bundle(inflight_payment.cart) else 'course'
    return Response(
        {
            'error': 'Given SKU item does not match add to cart requirements',
            'details': f'A payment for this {item_type} is still awaiting confirmation.',
        },
        status=status.HTTP_400_BAD_REQUEST,
    )


class PaymentErrorView(TemplateView):
    """Render the template that shows the error message to the user when the payment handling is failed."""

    template_name = 'zeitlabs_payments/payment_error.html'

    def get(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        """Handle the GET request."""
        context = {
            'merchant_reference': args[0],
        }
        return render(request, self.template_name, context)


def sells_a_bundle(cart: Optional[models.Cart]) -> bool:
    """Return whether the cart sells a bundle, so what is said about it names a bundle rather than a course."""
    return cart is not None and cart.items.filter(
        catalogue_item__type=models.CatalogueItem.ItemType.PROGRAM_BUNDLE,
    ).exists()


def retry_url_for(cart: Optional[models.Cart]) -> Optional[str]:
    """
    Return the checkout address that starts a new cart for the cart's item, or None when there is none to retry.

    A retry is a new attempt, never the old cart reused: the old cart ended, and its gateway order with it.
    """
    item = cart.items.select_related('catalogue_item').first() if cart else None
    if item is None:
        return None
    return f"{reverse('zeitlabs_payments:checkout')}?{urlencode({'sku': item.catalogue_item.sku})}"


class PaymentDeclineView(TemplateView):
    """
    Render the page a declined or cancelled payment ends on, offering a new attempt for the same item.

    The ``cancelled`` query parameter picks the wording for a payment the learner cancelled rather than one refused
    for them. The retry is offered only for a cart of the learner's own that the reference names, so a reference
    seen elsewhere offers nothing.
    """

    template_name = 'zeitlabs_payments/payment_decline.html'

    def get(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        """Handle the GET request."""
        gateway_order_id = args[0]
        cart = None
        if request.user.is_authenticated:
            cart = (
                models.Cart.objects.filter(user=request.user, gateway_order_id=gateway_order_id)
                .order_by('-created_at')
                .first()
            )
        context = {
            'merchant_reference': gateway_order_id,
            'cancelled': bool(request.GET.get('cancelled')),
            'retry_url': retry_url_for(cart),
        }
        return render(request, self.template_name, context)


class PaymentReconciliationView(TemplateView):
    """
    Render the page shown when a payment was not completed and must not be retried.

    Two cases reach it. Money has moved but the enrolment could not be completed, or the payment has not been confirmed
    yet and may still go through. Either way a retry could charge the learner twice, so the page offers none. The
    ``unconfirmed`` query parameter picks the second wording; it only changes what the page says. The merchant reference
    is required by the route because it is the only thing the learner can quote to support.

    The page names a bundle's courses rather than a course only for a bundle cart of the learner's own that the
    reference names, so a reference seen elsewhere tells nothing about what someone else bought.
    """

    template_name = 'zeitlabs_payments/payment_reconciliation.html'

    def get(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        """Handle the GET request."""
        gateway_order_id = args[0]
        cart = None
        if request.user.is_authenticated:
            cart = models.Cart.objects.filter(user=request.user, gateway_order_id=gateway_order_id).first()
        context = {
            'merchant_reference': gateway_order_id,
            'unconfirmed': bool(request.GET.get('unconfirmed')),
            'bundle': sells_a_bundle(cart),
        }
        return render(request, self.template_name, context)


def build_invoice_context(invoice: models.Invoice, request: Any) -> dict:
    """
    Build the context an invoice renders from, shared by the private and the public view.

    :param invoice: The invoice being rendered.
    :param request: The request rendering it.
    :returns: The template context.
    """
    return {
        'invoice': invoice,
        'payment_method': invoice.related_transaction.gateway,
        'currency': invoice.currency,
        **get_invoice_info_context(invoice, get_current_site(request)),
    }


def _host_key(netloc: str) -> tuple[Optional[str], Optional[int]]:
    """
    Return the host and any port other than a default one, which is what tells two hosts apart however they are spelled.

    A host is the same in capitals or not, and with or without the default port written out.

    :param netloc: A host, with or without a port: an origin's netloc, or a request's host.
    """
    parts = urlsplit(f'//{netloc}')
    return parts.hostname, parts.port if parts.port not in (80, 443) else None


def redirect_to_issuing_site(invoice: models.Invoice, request: Any) -> Optional[HttpResponse]:
    """
    Return a redirect to the same address on the site the invoice was issued on, or None when the request is there.

    What an invoice shows about the organization is read for the site serving the page, and which site that is, is up
    to whoever types the address. Serving an invoice only on the site its cart was paid on keeps the two the same. An
    invoice whose cart was paid before origins were recorded has no site to send anyone to, and is served wherever it
    is asked for.

    The origin and the request are compared by ``_host_key``: a site whose domain is spelled with capitals, or with a
    default port, would otherwise redirect to itself on every request, since the browser asks for the host without
    either.

    :param invoice: The invoice being shown, already known to be one the caller may see.
    :param request: The request showing it.
    """
    payment_origin = invoice.cart.payment_origin
    if not payment_origin or _host_key(urlsplit(payment_origin).netloc) == _host_key(request.get_host()):
        return None
    return redirect(urljoin(payment_origin, request.get_full_path()))


class InvoiceView(TemplateView):
    """
    Render an invoice to its owner, addressed by invoice number.

    This address is printed inside QR codes on copies already in learners' hands, so it keeps resolving for
    the owner indefinitely. Everyone else -- including whoever scans one of those older codes -- gets the page
    explaining that the address is private. Answering anonymous callers rather than redirecting them to a
    login form leaves a scanned QR on that explanation instead of on a sign-in page they have no account for.

    An invoice the caller may not see and an invoice that does not exist answer identically. Telling the two
    apart would confirm which numbers are live, and the series runs sequentially.
    """

    template_name = 'zeitlabs_payments/invoice.html'
    private_template_name = 'zeitlabs_payments/invoice_private.html'

    def get(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        """Handle the GET request."""
        invoice = None
        if request.user.is_authenticated:
            filters = {'invoice_number': args[0]}
            if not request.user.is_superuser:
                filters['cart__user'] = request.user
            invoice = models.Invoice.objects.filter(**filters).first()

        if invoice is None:
            response = render(request, self.private_template_name, status=410)
            response['Cache-Control'] = 'no-store'
            return response

        # Only once the caller may see the invoice: a redirect names the site it was issued on, which would tell a
        # caller who may not that the number is live.
        site_redirect = redirect_to_issuing_site(invoice, request)
        if site_redirect:
            return site_redirect
        return render(request, self.template_name, build_invoice_context(invoice, request))


class PublicInvoiceView(TemplateView):
    """
    Render an invoice to whoever holds its token.

    The token is unguessable, so holding it is the whole permission: someone handed a printed copy can check
    it against this page without an account, which is what the QR code on it is for. Admitting that an unknown
    token has no invoice gives nothing away, so a missing one is an ordinary 404 here.
    """

    template_name = 'zeitlabs_payments/invoice.html'

    def get(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        """Handle the GET request."""
        invoice = get_object_or_404(models.Invoice, public_token=kwargs['token'])
        site_redirect = redirect_to_issuing_site(invoice, request)
        if site_redirect:
            return site_redirect

        response = render(request, self.template_name, build_invoice_context(invoice, request))
        # The token addresses the invoice, so it must not ride a Referer header out to whatever this page links to.
        response['Referrer-Policy'] = 'no-referrer'
        return response


class OrderRow(NamedTuple):
    """One order of a learner's payment history, as the history page and the history API show it."""

    cart: models.Cart
    # The course or bundle the order bought; None for a cart with no item, which no checkout makes.
    item: Optional[models.CatalogueItem]
    # The invoice's total once invoiced, otherwise what the learner was asked to pay.
    amount: Decimal
    currency: str
    # 'paid', 'confirming', 'refund_requested' or 'refunded'.
    state: str
    # The paid invoice to link to, or None while the order has none.
    invoice_number: Optional[str]


# The statuses of an order that may have charged its learner: one handed off, whatever came of it. A cart never handed
# off, or ended without a charge, is left out of the history -- a declined payment had its own page.
HISTORY_STATUSES = (
    models.Cart.Status.PROCESSING,
    models.Cart.Status.PAYMENT_PENDING,
    models.Cart.Status.PAID,
    models.Cart.Status.UNRECONCILED,
    models.Cart.Status.REFUND_REQUESTED,
    models.Cart.Status.REFUNDED,
)
HISTORY_PAGE_SIZE = 20
REFUND_STATES = {
    models.Cart.Status.REFUND_REQUESTED: 'refund_requested',
    models.Cart.Status.REFUNDED: 'refunded',
}


def get_order_history(user: Any) -> Any:
    """
    Return the learner's orders that may have charged them, newest first, with their items and paid invoices loaded.

    Read in a fixed number of queries however long the history, so neither the page nor the API slows down with it.
    """
    return models.Cart.objects.filter(user=user, status__in=HISTORY_STATUSES).order_by(
        '-created_at', '-id',
    ).prefetch_related(
        Prefetch('items', queryset=models.CartItem.objects.select_related('catalogue_item')),
        Prefetch(
            'invoices',
            queryset=models.Invoice.objects.filter(status=models.Invoice.InvoiceStatus.PAID).order_by('id'),
            to_attr='paid_invoices',
        ),
    )


def describe_order(cart: models.Cart) -> OrderRow:
    """
    Describe an order of ``get_order_history`` as its history row.

    Paid with its invoice; still being confirmed -- awaiting the gateway, unreconciled, or paid with no invoice yet --
    with the reference the learner quotes to support; or refunded.
    """
    items = list(cart.items.all())
    item = items[0].catalogue_item if items else None
    invoice = cart.paid_invoices[0] if cart.paid_invoices else None
    if invoice is not None:
        amount, currency = invoice.total, invoice.currency
    else:
        amount, currency = cart.total, (item.currency if item else None) or ''
    if cart.status in REFUND_STATES:
        state = REFUND_STATES[cart.status]
    elif cart.status == models.Cart.Status.PAID and invoice is not None:
        state = 'paid'
    else:
        state = 'confirming'
    return OrderRow(
        cart=cart,
        item=item,
        amount=amount,
        currency=currency,
        state=state,
        invoice_number=invoice.invoice_number if invoice else None,
    )


class OrderHistoryView(LoginRequiredMixin, ListView):
    """
    Show the learner their own payment history, a page at a time; see ``get_order_history`` and ``describe_order``.
    """

    template_name = 'zeitlabs_payments/order_history.html'
    context_object_name = 'orders'
    paginate_by = HISTORY_PAGE_SIZE

    def get_queryset(self) -> Any:
        """Return the learner's own history."""
        return get_order_history(self.request.user)

    def get_context_data(self, **kwargs: Any) -> dict:
        """Return the page's context, each order on it described as a row."""
        context = super().get_context_data(**kwargs)
        context['orders'] = [describe_order(cart) for cart in context['orders']]
        return context


class IsStaffOrSuperuser(BasePermission):
    """Allow a logged-in staff member or superuser only."""

    def has_permission(self, request: Any, view: Any) -> bool:
        """Return whether the requesting user is staff or a superuser."""
        user = request.user
        return bool(user and user.is_authenticated and (user.is_staff or user.is_superuser))


class OrderHistoryPagination(PageNumberPagination):
    """Pages of the history API, as many orders to a page as the learner's own page shows."""

    page_size = HISTORY_PAGE_SIZE


class OrderHistoryApiView(ListAPIView):
    """
    Read any learner's payment history, for staff and superusers: the orders their own page lists, as JSON.

    The learner is named by user id in the address; an id naming nobody is answered 404, rather than as an empty
    history. A learner reads their own history on their page, not here.
    """

    permission_classes = [IsStaffOrSuperuser]
    serializer_class = OrderHistorySerializer
    pagination_class = OrderHistoryPagination

    def get_queryset(self) -> Any:
        """Return the named learner's history."""
        return get_order_history(get_object_or_404(User, pk=self.kwargs['user_id']))

    def list(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        """Answer one page of the history, each order on it described as a row."""
        page = self.paginate_queryset(self.get_queryset())
        rows = [describe_order(cart) for cart in page]
        return self.get_paginated_response(self.get_serializer(rows, many=True).data)
