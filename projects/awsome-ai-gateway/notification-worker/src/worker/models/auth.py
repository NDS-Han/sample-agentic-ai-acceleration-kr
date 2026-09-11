# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Read-only mirror of auth schema models.

U3 Notification Worker only reads auth.users and auth.teams.
This module uses a separate Base to avoid conflicts with the
notification schema's Base in models/notification.py.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class AuthBase(DeclarativeBase):
    pass


class Team(AuthBase):
    __tablename__ = "teams"
    __table_args__ = {"schema": "auth"}

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    dept_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    leader_user_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    members: Mapped[list["User"]] = relationship("User", back_populates="team", lazy="select")


class User(AuthBase):
    __tablename__ = "users"
    __table_args__ = {"schema": "auth"}

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    team_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("auth.teams.id"), nullable=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # ⚠️ Must be the PG enum type, NOT String. The real column is `auth.user_role`, and the
    # asyncpg dialect renders the bind cast from the declared type: String(20) emits
    # `role = $1::VARCHAR`, which PostgreSQL rejects with
    #   operator does not exist: auth.user_role = character varying
    # Declaring the enum emits `role = $1::auth.user_role` instead (verified on real PG 16).
    # RecipientResolver._resolve_admins filters on `User.role == "ADMIN"`, and its caller
    # swallows the exception (`except Exception: logger.exception(...); continue`), so the
    # drift did not surface as a crash — every admin notification silently resolved to zero
    # recipients. Mock-based unit tests cannot catch this; only a real PostgreSQL can.
    #
    # The label list must contain EVERY label in the Postgres enum, not just the ones this
    # service dispatches on: SQLAlchemy validates on READ, so an unlisted label raises
    # LookupError while fetching and breaks the whole query.
    # create_type=False — this worker only reads the auth schema and must never DDL it.
    role: Mapped[str] = mapped_column(
        Enum(
            "ADMIN",
            "TEAM_LEADER",
            "DEVELOPER",
            name="user_role",
            schema="auth",
            create_type=False,
        ),
        nullable=False,
        default="DEVELOPER",
    )
    sso_subject: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    team: Mapped[Optional[Team]] = relationship("Team", back_populates="members", lazy="select")
