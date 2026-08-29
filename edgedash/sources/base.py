"""
Source protocol and registry.

Every job-board connector must implement the Source protocol:
    name: str                      — unique key used in the registry
    fetch(config) -> list[dict]    — returns normalised rows

Each row must contain exactly these keys:
    source, external_id, title, company, location, url,
    description, posted_at, raw

Missing values must be None — never an empty string, never "N/A".

Adding a new source requires only:
    1. Create a class that satisfies the Source protocol.
    2. Decorate it with @register.
    No changes to the Fetcher or any other module.
"""

from __future__ import annotations

from typing import Callable, Protocol, Type, runtime_checkable

from edgedash.config import Config

# ---------------------------------------------------------------------------
# Required keys for every normalised row
# ---------------------------------------------------------------------------
NORMALISED_KEYS: tuple[str, ...] = (
    "source",
    "external_id",
    "title",
    "company",
    "location",
    "url",
    "description",
    "posted_at",
    "raw",
)


# ---------------------------------------------------------------------------
# Protocol — satisfied structurally; no ABC inheritance needed
# ---------------------------------------------------------------------------

@runtime_checkable
class Source(Protocol):
    """Structural protocol every source must satisfy."""

    name: str

    def fetch(self, config: Config) -> list[dict]:
        ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SOURCES: dict[str, type] = {}


def register(cls: type) -> type:
    """Class decorator that adds a source to the global registry.

    Usage::

        @register
        class MySource:
            name = "my_source"
            def fetch(self, config): ...
    """
    if not hasattr(cls, "name") or not isinstance(cls.name, str):
        raise TypeError(
            f"@register requires the class to have a string 'name' attribute. "
            f"Got: {cls}"
        )
    SOURCES[cls.name] = cls
    return cls
