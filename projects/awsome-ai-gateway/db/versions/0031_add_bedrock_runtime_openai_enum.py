# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""add provider enum value BEDROCK_RUNTIME_OPENAI (GPT-5.6 on the standard runtime plane)

Revision ID: 0031
Revises: 0030
Create Date: 2026-09-03

ENUM-ONLY by design (mirrors 0008 and 0016): PostgreSQL refuses "unsafe use of new
value of enum type" if a row uses a value added in the same transaction, and env.py
runs with ``transaction_per_migration``. The alias / pricing / routing rows that use
this value live in 0032.

## What the new value means

GPT-5.6 is now reachable on **two** Bedrock planes with the identical OpenAI dialect:

                     BEDROCK_MANTLE_OPENAI (0016)        BEDROCK_RUNTIME_OPENAI (this)
    host             bedrock-mantle.{r}.api.aws          bedrock-runtime.{r}.amazonaws.com
    auth             Bearer (BedrockTokenGenerator)      SigV4, service name "bedrock"
    model id         openai.gpt-5.6-terra                us./global.openai.gpt-5.6-terra
    wires            /openai/v1/responses                /openai/v1/responses AND
                                                         /openai/v1/chat/completions
    invocation log   NOT captured (AWS docs)             captured
    dialect          OpenAI Responses / Chat             IDENTICAL

Only ``api_format`` is unchanged: the existing ``OPENAI_RESPONSES`` value already
describes the dialect, and the dialect is what api_format names — the plane is what
``provider`` names. No api_format value is added here.

## Why a new provider value rather than reusing BEDROCK_MANTLE_OPENAI

``provider`` is what routes a request to an adapter (``ProviderRegistry`` keyed by
``ProviderType``), and the two planes need genuinely different adapters: one mints a
bearer token, the other hand-signs SigV4 over the exact request bytes. Reusing the
Mantle value would make the plane un-addressable from the catalogue.

## Irreversibility

``ALTER TYPE ... ADD VALUE`` cannot be undone without recreating the type, which would
require dropping every column that uses it. ``downgrade()`` is therefore a deliberate
no-op, exactly as in 0008/0016: a leftover enum LABEL with no rows referencing it is
inert. 0032's downgrade removes the rows.
"""
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS = idempotent (PG12+), so a re-run or a DB where 02_create_tables.sql
    # was regenerated with the value already present is a clean no-op.
    op.execute("ALTER TYPE model.provider ADD VALUE IF NOT EXISTS 'BEDROCK_RUNTIME_OPENAI'")


def downgrade() -> None:
    # PostgreSQL cannot DROP an enum value without recreating the type (which would
    # break model.model_aliases.provider). No-op by design (same as 0008 / 0016).
    pass
