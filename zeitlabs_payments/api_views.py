"""API views for anonymous access to course pricing information."""

import logging
from typing import Any

from common.djangoapps.course_modes.models import CourseMode
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from zeitlabs_payments.serializers import CoursePriceSerializer

logger = logging.getLogger(__name__)


class CoursePriceView(APIView):
    """
    API view for fetching course pricing information.

    This API is accessible to anonymous users and returns payment information
    for a single course including available modes, prices, and SKUs.
    """

    permission_classes = [AllowAny]

    def get(self, request: Any) -> Response:
        """
        Retrieve pricing information for a course by course_id.

        Query Parameters:
            course_id (str): Course ID in format 'course-v1:org+course+run'

        Returns:
            Response with course pricing data including:
                - course: Course details (name, id, image, org, run)
                - pricing_modes: List of available pricing modes with:
                    - mode_slug: The mode identifier (e.g., 'verified', 'professional')
                    - mode_display_name: Human-readable mode name
                    - price: Price for this mode
                    - currency: Currency code
                    - sku: SKU for checkout / payment

        Example Response:
        {
            'course': {
                'course_id': 'course-v1:org+course+run',
                'course_name': 'Introduction to Python',
                'course_image': 'https://example.com/image.jpg',
                'org': 'org',
                'run': 'run'
            },
            'pricing_modes': [
                {
                    'mode_slug': 'verified',
                    'mode_display_name': 'Verified Certificate',
                    'price': 99.99,
                    'currency': 'IQD',
                    'sku': 'course-v1:org+course+run-verified'
                },
                {
                    'mode_slug': 'no-id-professional',
                    'mode_display_name': 'Professional Certificate',
                    'price': 199.99,
                    'currency': 'IQD',
                    'sku': 'course-v1:org+course+run-professional'
                }
            ]
        }
        """
        course_id = request.query_params.get('course_id')

        if not course_id:
            return Response(
                {'error': 'course_id parameter is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            course_key = CourseKey.from_string(course_id)
        except (InvalidKeyError, ValueError) as exc:
            logger.error(f'Invalid course_id format: {course_id} - {exc}')
            return Response(
                {'error': f'Invalid course_id format: {course_id}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            course = CourseOverview.objects.get(id=course_key)
        except CourseOverview.DoesNotExist:
            logger.warning(f'Course not found for id: {course_id}')
            return Response(
                {'error': f'Course not found: {course_id}'},
                status=status.HTTP_404_NOT_FOUND,
            )

        modes = CourseMode.objects.filter(
            course_id=course_key, sku__isnull=False
        ).select_related('course')

        if not modes.exists():
            logger.warning(f'No pricing modes found for course: {course_id}')
            return Response(
                {'error': f'No pricing modes available for course: {course_id}'},
                status=status.HTTP_404_NOT_FOUND,
            )

        data = CoursePriceSerializer(
            course, context={'request': request, 'modes': modes}
        ).data
        return Response(data, status=status.HTTP_200_OK)
