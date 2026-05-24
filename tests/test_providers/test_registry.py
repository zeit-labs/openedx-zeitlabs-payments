"""Test processor registry"""
import types
from unittest.mock import patch

import pytest

from test_utils.dummy_processor import DummyProcessor
from zeitlabs_payments.providers import registry as registry_module
from zeitlabs_payments.providers.registry import PROCESSORS, get_processor, load_entrypoint_processors


def make_entry_point(name, cls):
    """Helper to create a fake entry point."""
    ep = types.SimpleNamespace()
    ep.name = name
    ep.value = f'{cls.__module__}:{cls.__name__}'
    ep.load = lambda: cls
    return ep


@patch('zeitlabs_payments.providers.registry.PROCESSORS', new={})
def test_loads_valid_processor(monkeypatch):
    ep = make_entry_point('dummy', DummyProcessor)
    monkeypatch.setattr(registry_module, 'entry_points', lambda group: [ep])
    load_entrypoint_processors()
    assert 'dummy' in PROCESSORS
    assert PROCESSORS['dummy'] is DummyProcessor


@patch('zeitlabs_payments.providers.registry.PROCESSORS', new={})
def test_raises_if_no_slug(monkeypatch):
    class NoSlugProcessor:
        pass

    ep = make_entry_point('noslug', NoSlugProcessor)
    monkeypatch.setattr(registry_module, 'entry_points', lambda group: [ep])
    with pytest.raises(ValueError, match='must define a SLUG'):
        load_entrypoint_processors()


def test_get_processor_returns_instance_for_known_slug():
    processor = get_processor('dummy')
    assert isinstance(processor, DummyProcessor)


def test_get_processor_raises_value_error_for_unknown_slug():
    with pytest.raises(ValueError) as exc_info:
        get_processor('unknown-slug')
    assert 'Unsupported payment provider' in str(exc_info.value)


def test_processor_is_registered_once():
    assert 'dummy' in PROCESSORS
    assert PROCESSORS['dummy'] is DummyProcessor
