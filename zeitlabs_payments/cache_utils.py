"""Cache utilities for course pricing data."""

import logging
from common.djangoapps.course_modes.models import CourseMode
from django.core.cache import cache
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from zeitlabs_payments.models import CatalogueItem

logger = logging.getLogger(__name__)

# Cache timeout: 1 hour (3600 seconds)
# Cache is invalidated when prices change, so this is just a fallback
CACHE_TIMEOUT = 3600

__all__ = [
    'get_course_price_cache_key',
    'invalidate_course_price_cache',
    'CACHE_TIMEOUT',
]


def get_course_price_cache_key(course_id: str) -> str:
    """
    Generate cache key for course pricing data.

    :param course_id: Course ID string
    :return: Cache key
    """
    return f'course_price:{course_id}'


def invalidate_course_price_cache(course_id: str) -> None:
    """
    Invalidate cache for a specific course.

    :param course_id: Course ID to invalidate
    """
    cache_key = get_course_price_cache_key(course_id)
    cache.delete(cache_key)
    logger.debug(f'Invalidated cache for course: {course_id}')


@receiver(post_save, sender=CatalogueItem)
@receiver(post_delete, sender=CatalogueItem)
def invalidate_on_catalogue_item_change(sender, instance, **kwargs) -> None:
    """
    Invalidate course price cache when a CatalogueItem is created, updated, or deleted.

    This ensures that any price changes are immediately reflected in the API.

    :param sender: Model class sending the signal
    :param instance: CatalogueItem instance
    """
    if instance.item_ref_id:
        invalidate_course_price_cache(instance.item_ref_id)
        logger.info(
            f'Cache invalidated for course {instance.item_ref_id} '
            f'due to CatalogueItem change (sku: {instance.sku})'
        )


@receiver(post_save, sender=CourseMode)
@receiver(post_delete, sender=CourseMode)
def invalidate_on_course_mode_change(sender, instance, **kwargs) -> None:
    """
    Invalidate course price cache when a CourseMode is created, updated, or deleted.

    This ensures that mode detail changes (mode_slug, mode_display_name) are
    immediately reflected in the API.

    :param sender: Model class sending the signal
    :param instance: CourseMode instance
    """
    if instance.course_id:
        course_id = str(instance.course_id)
        invalidate_course_price_cache(course_id)
        logger.info(
            f'Cache invalidated for course {course_id} '
            f'due to CourseMode change (sku: {instance.sku})'
        )
