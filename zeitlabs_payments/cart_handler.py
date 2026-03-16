"""Cart fullfillment."""

import logging
from typing import Any, List

from common.djangoapps.course_modes.models import CourseMode
from common.djangoapps.student.models import CourseEnrollment, CourseEnrollmentException
from django.contrib.auth import get_user_model

from zeitlabs_payments.exceptions import CartFulfillmentError, DuplicateCartError, InvalidCartError
from zeitlabs_payments.helpers import (
    cancel_old_pending_carts,
    check_duplicate_cart_with_item,
    check_user_enroll_conditions,
)
from zeitlabs_payments.models import AuditLog, Cart, CartItem, CatalogueItem, TaxRule

logger = logging.getLogger(__name__)


CART_HANDLER: dict[Any, Any] = {}


def validate_and_create_cart(
    user: get_user_model,
    catalogue_items: List[CatalogueItem],
    cancel_old_carts: bool = True,
) -> Cart:
    """
    Validate a list of catalogue items and create a single cart containing all of them.

    This function:
    - Validates that the list is non-empty and contains no duplicate SKUs.
    - Looks up the appropriate handler for each item's type via the CART_HANDLER registry.
    - Calls each handler's validate_add_to_cart() to ensure the user can purchase each item.
    - Cancels old pending carts (if cancel_old_carts is True).
    - Creates one Cart with N CartItems, computing tax for each.

    :param user: User instance.
    :param catalogue_items: List of CatalogueItem instances to add to cart.
    :param cancel_old_carts: Whether to cancel the user's old pending carts before creating a new one.
    :raises InvalidCartError: If validation fails for any item, the list is empty, or duplicates are found.
    :return: The newly created Cart instance.
    """
    if not catalogue_items:
        raise InvalidCartError(
            'At least one catalogue item is required to create a cart.'
        )

    # Check for duplicate SKUs within the request
    skus = [item.sku for item in catalogue_items]
    if len(skus) != len(set(skus)):
        duplicates = [sku for sku in skus if skus.count(sku) > 1]
        raise InvalidCartError(
            f'Duplicate SKUs found in request: {", ".join(set(duplicates))}'
        )

    # Validate each item via its registered handler
    for catalogue_item in catalogue_items:
        handler = CART_HANDLER.get(catalogue_item.type)
        if not handler:
            raise InvalidCartError(
                f'Item with SKU {catalogue_item.sku} has unsupported type: {catalogue_item.type}.'
            )
        handler.validate_add_to_cart(user, catalogue_item)

    if cancel_old_carts:
        cancel_old_pending_carts(user)

    cart = Cart.objects.create(user=user, status=Cart.Status.PENDING)
    logger.info(f'Created new pending cart {cart.id} for user {user}')

    for catalogue_item in catalogue_items:
        _, tax_amount = TaxRule.get_applicable_tax(catalogue_item.price)
        final_price = catalogue_item.price + tax_amount

        CartItem.objects.create(
            cart=cart,
            catalogue_item=catalogue_item,
            original_price=catalogue_item.price,
            tax_amount=tax_amount,
            final_price=final_price,
        )
        logger.info(f'Added catalogue item {catalogue_item.sku} to cart {cart.id}')

    return cart


class BaseCartHandler:
    """
    Base class/interface for fulfillment strategy handlers.
    """

    def validate_add_to_cart(
        self,
        user: get_user_model,  # pylint: disable=unused-argument
        catalogue_item: CatalogueItem,  # pylint: disable=unused-argument
    ) -> None:
        """
        Raise InvalidCartError if validation fails.
        """
        return

    def fulfill(self, item: CartItem, processor_slug: str) -> None:
        """
        Fulfill the given item in the cart.

        :param item: The cart item to fulfill.
        :raises NotImplementedError: If the subclass does not implement this method.
        :return: None
        """
        raise NotImplementedError('Subclasses must implement this.')

    def validate_item_and_create_cart(
        self,
        user: get_user_model,
        catalog_item: CatalogueItem,
        cancel_old_carts: bool = True,
    ) -> Cart:
        """
        Create an open cart for the given user with a single catalogue item.

        This is a backward-compatible wrapper around validate_and_create_cart().
        Before creating a new cart, this function will cancel all of the user's stale carts
        that are in the 'pending' state, ensuring the user has only one active pending cart at a time.

        :param user: User instance
        :param catalog_item: CatalogueItem instance to add to cart
        :param cancel_old_carts: Whether to cancel old pending carts before creating a new one.
        :return: Cart instance
        """
        return validate_and_create_cart(
            user, [catalog_item], cancel_old_carts=cancel_old_carts
        )


def register_handler(item_type: str) -> Any:
    """
    Register a fulfillment handler for a given catalogue item type.

    :param item_type: The type of catalogue item to register the handler for.
    :return: The class decorator function.
    """
    def wrapper(cls: Any) -> BaseCartHandler:
        CART_HANDLER[item_type] = cls()
        return cls
    return wrapper


@register_handler(CatalogueItem.ItemType.PAID_COURSE)
class PaidCourseCartHandler(BaseCartHandler):
    """
    Fulfillment handler for paid course catalogue items.
    """

    def validate_add_to_cart(self, user: get_user_model, catalogue_item: CatalogueItem) -> None:
        """
        Validate whether a user can add a given catalogue item (linked to a course) to their cart.

        This method:
        - Retrieves the corresponding CourseMode by SKU from the catalogue item.
        - Checks if the user meets enrollment conditions for that course.
        - Raises an appropriate error if validation fails.

        :param user: The user attempting to add the item to the cart.
        :param catalogue_item: The catalogue item representing the course to add.
        :raises InvalidCartError: If the course mode does not exist, or if the user does not meet enrollment conditions.
        :return: None
        """
        try:
            course_mode = CourseMode.objects.get(sku=catalogue_item.sku)
            if str(course_mode.course.id) != catalogue_item.item_ref_id:
                raise InvalidCartError(
                    'Unable to add item to the cart as Course mode found with given sku but course_id'
                    ' mismatch with catalogue item ref-id.'
                )
            check_user_enroll_conditions(user, course_mode)
            check_duplicate_cart_with_item(
                user,
                course_mode,
                status=Cart.Status.PAYMENT_PENDING,
                item_type=catalogue_item.ItemType.PAID_COURSE,
            )
        except CourseMode.DoesNotExist as exc:
            raise InvalidCartError('Unable to add item to the cart as CourseMode not found') from exc
        except DuplicateCartError as exc:
            raise InvalidCartError(
                f'Unable to add item to the cart as user has existing cart with same course. {str(exc)}'
            ) from exc
        except CourseEnrollmentException as exc:
            raise InvalidCartError(
                f'Unable to add item to the cart as user: {user} does not fulfill enrollment conditions. {str(exc)}'
            ) from exc

    def fulfill(self, item: CartItem, processor_slug: str) -> None:
        """
        Fulfill a paid course item by enrolling the user in the course.

        :param item: The cart item representing a paid course.
        :raises CartFulfillmentError: If course mode is not found or enrollment fails.
        :return: None
        """
        cart = item.cart
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

        if str(course_mode.course.id) != item.catalogue_item.item_ref_id:
            logger.error(
                f'CourseMode found with sku: {item.catalogue_item.sku} but course id: {course_mode.course.id} does '
                f'not match with item ref id {item.catalogue_item.item_ref_id} - Item ID: {item.id}'
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
            raise CartFulfillmentError('Course Mode found but item ref id mismatched. ')

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
                    'catalogue_item_id': item.catalogue_item.id,
                }
            )
            raise CartFulfillmentError('Unexpected enrollment error') from exc
