"""Lazy, chainable query wrappers over the Istari v2 ``list_*`` endpoints.

The two classes here give every ``client.list_<entity>`` method a uniform,
fluent surface:

    platform.systems()
    platform.systems().filter(archive_status="active").sort("-created").first()
    platform.resources().type("model").filter(file_name="report.pdf").take(5)

Nothing hits the network until you iterate, take a slice, or ask for a
count.  The classes are immutable: every ``filter``/``sort``/``type`` call
returns a fresh query, so the same base query can be forked and reused.
Pagination walks full result sets via the v2 client's ``Page.iter_items()``;
see ``fluent/python-client-usage.md`` for how wired pages fetch subsequent
pages.

This module is unaware of any specific entity type: ``ItemQuery`` works
against any v2 paginated list method whose response page exposes the
SDK's ``iter_items()`` helper.  Domain knowledge lives in the factory
methods on :class:`istari_fluent.IstariPlatform`.
"""

from __future__ import annotations

import itertools
from typing import Any, Callable, Generic, Iterator, Optional, TypeVar

from istari_digital_client.v2.models.resource_search_item import ResourceSearchItem
from istari_digital_client.v2.models.resource_type import ResourceType


T = TypeVar("T")

DEFAULT_PAGE_SIZE = 100
"""Page size used by :meth:`ItemQuery.__iter__` when the caller hasn't
chosen one.  100 is the maximum allowed by the v2 list endpoints, which
minimises the number of round-trips needed to walk a large result set.
Override per query with ``q.filter(size=...)``."""


class ItemQuery(Generic[T]):
    """Fluent, lazy wrapper over any v2 paginated list method.

    The query is **immutable**: every builder method (:meth:`filter`,
    :meth:`sort`) returns a *new* ``ItemQuery``, so a base query can be
    safely shared and forked.

    Iteration is **lazy** and walks pages transparently via the SDK's
    built-in ``iter_items()`` on the page response.  Terminals
    (:meth:`first`, :meth:`take`, :meth:`all`, :meth:`count`,
    :meth:`__len__`) only fetch what they need.

    Example::

        from istari_fluent import IstariPlatform

        platform = IstariPlatform.from_env()

        for system in platform.systems().sort("-created"):
            print(system.name, system.id)

        first_active = platform.systems().filter(archive_status="active").first()
        recent_five  = platform.systems().sort("-created").take(5)
        total        = platform.systems().count()
    """

    def __init__(self, list_fn: Callable[..., Any], **filters: Any) -> None:
        self._list_fn = list_fn
        self._filters: dict[str, Any] = dict(filters)

    # ----- Fluent builders (always return a NEW instance) ------------------

    def filter(self, **kwargs: Any) -> "ItemQuery[T]":
        """Return a new query with extra filter parameters merged in.

        Keyword arguments are forwarded straight to the underlying v2 list
        method, so any parameter that ``list_fn`` accepts works here
        (``archive_status``, ``filter_by``, ``status_name``, ``size``, ...).
        Re-using a key on an already-filtered query overrides the previous
        value.
        """
        return type(self)(self._list_fn, **{**self._filters, **kwargs})

    def sort(self, field: str) -> "ItemQuery[T]":
        """Return a new query sorted by ``field``.

        Prefix with ``-`` for descending order (e.g. ``sort("-created")``).
        """
        return self.filter(sort=field)

    # ----- Core iteration primitive ---------------------------------------

    def __iter__(self) -> Iterator[T]:
        """Yield every matching item, walking pages transparently.

        Defaults to the maximum page size (:data:`DEFAULT_PAGE_SIZE`) when
        the caller hasn't picked one, to minimise round-trips on long result
        sets.  Override with ``q.filter(size=N)``.
        """
        kwargs = {"size": DEFAULT_PAGE_SIZE, **self._filters}
        first_page = self._list_fn(**kwargs)
        yield from first_page.iter_items()

    # ----- Terminal helpers (all build on __iter__) ------------------------

    def first(self) -> Optional[T]:
        """Return the first matching item, or ``None`` when the query is empty.

        Stops iterating as soon as a hit lands, so at most one page is fetched.
        """
        return next(iter(self), None)

    def take(self, n: int) -> list[T]:
        """Return up to ``n`` items, fetching only the pages needed."""
        return list(itertools.islice(self, n))

    def all(self) -> list[T]:
        """Materialise every matching item into a list.

        Convenient for small result sets and tests; on a large tenant prefer
        streaming with ``for item in query:``.
        """
        return list(self)

    def count(self) -> int:
        """Return the total number of matching items.

        Issues a single page-1 request with ``size=1`` and reads
        ``page.total`` from the response &mdash; never iterates the data.
        """
        kwargs = {**self._filters, "page": 1, "size": 1}
        return self._list_fn(**kwargs).total

    def __len__(self) -> int:
        return self.count()

    def __repr__(self) -> str:
        filters_str = ", ".join(f"{k}={v!r}" for k, v in self._filters.items())
        fn_name = getattr(self._list_fn, "__name__", repr(self._list_fn))
        return f"{type(self).__name__}({fn_name}({filters_str}))"


class ResourceQuery(ItemQuery[ResourceSearchItem]):
    """A :class:`ItemQuery` bound to ``client.list_resources``.

    Adds resource-type sugar so the cookbook idiom

        platform.resources().type("model").filter(display_name="MQ-99").first()

    reads naturally.  All :class:`ItemQuery` builders/terminals work
    unchanged; ``filter()`` accepts every kwarg the v2 ``list_resources``
    endpoint takes (``file_name``, ``external_identifier``, ``mime_type``,
    ``archive_status``, ``access_type``, ...).
    """

    def type(self, type_name: str | ResourceType) -> "ResourceQuery":
        """Restrict the query to a single resource type.

        ``type_name`` accepts either the enum value or a string such as
        ``"model"``, ``"artifact"``, or ``"document"``.
        **File-backed uploads** (standalone files) list as **``artifact``** in
        v2; ``"resource"`` is a catch-all enum value in the API but maps to
        artifacts for typical file rows. **Jobs** are listed with
        ``type_name=job`` when using this endpoint, but load them with
        :meth:`IstariPlatform.get_job` — not :meth:`IstariPlatform.get_resource`.

        Currently re-applying ``type()`` overrides the previous selection.
        Pass a list explicitly via ``filter(type_name=[...])`` to query
        across several types in one call.
        """
        rt = ResourceType(type_name) if isinstance(type_name, str) else type_name
        return self.filter(type_name=[rt])
