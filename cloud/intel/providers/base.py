"""Provider interfaces: enrichment and technology.

Every paid provider implements :class:`EnrichmentProvider`. Its methods take
``allow_paid``; a method that would spend credits raises :class:`PaidCallRefused`
when it is false — it never silently skips, so a caller always knows whether
data is missing because nothing exists or because spending was not authorised.

Providers never hold another workspace's credentials: a connector is built per
workspace by :class:`~cloud.intel.providers.registry.ProviderRegistry` from that
workspace's encrypted ``provider_connections`` row.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Mapping, Optional, Sequence

__all__ = [
    "EnrichmentProvider",
    "PaidCallRefused",
    "ProviderNotConfigured",
    "ProviderError",
    "TechnologyProvider",
]


class ProviderError(RuntimeError):
    """A provider call failed (network, auth, entitlement, bad response)."""


class ProviderNotConfigured(ProviderError):
    """The workspace has not connected this provider (no credentials)."""


class PaidCallRefused(ProviderError):
    """A credit-consuming call was needed but ``allow_paid`` was false."""


class EnrichmentProvider(ABC):
    """Company/contact enrichment behind one interface (ZoomInfo, Seamless, …)."""

    name: str = "provider"
    #: "api" | "browser_login" | "public" | "partner"
    access_method: str = "api"

    @abstractmethod
    def health(self) -> Dict[str, Any]:
        """``{"status": "ok|not_configured|blocked|error", "detail": str}``. May make one cheap call."""

    @abstractmethod
    def estimate_cost(self, operation: str, n: int) -> float:
        """Credits ``n`` units of ``operation`` would consume (0 for free operations)."""

    def search_companies(self, filters: Mapping[str, Any], *, limit: int = 25, allow_paid: bool = False
                         ) -> List[Dict[str, Any]]:
        raise ProviderError(f"{self.name} does not support company search")

    def enrich_company(self, identifiers: Mapping[str, Any], *, allow_paid: bool = False) -> Optional[Dict[str, Any]]:
        raise ProviderError(f"{self.name} does not support company enrichment")

    def search_contacts(self, filters: Mapping[str, Any], *, limit: int = 25, allow_paid: bool = False
                        ) -> List[Dict[str, Any]]:
        raise ProviderError(f"{self.name} does not support contact search")

    def enrich_contacts(self, refs: Sequence[Mapping[str, Any]], *, allow_paid: bool = False
                        ) -> List[Dict[str, Any]]:
        raise ProviderError(f"{self.name} does not support contact enrichment")

    def credit_balance(self) -> Optional[float]:
        """The balance the provider last reported, if it reports one."""
        return None


class TechnologyProvider(ABC):
    """Technology install-base lookups (e.g. which companies run JD Edwards)."""

    name: str = "technology"

    @abstractmethod
    def search_technologies(self, query: str, *, limit: int = 25) -> List[Dict[str, Any]]:
        """Catalogue lookup: ``[{"technology","category","vendor","provider_id"}]``."""

    @abstractmethod
    def companies_using(self, technology_ids: Sequence[str], filters: Mapping[str, Any], *, limit: int = 25,
                        allow_paid: bool = False) -> List[Dict[str, Any]]:
        """Companies with the technology installed, each carrying source evidence."""
