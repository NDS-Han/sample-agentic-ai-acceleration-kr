# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from __future__ import annotations

from pydantic import BaseModel


class BodyLoggingSetting(BaseModel):
    """Current state of the global body-logging toggle."""

    enabled: bool


class BodyLoggingUpdateRequest(BaseModel):
    enabled: bool
