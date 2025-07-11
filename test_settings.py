"""
These settings are here to use during tests, because django requires them.

In a real-world use case, apps in this project are installed into other
Django applications, so these settings will not be used.
"""
from os.path import abspath, dirname, join


def root(*args):
    """
    Get the absolute path of the given path relative to the project root.
    """
    return join(abspath(dirname(__file__)), *args)


DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': 'default.db',
        'USER': '',
        'PASSWORD': '',
        'HOST': '',
        'PORT': '',
    }
}

INSTALLED_APPS = (
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.messages',
    'django.contrib.sites',
    'django.contrib.sessions',
    'fake_models',
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

PAYFORT_SETTINGS = {
    'access_code': 'test-code',
    'merchant_identifier': 'test-identifier',
    'request_sha_phrase': 'test-request-phrase',
    'response_sha_phrase': 'test-response-phrase',
    'sha_method': 'SHA-256',
    'redirect_url': 'https://sbcheckout.payfort.com/FortAPI/paymentPage'
}
ECOMMERCE_BASE_URL = 'test.com'
INVOICE_PREFIX = 'TEST'
ORGANIZATION = 'test_org'
CUSTOMER_NUMBER = '112233'
