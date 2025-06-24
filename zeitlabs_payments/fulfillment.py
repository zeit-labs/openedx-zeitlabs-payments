# fulfillment.py

import logging

from common.djangoapps.course_modes.models import CourseMode
from common.djangoapps.student.models import CourseEnrollment, CourseEnrollmentException

from zeitlabs_payments.exceptions import CartFulfillmentError, InvalidCartError
from zeitlabs_payments.models import Cart, CatalogueItem, AuditLog, CartItem
from zeitlabs_payments.helpers import check_user_enroll_conditions

logger = logging.getLogger(__name__)


FULFILLMENT_HANDLERS = {}


def register_handler(item_type):
    """
    Decorator to register a fulfillment handler for a given catalogue item type.

    :param item_type: The type of catalogue item to register the handler for.
    :return: The class decorator function.
    """
    def wrapper(cls):
        FULFILLMENT_HANDLERS[item_type] = cls()
        return cls
    return wrapper


class BaseFulfillmentStrategy:
    """
    Base class/interface for fulfillment strategy handlers.
    Subclasses must implement the fulfill method.
    """

    def validate(self, cart: Cart, item: CartItem) -> None:
        """
        Raise InvalidCartError if validation fails.
        """
        return cart

    def fulfill(self, cart: Cart, item: CartItem, processor_slug: str):
        """
        Fulfill the given item in the cart.

        :param cart: The Cart instance containing the item.
        :param item: The cart item to fulfill.
        :raises NotImplementedError: If the subclass does not implement this method.
        :return: None
        """
        raise NotImplementedError("Subclasses must implement fulfill()")


@register_handler(CatalogueItem.ItemType.PAID_COURSE)
class PaidCourseFulfillment(BaseFulfillmentStrategy):
    """
    Fulfillment handler for paid course catalogue items.
    """

    def validate_add_to_cart(self, user, catalogue_item: CatalogueItem) -> None:
        try:
            course_mode = CourseMode.objects.get(sku=catalogue_item.sku)
            check_user_enroll_conditions(user, course_mode)
        except CourseMode.DoesNotExist:
            raise InvalidCartError('Unable to add item to the cart as CourseMode not found')
        except CourseEnrollmentException as exc:
            raise InvalidCartError(
                f"Unable to add item to the cart as user: {user} does not fulfill enrollment conditions."
            ) from exc

    def fulfill(self, cart: Cart, item: CartItem, processor_slug: str) -> None:
        """
        Fulfill a paid course item by enrolling the user in the course.

        :param cart: The Cart instance.
        :param item: The cart item representing a paid course.
        :raises CartFulfillmentError: If course mode is not found or enrollment fails.
        :return: None
        """
        logger.debug(f'Processing item {item.id} in cart {cart.id}.')

        try:
            course_mode = CourseMode.objects.get(sku=item.catalogue_item.sku)
        except CourseMode.DoesNotExist as exc:
            logger.error(
                f'CourseMode not found for SKU: {item.catalogue_item.sku} - Item ID: {item.id}'
            )
            AuditLog.log(
                action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR,
                cart=cart,
                context={
                    'item_id': item.id,
                    'catalogue_item_id': item.catalogue_item.id,
                    'sku': item.catalogue_item.sku,
                }
            )
            raise CartFulfillmentError('CourseMode not found') from exc

        try:
            CourseEnrollment.enroll(
                cart.user,
                course_mode.course.id,
                mode=course_mode.mode_slug,
                check_access=True
            )
            AuditLog.log(
                action=AuditLog.AuditActions.USER_ENROLLED,
                cart=cart,
                context={
                    'course_id': course_mode.course.id,
                    'mode_slug': course_mode.mode_slug,
                    'catalogue_item_id': item.catalogue_item.id,
                }
            )
            logger.info(
                f'User {cart.user.id} enrolled in course {course_mode.course.id} '
                f'with mode {course_mode.mode_slug}'
            )
        except CourseEnrollmentException as exc:
            logger.exception(
                f'Unexpected error while enrolling user {cart.user.id} in course: '
                f'{course_mode.course.id}. Item ID: {item.id}'
            )
            AuditLog.log(
                action=AuditLog.AuditActions.USER_ENROLLED_ERROR,
                cart=cart,
                context={
                    'course_id': course_mode.course.id,
                    'mode_slug': course_mode.mode_slug,
                    'catalogue_item_id': item.catalogue_item.id
                }
            )
            raise CartFulfillmentError('Unexpected enrollment error') from exc
