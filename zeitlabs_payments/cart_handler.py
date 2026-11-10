"""Cart fullfillment."""

import logging
from typing import Any

from common.djangoapps.course_modes.models import CourseMode
from common.djangoapps.student.models import (
    AlreadyEnrolledError,
    CourseEnrollment,
    CourseEnrollmentException,
    CourseFullError,
    EnrollmentClosedError,
)
from django.contrib.auth import get_user_model
from django.utils.translation import gettext as _

from zeitlabs_payments.exceptions import CartFulfillmentError, DuplicateCartError, InvalidCartError
from zeitlabs_payments.helpers import (
    cancel_old_pending_carts,
    check_carts_holding_item,
    check_user_enroll_conditions,
    learner_holds_course,
)
from zeitlabs_payments.models import AuditLog, BundleCourseItem, Cart, CartItem, CatalogueItem

logger = logging.getLogger(__name__)

CART_HANDLER: dict = {}


def _refuse(user: get_user_model, learner_message: str, detail: str) -> None:
    """
    Log what actually went wrong, then refuse with the one sentence the learner sees.

    A refusal is read by a learner, so its sentence carries no username, SKU, model name or internal status value; the
    cart number stays only where support needs it to find the cart. The technical detail goes to the log, and the
    add-to-cart API returns it in ``details``.

    :param user: The learner whose checkout is refused.
    :param learner_message: The plain, translated sentence shown to the learner.
    :param detail: The technical detail: the user, the SKU, the course id and the error behind the refusal.
    :raises InvalidCartError: Always.
    """
    logger.warning('Checkout refused for user %s: %s', user.username, detail)
    raise InvalidCartError(learner_message, detail)


def _duplicate_cart_message(error: DuplicateCartError) -> str:
    """Return the sentence for a cart that blocks the item, keeping its number so support can find it."""
    if error.reason == DuplicateCartError.BOUGHT:
        return _('You already bought this item in cart %(cart_id)s.') % {'cart_id': error.cart_id}
    return _('A payment for this item is still being processed in cart %(cart_id)s.') % {'cart_id': error.cart_id}


def _enrollment_message(error: CourseEnrollmentException, bundle: bool = False) -> str:
    """Return the sentence for an enrolment that refuses the item, from the error behind it."""
    if isinstance(error, AlreadyEnrolledError):
        if bundle:
            return _("You are already enrolled in one of this bundle's courses.")
        return _('You are already enrolled in this course.')
    if isinstance(error, EnrollmentClosedError):
        if bundle:
            return _("Enrollment is closed for one of this bundle's courses.")
        return _('Enrollment for this course is closed.')
    if isinstance(error, CourseFullError):
        if bundle:
            return _("One of this bundle's courses is full.")
        return _('This course is full.')
    if bundle:
        return _("You can't buy this bundle right now.")
    return _("You can't buy this course right now.")


class BaseCartHandler:
    """
    Base class/interface for fulfillment strategy handlers.
    """

    def validate_add_to_cart(
        self, user: get_user_model, catalogue_item: CatalogueItem  # pylint: disable=unused-argument
    ) -> None:
        """
        Raise InvalidCartError if validation fails.
        """
        return

    def fulfill(self, item: CartItem, processor: Any) -> None:
        """
        Fulfill the given item in the cart.

        :param item: The cart item to fulfill.
        :param processor: The processor settling the item, named in the audit entries this writes.
        :raises NotImplementedError: If the subclass does not implement this method.
        :return: None
        """
        raise NotImplementedError('Subclasses must implement this.')

    def validate_item_and_create_cart(self, user: get_user_model, catalog_item: CatalogueItem) -> Cart:
        """
        Create an open cart for the given user.
        Before creating a new cart, this function will cancel all of the user's stale carts
        that are in the 'pending' state, ensuring the user has only one active pending cart at a time.

        :param user: User instance
        :param catalog_item: CatalogueItem instance to add to cart
        :return: Cart instance
        """
        self.validate_add_to_cart(user, catalog_item)
        cancel_old_pending_carts(user)
        cart = Cart.objects.create(user=user, status=Cart.Status.PENDING)
        logger.info(f'Created new pending cart {cart.id} for user {user}')

        CartItem.objects.create(
            cart=cart,
            catalogue_item=catalog_item,
            original_price=catalog_item.price,
            final_price=catalog_item.price,
        )
        logger.info(f'Added catalogue item {catalog_item.sku} to cart {cart.id}')
        return cart


def enrol_or_upgrade(cart: Cart, processor: Any, course_mode: CourseMode, catalogue_item_id: int) -> bool:
    """
    Give the cart's learner the course mode bought, record how, and return whether their enrolment changed.

    - A learner already holding the mode is left as they are: they hold what they paid for.
    - A learner enrolled in a mode Open edX lets upgrade -- audit or honor -- has that enrolment moved to the mode
      bought, which emits Open edX's mode-change events rather than those of a new enrolment.
    - Anyone else is enrolled with Open edX's access checks, which refuse a learner enrolled in any other mode.

    :param cart: The cart being fulfilled.
    :param processor: The processor settling it, named on the audit entries.
    :param course_mode: The course mode bought.
    :param catalogue_item_id: The catalogue item the course was bought as.
    :raises CourseEnrollmentException: When Open edX refuses the enrolment.
    :return: False when the learner already held the mode, True when they were upgraded or enrolled.
    """
    course_key = course_mode.course.id
    context = {'course_id': course_key, 'mode_slug': course_mode.mode_slug, 'catalogue_item_id': catalogue_item_id}
    mode, is_active = CourseEnrollment.enrollment_mode_for_user(cart.user, course_key)

    if is_active and mode == course_mode.mode_slug:
        AuditLog.log(
            action=AuditLog.AuditActions.USER_ALREADY_ENROLLED, cart=cart, processor=processor, context=context,
        )
        logger.info(f'User {cart.user.id} already holds mode {course_mode.mode_slug} in course {course_key}.')
        return False

    if is_active and CourseMode.is_mode_upgradeable(mode):
        CourseEnrollment.get_enrollment(cart.user, course_key).update_enrollment(
            mode=course_mode.mode_slug, is_active=True,
        )
        AuditLog.log(
            action=AuditLog.AuditActions.USER_UPGRADED,
            cart=cart,
            processor=processor,
            context={**context, 'from_mode': mode},
        )
        logger.info(f'User {cart.user.id} upgraded in course {course_key} from mode {mode} to {course_mode.mode_slug}.')
        return True

    CourseEnrollment.enroll(cart.user, course_key, mode=course_mode.mode_slug, check_access=True)
    AuditLog.log(action=AuditLog.AuditActions.USER_ENROLLED, cart=cart, processor=processor, context=context)
    logger.info(f'User {cart.user.id} enrolled in course {course_key} with mode {course_mode.mode_slug}.')
    return True


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

        A refusal is one plain sentence for the learner, translated, with no username, SKU, model name or internal
        status value; the cart number stays in it only where support needs it to find the cart. What actually went
        wrong -- the user, the SKU, the course id and the error behind it -- is logged, and the add-to-cart API
        returns it in ``details``.

        :param user: The user attempting to add the item to the cart.
        :param catalogue_item: The catalogue item representing the course to add.
        :raises InvalidCartError: If the course mode does not exist, if the user does not meet enrollment conditions,
            or if another of their carts for the course blocks buying it.
        :raises InFlightPaymentError: If the user's earlier payment for the course is still open, carrying its cart.
        :return: None
        """
        try:
            course_mode = CourseMode.objects.get(sku=catalogue_item.sku)
            if str(course_mode.course.id) != catalogue_item.item_ref_id:
                _refuse(
                    user,
                    _('This course is not available to buy right now.'),
                    f'CourseMode {catalogue_item.sku} belongs to course {course_mode.course.id}, not to catalogue '
                    f'item ref id {catalogue_item.item_ref_id}.',
                )
            check_user_enroll_conditions(user, course_mode)
            check_carts_holding_item(user, catalogue_item)
        except CourseMode.DoesNotExist:
            _refuse(
                user,
                _('This course is not available to buy right now.'),
                f'No CourseMode found for sku {catalogue_item.sku}.',
            )
        except DuplicateCartError as exc:
            _refuse(
                user,
                _duplicate_cart_message(exc),
                f'Cart {exc.cart_id} blocks buying course {catalogue_item.item_ref_id} '
                f'(sku {catalogue_item.sku}): {exc}',
            )
        except CourseEnrollmentException as exc:
            _refuse(
                user,
                _enrollment_message(exc),
                f'User {user.username} does not fulfill enrollment conditions for course '
                f'{catalogue_item.item_ref_id} (sku {catalogue_item.sku}): {exc}',
            )

    def fulfill(self, item: CartItem, processor: Any) -> None:
        """
        Fulfill a paid course item by enrolling the user in the course, or upgrading them to its mode from a free one.

        A learner who already holds the course's mode -- enrolled in it by hand while the payment was settling, say --
        is left as they are, since they hold what they paid for, and that is recorded. See ``enrol_or_upgrade``.

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
                processor=processor,
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
                processor=processor,
                context={
                    'item_id': item.id,
                    'catalogue_item_id': item.catalogue_item.id,
                    'sku': item.catalogue_item.sku,
                }
            )
            raise CartFulfillmentError('Course Mode found but item ref id mismatched. ')

        try:
            enrol_or_upgrade(cart, processor, course_mode, item.catalogue_item.id)
        except CourseEnrollmentException as exc:
            logger.exception(
                f'Unexpected error while enrolling user {cart.user.id} in course: '
                f'{course_mode.course.id}. Item ID: {item.id}'
            )
            AuditLog.log(
                action=AuditLog.AuditActions.USER_ENROLLED_ERROR,
                cart=cart,
                processor=processor,
                context={
                    'course_id': course_mode.course.id,
                    'mode_slug': course_mode.mode_slug,
                    'catalogue_item_id': item.catalogue_item.id
                }
            )
            raise CartFulfillmentError('Unexpected enrollment error') from exc


@register_handler(CatalogueItem.ItemType.PROGRAM_BUNDLE)
class ProgramBundleCartHandler(BaseCartHandler):
    """
    Fulfillment handler for program bundle catalogue items.

    A program bundle is a single CatalogueItem linked to multiple course
    CatalogueItems via BundleCourseItem.  Validation ensures every constituent
    course is purchasable; fulfillment enrolls the user in all of them.

    Only the learner's carts for the bundle itself can block buying it. Their carts for its courses never do: buying a
    course must never stop its learner buying a bundle that contains it. A course the learner already holds -- bought
    before, or enrolled in a paid mode -- is neither checked nor enrolled in again; a bundle whose every course they
    hold is refused, since they would pay for nothing.
    """

    def validate_add_to_cart(self, user: get_user_model, catalogue_item: CatalogueItem) -> None:
        """
        Validate whether the user can purchase this program bundle.

        The bundle's own configuration is checked first. A bundle with no courses linked cannot be bought by anyone,
        so that is what the learner is told -- before the cart checks below, which would otherwise refuse them for a
        cart of their own, or send them back to an earlier payment for a bundle that cannot be bought.

        A bundle whose every course the learner already holds (``learner_holds_course``) is refused too: fulfilment
        would skip every one of them, so the learner would pay its full price for nothing. Holding only some of its
        courses does not stop the bundle -- they buy the rest as usual -- and a free mode, audit or honor, is upgraded
        rather than held. A held course is not checked against its enrolment conditions: nothing is granted for it, so
        whatever mode it is held in, and whether the learner has since unenrolled, its closing or filling up does not
        stop the bundle.

        Checks:
        - The bundle has at least one constituent course.
        - No other cart of the user for this bundle blocks buying it.
        - The user does not already hold every constituent course.
        - Each constituent course has a matching CourseMode (by SKU).
        - Each CourseMode course_id matches the course CatalogueItem's item_ref_id.
        - The user meets enrollment conditions for every course they do not already hold.

        A refusal is one plain sentence for the learner, translated, with no username, SKU, model name or internal
        status value; the cart number stays in it only where support needs it to find the cart. What actually went
        wrong -- the user, the SKU, the course id and the error behind it -- is logged, and the add-to-cart API
        returns it in ``details``.

        :param user: The user attempting to add the bundle to the cart.
        :param catalogue_item: The program_bundle CatalogueItem.
        :raises InvalidCartError: If any validation fails.
        :raises InFlightPaymentError: If the user's earlier payment for this bundle is still open, carrying its cart.
        """
        bundle_courses = BundleCourseItem.objects.filter(bundle=catalogue_item).select_related('course_item')

        if not bundle_courses.exists():
            _refuse(
                user,
                _('This bundle is not available to buy right now.'),
                f'No courses are linked to bundle {catalogue_item.sku}.',
            )

        try:
            check_carts_holding_item(user, catalogue_item)
        except DuplicateCartError as exc:
            _refuse(
                user,
                _duplicate_cart_message(exc),
                f'Cart {exc.cart_id} blocks buying bundle {catalogue_item.sku}: {exc}',
            )

        courses_held = 0
        for link in bundle_courses:
            course_item = link.course_item
            try:
                course_mode = CourseMode.objects.get(sku=course_item.sku)
                if str(course_mode.course.id) != course_item.item_ref_id:
                    _refuse(
                        user,
                        _('This bundle is not available to buy right now.'),
                        f'Bundle {catalogue_item.sku}: CourseMode {course_item.sku} belongs to course '
                        f'{course_mode.course.id}, not to catalogue item ref id {course_item.item_ref_id}.',
                    )
                if learner_holds_course(user, course_mode.course.id):
                    courses_held += 1
                    continue
                check_user_enroll_conditions(user, course_mode)
            except CourseMode.DoesNotExist:
                _refuse(
                    user,
                    _('This bundle is not available to buy right now.'),
                    f'Bundle {catalogue_item.sku}: no CourseMode found for sku {course_item.sku}.',
                )
            except CourseEnrollmentException as exc:
                _refuse(
                    user,
                    _enrollment_message(exc, bundle=True),
                    f'Bundle {catalogue_item.sku}: user {user.username} does not fulfill enrollment conditions '
                    f'for course {course_item.item_ref_id} (sku {course_item.sku}): {exc}',
                )

        if courses_held == len(bundle_courses):
            _refuse(
                user,
                _('You already have all the courses in this bundle.'),
                f'Bundle {catalogue_item.sku}: the learner already holds all {len(bundle_courses)} of its courses.',
            )

    def fulfill(self, item: CartItem, processor: Any) -> None:
        """
        Fulfill a program bundle by enrolling the user in every constituent course.

        Uses a two-pass approach: first validates all course modes exist and match,
        then enrolls in a second pass. The second pass runs inside the payment
        transaction, so a failure part-way through rolls the enrollment rows back;
        the raised CartFulfillmentError then carries the list of courses whose
        enrollment had completed before the failure, because the rollback does not
        undo the enrollment call's non-transactional side effects and staff need
        the record.

        A course the learner already holds (``learner_holds_course``), not counting this cart, is left as it is: in the
        mode it is held in, or unenrolled if they have unenrolled from it, since staff re-enrol a learner who left. It
        is among the courses held, not those that landed, since nothing changed for it. Every other course is enrolled
        or upgraded as ``enrol_or_upgrade`` decides; one upgraded from a free mode is among those that landed, since
        its mode-change events outlive a rollback as an enrolment's do.

        :param item: The cart item representing a program bundle.
        :param processor: The payment processor slug.
        :raises CartFulfillmentError: If any validation or enrollment fails. The
            error carries ``landed_courses`` (the course ids enrolled before the
            failure), ``held_courses`` (the course ids the learner already held before
            it), ``failed_course`` (the course id of the member that failed) and
            ``bundle_sku`` so the caller can write an audit entry that survives the
            rollback.
        """
        cart = item.cart
        logger.debug(f'Processing program bundle item {item.id} in cart {cart.id}.')

        bundle_courses = BundleCourseItem.objects.filter(bundle=item.catalogue_item).select_related('course_item')

        if not bundle_courses.exists():
            logger.error(f'No courses found for bundle SKU: {item.catalogue_item.sku} - Item ID: {item.id}')
            AuditLog.log(
                action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR,
                cart=cart,
                processor=processor,
                context={
                    'item_id': item.id,
                    'catalogue_item_id': item.catalogue_item.id,
                    'sku': item.catalogue_item.sku,
                },
            )
            raise CartFulfillmentError(
                'No courses linked to this bundle.',
                bundle_sku=item.catalogue_item.sku,
            )

        # Pass 1: Validate all course modes exist and match before any enrollment.
        validated_modes = []
        for link in bundle_courses:
            course_item = link.course_item
            try:
                course_mode = CourseMode.objects.get(sku=course_item.sku)
            except CourseMode.DoesNotExist as exc:
                logger.error(f'CourseMode not found for bundle course SKU: {course_item.sku} - Item ID: {item.id}')
                AuditLog.log(
                    action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR,
                    cart=cart,
                    processor=processor,
                    context={
                        'item_id': item.id,
                        'catalogue_item_id': course_item.id,
                        'sku': course_item.sku,
                    },
                )
                raise CartFulfillmentError(
                    f'CourseMode not found for bundle course {course_item.sku}',
                    failed_course=course_item.item_ref_id,
                    bundle_sku=item.catalogue_item.sku,
                ) from exc

            if str(course_mode.course.id) != course_item.item_ref_id:
                logger.error(f'CourseMode course_id mismatch for bundle course {course_item.sku} - Item ID: {item.id}')
                AuditLog.log(
                    action=AuditLog.AuditActions.CART_FULFILLMENT_ERROR,
                    cart=cart,
                    processor=processor,
                    context={
                        'item_id': item.id,
                        'catalogue_item_id': course_item.id,
                        'sku': course_item.sku,
                    },
                )
                raise CartFulfillmentError(
                    f'Bundle course {course_item.sku}: item ref id mismatched.',
                    failed_course=course_item.item_ref_id,
                    bundle_sku=item.catalogue_item.sku,
                )

            validated_modes.append((course_item, course_mode))

        # Pass 2: Enroll in all validated courses, remembering which of them landed and which the learner held already.
        landed_courses = []
        held_courses = []
        for course_item, course_mode in validated_modes:
            course_id = str(course_mode.course.id)
            try:
                if learner_holds_course(cart.user, course_mode.course.id, ignored_cart=cart):
                    AuditLog.log(
                        action=AuditLog.AuditActions.USER_ALREADY_ENROLLED,
                        cart=cart,
                        processor=processor,
                        context={
                            'course_id': course_mode.course.id,
                            'mode_slug': course_mode.mode_slug,
                            'catalogue_item_id': course_item.id,
                        },
                    )
                    logger.info(f'User {cart.user.id} holds course {course_id} already; cart {cart.id} skips it.')
                    held_courses.append(course_id)
                elif enrol_or_upgrade(cart, processor, course_mode, course_item.id):
                    landed_courses.append(course_id)
                else:
                    held_courses.append(course_id)
            except Exception as exc:
                logger.exception(
                    f'Enrollment failed for user {cart.user.id} in course '
                    f'{course_mode.course.id} (bundle {item.catalogue_item.sku}). '
                    f'Item ID: {item.id}'
                )
                AuditLog.log(
                    action=AuditLog.AuditActions.USER_ENROLLED_ERROR,
                    cart=cart,
                    processor=processor,
                    context={
                        'course_id': course_mode.course.id,
                        'mode_slug': course_mode.mode_slug,
                        'catalogue_item_id': course_item.id,
                    },
                )
                raise CartFulfillmentError(
                    f'Enrollment error for bundle course {course_item.sku}',
                    landed_courses=landed_courses,
                    failed_course=str(course_mode.course.id),
                    bundle_sku=item.catalogue_item.sku,
                    held_courses=held_courses,
                ) from exc
