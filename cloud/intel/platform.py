"""The platform container: one object that owns the store, queue, storage and
every track's service, built once per process (API or worker).

Services are resolved lazily by name from :data:`SERVICES`, so a track's module
is imported only when first used — the API process never imports the crawler
bridge, Playwright or a provider SDK it does not need.

    platform = Platform.from_settings(settings)
    crm = platform.service("crm")          # cloud.intel.crm.service.CrmService(platform)
"""

from __future__ import annotations

import importlib
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from cloud.intel.store.base import Store
from cloud.intel.tasks.service import TaskService

__all__ = ["Platform", "PlatformConfig", "SERVICES"]

log = logging.getLogger(__name__)

#: name -> "module:Class". Each class is constructed as ``Class(platform)``.
SERVICES: Dict[str, str] = {
    # Track A: CRM + internal data engine
    "crm": "cloud.intel.crm.service:CrmService",
    "imports": "cloud.intel.imports.service:ImportService",
    "dedupe": "cloud.intel.imports.dedupe:CompanyResolver",
    "exports": "cloud.intel.exports.service:ExportService",
    # Track B/C/D + technology + monitoring
    "discovery": "cloud.intel.discovery.service:DiscoveryService",
    "jobs": "cloud.intel.jobs.service:JobIntelService",
    "signals": "cloud.intel.signals.service:SignalService",
    "technology": "cloud.intel.technology.service:TechnologyService",
    "monitoring": "cloud.intel.monitoring.service:MonitoringService",
    # Track E/G/H: sources, providers, credits, email, contacts
    "sources": "cloud.intel.sources.service:SourceService",
    "providers": "cloud.intel.providers.registry:ProviderRegistry",
    "credits": "cloud.intel.providers.credits:CreditLedger",
    "email": "cloud.intel.email.service:EmailValidationService",
    "contacts": "cloud.intel.providers.contacts:ContactIntelService",
    # Track F/J + AI
    "ai": "cloud.intel.ai.registry:AIRegistry",
    "scraper": "cloud.intel.scraper.service:ScraperService",
    "research": "cloud.intel.research.service:ResearchService",
    # Track I/K
    "campaigns": "cloud.intel.gtm.service:CampaignService",
    "sequences": "cloud.intel.gtm.sequences:SequenceService",
    "automation": "cloud.intel.automation.engine:AutomationEngine",
    "analytics": "cloud.intel.analytics.service:AnalyticsService",
    # AI Control Room
    "agent": "cloud.intel.agent.service:AgentService",
    "agent_memory": "cloud.intel.agent.memory:MemoryService",
    "insights": "cloud.intel.agent.insights:InsightService",
    # SANA GTM completion (migration 0007)
    "email_jobs": "cloud.intel.email.jobs:EmailValidationJobService",
    "mailboxes": "cloud.intel.sending.mailboxes:MailboxService",
    "outbox": "cloud.intel.sending.outbox:OutboxService",
    "events": "cloud.intel.sending.events:EventService",
    "suppression": "cloud.intel.gtm.suppression:SuppressionService",
    "scoring": "cloud.intel.scoring.service:ScoringService",
    "reports": "cloud.intel.analytics.reports:ReportService",
    "admin": "cloud.intel.admin.service:AdminService",
    "notifications": "cloud.intel.admin.notifications:NotificationService",
    "integrations": "cloud.intel.integrations.service:IntegrationService",
    "internal_data": "cloud.intel.imports.internal:InternalDataService",
    "gtm_bridge": "cloud.intel.gtm.bridge:GtmBridgeService",
    "enrichment": "cloud.intel.providers.enrichment:EnrichmentService",
    # Job source monitors (migration 0011)
    "job_monitors": "cloud.intel.job_monitor.service:JobMonitorService",
    "job_imports": "cloud.intel.job_monitor.importer:JobImportService",
    # Signal outcomes / contact snapshots (migration 0013)
    "signal_outcomes": "cloud.intel.signals.outcomes:SignalOutcomeService",
    # Jobs CSV export (migration 0014)
    "job_exports": "cloud.intel.job_monitor.exports:JobExportService",
}


@dataclass
class PlatformConfig:
    environment: str = "development"
    #: Where local result files go (exports, scraper output) in development.
    files_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parents[1] / ".localdev" / "platform-files")
    #: Fernet key (urlsafe base64, 32 bytes) for provider secrets at rest.
    secrets_key: Optional[str] = None
    #: Outbound email is never sent unless this is true AND environment == "production".
    allow_email_sending: bool = False
    #: Paid provider calls always need an explicit task/action; this caps one task.
    max_credits_per_task: float = 500.0
    #: AI provider defaults (server-side only; never exposed to the browser).
    ai_provider: str = "rules"
    ai_model: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


class Platform:
    def __init__(self, store: Store, *, queue: Any = None, storage: Any = None,
                 config: Optional[PlatformConfig] = None) -> None:
        self.store = store
        self.queue = queue
        self.config = config or PlatformConfig()
        if storage is None:
            from cloud.shared.storage import LocalFileStorage

            storage = LocalFileStorage(self.config.files_dir)
        self.storage = storage
        self.tasks = TaskService(store, queue=queue)
        self._services: Dict[str, Any] = {}
        self._overrides: Dict[str, Any] = {}
        self._lock = threading.RLock()

    def service(self, name: str) -> Any:
        with self._lock:
            if name in self._overrides:
                return self._overrides[name]
            if name not in self._services:
                try:
                    target = SERVICES[name]
                except KeyError:
                    raise KeyError(f"unknown platform service {name!r}") from None
                module_name, _, class_name = target.partition(":")
                cls = getattr(importlib.import_module(module_name), class_name)
                self._services[name] = cls(self)
            return self._services[name]

    def override(self, name: str, instance: Any) -> None:
        """Replace a service (tests; or wiring a real provider at startup)."""
        with self._lock:
            self._overrides[name] = instance

    def close(self) -> None:
        for service in list(self._services.values()):
            close = getattr(service, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    log.exception("closing a platform service failed")
        self.store.close()
