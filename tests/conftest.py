"""Required fixtures for tests."""
# tests/conftest.py
import random

import pytest
from common.djangoapps.course_modes.models import CourseMode
from django.contrib.auth import get_user_model
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview

from zeitlabs_payments.models import CatalogueItem

User = get_user_model()


USERS = {
    'total': 10,
    'super_users': [1, 8],
    'staff_users': [2, 8, 9],
    'inactive_users': [9, 10],
}
COURSES = {
    'org1': {
        1: {
            'display_name': 'Custom Course 1 of org1',  # optional, fallback to default if missing
            'modes': {
                CourseMode.NO_ID_PROFESSIONAL_MODE: {
                    'override_mode': {
                        'sku': 'custom-sku-1',
                        'price': 45,
                        'currency': 'SAR',
                    },
                    'catalogue_item': {
                        'enabled': True,
                        'override': {
                            'price': 50,  # catalogue item contains price different from course mode, should be okay
                        }
                    }
                },
                CourseMode.VERIFIED: {
                    'override_mode': {
                        'sku': 'custom-sku-with_invlaid_ref_id',
                    },
                    'catalogue_item': {
                        'enabled': True,
                        'override': {
                            # catalogue_item does not contain valid course_id in item_ref_id
                            'item_ref_id': 'course-v1:invalid+1+1',
                        }
                    }
                }
            }
        },
        2: {
            'modes': {
                CourseMode.NO_ID_PROFESSIONAL_MODE: {
                    'catalogue_item': {'enabled': False}  # course mode exist, but catalogue_item is not there
                },
            }
        },
        3: {
            'modes': {}
        },
    },
    'org2': {
        1: {
            'modes': {
                CourseMode.NO_ID_PROFESSIONAL_MODE: {
                    'catalogue_item': {'enabled': True}
                }
            }
        },
        2: {'modes': {}},
        3: {
            'modes': {
                CourseMode.NO_ID_PROFESSIONAL_MODE: {
                    'override_mode': {
                        'sku': 'custom-sku-2',
                        'currency': 'invlaid-curr',  # contians unsupported currency
                    },
                    'catalogue_item': {
                        'enabled': True,
                    }
                }
            }
        }
    }
}


@pytest.fixture(scope='session')
def base_data(django_db_setup, django_db_blocker):  # pylint: disable=unused-argument
    """Create base data for tests."""

    def _create_users():
        """Create users."""
        user = get_user_model()
        for i in range(1, USERS['total'] + 1):
            user.objects.create(
                id=i,
                username=f'user{i}',
                email=f'user{i}@example.com',
            )
        for user_id in USERS['super_users']:
            user.objects.filter(id=user_id).update(is_superuser=True, is_staff=True)
        for user_id in USERS['staff_users']:
            user.objects.filter(id=user_id).update(is_staff=True)
        for user_id in USERS['inactive_users']:
            user.objects.filter(id=user_id).update(is_active=False)

    def _create_courses_and_catalogue_items():
        """Create course overviews."""
        for org, courses in COURSES.items():
            for number, course_data in courses.items():
                course_id = f'course-v1:{org}+{number}+{number}'
                display_name = course_data.get('display_name', f'Course {number} of {org}')
                CourseOverview.objects.create(id=course_id, org=org, display_name=display_name)

                for slug, mode_data in course_data.get('modes', {}).items():
                    override_mode = mode_data.get('override_mode', {})
                    sku = override_mode.get('sku', f'course{number}-{org}-{slug}')
                    price = override_mode.get('price', random.randint(1, 100))
                    currency = override_mode.get('currency', 'SAR')

                    CourseMode.objects.create(
                        course_id=course_id,
                        mode_slug=slug,
                        sku=sku,
                        min_price=price,
                        currency=currency,
                    )

                    catalogue = mode_data.get('catalogue_item', {})
                    if catalogue.get('enabled'):
                        override_catalogue = catalogue.get('override', {})
                        item_ref_id = override_catalogue.get('item_ref_id', course_id)
                        catalogue_sku = override_catalogue.get('sku', sku)
                        catalogue_price = override_catalogue.get('price', price)
                        catalogue_currency = override_catalogue.get('currency', currency)

                        CatalogueItem.objects.create(
                            sku=catalogue_sku,
                            type=CatalogueItem.ItemType.PAID_COURSE,
                            item_ref_id=item_ref_id,
                            price=catalogue_price,
                            currency=catalogue_currency,
                        )

    with django_db_blocker.unblock():
        _create_users()
        _create_courses_and_catalogue_items()
