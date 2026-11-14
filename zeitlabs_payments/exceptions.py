"""Zeitlasb payments generic exceptions."""
from typing import Any


class GatewayError(Exception):
    """Custom exception for payment gateway related errors."""


class CartFulfillmentError(Exception):
    """
    Custom exception raised when cart fulfillment fails.

    A fulfillment failure can roll back every enrollment row written so far, but the enrollment
    call's non-transactional side effects may already have landed, so the error carries the record
    of what happened: the course ids that completed enrollment before the failure, the course id of
    the member that failed, and the bundle SKU the failure belongs to. Callers outside the
    rolled-back transaction use these to write an audit entry that survives the rollback.

    It also carries the course ids the learner already held before the failure, which nothing was enrolled for: the
    entry recording each of them is rolled back with the rest, and without them staff could not tell a course that
    needs no action on retry from one the failure stopped short of.
    """

    def __init__(self, message: str, landed_courses: list[str] | None = None, failed_course: str | None = None,
                 bundle_sku: str | None = None, held_courses: list[str] | None = None) -> None:
        """Record the message, and the courses that had already landed or were already held before the failure."""
        super().__init__(message)
        self.landed_courses = landed_courses or []
        self.failed_course = failed_course
        self.bundle_sku = bundle_sku
        self.held_courses = held_courses or []


class InvalidCartError(Exception):
    """
    A checkout refusal: the sentence a learner sees, and the technical detail behind it.

    ``str(error)`` is the learner-facing sentence -- one plain, translated sentence, with no usernames, SKUs, model
    names or internal status values. ``detail`` says what actually went wrong, for the log and the add-to-cart API.
    """

    def __init__(self, learner_message: str, detail: str = '') -> None:
        """Record the sentence for the learner, and the technical detail behind it when known."""
        super().__init__(learner_message)
        self.detail = detail


class CartStatusError(Exception):
    """Custom exception raised when a cart's status cannot be changed as asked."""


class InFlightPaymentError(Exception):
    """
    Raised when a cart cannot be created because a payment for the same item is still open.

    It carries the in-flight cart, so the caller can send the learner back to that payment, tell them it is in progress,
    or close it as a failed hand-off and create the cart after all.
    """

    def __init__(self, message: str, cart: Any = None) -> None:
        """Record the message and the cart whose payment is open."""
        super().__init__(message)
        self.cart: Any = cart


class DuplicateCartError(Exception):
    """
    Raised when another of the learner's carts blocks buying the item.

    Carries the blocking cart's id and why it blocks, so the caller can word the refusal for the learner while keeping
    the cart number support needs to find it.
    """

    BOUGHT = 'bought'
    UNRESOLVED = 'unresolved'

    def __init__(self, message: str, cart_id: int | None = None, reason: str = '') -> None:
        """Record the technical message, the blocking cart, and why it blocks."""
        super().__init__(message)
        self.cart_id = cart_id
        self.reason = reason


class InvoiceError(Exception):
    """Custom exception raised for invoice errors."""


class ResolutionError(Exception):
    """Raised when staff resolving a cart by hand are refused, or the resolution fails, saying why."""


class DuplicateTransactionError(Exception):
    """TRansaction already exist for given cart."""


class ConfigurationError(Exception):
    """Custom exception raised when a required plugin setting is missing or unusable."""


class CurrencyMismatchError(Exception):
    """Custom exception raised when a stored currency disagrees with the deployment currency."""
