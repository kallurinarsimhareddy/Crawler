"""A generic, workspace-scoped CRUD router for any entity in the spec.

    router.include_router(crud_router("tags"))

gives::

    GET    /w/{ws}/tags?limit=&offset=&order=&q=&<field>=&<field>__gte=…
    POST   /w/{ws}/tags                      (Idempotency-Key honoured)
    GET    /w/{ws}/tags/{id}
    PATCH  /w/{ws}/tags/{id}                 ({"changes": {...}, "expected_version": n})
    DELETE /w/{ws}/tags/{id}

Filters are the store's filter language; unknown fields are a 422, never
silently ignored. Every write is audited. Tracks with richer behaviour
(companies, opportunities, imports…) pass hooks or write their own routes.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder

from cloud.intel.api.deps import get_platform, http_error, idempotent, page_response, workspace_ctx, write_ctx
from cloud.intel.core.audit import audit
from cloud.intel.core.context import Ctx, PlatformError
from cloud.intel.platform import Platform
from cloud.intel.store.spec import get_spec

__all__ = ["crud_router", "list_filters"]

_RESERVED = {"limit", "offset", "order"}


def list_filters(request: Request) -> Dict[str, Any]:
    filters: Dict[str, Any] = {}
    for key, value in request.query_params.multi_items():
        if key in _RESERVED:
            continue
        if key.endswith("__in"):
            filters[key] = [v for v in value.split(",") if v != ""]
        elif value == "" and key != "q":
            continue
        else:
            filters[key] = value
    return filters


Hook = Callable[[Platform, Ctx, Dict[str, Any]], Dict[str, Any]]


def crud_router(entity: str, *, path: Optional[str] = None, tag: Optional[str] = None,
                create: Optional[Hook] = None, update: Optional[Callable[..., Dict[str, Any]]] = None,
                allow_create: bool = True, allow_update: bool = True, allow_delete: bool = True) -> APIRouter:
    spec = get_spec(entity)
    base = path or f"/{entity.replace('_', '-')}"
    router = APIRouter(prefix="/w/{workspace_id}", tags=[tag or entity])
    user_writable = not spec.system_write

    @router.get(base, name=f"list_{entity}")
    def list_rows(request: Request, limit: int = 50, offset: int = 0, order: Optional[str] = None,
                  ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
        try:
            return page_response(platform.store.list(ctx, entity, list_filters(request), order=order,
                                                     limit=limit, offset=offset))
        except PlatformError as error:
            raise http_error(error) from error

    @router.get(base + "/{row_id}", name=f"get_{entity}")
    def get_row(row_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
        try:
            return jsonable_encoder(platform.store.get(ctx, entity, row_id))
        except PlatformError as error:
            raise http_error(error) from error

    if allow_create and user_writable:
        @router.post(base, status_code=status.HTTP_201_CREATED, name=f"create_{entity}")
        def create_row(request: Request, values: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                       platform: Platform = Depends(get_platform)):
            def produce():
                if create is not None:
                    row = create(platform, ctx, values)
                else:
                    row = platform.store.insert(ctx, entity, values)
                    audit(platform.store, ctx, f"{entity}.create", entity_type=entity, entity_id=row["id"])
                return jsonable_encoder(row)

            try:
                return idempotent(platform, ctx, request, values, produce)
            except PlatformError as error:
                raise http_error(error) from error

    if allow_update and user_writable and not spec.append_only:
        @router.patch(base + "/{row_id}", name=f"update_{entity}")
        def update_row(row_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                       platform: Platform = Depends(get_platform)):
            changes = body.get("changes")
            if not isinstance(changes, Mapping):
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, 'body must be {"changes": {...}}')
            expected = body.get("expected_version")
            try:
                if update is not None:
                    row = update(platform, ctx, row_id, dict(changes), expected)
                else:
                    row = platform.store.update(ctx, entity, row_id, changes, expected_version=expected)
                    audit(platform.store, ctx, f"{entity}.update", entity_type=entity, entity_id=row_id,
                          changes=dict(changes))
                return jsonable_encoder(row)
            except PlatformError as error:
                raise http_error(error) from error

    if allow_delete and user_writable and not spec.append_only:
        @router.delete(base + "/{row_id}", status_code=status.HTTP_204_NO_CONTENT, name=f"delete_{entity}")
        def delete_row(row_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
            try:
                platform.store.delete(ctx, entity, row_id)
                audit(platform.store, ctx, f"{entity}.delete", entity_type=entity, entity_id=row_id)
            except PlatformError as error:
                raise http_error(error) from error

    return router
