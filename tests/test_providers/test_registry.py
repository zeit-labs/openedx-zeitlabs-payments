"""Test processor registry"""
import pytest

from zeitlabs_payments.providers.base import BaseProcessor
from zeitlabs_payments.providers.payfort.processor import PayFort
from zeitlabs_payments.providers.registry import get_processor


def test_get_processor_returns_instance_for_known_slug():
    processor = get_processor('payfort')
    assert isinstance(processor, PayFort)
    assert isinstance(processor, BaseProcessor)


def test_get_processor_raises_value_error_for_unknown_slug():
    with pytest.raises(ValueError) as exc_info:
        get_processor('unknown-slug')
    assert 'Unsupported payment provider' in str(exc_info.value)
