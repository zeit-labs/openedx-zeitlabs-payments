"""zeitlabs payments serializers."""
import logging
from typing import Any, List, Optional

from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
from rest_framework import serializers

from zeitlabs_payments.helpers import relative_url_to_absolute_url
from zeitlabs_payments.models import Cart, CartItem, CatalogueItem

logger = logging.getLogger(__name__)


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
    courses = serializers.SerializerMethodField()
    title = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()

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
            'courses',
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

    def get_courses(self, obj: CartItem) -> List[Any]:
        """
        Return a list of serialized courses if the item type is PAID_COURSE.

        :param obj: CartItem instance
        :return: List of serialized course data
        """
        courses = []
        if obj.catalogue_item.type == CatalogueItem.ItemType.PAID_COURSE:
            try:
                courses = [CourseOverview.objects.get(id=obj.catalogue_item.item_ref_id)]
                return CourseSerializer(instance=courses, many=True, context=self.context).data
            except CourseOverview.DoesNotExist as exc:
                logger.warning(f'CourseOverview not found for id {obj.catalogue_item.item_ref_id}')
                raise Exception(
                    f'Catalogue item of type: {CatalogueItem.ItemType.PAID_COURSE:} must be linked with course_id.'
                ) from exc
        else:
            raise Exception('Unsupported catalogue item type.')


class CartSerializer(serializers.ModelSerializer):
    """Cart serializer."""

    items = serializers.SerializerMethodField()

    class Meta:
        model = Cart
        fields = ['id', 'user', 'status', 'created_at', 'items', 'total']

    def get_items(self, obj: Cart) -> List[Any]:
        """
        Return serialized cart items.

        :param obj: Cart instance
        :return: List of serialized cart item data
        """
        serializer = CartItemSerializer(obj.items.all(), many=True, context=self.context)
        return serializer.data
