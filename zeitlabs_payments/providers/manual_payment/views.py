"""Manual oayment views"""

import logging
import re
from typing import Any, Optional

from common.djangoapps.course_modes.models import CourseMode
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from zeitlabs_payments import models
from zeitlabs_payments.cart_handler import CART_HANDLER
from zeitlabs_payments.exceptions import InvalidCartError
from zeitlabs_payments.providers.manual_payment.processor import ManualPaymentProcessor

logger = logging.getLogger(__name__)
User = get_user_model()

ID_PART = r'[a-zA-Z0-9_-]+'
COURSE_ID_REGX = \
    fr'(?P<course_id>course-v1:(?P<org>{ID_PART})\+(?P<course>{ID_PART})\+(?P<run>{ID_PART}))'
COURSE_ID_REGX_EXACT = rf'^{COURSE_ID_REGX}$'


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

        required_fields = ['course_key', 'mode', 'transaction_id', 'transaction_status']
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
                "user_id": 111,  # Optional if "username" is provided
                "username": "me",  # Optional if "user_id" is provided
                "course_run_key": "course-v1:TestX+Test100+2019_T1",  # Required
                "mode": "verified",  # Required, e.g., 'verified' or 'professional',
                "transaction_id": "manual-123",  # Required
                "transaction_status": "success",  # Required
                "reason": "some reason",  # Optional
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
            cart = handler.validate_item_and_create_cart(user, course_catalog_item, cancel_old_carts=False)
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
            result = processor.process_payment(
                request,
                cart,
                request.data['transaction_id'],
                request.data['transaction_status'],
                request.data.get('reason')
            )
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
