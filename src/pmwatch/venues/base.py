"""Abstract venue client interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import BookSnapshot


class VenueError(Exception):
    """Raised when a venue cannot serve a request (network, auth, shape)."""


class VenueClient(ABC):
    """Read-only market data source."""

    venue: str = ""

    @abstractmethod
    def list_markets(self) -> list[dict]:
        """Return venue-native metadata for markets this client can serve."""

    @abstractmethod
    def get_book(self, market_id: str) -> BookSnapshot:
        """Return the current book snapshot for ``market_id``.

        Implementations must raise :class:`VenueError` (or a subclass) with a
        message that identifies the venue and the failing market.
        """
