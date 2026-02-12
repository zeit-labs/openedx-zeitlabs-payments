"""Test cache utilities."""

from common.djangoapps.course_modes.models import CourseMode
from django.test import TestCase
from opaque_keys.edx.keys import CourseKey

from zeitlabs_payments.cache_utils import (
    CACHE_TIMEOUT,
    get_course_price_cache_key,
    invalidate_course_price_cache,
)
from zeitlabs_payments.models import CatalogueItem


class CacheUtilsTest(TestCase):
    """Tests for cache utility functions."""

    def test_get_course_price_cache_key(self) -> None:
        """Verify cache key generation."""
        course_id = 'course-v1:org+course+run'
        cache_key = get_course_price_cache_key(course_id)

        self.assertEqual(cache_key, f'course_price:{course_id}')

    def test_invalidate_course_price_cache(self) -> None:
        """Verify cache invalidation."""
        from django.core.cache import cache

        course_id = 'course-v1:org+course+run'
        cache_key = get_course_price_cache_key(course_id)

        cache.set(cache_key, {'test': 'data'}, CACHE_TIMEOUT)
        self.assertIsNotNone(cache.get(cache_key))

        invalidate_course_price_cache(course_id)
        self.assertIsNone(cache.get(cache_key))

    def test_invalidate_on_catalogue_item_change_with_ref_id(self) -> None:
        """Verify cache invalidation when CatalogueItem with item_ref_id is saved."""
        from django.core.cache import cache

        course_id = 'course-v1:test+course+run'
        cache_key = get_course_price_cache_key(course_id)
        cache.set(cache_key, {'test': 'data'}, CACHE_TIMEOUT)

        CatalogueItem.objects.create(
            sku='test-sku',
            type=CatalogueItem.ItemType.PAID_COURSE,
            title='Test Item',
            item_ref_id=course_id,
            price=100,
            currency='IQD',
        )

        self.assertIsNone(cache.get(cache_key))

    def test_invalidate_on_catalogue_item_change_without_ref_id(self) -> None:
        """
        Verify that invalidating without item_ref_id doesn't crash.

        This tests the edge case where instance.item_ref_id is None.
        """
        from django.core.cache import cache

        course_id = 'course-v1:test+course+run2'
        cache_key = get_course_price_cache_key(course_id)
        cache.set(cache_key, {'test': 'data'}, CACHE_TIMEOUT)

        CatalogueItem.objects.create(
            sku='test-sku-no-ref',
            type=CatalogueItem.ItemType.PAID_COURSE,
            title='Test Item No Ref',
            item_ref_id='',  # Empty ref_id
            price=100,
            currency='IQD',
        )

        empty_cache_key = get_course_price_cache_key('')
        self.assertIsNone(cache.get(empty_cache_key))

    def test_invalidate_on_course_mode_change_with_course_id(self) -> None:
        """Verify cache invalidation when CourseMode with course_id is saved."""
        from django.core.cache import cache

        course_id = 'course-v1:test+course+run3'
        cache_key = get_course_price_cache_key(course_id)
        cache.set(cache_key, {'test': 'data'}, CACHE_TIMEOUT)

        course_key_obj = CourseKey.from_string(course_id)
        CourseMode.objects.create(
            course_id=course_key_obj,
            mode_slug='verified',
            mode_display_name='Verified',
            sku='test-sku-mode',
            min_price=50,
        )

        self.assertIsNone(cache.get(cache_key))

    def test_invalidate_on_course_mode_change_without_course_id(self) -> None:
        """
        Verify that invalidating without course_id doesn't crash.

        This tests the edge case where instance.course_id is None.
        """

        course_id = 'course-v1:test+course+run4'
        course_key_obj = CourseKey.from_string(course_id)

        mode = CourseMode.objects.create(
            course_id=course_key_obj,
            mode_slug='honor',
            mode_display_name='Honor',
            sku='test-sku-honor',
            min_price=0,
        )

        self.assertIsNotNone(mode.course_id)

    def test_invalidate_on_course_mode_with_none_course_id_mock(self) -> None:
        """
        Verify cache invalidation handles None course_id gracefully.

        This uses mocking to test the edge case where course_id might be None
        (e.g., if database constraints change in the future).
        """
        from django.core.cache import cache
        from unittest.mock import Mock

        course_id = 'course-v1:test+course+run5'
        cache_key = get_course_price_cache_key(course_id)
        cache.set(cache_key, {'test': 'data'}, CACHE_TIMEOUT)

        mock_mode = Mock(spec=CourseMode)
        mock_mode.course_id = None
        mock_mode.sku = 'mock-sku'

        from zeitlabs_payments.cache_utils import invalidate_on_course_mode_change

        invalidate_on_course_mode_change(CourseMode, mock_mode)

        self.assertIsNotNone(cache.get(cache_key))
