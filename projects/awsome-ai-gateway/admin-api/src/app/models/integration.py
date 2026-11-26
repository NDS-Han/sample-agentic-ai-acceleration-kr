# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""외부 통합(AICM /aicm/v1) 전용 테이블 — migration 0064/0065.

``AICM_API_ENABLED=false`` 인 게이트웨이에서는 어떤 코드 경로도 이 테이블을 읽거나 쓰지 않는다.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import ARRAY, CIDR, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class IntegrationToken(Base):
    """``itg-`` 토큰의 권한 부속 행. 토큰 자체는 ``auth.service_tokens``."""

    __tablename__ = "integration_tokens"
    __table_args__ = {"schema": "auth"}

    token_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("auth.service_tokens.id", ondelete="CASCADE"), primary_key=True
    )
    integration: Mapped[str] = mapped_column(String(32), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    allowed_cidrs: Mapped[list[str] | None] = mapped_column(ARRAY(CIDR), nullable=True)
    rate_limit_per_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lineage_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IntegrationIdempotencyKey(Base):
    __tablename__ = "integration_idempotency_keys"
    __table_args__ = {"schema": "auth"}

    integration: Mapped[str] = mapped_column(String(32), primary_key=True)
    idem_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
