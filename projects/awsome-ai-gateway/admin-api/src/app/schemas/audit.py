# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""감사 로그 뷰어 스키마 (ADMIN 전용, 읽기 전용)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from app.schemas.common import PaginatedResponse


class ServiceTokenRef(BaseModel):
    """합성 행위자(…000011)가 실제 어떤 토큰인지 — changes._service_token_id 해소."""

    id: uuid.UUID
    name: str
    token_prefix: str


class AuditLogItem(BaseModel):
    id: uuid.UUID
    timestamp: datetime
    action: str
    # 카탈로그 그룹 — 프론트가 다시 매핑하지 않게 서버가 내려준다.
    group: str
    actor_user_id: uuid.UUID
    actor_role: str
    actor_email: str | None
    actor_display_name: str | None
    # user | service_token | dev | unknown — 행 렌더 분기용.
    actor_kind: Literal["user", "service_token", "dev", "unknown"]
    service_token: ServiceTokenRef | None
    resource_type: str
    resource_id: str
    # _service_token_id 는 service_token 필드로 옮기고 여기선 제거한다.
    changes: dict[str, Any]
    result: str
    ip_address: str
    request_id: str


class AuditLogListResponse(PaginatedResponse[AuditLogItem]):
    pass


class ActionGroupCatalog(BaseModel):
    key: str
    actions: list[str]  # 정규화된(_CONFIRMED 제거) 액션 이름


class ActionCatalogResponse(BaseModel):
    groups: list[ActionGroupCatalog]
