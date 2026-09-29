"""Scraper templates: a reusable instruction + (edited) schema + run options.

Built-in templates are part of the code (id ``builtin:<slug>``, read-only; duplicate
one to change it). Saved templates live in ``scrape_templates``, per workspace,
with unique names.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional

from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, NotFoundError, ValidationError

__all__ = ["BUILTIN_TEMPLATES", "TemplateStore"]

BUILTIN_TEMPLATES: List[Dict[str, Any]] = [
    {"slug": "company-research", "name": "Company Research", "category": "company",
     "description": "Who the company is: name, website, industry, location, size, description and profiles.",
     "instruction": "Get company name, website, industry, location, employee count, description, LinkedIn URL and "
                    "contact page.", "options": {}},
    {"slug": "company-website", "name": "Company + Website", "category": "company",
     "description": "Just the company's name and website, normalised.",
     "instruction": "Get company name and website.", "options": {}},
    {"slug": "company-jobs", "name": "Company + Jobs", "category": "jobs",
     "description": "The company and every open job with its URL, location and posting date.",
     "instruction": "Get company name, website, job titles, job URLs, location and posted date.",
     "options": {"pagination": True}},
    {"slug": "company-erp", "name": "Company + ERP Technology", "category": "technology",
     "description": "ERP and other technology the company's pages name.",
     "instruction": "Get company name, website, ERP, cloud provider and technology.", "options": {}},
    {"slug": "hiring-intelligence", "name": "Hiring Intelligence", "category": "jobs",
     "description": "Careers page, ATS, and each job's department, seniority, location, remote mode and date — "
                    "with detail pages opened.",
     "instruction": "Get company name, careers URL, ATS, job titles, job URLs, department, location, seniority, "
                    "remote mode and posted date.", "options": {"pagination": True, "follow_details": True}},
    {"slug": "contact-discovery", "name": "Contact Discovery", "category": "contacts",
     "description": "Publicly listed contact points: email, phone, contact page, CEO, hiring manager, LinkedIn.",
     "instruction": "Get company name, website, email, phone, contact page, CEO, hiring manager and LinkedIn URL.",
     "options": {}},
    {"slug": "job-extraction", "name": "Job Extraction", "category": "jobs",
     "description": "Every job on a board, enriched from its detail page: salary, skills, experience.",
     "instruction": "Find all job titles, job URLs, location, department, employment type, salary, skills and years "
                    "of experience.", "options": {"pagination": True, "follow_details": True}},
    {"slug": "custom-research", "name": "Custom Research", "category": "custom",
     "description": "A starting point: describe any fields you need; unknown ones become custom fields.",
     "instruction": "Get company name, website and ", "options": {}},
]
_BY_SLUG = {t["slug"]: t for t in BUILTIN_TEMPLATES}


def _builtin_row(template: Mapping[str, Any]) -> Dict[str, Any]:
    return {"id": f"builtin:{template['slug']}", "name": template["name"], "description": template["description"],
            "category": template["category"], "instruction": template["instruction"], "schema": {},
            "options": dict(template["options"]), "builtin": True}


class TemplateStore:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store

    def list(self, ctx: Ctx) -> List[Dict[str, Any]]:
        saved = [{**row, "builtin": False} for row in self.store.all(ctx, "scrape_templates", order="name asc")]
        return [_builtin_row(t) for t in BUILTIN_TEMPLATES] + saved

    def get(self, ctx: Ctx, template_id: str) -> Dict[str, Any]:
        if template_id.startswith("builtin:"):
            template = _BY_SLUG.get(template_id.split(":", 1)[1])
            if template is None:
                raise NotFoundError("template not found")
            return _builtin_row(template)
        return {**self.store.get(ctx, "scrape_templates", template_id), "builtin": False}

    def _clean(self, values: Mapping[str, Any], *, partial: bool = False) -> Dict[str, Any]:
        from cloud.intel.scraper.models import CrawlOptions
        from cloud.intel.scraper.service import clean_schema

        out: Dict[str, Any] = {}
        if "name" in values or not partial:
            name = re.sub(r"\s+", " ", str(values.get("name") or "")).strip()
            if not name:
                raise ValidationError("give the template a name")
            out["name"] = name[:200]
        if "instruction" in values or not partial:
            instruction = str(values.get("instruction") or "").strip()
            if not instruction:
                raise ValidationError("a template needs an instruction")
            out["instruction"] = instruction[:4000]
        for key in ("description", "category"):
            if key in values:
                out[key] = (str(values[key])[:1000 if key == "description" else 60]) if values[key] else None
        if values.get("schema"):
            out["schema"] = clean_schema(values["schema"])
        elif "schema" in values or not partial:
            out["schema"] = {}
        if "options" in values or not partial:
            out["options"] = {k: v for k, v in CrawlOptions.from_mapping(values.get("options")).as_dict().items()
                              if k in (values.get("options") or {})}
        return out

    def create(self, ctx: Ctx, values: Mapping[str, Any]) -> Dict[str, Any]:
        ctx.require_write()
        clean = self._clean(values)
        if self.store.first(ctx, "scrape_templates", {"name": clean["name"]}) is not None:
            raise ConflictError(f"a template named {clean['name']!r} already exists")
        row = self.store.insert(ctx, "scrape_templates", clean)
        audit(self.store, ctx, "scraper.template.create", entity_type="scrape_templates", entity_id=row["id"],
              summary=row["name"])
        return {**row, "builtin": False}

    def update(self, ctx: Ctx, template_id: str, values: Mapping[str, Any]) -> Dict[str, Any]:
        ctx.require_write()
        if template_id.startswith("builtin:"):
            raise ValidationError("built-in templates cannot be edited; duplicate it first")
        clean = self._clean(values, partial=True)
        if "name" in clean:
            other = self.store.first(ctx, "scrape_templates", {"name": clean["name"]})
            if other is not None and other["id"] != template_id:
                raise ConflictError(f"a template named {clean['name']!r} already exists")
        row = self.store.update(ctx, "scrape_templates", template_id, clean)
        audit(self.store, ctx, "scraper.template.update", entity_type="scrape_templates", entity_id=template_id)
        return {**row, "builtin": False}

    def duplicate(self, ctx: Ctx, template_id: str, name: Optional[str] = None) -> Dict[str, Any]:
        source = self.get(ctx, template_id)
        base = name or f"{source['name']} (copy)"
        candidate, n = base, 2
        while self.store.first(ctx, "scrape_templates", {"name": candidate}) is not None:
            candidate, n = f"{base} {n}", n + 1
        return self.create(ctx, {"name": candidate, "description": source.get("description"),
                                 "category": source.get("category"), "instruction": source["instruction"],
                                 "schema": source.get("schema") or {}, "options": source.get("options") or {}})

    def delete(self, ctx: Ctx, template_id: str) -> None:
        ctx.require_write()
        if template_id.startswith("builtin:"):
            raise ValidationError("built-in templates cannot be deleted")
        self.store.get(ctx, "scrape_templates", template_id)
        self.store.delete(ctx, "scrape_templates", template_id)
        audit(self.store, ctx, "scraper.template.delete", entity_type="scrape_templates", entity_id=template_id)
