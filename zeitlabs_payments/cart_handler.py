"""Cart fullfillment."""

import logging
from typing import Any

from common.djangoapps.course_modes.models import CourseMode
from common.djangoapps.student.models import CourseEnrollment, CourseEnrollmentException
from django.contrib.auth import get_user_model

from zeitlabs_payments.exceptions import CartFulfillmentError, DuplicateCartError, InvalidCartError
from zeitlabs_payments.helpers import (
    cancel_old_pending_carts,
    check_duplicate_cart_with_item,
    check_user_enroll_conditions,
)
from zeitlabs_payments.models import AuditLog, BundleCourseItem, Cart, CartItem, CatalogueItem, TaxRule

logger = logging.getLogger(__name__)


CART_HANDLER = {}


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
        Create an open cart for the given user.
        Before creating a new cart, this function will cancel all of the user's stale carts
        that are in the 'pending' state, ensuring the user has only one active pending cart at a time.

        :param user: User instance
        :param catalog_item: CatalogueItem instance to add to cart
        :return: Cart instance
        """
        self.validate_add_to_cart(user, catalog_item)
        if cancel_old_carts:
            cancel_old_pending_carts(user)
        cart = Cart.objects.create(user=user, status=Cart.Status.PENDING)
        logger.info(f'Created new pending cart {cart.id} for user {user}')

        _, tax_amount = TaxRule.get_applicable_tax(catalog_item.price)
        final_price = catalog_item.price + tax_amount

        CartItem.objects.create(
            cart=cart,
            catalogue_item=catalog_item,
            original_price=catalog_item.price,
            tax_amount=tax_amount,
            final_price=final_price,
        )
        logger.info(f'Added catalogue item {catalog_item.sku} to cart {cart.id}')
        return cart


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
            logger.error(f'CourseMode not found for SKU: {item.catalogue_item.sku} - Item ID: {item.id}')
            AuditLog.log(
                action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR,
                cart=cart,
                context={
                    'item_id': item.id,
                    'catalogue_item_id': item.catalogue_item.id,
                    'sku': item.catalogue_item.sku,
                },
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
                },
            )
            raise CartFulfillmentError('Course Mode found but item ref id mismatched. ')

        try:
            CourseEnrollment.enroll(
                cart.user,
                course_mode.course.id,
                mode=course_mode.mode_slug,
                check_access=True,
            )
            AuditLog.log(
                action=AuditLog.AuditActions.USER_ENROLLED,
                cart=cart,
                context={
                    'course_id': course_mode.course.id,
                    'mode_slug': course_mode.mode_slug,
                    'catalogue_item_id': item.catalogue_item.id,
                },
            )
            logger.info(
                f'User {cart.user.id} enrolled in course {course_mode.course.id} with mode {course_mode.mode_slug}'
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
                },
            )
            raise CartFulfillmentError('Unexpected enrollment error') from exc


@register_handler(CatalogueItem.ItemType.PROGRAM_BUNDLE)
class ProgramBundleCartHandler(BaseCartHandler):
    """
    Fulfillment handler for program bundle catalogue items.

    A program bundle is a single CatalogueItem whose bulk_sku maps to
    multiple courses via BundleCourseItem.  Validation ensures every
    constituent course is purchasable; fulfillment enrolls the user in
    all of them.
    """

    def validate_add_to_cart(self, user: get_user_model, catalogue_item: CatalogueItem) -> None:
        """
        Validate whether the user can purchase this program bundle.

        Checks:
        - The bundle has at least one constituent course.
        - Each constituent course has a matching CourseMode (by SKU).
        - Each CourseMode course_id matches the course CatalogueItem's item_ref_id.
        - The user meets enrollment conditions for every course.
        - No duplicate payment-pending carts exist for any course.

        :param user: The user attempting to add the bundle to the cart.
        :param catalogue_item: The program_bundle CatalogueItem.
        :raises InvalidCartError: If any validation fails.
        """
        bundle_courses = BundleCourseItem.objects.filter(bundle=catalogue_item).select_related('course_item')

        if not bundle_courses.exists():
            raise InvalidCartError('Unable to add bundle to the cart: no courses linked to this bundle.')

        for link in bundle_courses:
            course_item = link.course_item
            try:
                course_mode = CourseMode.objects.get(sku=course_item.sku)
                if str(course_mode.course.id) != course_item.item_ref_id:
                    raise InvalidCartError(
                        f'Bundle course {course_item.sku}: CourseMode course_id mismatch with catalogue item ref-id.'
                    )
                check_user_enroll_conditions(user, course_mode)
                check_duplicate_cart_with_item(
                    user,
                    course_mode,
                    status=Cart.Status.PAYMENT_PENDING,
                    item_type=CatalogueItem.ItemType.PAID_COURSE,
                )
            except CourseMode.DoesNotExist as exc:
                raise InvalidCartError(f'Bundle course {course_item.sku}: CourseMode not found.') from exc
            except DuplicateCartError as exc:
                raise InvalidCartError(
                    f'Bundle course {course_item.sku}: user has existing cart with same course. {str(exc)}'
                ) from exc
            except CourseEnrollmentException as exc:
                raise InvalidCartError(
                    f'Bundle course {course_item.sku}: user does not fulfill enrollment conditions. {str(exc)}'
                ) from exc

    def fulfill(self, item: CartItem, processor_slug: str) -> None:
        """
        Fulfill a program bundle by enrolling the user in every constituent course.

        :param item: The cart item representing a program bundle.
        :param processor_slug: The payment processor slug.
        :raises CartFulfillmentError: If any enrollment fails.
        """
        cart = item.cart
        logger.debug(f'Processing program bundle item {item.id} in cart {cart.id}.')

        bundle_courses = BundleCourseItem.objects.filter(bundle=item.catalogue_item).select_related('course_item')

        if not bundle_courses.exists():
            logger.error(f'No courses found for bundle SKU: {item.catalogue_item.sku} - Item ID: {item.id}')
            AuditLog.log(
                action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR,
                cart=cart,
                context={
                    'item_id': item.id,
                    'catalogue_item_id': item.catalogue_item.id,
                    'sku': item.catalogue_item.sku,
                },
            )
            raise CartFulfillmentError('No courses linked to this bundle.')

        for link in bundle_courses:
            course_item = link.course_item
            try:
                course_mode = CourseMode.objects.get(sku=course_item.sku)
            except CourseMode.DoesNotExist as exc:
                logger.error(f'CourseMode not found for bundle course SKU: {course_item.sku} - Item ID: {item.id}')
                AuditLog.log(
                    action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR,
                    cart=cart,
                    context={
                        'item_id': item.id,
                        'catalogue_item_id': course_item.id,
                        'sku': course_item.sku,
                    },
                )
                raise CartFulfillmentError(f'CourseMode not found for bundle course {course_item.sku}') from exc

            if str(course_mode.course.id) != course_item.item_ref_id:
                logger.error(f'CourseMode course_id mismatch for bundle course {course_item.sku} - Item ID: {item.id}')
                AuditLog.log(
                    action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR,
                    cart=cart,
                    context={
                        'item_id': item.id,
                        'catalogue_item_id': course_item.id,
                        'sku': course_item.sku,
                    },
                )
                raise CartFulfillmentError(f'Bundle course {course_item.sku}: item ref id mismatched.')

            try:
                CourseEnrollment.enroll(
                    cart.user,
                    course_mode.course.id,
                    mode=course_mode.mode_slug,
                    check_access=True,
                )
                AuditLog.log(
                    action=AuditLog.AuditActions.USER_ENROLLED,
                    cart=cart,
                    context={
                        'course_id': course_mode.course.id,
                        'mode_slug': course_mode.mode_slug,
                        'catalogue_item_id': course_item.id,
                    },
                )
                logger.info(
                    f'User {cart.user.id} enrolled in course {course_mode.course.id} '
                    f'with mode {course_mode.mode_slug} (bundle {item.catalogue_item.sku})'
                )
            except CourseEnrollmentException as exc:
                logger.exception(
                    f'Enrollment failed for user {cart.user.id} in course '
                    f'{course_mode.course.id} (bundle {item.catalogue_item.sku}). '
                    f'Item ID: {item.id}'
                )
                AuditLog.log(
                    action=AuditLog.AuditActions.USER_ENROLLED_ERROR,
                    cart=cart,
                    context={
                        'course_id': course_mode.course.id,
                        'mode_slug': course_mode.mode_slug,
                        'catalogue_item_id': course_item.id,
                    },
                )
                raise CartFulfillmentError(f'Enrollment error for bundle course {course_item.sku}') from exc
