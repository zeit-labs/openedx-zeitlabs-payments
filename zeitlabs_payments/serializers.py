"""zeitlabs payments serializers."""

import logging
from typing import Any, List, Optional

from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
from rest_framework import serializers

from zeitlabs_payments.helpers import get_currency, relative_url_to_absolute_url
from zeitlabs_payments.models import BundleCourseItem, Cart, CartItem, Invoice

logger = logging.getLogger(__name__)


class InvoiceSerializer(serializers.ModelSerializer):
    """Invoice serializer."""

    class Meta:
        model = Invoice
        fields = ['invoice_number', 'currency', 'paid_at']


class CourseSerializer(serializers.ModelSerializer):
    """Course serializer."""

    course_name = serializers.SerializerMethodField()
    course_id = serializers.SerializerMethodField()
    course_image = serializers.SerializerMethodField()
    org = serializers.SerializerMethodField()
    run = serializers.SerializerMethodField()

    class Meta:
        model = CourseOverview
        fields = ['course_id', 'course_name', 'course_image', 'org', 'run']

    def get_course_name(self, obj: CourseOverview) -> str:
        """
        Return the display name of the course.

        :param obj: CourseOverview instance
        :return: Course display name
        """
        return obj.display_name

    def get_course_id(self, obj: CourseOverview) -> str:
        """
        Return the ID of the course as string.

        :param obj: CourseOverview instance
        :return: Course ID as string
        """
        return str(obj.id)

    def get_org(self, obj: CourseOverview) -> str:
        """
        Return the org of the course.

        :param obj: CourseOverview instance
        :return: Course org as string
        """
        return str(obj.id.org)

    def get_run(self, obj: CourseOverview) -> str:
        """
        Return the run of the course.

        :param obj: CourseOverview instance
        :return: course run as string
        """
        return str(obj.id.run)

    def get_course_image(self, obj: CourseOverview) -> Optional[str]:
        """
        Return the absolute URL of the course image.

        :param obj: CourseOverview instance
        :return: Absolute URL or None
        """
        request = self.context.get('request')
        try:
            return relative_url_to_absolute_url(obj.course_image_url, request)
        except (AttributeError, TypeError, ValueError) as exc:
            logger.error(f'Failed to get course image URL: {exc}')
            return None


class CartItemSerializer(serializers.ModelSerializer):
    """Cart Item serializer."""

    sku = serializers.SerializerMethodField()
    type = serializers.SerializerMethodField()
    currency = serializers.SerializerMethodField()
    title = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    details = serializers.SerializerMethodField()

    class Meta:
        model = CartItem
        fields = [
            'sku',
            'title',
            'description',
            'type',
            'currency',
            'original_price',
            'discount_amount',
            'final_price',
            'coupon',
            'details',
        ]

    def get_sku(self, obj: CartItem) -> str:
        """
        Return the SKU of the catalogue item.

        :param obj: CartItem instance
        :return: SKU string
        """
        return obj.catalogue_item.sku

    def get_type(self, obj: CartItem) -> str:
        """
        Return the type of the catalogue item.

        :param obj: CartItem instance
        :return: Type string
        """
        return obj.catalogue_item.type

    def get_currency(self, obj: CartItem) -> Optional[str]:
        """
        Return the currency of the catalogue item.

        :param obj: CartItem instance
        :return: Currency string or None
        """
        return obj.catalogue_item.currency

    def get_title(self, obj: CartItem) -> Optional[str]:
        """
        Return the title of the catalogue item.

        :param obj: CartItem instance
        :return: Title string or None
        """
        return obj.catalogue_item.title

    def get_description(self, obj: CartItem) -> Optional[str]:
        """
        Return the description of the catalogue item.

        :param obj: CartItem instance
        :return: Description string or None
        """
        return obj.catalogue_item.description

    def get_details(self, obj: CartItem) -> Any:
        """
        Return item-specific details based on its type.
        """
        item_type = obj.catalogue_item.type
        handler_method = getattr(self, f'get_{item_type}_details', None)

        if callable(handler_method):
            return handler_method(obj)  # pylint: disable=not-callable

        logger.warning(
            f'No handler implemented for item type \'{item_type}\'. Returning empty details.'
        )
        return {}

    def get_paid_course_details(self, obj: CartItem) -> dict:
        """Return details for a single paid course item."""
        item_ref_id = obj.catalogue_item.item_ref_id
        courses_map = self.context.get('prefetched_courses', {})

        course = (
            courses_map.get(str(item_ref_id))
            if courses_map
            else CourseOverview.objects.filter(id=item_ref_id).first()
        )
        if not course:
            logger.warning(f'CourseOverview not found for id {item_ref_id}')
            return {'courses': []}

        return {
            'courses': CourseSerializer([course], many=True, context=self.context).data
        }

    def get_program_bundle_details(self, obj: CartItem) -> dict:
        """Return details for a program bundle item, listing all constituent courses."""
        courses_map = self.context.get('prefetched_courses', {})
        bundle_links = BundleCourseItem.objects.filter(bundle=obj.catalogue_item).select_related('course_item')

        courses: List[CourseOverview] = []

        if courses_map:
            for link in bundle_links:
                ref_id = link.course_item.item_ref_id
                course = courses_map.get(str(ref_id))
                if course:
                    courses.append(course)
                else:
                    logger.warning(f'CourseOverview not found for bundle course ref_id {ref_id}')
        else:
            # Bulk fetch all CourseOverview records in a single query to avoid N+1.
            ref_ids = [link.course_item.item_ref_id for link in bundle_links]
            if ref_ids:
                course_overviews = CourseOverview.objects.filter(id__in=ref_ids)
                courses_by_id = {str(c.id): c for c in course_overviews}

                for link in bundle_links:
                    ref_id = link.course_item.item_ref_id
                    course = courses_by_id.get(str(ref_id))
                    if course:
                        courses.append(course)
                    else:
                        logger.warning(f'CourseOverview not found for bundle course ref_id {ref_id}')

        return {'courses': CourseSerializer(courses, many=True, context=self.context).data}


class CartSerializer(serializers.ModelSerializer):
    """Cart serializer."""

    items = serializers.SerializerMethodField()
    currency = serializers.SerializerMethodField()
    invoice = serializers.SerializerMethodField()
    user = serializers.SerializerMethodField()

    class Meta:
        model = Cart
        fields = [
            'id',
            'user',
            'status',
            'created_at',
            'items',
            'total',
            'currency',
            'invoice',
        ]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize serializer and remove invoice from fields if not required."""
        super().__init__(*args, **kwargs)
        if not self.context.get('include_invoice'):
            self.fields.pop('invoice', None)

    def get_items(self, obj: Cart) -> List[Any]:
        """
        Return serialized cart items.

        :param obj: Cart instance
        :return: List of serialized cart item data
        """
        serializer = CartItemSerializer(obj.items.all(), many=True, context=self.context)
        return serializer.data

    def get_currency(self, obj: Cart) -> str:
        """
        Return currency.

        :param obj: Cart instance
        :return: currency str
        """
        return get_currency(obj)

    def get_invoice(self, obj: Cart) -> dict | None:
        """
        Return serialized invoice. Invoice is only return if include_invoice is send in context.

        :param obj: Cart instance
        :return: Serialized invoice data
        """
        if not self.context.get('include_invoice', False) or obj.status != Cart.Status.PAID:
            return None

        invoice = obj.invoices.filter(status=Invoice.InvoiceStatus.PAID).first()
        return InvoiceSerializer(invoice).data if invoice else None

    def get_user(self, obj: Cart) -> dict | None:
        """
        Return user details or just the user ID based on include_user flag.
        """
        if not self.context.get('include_user_details', False):
            return obj.user_id

        user = obj.user
        return {
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'full_name': user.get_full_name(),
        }


class CoursePriceSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """
    Serializer for course pricing information.

    Returns course details and available pricing modes for anonymous users.
    This is a read-only serializer, so create() and update() are not implemented.
    """

    course = serializers.SerializerMethodField()
    pricing_modes = serializers.SerializerMethodField()

    def get_course(self, obj: CourseOverview) -> dict:
        """
        Return serialized course information.

        :param obj: CourseOverview instance
        :return: Course data dictionary
        """
        return CourseSerializer(obj, context=self.context).data

    def get_pricing_modes(self, obj: CourseOverview) -> List[dict]:  # pylint: disable=unused-argument
        """
        Return list of available pricing modes for the course.

        Combines CatalogueItem (localized price/currency) with CourseMode (mode details).

        :param obj: CourseOverview instance
        :return: List of pricing mode dictionaries with localized pricing
        """
        pricing_data = self.context.get('pricing_data', [])
        result = []

        for item_data in pricing_data:
            catalogue_item = item_data['catalogue_item']
            course_mode = item_data['course_mode']

            result.append(
                {
                    'mode_slug': course_mode.mode_slug,
                    'mode_display_name': course_mode.mode_display_name,
                    'price': catalogue_item.price,  # Localized price from CatalogueItem
                    'currency': catalogue_item.currency,  # Localized currency from CatalogueItem
                    'sku': catalogue_item.sku,
                }
            )

        return result
