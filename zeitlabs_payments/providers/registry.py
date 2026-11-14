"""Processors registry"""
import logging
import re
from importlib.metadata import entry_points
from typing import Dict, List, Type

from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest

from .base import BaseProcessor

logger = logging.getLogger(__name__)

PROCESSORS: Dict[str, Type[BaseProcessor]] = {}

# A processor's slug names it everywhere it is recorded, so it is checked once here rather than at every writer.
# Lowercase alphanumerics and underscores, matching the shape audit action names take, and short enough for the
# gateway columns it is written into (Transaction, WebhookEvent and AuditLog all hold 50 characters).
SLUG_PATTERN = re.compile(r'^[a-z0-9_]+$')
SLUG_MAX_LENGTH = 50


def missing_hand_off_contract(cls: Type[BaseProcessor]) -> List[str]:
    """
    Return what the processor lacks of the hand-off contract, or nothing when it meets it.

    Every payment is found again through the gateway order recorded at hand-off, and resumed on the processor's waiting
    page, which polls its status view. So a processor records its order (``get_gateway_order_id``), and names its
    return view, its status view and the parameter its gateway's redirect carries the order id in.
    """
    missing = [
        attribute for attribute in ('RETURN_VIEW', 'STATUS_VIEW', 'GATEWAY_ORDER_ID_PARAM')
        if not getattr(cls, attribute, None)
    ]
    if getattr(cls, 'get_gateway_order_id', None) is BaseProcessor.get_gateway_order_id:
        missing.insert(0, 'get_gateway_order_id')
    return missing


def load_entrypoint_processors() -> None:
    """
    Discover and register processors defined via entry_points.

    Registration follows two rules: a processor that cannot be loaded (import error, missing or malformed SLUG, a
    hand-off contract it does not meet, or any other failure at registration) is logged and skipped so one broken
    processor cannot stop the LMS from booting; a duplicate slug is a developer error and raises
    ``ImproperlyConfigured`` so the conflict is fixed rather than silently overwriting the first registration.

    The slug rule is enforced here because this is the boundary the name enters through. Everything downstream
    reads it off a registered processor, so a malformed name costs one processor at boot, with a logged reason,
    rather than an exception raised inside an ``except`` block on the payment path.
    """
    for ep in entry_points(group='zeitlabs_payments.v1'):
        try:
            cls = ep.load()
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("Failed to load processor from entry point '%s'; skipping.", ep.name)
            continue

        slug = getattr(cls, 'SLUG', None)
        if not slug:
            logger.error("Processor %s from entry point '%s' must define a SLUG; skipping.", cls.__name__, ep.name)
            continue

        declared_version = getattr(cls, 'contract_version', None)
        if declared_version != BaseProcessor.contract_version:
            logger.error(
                'Processor %s from entry point "%s" declares contract version %r but the plugin '
                'implements %r; skipping.',
                cls.__name__, ep.name, declared_version, BaseProcessor.contract_version,
            )
            continue

        missing = missing_hand_off_contract(cls)
        if missing:
            logger.error(
                'Processor %s from entry point "%s" does not meet the hand-off contract, lacking %s; skipping.',
                cls.__name__, ep.name, ', '.join(missing),
            )
            continue

        if len(slug) > SLUG_MAX_LENGTH or not SLUG_PATTERN.match(slug):
            logger.error(
                'Processor %s from entry point "%s" declares SLUG %r, which must be at most %d characters of '
                'lowercase alphanumerics and underscores; skipping.',
                cls.__name__, ep.name, slug, SLUG_MAX_LENGTH,
            )
            continue

        if slug in PROCESSORS:
            raise ImproperlyConfigured(
                f'Duplicate processor slug {slug!r}: both '
                f'{PROCESSORS[slug].__module__}.{PROCESSORS[slug].__name__} and '
                f'{cls.__module__}.{cls.__name__} register it.'
            )
        PROCESSORS[slug] = cls


def get_processor(slug: str) -> BaseProcessor:
    """
    Return an *instance* of the processor that matches `slug`
    or raise ValueError if unknown.
    """
    try:
        return PROCESSORS[slug]()
    except KeyError as exc:
        raise ValueError(f'Unsupported payment provider: {slug}, Original error: {exc}') from exc


def available_processors(request: HttpRequest) -> List[BaseProcessor]:
    """
    Return instances of every registered processor that is not hidden for the given request.

    The hide decision is made by ``BaseProcessor.is_hidden_for``, which each processor overrides; this function is
    the single consumer-facing entry point so the decision lives with the processor rather than in presentation
    code. A processor that fails to instantiate (for example because it is not configured for the current site)
    is logged and skipped so the checkout page keeps working without it.
    """
    available = []
    for processor_class in PROCESSORS.values():
        try:
            processor = processor_class()
            hidden = processor.is_hidden_for(request)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception('Failed to prepare processor %s; skipping.', processor_class.__name__)
            continue
        if not hidden:
            available.append(processor)
    return available
