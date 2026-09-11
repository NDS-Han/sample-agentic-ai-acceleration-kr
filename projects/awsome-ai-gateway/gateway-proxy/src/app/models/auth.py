# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": "auth"}

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    team_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("auth.teams.id"), nullable=True
    )
    # The real column is the PG enum `auth.user_role`, so declare it as such — see the
    # sibling enums in models/model.py. With String(20) the asyncpg dialect renders
    # `role = $1::VARCHAR`, which PostgreSQL rejects (`operator does not exist:
    # auth.user_role = character varying`). This proxy currently only ever *reads* the
    # column, so the drift is latent here, but the same declaration in notification-worker
    # silently zeroed out every admin notification. Declared correctly so the first
    # `where(User.role == ...)` added here does not resurrect that bug.
    role: Mapped[str] = mapped_column(
        Enum(
            # Every label in the Postgres enum must be listed: SQLAlchemy validates on READ,
            # so an unlisted label raises LookupError while fetching the row.
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


class JwtPublicKey(Base):
    __tablename__ = "admin_jwt_configs"
    __table_args__ = {"schema": "auth"}

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    audience: Mapped[str] = mapped_column(String(255), nullable=False)
    public_key_pem: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm: Mapped[str] = mapped_column(String(20), nullable=False, default="RS256")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
