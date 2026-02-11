"""Test API views for anonymous access to course pricing information."""

import pytest
from django.urls import reverse
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
from rest_framework import status as http_status
from rest_framework.test import APITestCase

from zeitlabs_payments.models import CatalogueItem


@pytest.mark.usefixtures('base_data')
class CoursePriceViewTest(APITestCase):
    """Tests for CoursePriceView - Anonymous access to course pricing."""

    def test_get_price_anonymous_success(self):
        """
        Verify that anonymous users can fetch course pricing information.
        """
        url = reverse('zeitlabs_payments:course-price')
        course_id = 'course-v1:org1+1+1'

        response = self.client.get(url, {'course_id': course_id})

        self.assertEqual(response.status_code, http_status.HTTP_200_OK)

        assert 'course' in response.data
        assert 'pricing_modes' in response.data

        course_data = response.data['course']
        assert course_data['course_id'] == course_id
        assert 'course_name' in course_data
        assert 'course_image' in course_data
        assert 'org' in course_data
        assert 'run' in course_data

        # Verify pricing modes
        pricing_modes = response.data['pricing_modes']
        assert isinstance(pricing_modes, list)
        assert len(pricing_modes) > 0
        assert 'mode_slug' in pricing_modes[0]
        assert 'mode_display_name' in pricing_modes[0]
        assert 'price' in pricing_modes[0]
        assert 'currency' in pricing_modes[0]
        assert 'sku' in pricing_modes[0]

    def test_get_price_missing_course_id(self):
        """
        Verify that the API returns 400 when course_id is not provided.
        """
        url = reverse('zeitlabs_payments:course-price')

        response = self.client.get(url)

        self.assertEqual(response.status_code, http_status.HTTP_400_BAD_REQUEST)
        assert 'error' in response.data
        assert 'course_id parameter is required' in response.data['error']

    def test_get_price_invalid_course_id(self):
        """
        Verify that the API returns 400 for invalid course_id format.
        """
        url = reverse('zeitlabs_payments:course-price')
        invalid_course_id = 'invalid-course-id-format'

        response = self.client.get(url, {'course_id': invalid_course_id})

        self.assertEqual(response.status_code, http_status.HTTP_400_BAD_REQUEST)
        assert 'error' in response.data
        assert 'Invalid course_id format' in response.data['error']

    def test_get_price_course_not_found(self):
        """
        Verify that the API returns 404 when the course does not exist.
        """
        url = reverse('zeitlabs_payments:course-price')
        non_existent_course = 'course-v1:NonExistent+Course+2024'

        response = self.client.get(url, {'course_id': non_existent_course})

        self.assertEqual(response.status_code, http_status.HTTP_404_NOT_FOUND)
        assert 'error' in response.data
        assert 'Course not found' in response.data['error']

    def test_get_price_no_pricing_modes(self):
        """
        Verify that the API returns 404 when course exists but has no pricing modes with SKUs.
        """
        url = reverse('zeitlabs_payments:course-price')
        course_without_modes = 'course-v1:org1+3+3'

        response = self.client.get(url, {'course_id': course_without_modes})

        self.assertEqual(response.status_code, http_status.HTTP_404_NOT_FOUND)
        assert 'error' in response.data
        assert 'No pricing modes available' in response.data['error']

    def test_get_price_catalogue_items_without_matching_modes(self):
        """
        Verify that the API returns 404 when catalogue items exist but have no matching course modes.
        This tests the edge case where CatalogueItems exist but CourseMode.objects.filter() returns empty.
        """
        course_id = 'course-v1:org1+orphan+2026'
        CourseOverview.objects.create(
            id=course_id, org='org1', display_name='Orphan Course'
        )

        CatalogueItem.objects.create(
            sku='orphan-sku',
            type=CatalogueItem.ItemType.PAID_COURSE,
            title='Orphan Catalogue Item',
            item_ref_id=course_id,
            price=100,
            currency='IQD',
        )

        url = reverse('zeitlabs_payments:course-price')
        response = self.client.get(url, {'course_id': course_id})

        self.assertEqual(response.status_code, http_status.HTTP_404_NOT_FOUND)
        assert 'error' in response.data
        assert 'No pricing modes available' in response.data['error']
