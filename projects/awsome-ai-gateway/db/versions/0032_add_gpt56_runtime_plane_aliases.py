# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""add GPT-5.6 aliases on the STANDARD bedrock-runtime plane (SigV4 + CRIS) + widen backend CHECK

Revision ID: 0032
Revises: 0031
Create Date: 2026-09-03

Runs AFTER 0031 committed the ``BEDROCK_RUNTIME_OPENAI`` provider enum value, so the
INSERTs below are safe here (mirrors 0009-after-0008 and 0017-after-0016).

    gpt-5.6-sol    -> us.openai.gpt-5.6-sol      coding / long-horizon agentic work
    gpt-5.6-terra  -> us.openai.gpt-5.6-terra    balanced
    gpt-5.6-luna   -> us.openai.gpt-5.6-luna     cheapest, high volume

These are the SECOND registration of the same three models. 0025 registered them on the
**Mantle** plane as ``codex-gpt-5.6-*`` -> ``openai.gpt-5.6-*``. Both sets stay: the
model row's ``provider`` is what selects the plane at dispatch time
(routers/openai_compat.py), so an operator/client picks a plane by picking a name.

    codex-gpt-5.6-terra  -> Mantle  (bearer, /openai/v1/responses only, no invocation log)
    gpt-5.6-terra        -> runtime (SigV4, Responses AND Chat, invocation log captured)

The unprefixed names are deliberate. ``codex-`` marks the Mantle rows because that path
is Codex-only (the Mantle adapter posts to /v1/responses and nowhere else); the runtime
rows also serve /v1/chat/completions, so ANY OpenAI-dialect client can use them and a
client prefix would be wrong. Matches the unprefixed convention already used by
``claude-sonnet-5`` (0027) and ``llama-3-70b`` (0029).

## CRIS is mandatory — verified live, 2026-09-03

``openai.gpt-5.6-*`` reports ``inferenceTypesSupported = [INFERENCE_PROFILE]``: the bare
model id is NOT callable on the runtime plane, only a cross-region inference profile id.
Probed with SigV4 (IAM user 123456789012:user/kyutae, ``AWS_BEARER_TOKEN_BEDROCK``
unset so the signature is what is actually being tested), POST to both
``/openai/v1/responses`` and ``/openai/v1/chat/completions``:

    region          model id                        responses  chat
    us-east-2       us.openai.gpt-5.6-{sol,terra,luna}   200     200
    us-east-2       global.openai.gpt-5.6-{...}          200     200
    ap-northeast-2  global.openai.gpt-5.6-{...}          200     200
    ap-northeast-2  us.openai.gpt-5.6-{...}              400     400   <- negative control
                                                  "The provided model identifier is invalid."

18/18 expected 200s and 6/6 expected failures. The negative control matters: it proves
the prefix is not cosmetic and that ``list-inference-profiles`` is telling the truth —
**ap-northeast-2 publishes only the ``global.`` profiles**, so an operator moving these
rows to Seoul must change the model id as well as the endpoint (see MOVING REGIONS).

``us.openai.gpt-5.6-terra`` routes to us-east-1 / us-east-2 / us-west-2;
``global.openai.gpt-5.6-terra`` routes "globally across all supported AWS Regions"
(get-inference-profile, 2026-09-03). ``us.`` is chosen here because a US-only routing
scope is a narrower and more explainable data boundary than "anywhere", and because the
IAM policy can then enumerate the three underlying foundation-model ARNs instead of
granting a region-less one. There is no APAC-resident option for GPT-5.6 in any form —
no ``apac.`` profile exists — so no choice here keeps traffic inside Korea.

## Region: us-east-2, matching 0025

Same reasoning as 0025 (Sol exists in us-east-1/us-east-2, not us-west-2), plus: keeping
both planes in one region means one region's worth of IAM, and the ``codex`` routing
profile is already ``us-east-2`` so the cross-plane comparison is apples to apples.

## PRICING — from the AWS model cards, per inference option

The authoritative source is the model card, not the Price List API:
``https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-56-{sol,terra,luna}.html``
(fetched 2026-09-03). ``https://aws.amazon.com/bedrock/pricing/`` does not list GPT-5.6
inline at all. Each card carries one pricing table that covers BOTH endpoints — the price
is a property of the model, not the plane (``endpoints.html``: *"Choose an endpoint based
on the APIs and capabilities you need, not cost"*) — split by inference option:

    Short Context Window (272K), $ /1M:   input  30m-cache-write  cache-read  output
      sol    In-Region / Geo CRIS          4.40      5.50            0.44      22.00
             Global CRIS                   4.00      5.00            0.40      20.00
      terra  In-Region / Geo CRIS          2.20      2.75            0.22      13.20
             Global CRIS                   2.00      2.50            0.20      12.00
      luna   In-Region / Geo CRIS          0.22      0.275           0.022      1.32
             Global CRIS                   0.20      0.25            0.02       1.20

These rows use ``us.`` = **Geo CRIS**, so the Geo column is the one seeded (÷1000 for the
per-1k columns). Switching a row to ``global.`` is a **price change too**, not just an id
change: Global is ~9.09% cheaper across all four dimensions. Anyone doing the
ap-northeast-2 move below must reprice, not only re-point.

⚠️ **sol is promotional.** The 2026-08-21 announcement
(``https://aws.amazon.com/about-aws/whats-new/2026/08/bedrock-openai-gpt-56-sol-reduced-pricing/``)
cut sol by 20% input / 33.3% output — from exactly the 5.50/33.00 that 0025 seeded — and
says the new rate is *"available at least through November 21, 2026."* Re-check the card
around that date and add another ``effective_from`` row if it reverts. (The announcement's
headline "$4 per million input tokens and $20 per million output tokens" is the **Global
CRIS** column; Geo, which is what we use, is 4.40/22.00. Both figures are real — they are
different inference options, and conflating them under-bills by 9.09%.)

The same cut makes 0025's **Mantle** sol row stale by the identical amount, since the
price is plane-independent. 0033 corrects it.

Long-context (>272K input) rates are on the same cards and are exactly input x2, cache x2,
output x1.5 (sol 22.00 -> 33.00, terra 13.20 -> 19.80, luna 1.32 -> 1.98). OpenAI's own
docs state the trigger verbatim — *"Prompts with >272K input tokens are priced at 2x input
and 1.5x output for the full request"* — i.e. a whole-request switch, not a marginal rate.
``model_pricings`` has one rate per alias with no input-size band, so a >272K request is
UNDER-billed on both planes. Pre-existing (0025 flagged it), out of scope here.

## Price List API cross-check — why it is NOT the source used

Full scan of all four Bedrock offer files (2026-09-03):

    AmazonBedrock                  version 20260901205051   48 gpt-5.6 SKUs
    AmazonBedrockFoundationModels  version 20260901183649    0
    AmazonBedrockService           version 20260831092141    0
    AmazonBedrockAgentCore         version 20260901164424    0

All 48 are ``us-gov-west-1`` and every single usagetype contains ``-mantle-``
(0 non-mantle usagetypes). Dividing the GovCloud ``standard`` tier by the 1.2 GovCloud
premium reproduces 0025's seeded rates **exactly**, to the last digit:

    terra  input  2.6400/1.2 = 2.2000   output 15.8400/1.2 = 13.2000
           cw-30m 3.3000/1.2 = 2.7500   cread   0.2640/1.2 =  0.2200
    luna   input  0.2640/1.2 = 0.2200   output  1.5840/1.2 =  1.3200
           cw-30m 0.3300/1.2 = 0.2750   cread   0.0264/1.2 =  0.0220

(SKUs: terra 25W7XV4U7BWMDBB2 / AJZX4S4XEXZB5UN8 / Z53AT5EDHYWBM9YC / QT3FUQSB2XW29ABM;
luna E5VV3ACUZVYNTRCM / VZMCKGYYEZJ2UE45 / Y5M8XECNRGNCHRQ5 / W2YGCZ66ZX8RG9BT.
nterms=1 for all — no reserved second term, no promo usagetype.)

So terra and luna are corroborated by two independent sources that agree to the last
digit, which is why they are seeded unchanged from 0025.

``sol`` has **0 SKUs in all four offers** — as 0030 recorded, and why 0030 deliberately
left it alone. That absence is not evidence of a price: the model card carries one, and it
is dated and announced. The card wins. This is the correction 0030 said it could not make
without machine-readable grounds; the grounds are the card, not the Price List.

⚠️ Do NOT widen ``model_service.py`` ``provider != Provider.BEDROCK`` to cover this plane
so that ``PricingSyncService`` maintains these rows. The sync's ``_classify_usage`` has no
inference-option, context-band, or service-tier dimension and its accumulator is
last-write-wins, so a GovCloud ``flex`` (0.5x) or ``long-ctx`` (2x) SKU would silently
overwrite a correct rate. Widening it is a mis-billing feature until it gains those
dimensions.

Two further findings from the same scan, recorded here so they are not re-derived:

* ``service_tier`` is a real price dimension: ``flex`` = 0.5x, ``standard`` = 1.0x,
  ``priority`` = 2.0x. The gateway never sends ``service_tier``, so ``standard``
  applies — these rows are correct as long as that stays true. Sending ``flex`` or
  ``priority`` through would silently mis-bill by 2x in either direction. (The model cards
  say only Standard is supported on these models, and a live ``flex`` probe returned 400,
  so the other tiers are currently unreachable rather than merely unused.)
* Long-context rates: see the PRICING section above.

## ⚠️ Quota, not price: 10x output burndown on this plane

``https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-token-burndown.html`` and
each model card: on the ``bedrock-runtime`` endpoint these models burn **10 tokens of TPM
quota per output token**. That is a THROTTLING characteristic, not a billing one — nothing
in ``model_pricings`` should encode it — but it means the same workload consumes ~10x the
output-side TPM here versus a 1:1 model, so a plane migration can start throttling at a
volume that was previously fine. ``global-cross-region-inference.html`` still claims "for
all other models, the burndown rate is 1:1"; that page is stale and the burndown page and
model cards are newer and specific.

## routing_profiles.backend gains 'bedrock_openai'

0009 created ``CONSTRAINT routing_profiles_backend_check CHECK (backend IN
('invoke','mantle'))``. A client with no Mantle entitlement at all needs a profile that
says "Responses is serviceable, runtime plane only", and routers/openai_compat accepts
``{'mantle','bedrock_openai'}`` for /v1/responses. Without widening the CHECK, inserting
such a profile is rejected on any migrated DB, so editing init SQL alone is not enough
(same reasoning as 0018).

No profile row is inserted or changed here. The existing ``codex`` profile
(``backend='mantle'``) can already reach these aliases, because the PLANE comes from the
model row and the profile only gates route eligibility. ``default_model`` is likewise
left alone — flipping the default plane is a separate, reversible operator decision:

    UPDATE model.routing_profiles SET default_model = 'gpt-5.6-terra' WHERE client = 'codex';

## ⚠️ IAM must be granted before these rows are usable

Registering a row does not grant access. The pod's IRSA role needs, in us-east-2:

    arn:aws:bedrock:us-east-2:<acct>:inference-profile/us.openai.gpt-5.6-*
    arn:aws:bedrock:us-east-1::foundation-model/openai.gpt-5.6-*
    arn:aws:bedrock:us-east-2::foundation-model/openai.gpt-5.6-*
    arn:aws:bedrock:us-west-2::foundation-model/openai.gpt-5.6-*

BOTH the inference-profile ARN and every underlying foundation-model ARN are required —
granting only the profile yields AccessDeniedException (the same trap Claude 5 hit).
Unlike Mantle, the resource ARNs here are NOT model-wildcarded, so adding a MODEL needs
an IAM change too. See deployment/terraform/modules/irsa.

## ⚠️ Redis flush after applying

    redis-cli --scan --pattern 'model:*' | xargs -r redis-cli DEL
    redis-cli DEL model:list

``MODEL_CACHE_TTL = MODEL_LIST_CACHE_TTL = 300``, so skipping the flush just delays
visibility by up to 5 minutes — not an outage. Only needed if you also change
``routing_profiles``: ``redis-cli DEL routing_profile:codex`` (the key is
``routing_profile:{client}``, NOT ``routing:{client}`` — 0025/0028 recorded that trap).

## MOVING REGIONS (ap-northeast-2)

Both columns must change together, and the model id must switch prefix:

    UPDATE model.model_aliases
       SET endpoint_url = 'https://bedrock-runtime.ap-northeast-2.amazonaws.com/openai',
           provider_model_id = replace(provider_model_id, 'us.openai.', 'global.openai.')
     WHERE provider = 'BEDROCK_RUNTIME_OPENAI';

Changing only the endpoint gives HTTP 400 "The provided model identifier is invalid"
(measured above), and changing only the model id gives a SigV4 403 — the adapter derives
the signing region from the endpoint HOST, so host and id have to agree.

⚠️ That UPDATE alone would then **over-bill by 10%**: ``global.`` is Global CRIS, which is
~9.09% cheaper than the Geo rates seeded here (see PRICING). A region move needs a new
``model_pricings`` row per alias at the Global column, not just the two column changes
above. It also widens data residency from "US only" to "any supported commercial Region",
which is the actual reason to think twice about it.
"""
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

SYSTEM_USER = "00000000-0000-4000-a000-000000000010"
ENDPOINT = "https://bedrock-runtime.us-east-2.amazonaws.com/openai"
# Pinned (not now()) for cross-environment parity in price audits, like 0025/0027/0030.
EFFECTIVE_FROM = "2026-09-03T00:00:00Z"

_BACKEND_OLD = "('invoke','mantle')"
_BACKEND_NEW = "('invoke','mantle','bedrock_openai')"

# (alias, provider_model_id, display_name, description,
#  input/1k, cache_write/1k, cache_read/1k, output/1k)
#
# cache_write goes into BOTH the 5m and the 1h column, exactly as 0025 does: Bedrock
# publishes ONE cache-write rate for the OpenAI models (usagetype
# ``cache-write-tokens-30m``); the 5m/1h split in model_pricings exists for Anthropic's
# two TTLs. The 1h column is unreachable on this path anyway — cost_recorder picks it
# only when ``usage.cache_ttl_1h`` is set, which happens exclusively in
# routers/messages.py (Anthropic dialect).
MODELS = [
    (
        "gpt-5.6-sol",
        "us.openai.gpt-5.6-sol",
        "GPT-5.6 Sol (Bedrock runtime)",
        "bedrock-runtime GPT-5.6 Sol (us-east-2, US CRIS; Responses + Chat) "
        "— coding / agentic. Pricing = AWS model card, Geo CRIS short-context "
        "(promotional since 2026-08-21, guaranteed only through 2026-11-21).",
        "0.004400", "0.005500", "0.000440", "0.022000",
    ),
    (
        "gpt-5.6-terra",
        "us.openai.gpt-5.6-terra",
        "GPT-5.6 Terra (Bedrock runtime)",
        "bedrock-runtime GPT-5.6 Terra (us-east-2, US CRIS; Responses + Chat) "
        "— balanced. Pricing = AWS published (GovCloud standard SKU / 1.2, exact match).",
        "0.002200", "0.002750", "0.000220", "0.013200",
    ),
    (
        "gpt-5.6-luna",
        "us.openai.gpt-5.6-luna",
        "GPT-5.6 Luna (Bedrock runtime)",
        "bedrock-runtime GPT-5.6 Luna (us-east-2, US CRIS; Responses + Chat) "
        "— high volume. Pricing = AWS published (GovCloud standard SKU / 1.2, exact match).",
        "0.000220", "0.000275", "0.000022", "0.001320",
    ),
]


def _recreate_backend_check(values: str) -> None:
    op.execute(
        "ALTER TABLE model.routing_profiles "
        "DROP CONSTRAINT IF EXISTS routing_profiles_backend_check"
    )
    op.execute(
        f"""
        DO $$ BEGIN
            ALTER TABLE model.routing_profiles
                ADD CONSTRAINT routing_profiles_backend_check
                CHECK (backend IN {values});
        EXCEPTION WHEN duplicate_object THEN null;
        END $$
        """
    )


def upgrade() -> None:
    _recreate_backend_check(_BACKEND_NEW)

    for alias, model_id, display_name, description, p_in, p_cw, p_cr, p_out in MODELS:
        op.execute(
            f"""
            INSERT INTO model.model_aliases
                (alias, provider, provider_model_id, endpoint_url, api_format, status,
                 description, display_name, created_by)
            VALUES
                ('{alias}', 'BEDROCK_RUNTIME_OPENAI', '{model_id}',
                 '{ENDPOINT}', 'OPENAI_RESPONSES', 'ACTIVE',
                 '{description}', '{display_name}', '{SYSTEM_USER}')
            ON CONFLICT (alias) DO NOTHING
            """
        )

        # model_pricings PK is gen_random_uuid(), so ON CONFLICT cannot dedupe; guard on
        # (model_alias, effective_from) like 0025/0027/0030. Keying on effective_from (not
        # just the alias) keeps a future price revision insertable.
        op.execute(
            f"""
            INSERT INTO model.model_pricings
                (id, model_alias, input_price_per_1k_tokens, output_price_per_1k_tokens,
                 cache_creation_5m_price_per_1k_tokens, cache_creation_1h_price_per_1k_tokens,
                 cache_read_price_per_1k_tokens, effective_from, created_by)
            SELECT gen_random_uuid(), '{alias}',
                   {p_in}, {p_out}, {p_cw}, {p_cw}, {p_cr},
                   '{EFFECTIVE_FROM}', '{SYSTEM_USER}'
            WHERE NOT EXISTS (
                SELECT 1 FROM model.model_pricings
                 WHERE model_alias = '{alias}' AND effective_from = '{EFFECTIVE_FROM}'
            )
            """
        )


def downgrade() -> None:
    """Remove the three aliases and every row that references them, then narrow the CHECK.

    SIX tables carry a FK to model_aliases.alias and NONE cascade (all ON DELETE NO
    ACTION), so deleting only the pricing row is not enough — as soon as an operator
    grants one of these models to a team or user (the normal way to make it usable),
    downgrade would die with ForeignKeyViolationError, and because env.py runs with
    ``transaction_per_migration`` that rollback leaves the DB pinned here with no way
    down. 0025's downgrade documents the same list, reproduced against real Postgres.

    routing_profiles has no FK (default_model is a plain column), so a profile pointing
    at one of these aliases is repaired by UPDATE — dropping an alias a profile still
    names would leave a dangling default and 404 every request for that client. It is
    pointed back at 'codex-gpt-5.6-terra' (0028's default) only if that alias is present
    and ACTIVE; otherwise the profile is left alone rather than aimed at nothing.

    Narrowing the CHECK last, and only after any 'bedrock_openai' profile has been
    migrated back to 'mantle' — an existing row with the value being removed would make
    the ADD CONSTRAINT fail.
    """
    aliases = ", ".join(f"'{m[0]}'" for m in MODELS)

    op.execute(
        f"""
        UPDATE model.routing_profiles
           SET default_model = 'codex-gpt-5.6-terra'
         WHERE default_model IN ({aliases})
           AND EXISTS (
                 SELECT 1 FROM model.model_aliases
                  WHERE alias = 'codex-gpt-5.6-terra' AND status = 'ACTIVE'
           )
        """
    )

    for table, column in (
        ("model.model_pricings", "model_alias"),
        ("model.team_allowed_models", "model_alias"),
        ("model.user_allowed_models", "model_alias"),
        ("model.rate_limit_configs", "model_alias"),
        ("budget.downgrade_policies", "from_model_alias"),
        ("budget.downgrade_policies", "to_model_alias"),
    ):
        op.execute(f"DELETE FROM {table} WHERE {column} IN ({aliases})")

    op.execute(f"DELETE FROM model.model_aliases WHERE alias IN ({aliases})")

    # A runtime-only profile has no meaning once the runtime aliases are gone, and the
    # narrowed CHECK would reject it. 'mantle' is the value it would have had before.
    op.execute(
        "UPDATE model.routing_profiles SET backend = 'mantle' WHERE backend = 'bedrock_openai'"
    )
    _recreate_backend_check(_BACKEND_OLD)
    # The provider enum label from 0031 is intentionally left in place (see 0031).
