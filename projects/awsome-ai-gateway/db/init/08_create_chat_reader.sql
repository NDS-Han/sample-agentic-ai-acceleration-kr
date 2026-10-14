-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- 08_create_chat_reader.sql
-- LLM Gateway — gateway_chat_reader read-only role for admin-chat-agent (Phase 2)
--
-- query_db Lambda 가 사용하는 read-only DB user. SELECT 만 가능, schema
-- whitelist 에 따른 컬럼만 노출. password_hash / virtual_keys.value 같은
-- 민감 컬럼은 명시적으로 제외.
--
-- 비밀번호: ESO 가 Secrets Manager 에서 동기화. 실제 password 는 Terraform 또는
-- 운영자가 ALTER ROLE 로 설정한다 (terraform output chat_reader_secret_arn).
--
-- ⚠️ 여기에 리터럴 placeholder 를 박지 말 것. 이 파일은 git 트리에 있고 이 스크립트는
--    모든 환경(prod 포함)의 migration Job 에서 실행되므로, 리터럴은 곧 "공개된 비밀번호로
--    LOGIN 가능한 롤을 prod 에 만든다"는 뜻이다. 대신 매 실행 랜덤값을 넣어 아무도 모르는
--    상태로 만든다 — 운영자가 ALTER ROLE 로 설정하기 전까지 로그인 불가.
--    gen_random_uuid() 는 PG13+ core 내장이라 pgcrypto 확장이 필요 없다.
--    IF NOT EXISTS 가드 덕에 이미 롤이 있는 환경(운영자가 손으로 만든 dev 등)의
--    비밀번호는 건드리지 않는다 — 재실행 멱등.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gateway_chat_reader') THEN
        EXECUTE format(
            'CREATE ROLE gateway_chat_reader LOGIN PASSWORD %L NOINHERIT',
            gen_random_uuid()::text || gen_random_uuid()::text
        );
    END IF;
END
$$;

-- DB connect
GRANT CONNECT ON DATABASE gateway TO gateway_chat_reader;

-- Schema USAGE — SELECT 가능하기 위한 prerequisite
-- (R17-S1: chat_agent 는 SELECT 미부여 스키마라 USAGE 도 뺀다 — 테이블 권한이
--  없으면 USAGE 는 아무 권한도 주지 않지만, 최소권한 원칙상 명시적으로 제외)
GRANT USAGE ON SCHEMA auth, public, model, budget, usage TO gateway_chat_reader;

-- 명시적 SELECT — 화이트리스트에 있는 테이블만. 컬럼명은 ground truth
-- (db/init/02_create_tables.sql) 기준. 새 테이블 생기면 여기에 추가 +
-- spec §2.5.3 의 schema_whitelist.yaml 도 갱신.

-- auth schema (last_login_at 컬럼 없음 → updated_at. dept_id (NOT department_id).
-- virtual_keys 는 status (NOT is_active/ttl_seconds), value(key_value_encrypted) 미부여.)
-- ⚠️ GRANT 컬럼 목록은 schema_whitelist.yaml 과 정확히 일치해야 함 — whitelist 에
--    있는데 GRANT 누락이면 sqlglot 통과 후 DB permission denied 로 query_db 실패.
GRANT SELECT (id, team_id, email, display_name, role, provider, is_active, created_at, updated_at)
    ON auth.users TO gateway_chat_reader;
GRANT SELECT (id, dept_id, name, leader_user_id, created_at, updated_at)
    ON auth.teams TO gateway_chat_reader;
-- virtual_keys: key_value_encrypted (키 원문) 절대 미부여 (보안 핵심)
GRANT SELECT (id, user_id, key_prefix, status, issued_at, expires_at,
              last_used_at, revoked_at, revoked_by, created_at)
    ON auth.virtual_keys TO gateway_chat_reader;

-- 운영 데이터 — 실제 스키마는 usage/budget/model (NOT public).
-- 테이블/컬럼은 db/init/02_create_tables.sql 과 schema_whitelist.yaml 에 일치.
GRANT SELECT (id, request_id, user_id, team_id, dept_id, model_alias, provider,
              input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
              cost_usd, latency_ms, status, requested_at, completed_at,
              is_streaming, estimated_usage, downgraded_from)
    ON usage.usage_logs TO gateway_chat_reader;
GRANT SELECT (id, scope, scope_id, max_budget_usd, period_type, policy,
              allocated_by, effective_from, is_active, created_at)
    ON budget.budget_configs TO gateway_chat_reader;
GRANT SELECT (id, scope, scope_id, model_alias, rpm_limit, tpm_limit,
              cpm_limit_usd, cph_limit_usd, is_active, created_by, created_at, updated_at)
    ON model.rate_limit_configs TO gateway_chat_reader;

-- chat_agent schema — 명시적으로 SELECT 를 **부여하지 않는다** (R17-S1).
-- 예전엔 "자기 작성 history 조회" 목적으로 GRANT 했지만, 실제로는:
--   - schema_whitelist.yaml 에 chat_agent 테이블이 없어 validate_sql 이
--     해당 쿼리를 애초에 거절 → GRANT 는 죽은 권한이었고,
--   - 함수 우회(QUERY_TO_XML 등 — R17 에서 차단)에만 살아있는 권한으로,
--     한 관리자가 다른 관리자의 채팅 내용 전체를 읽는 경로였다.
-- whitelist 가 진실의 원천이므로 권한을 회수한다. 기존 설치본은 GRANT 가 이미
-- 부여된 상태라 명시 REVOKE 가 필요(없으면 과거 부여분이 계속 살아있다).
-- 테이블 미존재 시(첫 부팅) REVOKE 는 실패하므로 to_regclass 가드 유지.
DO $$
BEGIN
    IF to_regclass('chat_agent.sessions') IS NOT NULL THEN
        REVOKE ALL ON chat_agent.sessions, chat_agent.messages FROM gateway_chat_reader;
    END IF;
    IF to_regclass('chat_agent.schema_embeddings') IS NOT NULL THEN
        REVOKE ALL ON chat_agent.schema_embeddings, chat_agent.golden_examples FROM gateway_chat_reader;
    END IF;
    -- 스키마 USAGE 도 회수 — 기존 설치본은 위 GRANT 라인에서 이미 부여됐다.
    -- (USAGE 만으로는 테이블 읽기 불가지만, 미래 테이블에 대한 DEFAULT
    --  권한 결합을 막고 최소권한 의도를 명확히 한다)
    IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'chat_agent') THEN
        REVOKE USAGE ON SCHEMA chat_agent FROM gateway_chat_reader;
    END IF;
END $$;

-- audit schema 는 super-admin 만. gateway_chat_reader 는 미부여.

-- statement_timeout 강제 — 무한 query 방지 (spec §3.1.6)
ALTER ROLE gateway_chat_reader SET statement_timeout = '10s';

-- 새 테이블 생성 시 자동 SELECT 부여 안 함 (default privileges 변경 안 함).
-- 새 테이블이 생기면 명시적으로 GRANT 후 schema_whitelist.yaml 도 갱신.
