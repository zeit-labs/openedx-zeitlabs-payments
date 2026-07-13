"""Helpers for reading raw template source from tests."""
from pathlib import Path

TEMPLATES_DIR = (
    Path(__file__).resolve().parent.parent /
    'zeitlabs_payments' /
    'templates' /
    'zeitlabs_payments'
)


def template_source(name):  # type: ignore[no-untyped-def]
    """Return the raw source of a bundle template for tests."""
    return (TEMPLATES_DIR / name).read_text(encoding='utf-8')
