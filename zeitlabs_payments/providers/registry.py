"""Processors registry"""
from typing import Dict, Type

import pkg_resources

from .base import BaseProcessor
from .manual_payment.processor import ManualPaymentProcessor

from zeitlabs_payments.helpers import get_settings

PROCESSORS: Dict[str, Type[BaseProcessor]] = {}

def load_entrypoint_processors() -> None:
    """
    Discover and register processors defined via entry_points.
    """
    for ep in pkg_resources.iter_entry_points(group='zeitlabs_payments.v1'):
        cls = ep.load()
        slug = getattr(cls, 'SLUG', None)
        if not slug:
            raise ValueError(f"Processor {cls.__name__} from entry point '{ep.name}' must define a SLUG")
        if slug == ManualPaymentProcessor.SLUG and not get_settings().is_manual_payment_enabled:
            continue
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
