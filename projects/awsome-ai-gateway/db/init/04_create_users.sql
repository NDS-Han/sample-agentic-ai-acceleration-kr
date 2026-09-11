-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- 04_create_users.sql
-- LLM Gateway — Database users and schema-level privileges
-- Based on shared-infrastructure.md Section 2.2 principle: least privilege

-- ============================================================
-- Database users — 권한 컨테이너 전용, LOGIN 불가
-- ============================================================
--
-- ⚠️ **이 파일에 리터럴 비밀번호를 박지 말 것.** 이 스크립트는 로컬 docker-compose 뿐
--    아니라 **prod 를 포함한 모든 환경의 migration Job 에서 매 helm install/upgrade 마다
--    실행된다**(db/run_migration.sh:54-58 의 `for f in /app/init/*.sql`,
--    deployment/docs/eks-fargate/03-secrets.md:166). 따라서 리터럴은 곧 "git 에 공개된
--    비밀번호로 LOGIN 가능한 롤을 prod Aurora 에 만든다"는 뜻이다.
--    예전엔 정확히 그랬다 — 'proxy_password_change_me' / 'admin_api_password_change_me' /
--    'notification_worker_password_change_me'. 주석은 "환경변수로 override 하라"고 했지만
--    .sql 파일에는 환경변수 치환 메커니즘이 없어서 override 는 애초에 불가능했고,
--    admin_api_user 는 auth(=virtual_keys)·budget·model 에 full CRUD 를 갖는다.
--    VPC 안에 발판만 있으면(pod/bastion) 공개된 비번으로 권한상승이 가능한 상태였다.
--
-- 그래서 세 롤은 **NOLOGIN + 비밀번호 없음**으로 만든다. 랜덤 비밀번호
-- (08_create_chat_reader.sql 방식)보다 강하다 — 비번 유무와 무관하게 인증 자체가 막힌다.
-- GRANT 는 그대로 남긴다: 의도된 최소권한 설계를 문서화하고, 나중에 서비스별 유저로
-- 되돌릴 때 권한을 다시 짤 필요가 없다.
--
-- 실제 배포는 이 롤들을 **쓰지 않는다** — dev·prod 모두 migration Job 이 만드는 단일
-- 'gateway' 유저로 접속한다(run_migration.sh:60-79, values-eks-fargate-prod.yaml:37
-- `user: "gateway"` + :40 `notificationWorkerUser: "gateway"`). 즉 이 변경으로 끊기는
-- 접속 경로는 없다.
--
-- 서비스별 유저를 실제로 쓰고 싶으면: 운영자가 out-of-band 로
--   ALTER ROLE proxy_user WITH LOGIN PASSWORD '<secrets manager 값>';
-- 를 실행하고 그 비밀번호를 ESO/Secrets Manager 로 주입한다
-- (08_create_chat_reader.sql 의 gateway_chat_reader 와 같은 운영 방식).
-- ============================================================


-- proxy_user: Used by U1 Gateway Proxy
-- Reads: auth, model (config lookup), budget (usage check)
-- Writes: usage (usage_logs), budget (budget_usages atomic update)
--
-- ⚠️ ALTER 분기가 반드시 필요하다. IF NOT EXISTS 만 두면 **이미 배포된 dev·prod 의 롤은
--    공개된 옛 비밀번호를 그대로 유지한다** — 롤이 이미 존재하므로 CREATE 가 스킵되기
--    때문이다. PASSWORD NULL 로 저장된 해시를 지우고 NOLOGIN 으로 인증을 막는다.
--    (ALTER 는 멱등이라 재실행 안전.)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'proxy_user') THEN
        CREATE ROLE proxy_user WITH NOLOGIN;
    ELSE
        ALTER ROLE proxy_user WITH NOLOGIN PASSWORD NULL;
    END IF;
END
$$;

-- admin_api_user: Used by U2 Admin API + Scheduler
-- Full CRUD on auth, budget, model, audit schemas
-- Read on usage schema (analytics queries)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'admin_api_user') THEN
        CREATE ROLE admin_api_user WITH NOLOGIN;
    ELSE
        ALTER ROLE admin_api_user WITH NOLOGIN PASSWORD NULL;
    END IF;
END
$$;

-- ============================================================
-- proxy_user privileges
-- ============================================================

-- auth: SELECT only (user/key lookup)
GRANT USAGE ON SCHEMA auth TO proxy_user;
GRANT SELECT ON ALL TABLES IN SCHEMA auth TO proxy_user;
-- virtual_keys.last_used_at update
GRANT UPDATE (last_used_at) ON auth.virtual_keys TO proxy_user;

-- model: SELECT only (model config, pricing, rate limits)
GRANT USAGE ON SCHEMA model TO proxy_user;
GRANT SELECT ON ALL TABLES IN SCHEMA model TO proxy_user;

-- budget: SELECT + UPDATE on budget_usages (atomic budget deduction)
GRANT USAGE ON SCHEMA budget TO proxy_user;
GRANT SELECT ON ALL TABLES IN SCHEMA budget TO proxy_user;
GRANT UPDATE ON budget.budget_usages TO proxy_user;
GRANT INSERT ON budget.budget_usages TO proxy_user;

-- usage: INSERT on usage_logs (write usage records)
GRANT USAGE ON SCHEMA usage TO proxy_user;
GRANT INSERT ON usage.usage_logs TO proxy_user;
GRANT SELECT ON usage.usage_logs TO proxy_user;

-- ============================================================
-- admin_api_user privileges
-- ============================================================

-- auth: full CRUD
GRANT USAGE ON SCHEMA auth TO admin_api_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA auth TO admin_api_user;

-- budget: full CRUD
GRANT USAGE ON SCHEMA budget TO admin_api_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA budget TO admin_api_user;

-- model: full CRUD
GRANT USAGE ON SCHEMA model TO admin_api_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA model TO admin_api_user;

-- usage: SELECT (analytics) + INSERT/UPDATE on roi_aggregations (scheduler)
GRANT USAGE ON SCHEMA usage TO admin_api_user;
GRANT SELECT ON ALL TABLES IN SCHEMA usage TO admin_api_user;
GRANT INSERT, UPDATE ON usage.roi_aggregations TO admin_api_user;

-- audit: full CRUD (audit logs + cache invalidation failures)
GRANT USAGE ON SCHEMA audit TO admin_api_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA audit TO admin_api_user;

-- notification_worker_user: Used by U3 Notification Worker
-- Reads: auth (recipient lookup), notification (config/log)
-- Writes: notification (delivery log)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'notification_worker_user') THEN
        CREATE ROLE notification_worker_user WITH NOLOGIN;
    ELSE
        ALTER ROLE notification_worker_user WITH NOLOGIN PASSWORD NULL;
    END IF;
END
$$;

-- ============================================================
-- notification_worker_user privileges (auth only)
-- notification schema grants are in 07_grant_notification_privileges.sql
-- (must run after 05_create_notification_schema.sql creates the tables)
-- ============================================================

-- auth: SELECT only (users, teams — recipient resolution)
GRANT USAGE ON SCHEMA auth TO notification_worker_user;
GRANT SELECT ON auth.users, auth.teams TO notification_worker_user;

-- ============================================================
-- Default privileges for future tables
-- ============================================================

ALTER DEFAULT PRIVILEGES IN SCHEMA auth  GRANT SELECT ON TABLES TO proxy_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA model GRANT SELECT ON TABLES TO proxy_user;

ALTER DEFAULT PRIVILEGES IN SCHEMA auth   GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO admin_api_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA budget GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO admin_api_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA model  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO admin_api_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA audit  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO admin_api_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA usage  GRANT SELECT ON TABLES TO admin_api_user;
