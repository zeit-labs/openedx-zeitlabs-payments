"""
These settings are here to use during tests, because django requires them.

In a real-world use case, apps in this project are installed into other
Django applications, so these settings will not be used.
"""
import os
from os.path import abspath, dirname, join

from dotenv import load_dotenv

MASTER_DB_FLAG = os.getenv('I_KNOW_I_AM_CONNECTING_TO_REAL_DB', '').lower() in ('1', 'true', 'yes')
if MASTER_DB_FLAG:
    print('⚠️  WARNING: I_KNOW_I_AM_CONNECTING_TO_REAL_DB is set; real DB credentials may be used!')
    load_dotenv()


def db_setting(key, default):
    if not MASTER_DB_FLAG:
        return default
    return os.getenv(key, default)


def root(*args):
    """
    Get the absolute path of the given path relative to the project root.
    """
    return join(abspath(dirname(__file__)), *args)


DATABASES = {
    'default': {
        'ENGINE': db_setting('DB_ENGINE', 'django.db.backends.sqlite3'),
        'NAME': db_setting('DB_NAME', 'default.db'),
        'USER': db_setting('DB_USER', ''),
        'PASSWORD': db_setting('DB_PASSWORD', ''),
        'HOST': db_setting('DB_HOST', ''),
        'PORT': db_setting('DB_PORT', ''),
    },
}

INSTALLED_APPS = (
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.messages',
    'django.contrib.sites',
    'django.contrib.sessions',
    'fake_models',
    'dummy_tag_app',
    'zeitlabs_payments',
)

LOCALE_PATHS = [
    root('zeitlabs_payments', 'conf', 'locale'),
]

ROOT_URLCONF = 'tests.test_urls'

SECRET_KEY = 'insecure-secret-key'

MIDDLEWARE = (
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
)

TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': ['tests/templates'],
    'APP_DIRS': True,
    'OPTIONS': {
        'context_processors': [
            'django.contrib.auth.context_processors.auth',  # this is required for admin
            'django.contrib.messages.context_processors.messages',  # this is required for admin
            'django.template.context_processors.request'
        ],
    },
}]

# Avoid warnings about migrations
DEFAULT_AUTO_FIELD = 'django.db.models.AutoField'

ECOMMERCE_PUBLIC_URL_ROOT = 'test.com'
ZEITLABS_PAYMENTS_SETTINGS = {
    'invoice_prefix': 'TEST',
    'organization': 'test_org',
    'customer_number': '112233',
    'valid_currency': 'SAR',
}
IS_ZEITLABS_PAYMENTS_ENABLED = False
SITE_ID = 1
