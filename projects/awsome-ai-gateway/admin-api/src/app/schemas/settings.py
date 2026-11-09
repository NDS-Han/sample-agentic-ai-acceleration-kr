# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from __future__ import annotations

from pydantic import BaseModel


class BodyLoggingSetting(BaseModel):
    """Current state of the global body-logging toggle."""

    enabled: bool
    # 인프라 배선(BODY_LOG_S3_BUCKET env) 존재 여부 — false 면 런타임 토글을
    # 켜도 수집 경로가 없어 PUT 이 거부되고 UI 가 스위치를 잠근다.
    provisioned: bool = True


class BodyLoggingUpdateRequest(BaseModel):
    enabled: bool
