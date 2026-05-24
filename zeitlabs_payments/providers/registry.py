"""Processors registry"""
from importlib.metadata import entry_points as _entry_points

from .base import BaseProcessor

entry_points = _entry_points

PROCESSORS: dict[str, type[BaseProcessor]] = {}


def load_entrypoint_processors() -> None:
    """
    Discover and register processors defined via entry_points.
    """
    for ep in entry_points(group='zeitlabs_payments.v1'):
        cls = ep.load()
        slug = getattr(cls, 'SLUG', None)
        if not slug:
            raise ValueError(f"Processor {cls.__name__} from entry point '{ep.name}' must define a SLUG")
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
