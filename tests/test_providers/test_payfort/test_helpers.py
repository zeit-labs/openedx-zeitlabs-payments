"""Test file for zietlabs payments helpers."""
import hashlib
from typing import Dict

import pytest
from django.contrib.auth import get_user_model

from zeitlabs_payments.providers.payfort.exceptions import PayFortBadSignatureException, PayFortException
from zeitlabs_payments.providers.payfort.helpers import get_signature, verify_response_format, verify_signature
from zeitlabs_payments.exceptions import GatewayError


User = get_user_model()
VALID_RESPONSE: Dict[str, str] = {
    'amount': '150',
    'currency': 'SAR',
    'command': 'PURCHASE',
    'merchant_reference': '1-1',
    'merchant_identifier': 'abcd',
    'status': '14',
    'eci': 'ECOMMERCE',
    'fort_id': '12341',
    'response_code': '14000',
    'signature': 'secretabcd',
}


@pytest.mark.parametrize(
    'sha_phrase, sha_method, params, expected_hash_start, usecase',
    [
        (
            'secret',
            'SHA-256',
            {'Amount': '100', 'Currency': 'USD'},
            hashlib.sha256('secretAmount=100Currency=USDsecret'.encode()).hexdigest(),
            'Valid SHA256 signature'
        ),
        (
            'phrase',
            'SHA-512',
            {'b': '2', 'a': '1'},
            hashlib.sha512('phrasea=1b=2phrase'.encode()).hexdigest(),
            'Valid SHA512 signature with sorted keys'
        ),
    ]
)
def test_get_signature_valid(sha_phrase, sha_method, params, expected_hash_start, usecase):
    """
    Test that get_signature returns the correct hash signature for valid
    inputs with different SHA methods and parameters.
    """
    signature = get_signature(sha_phrase, sha_method, params)
    assert signature == expected_hash_start, f'failed for usecase: {usecase}'


@pytest.mark.parametrize(
    'sha_phrase, sha_method, params, expected_error, usecase',
    [
        ('secret', 'md5', {'Amount': '100'}, 'Unsupported SHA method', 'Unsupported SHA'),
        (None, 'SHA-256', {'Amount': '100'}, 'sha_phrase is required', 'sha_phrase is None'),
        ('secret', None, {'Amount': '100'}, 'sha_method is required', 'sha_method is None'),
        ('secret', 'SHA-256', None, 'transaction_parameters is required', 'params is None'),
        ('secret', 'SHA-256', 'not a dict', 'transaction_parameters is required', 'params wrong type'),
    ]
)
def test_get_signature_invalid(sha_phrase, sha_method, params, expected_error, usecase):
    """
    Test that get_signature raises PayFortException with appropriate
    error messages when called with invalid or missing parameters.
    """
    with pytest.raises(GatewayError) as exc_info:
        get_signature(sha_phrase, sha_method, params)
    assert expected_error in str(exc_info.value), f'Failed for case: {usecase}.'


@pytest.mark.parametrize('modified_response, expected_error, usecase', [
    (
        VALID_RESPONSE, None,
        'Valid response passes'
    ),
    (
        {k: v for k, v in VALID_RESPONSE.items() if k != 'amount'}, 'Missing field in response: amount',
        'Missing amount field'
    ),
    (
        {**VALID_RESPONSE, 'amount': 1000}, 'Invalid field type in response: amount',
        'Amount not str'
    ),
    (
        {**VALID_RESPONSE, 'amount': '-1000'}, 'Invalid amount in response',
        'Negative amount string'
    ),
    (
        {**VALID_RESPONSE, 'amount': '10.5'}, 'Invalid amount in response',
        'Non-integer amount'
    ),
    (
        {**VALID_RESPONSE, 'currency': 'USD'}, 'Invalid currency in response',
        'Wrong currency'
    ),
    (
        {**VALID_RESPONSE, 'command': 'INVALID'}, 'Invalid command in response',
        'Wrong command'
    ),
    (
        {**VALID_RESPONSE, 'merchant_reference': 'abc'}, 'Invalid merchant_reference in response',
        'Bad merchant reference'
    ),
    (
        {**{k: v for k, v in VALID_RESPONSE.items() if k not in ('fort_id', 'eci')}}, 'Unexpected successful payment',
        'Missing fort_id and eci with success'
    ),
    (
        {**{k: v for k, v in VALID_RESPONSE.items() if k not in ('fort_id', 'eci')}, 'status': '00'}, None,
        'Missing fort_id/eci with non-success status'
    )
])
def test_verify_response_format(modified_response, expected_error, usecase):
    """
    Test that verify_response_format validates the response dictionary correctly,
    raising PayFortException for missing or invalid fields, and passes for valid data.
    """
    if expected_error:
        with pytest.raises(PayFortException) as exc_info:
            verify_response_format(modified_response)
        assert expected_error in str(exc_info.value), f'Failed case: {usecase}'
    else:
        verify_response_format(modified_response)  # Should not raise


@pytest.mark.parametrize(
    'data_override, expected_exception, expected_message',
    [
        (
            {},
            PayFortBadSignatureException,
            'Signature not found',
        ),
        (
            {'signature': 'wrong-signature'},
            PayFortBadSignatureException,
            'Response signature mismatch',
        ),
        (
            {'signature': 'whatever'},
            PayFortException,
            'Unsupported SHA method',
        ),
    ]
)
def test_verify_signature_exceptions(data_override, expected_exception, expected_message):
    """
    Test that verify_signature raises the appropriate exceptions when the
    signature is missing, incorrect, or when an unsupported SHA method is used.
    """
    data = {
        'amount': '1000',
        'currency': 'NZD',
        'command': 'PURCHASE',
        'merchant_reference': '1234-5678'
    }
    data.update(data_override)

    if expected_message == 'Unsupported SHA method':
        sha_method = 'unsupported'
    else:
        sha_method = 'SHA-256'

    with pytest.raises(expected_exception) as exc_info:
        verify_signature('secret', sha_method, data)
    assert expected_message in str(exc_info.value)


def test_verify_signature_valid():
    """
    Test that verify_signature succeeds without exception when provided
    with a valid signature and correct data.
    """
    data = {
        'amount': '1000',
        'currency': 'NZD',
        'command': 'PURCHASE',
        'merchant_reference': '1234-5678'
    }
    data['signature'] = get_signature('secret', 'SHA-256', data)
    verify_signature('secret', 'SHA-256', data)
