"""Test processor registry"""
import logging
import types
from unittest.mock import MagicMock, patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest

from test_utils.dummy_processor import DummyProcessor
from zeitlabs_payments.providers import registry
from zeitlabs_payments.providers.base import BaseProcessor
from zeitlabs_payments.providers.registry import (
    PROCESSORS,
    SLUG_MAX_LENGTH,
    available_processors,
    get_processor,
    load_entrypoint_processors,
)


def make_entry_point(name, cls):
    """Helper to create a fake entry point."""
    ep = types.SimpleNamespace()
    ep.name = name
    ep.value = f'{cls.__module__}:{cls.__name__}'
    ep.load = lambda: cls
    return ep


@patch('zeitlabs_payments.providers.registry.PROCESSORS', new={})
def test_loads_valid_processor(monkeypatch):
    """Verify that a valid entry point is registered under its slug."""
    ep = make_entry_point('dummy', DummyProcessor)
    monkeypatch.setattr(registry, 'entry_points', lambda group: [ep])
    load_entrypoint_processors()
    assert 'dummy' in PROCESSORS
    assert PROCESSORS['dummy'] is DummyProcessor


@patch('zeitlabs_payments.providers.registry.PROCESSORS', new={})
def test_skips_processor_without_slug(monkeypatch, caplog):
    """Verify that a processor without a SLUG is skipped instead of stopping LMS boot, and an error is logged."""
    class NoSlugProcessor:
        pass

    ep = make_entry_point('noslug', NoSlugProcessor)
    monkeypatch.setattr(registry, 'entry_points', lambda group: [ep])
    with caplog.at_level(logging.ERROR):
        load_entrypoint_processors()
    assert 'noslug' not in PROCESSORS
    assert 'must define a SLUG' in caplog.text


@patch('zeitlabs_payments.providers.registry.PROCESSORS', new={})
def test_skips_processor_with_contract_version_mismatch(monkeypatch, caplog):
    """Verify that a processor declaring an incompatible contract version is skipped, and an error is logged."""
    class MismatchedVersionProcessor(DummyProcessor):
        contract_version = 999

    ep = make_entry_point('mismatch', MismatchedVersionProcessor)
    monkeypatch.setattr(registry, 'entry_points', lambda group: [ep])
    with caplog.at_level(logging.ERROR):
        load_entrypoint_processors()
    assert 'mismatch' not in PROCESSORS
    assert 'declares contract version 999' in caplog.text
    assert 'implements 1' in caplog.text


@patch('zeitlabs_payments.providers.registry.PROCESSORS', new={})
def test_skips_processor_with_contract_version_attribute_missing(monkeypatch, caplog):
    """Verify that an old processor without the contract_version attribute is skipped, and an error is logged."""
    class ProcessorWithoutContractVersion:
        SLUG = 'ancient-interface'

    ep = make_entry_point('ancient', ProcessorWithoutContractVersion)
    monkeypatch.setattr(registry, 'entry_points', lambda group: [ep])
    with caplog.at_level(logging.ERROR):
        load_entrypoint_processors()
    assert 'ancient' not in PROCESSORS
    assert 'declares contract version None' in caplog.text


class NamesNoOrder(DummyProcessor):
    """A processor that leaves get_gateway_order_id to the base class, so it never names its order."""

    SLUG = 'incomplete'
    get_gateway_order_id = BaseProcessor.get_gateway_order_id


@pytest.mark.parametrize('processor, missing', [
    (NamesNoOrder, 'get_gateway_order_id'),
    (type('NoReturnView', (DummyProcessor,), {'SLUG': 'incomplete', 'RETURN_VIEW': None}), 'RETURN_VIEW'),
    (type('NoStatusView', (DummyProcessor,), {'SLUG': 'incomplete', 'STATUS_VIEW': None}), 'STATUS_VIEW'),
    (
        type('NoOrderParam', (DummyProcessor,), {'SLUG': 'incomplete', 'GATEWAY_ORDER_ID_PARAM': None}),
        'GATEWAY_ORDER_ID_PARAM',
    ),
])
def test_skips_processor_not_meeting_the_hand_off_contract(monkeypatch, caplog, processor, missing):
    """
    Verify that a processor that does not record its gateway order at hand-off, or names no waiting page, status view
    or order parameter, is skipped with an error naming what it lacks, and the LMS starts all the same.

    Every payment is resumed through its recorded order and its waiting page, so a processor without them could take
    payments nothing could ever look for again.
    """
    ep = make_entry_point('incomplete', processor)
    monkeypatch.setattr(registry, 'PROCESSORS', {})
    monkeypatch.setattr(registry, 'entry_points', lambda group: [ep, make_entry_point('dummy', DummyProcessor)])

    with caplog.at_level(logging.ERROR):
        load_entrypoint_processors()

    assert set(registry.PROCESSORS) == {'dummy'}, f'A processor without {missing} was registered, or stopped the others'
    assert missing in caplog.text, f'The error does not name {missing}'


@pytest.mark.parametrize('slug, usecase', [
    ('HyperPay', 'upper-case letters'),
    ('hyper pay', 'an inner space'),
    ('hyper-pay', 'a hyphen'),
    ('hyperpay!', 'punctuation'),
    ('hyperpay.mada', 'a dot'),
    (' hyperpay', 'a leading space'),
    ('x' * (SLUG_MAX_LENGTH + 1), 'a name too long for the gateway columns'),
])
@patch('zeitlabs_payments.providers.registry.PROCESSORS', new={})
def test_skips_processor_with_malformed_slug(monkeypatch, caplog, slug, usecase):
    """
    Verify that a processor whose slug is not a usable name is skipped, and an error names it.

    The slug is what every record of a payment is attributed to, so it is checked once at the boundary it
    enters through. One processor is lost, the reason is in the log, and the site still boots -- rather than the
    name being discovered wrong later, from inside an except block on the payment path.
    """
    class MalformedSlugProcessor(DummyProcessor):
        SLUG = slug

    ep = make_entry_point('malformed', MalformedSlugProcessor)
    monkeypatch.setattr(registry, 'entry_points', lambda group: [ep])

    with caplog.at_level(logging.ERROR):
        load_entrypoint_processors()

    assert not registry.PROCESSORS, f'A processor named with {usecase} should not be registered'
    assert repr(slug) in caplog.text, 'The log should name the slug it refused'
    assert 'lowercase alphanumerics and underscores' in caplog.text, 'The log should say what a slug may be'


@pytest.mark.parametrize('slug', ['dummy', 'hyperpay', 'hyperpay_mada', 'tabadul', 'x' * SLUG_MAX_LENGTH])
@patch('zeitlabs_payments.providers.registry.PROCESSORS', new={})
def test_registers_processor_with_a_well_formed_slug(monkeypatch, slug):
    """Verify that every slug shape a real processor uses still registers."""
    class WellNamedProcessor(DummyProcessor):
        SLUG = slug

    ep = make_entry_point('well-named', WellNamedProcessor)
    monkeypatch.setattr(registry, 'entry_points', lambda group: [ep])

    load_entrypoint_processors()

    assert registry.PROCESSORS[slug] is WellNamedProcessor, f'{slug!r} should be a registrable name'


@patch('zeitlabs_payments.providers.registry.PROCESSORS', new={})
def test_raises_on_duplicate_slug(monkeypatch):
    """Verify that a duplicate slug raises ImproperlyConfigured instead of overwriting."""
    class DuplicateDummyProcessor(DummyProcessor):
        pass

    eps = [
        make_entry_point('first', DummyProcessor),
        make_entry_point('second', DuplicateDummyProcessor),
    ]
    monkeypatch.setattr(registry, 'entry_points', lambda group: eps)
    with pytest.raises(ImproperlyConfigured, match='Duplicate processor slug'):
        load_entrypoint_processors()


@patch('zeitlabs_payments.providers.registry.PROCESSORS', new={})
def test_skips_processor_that_fails_to_load(monkeypatch, caplog):
    """Verify that an entry point that raises on load is skipped, and the failure is logged."""

    def broken_load():
        raise ImportError('broken processor package')

    ep = types.SimpleNamespace()
    ep.name = 'broken'
    ep.value = 'broken.module:BrokenProcessor'
    ep.load = broken_load
    monkeypatch.setattr(registry, 'entry_points', lambda group: [ep])
    with caplog.at_level(logging.ERROR):
        load_entrypoint_processors()
    assert 'broken' not in PROCESSORS
    assert 'Failed to load processor from entry point' in caplog.text


def test_available_processors_returns_all_when_none_hidden(monkeypatch):
    """Verify that available_processors returns every registered processor when none is hidden."""
    monkeypatch.setattr('zeitlabs_payments.providers.registry.PROCESSORS', {'dummy': DummyProcessor})
    request = MagicMock(spec=HttpRequest)
    available = available_processors(request)
    assert [processor.SLUG for processor in available] == ['dummy']


def test_available_processors_filters_hidden_processors(monkeypatch):
    """Verify that available_processors excludes processors whose is_hidden_for returns True."""
    class HiddenProcessor(DummyProcessor):
        SLUG = 'hidden'

        def is_hidden_for(self, request: HttpRequest) -> bool:
            return True

    monkeypatch.setattr(
        'zeitlabs_payments.providers.registry.PROCESSORS',
        {'dummy': DummyProcessor, 'hidden': HiddenProcessor},
    )
    request = MagicMock(spec=HttpRequest)
    available = available_processors(request)
    assert [processor.SLUG for processor in available] == ['dummy']


def test_available_processors_skips_processor_that_fails_to_instantiate(monkeypatch, caplog):
    """Verify that a broken processor is skipped without breaking checkout, and the failure is logged."""
    class BrokenProcessor(DummyProcessor):
        SLUG = 'broken'

        def __init__(self) -> None:
            raise KeyError('missing processor settings')

    monkeypatch.setattr(
        'zeitlabs_payments.providers.registry.PROCESSORS',
        {'dummy': DummyProcessor, 'broken': BrokenProcessor},
    )
    request = MagicMock(spec=HttpRequest)
    with caplog.at_level(logging.ERROR):
        available = available_processors(request)
    assert [processor.SLUG for processor in available] == ['dummy']
    assert 'Failed to prepare processor BrokenProcessor' in caplog.text


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
