#!/bin/sh
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# LLM Gateway — Migration runner
#
# 두 가지 모드로 동작:
#
# [로컬 docker-compose]
#   - postgres 컨테이너가 자체적으로 /docker-entrypoint-initdb.d 의 *.sql 을 자동 실행.
#   - 이 스크립트는 DB_MASTER_URL 이 없는 상태로 호출됨 → init SQL 건너뛰고 alembic upgrade 만 수행.
#
# [클라우드 (EKS + Aurora 등)]
#   - Aurora 는 docker-entrypoint-initdb.d 메커니즘 없음 → 이 스크립트가 직접 수행.
#   - DB_MASTER_URL (master user 권한) 필수.
#   - APP_DB_USER/APP_DB_PASSWORD 제공 시 application user 생성/업데이트 + 모든 schema 에 GRANT.
#   - 순서: init/*.sql 실행 → application user 처리 → alembic upgrade head.
#   - 모든 SQL 은 idempotent (IF NOT EXISTS / ON CONFLICT / DO IF NOT EXISTS) — 재실행 안전.
#
# NOTE: 0001_baseline 은 no-op (DDL 은 init SQL 이 수행). 0002 부터 실제 DML/DDL 포함.
#       alembic upgrade head 는 이미 head 인 경우 no-op 이므로 재실행 안전.
set -e

MASTER_URL="${DB_MASTER_URL:-}"
APP_USER="${APP_DB_USER:-}"
APP_PASSWORD="${APP_DB_PASSWORD:-}"
# chat_agent 를 반드시 포함할 것. 이 스키마의 **테이블은 alembic**(0005)이 만들지만
# 스키마 자체는 01_create_schemas.sql 이 이 루프보다 먼저 만들어 두므로, 여기서
# GRANT + ALTER DEFAULT PRIVILEGES 를 걸면 뒤이어 alembic(master)이 만드는 테이블에도
# 권한이 자동 적용된다. 누락 시 배포는 성공하지만(alembic 은 DB_MASTER_URL 로 실행 —
# db/env.py:15-18) 런타임 앱유저(gateway)가 chat 세션을 만들 때
# `permission denied for schema chat_agent` 로 500. 실측 검증: 누락=재현, 추가=해소,
# 재실행=멱등(2026-08-14, PostgreSQL 16).
SCHEMAS="auth model budget usage audit notification chat_agent public"

# psql용: password에 특수문자가 있으면 URL 파싱이 깨지므로 PGPASSWORD 환경변수 사용.
# DB_MASTER_PASSWORD는 K8s Secret에서 직접 주입됨 (Helm template env).
# URL에서 password 부분을 제거하고 PGPASSWORD로 분리.
if [ -n "${DB_MASTER_PASSWORD:-}" ]; then
    export PGPASSWORD="$DB_MASTER_PASSWORD"
fi
# password 없는 connection string — URL 의 user 를 **보존**한다. 예전엔
# ${DB_MASTER_USER:-postgres_admin} 으로 덮어썼는데, Secret 의 user 와 URL 의
# user 가 다르면(예: URL 은 postgres, env 는 미주입) 조용히 잘못된 계정으로
# 접속을 시도해 인증 실패가 원인 불명으로 보인다. 비밀번호에 `:` 가 섞여도
# [^@]* 가 흡수한다. (`@` 를 포함하는 비밀번호는 RDS 관리형 비밀번호 문자셋에서
# 제외 — env.py 의 리댁션 주석과 같은 근거.)
MASTER_URL_NO_PASS=$(echo "$MASTER_URL" | sed 's|://\([^:]*\):[^@]*@|://\1@|')

# psql 공통 옵션:
#   -X          — ~/.psqlrc 를 읽지 않는다. Job 의 appuser 홈은 깨끗하지만 운영자가
#                 이 스크립트를 손으로 재실행할 때 개인 psqlrc 가 개입하는 것을 막는다.
#   SET         — init SQL/GRANT 단계에도 lock_timeout 을 건다. 앱이 도는 중인 배포에서
#                 DDL 이 락 큐에 서면 그 뒤의 앱 쿼리가 전부 대기한다 — 큐잉보다 빠른 실패가
#                 낫다(모든 SQL 은 멱등이라 Job 재시도로 재개된다).
#
# ⚠️ lock_timeout 을 PGOPTIONS(= startup packet 의 command-line options)로 넘기면
#    안 된다 — RDS Proxy 가 startup options 를 거부한다:
#      "Feature not supported: RDS Proxy currently doesn't support
#       command-line options."  (dev 실측, migration-223, 2026-10-19)
#    일반 SQL 인 SET 은 프록시를 통과하므로 각 psql 세션 첫 명령으로 내린다.
#    asyncpg 쪽의 같은 함정(server_settings)은 env.py 주석 참조.
PSQL_OPTS="-X -v ON_ERROR_STOP=1"
MIG_LOCK="${DB_MIGRATION_LOCK_TIMEOUT:-60s}"
# SET 문에 그대로 삽입되므로 PG duration 문자셋(숫자+단위)만 허용한다.
case "$MIG_LOCK" in
    ''|*[!0-9a-zA-Z]*)
        echo "ERROR: DB_MIGRATION_LOCK_TIMEOUT='$MIG_LOCK' — '60s', '15min' 같은 형식만 허용" >&2
        exit 1 ;;
esac
SET_LOCK_TIMEOUT_SQL="SET lock_timeout = '${MIG_LOCK}';"

if [ -n "$MASTER_URL" ]; then
    echo "[migration] cloud mode — DB_MASTER_URL detected, applying init SQL"
    # ⚠️ 파일을 하나씩 열거하지 말 것 — 새로 추가된 init SQL 이 조용히 누락된다.
    #    실사고: 08_create_chat_reader.sql (2026-05-28 추가) 이 이 루프에 들어온 적이 없어
    #    cloud 모드에서 한 번도 실행되지 않았다. 결과: prod DB 에 gateway_chat_reader 롤 부재
    #    (2026-09-09 pg_roles 실측 — dev=존재(운영자 수동 생성), prod=부재).
    #    POSIX 글롭은 collation 순서로 확장되므로 01_ → 08_ 실행 순서는 그대로 보장되고,
    #    로컬 docker-compose 의 postgres /docker-entrypoint-initdb.d 도 *.sql 전체를
    #    실행하므로 와일드카드가 두 runner 의 동작을 일치시킨다.
    #    (드리프트 재발 방지 테스트: db/tests/test_init_sql_role_and_glob_guards.py —
    #     같은 파일이 "리터럴 비밀번호로 LOGIN 가능한 롤" 도 막는다. 이 SQL 들은 prod
    #     migration Job 에서도 실행되므로 리터럴 비번은 곧 prod 권한상승이다.)
    for f in /app/init/*.sql; do
        [ -f "$f" ] || continue
        echo "[migration]   applying $(basename "$f")"
        # -c → -f 순서로 같은 세션에서 실행된다 — SET 이 init SQL 전체에 적용.
        # shellcheck disable=SC2086  # PSQL_OPTS 는 플래그 목록 — 단어분리가 의도
        psql "$MASTER_URL_NO_PASS" $PSQL_OPTS -c "$SET_LOCK_TIMEOUT_SQL" -f "$f"
    done

    if [ -n "$APP_USER" ] && [ -n "$APP_PASSWORD" ]; then
        echo "[migration] creating/updating application user '$APP_USER'"
        # ⚠️ 비밀값 인용 규칙 — 아래 방법들이 전부 다른 이유로 실패한다:
        #   * 셸 치환 그대로 SQL 에 박기(이전 코드): 비밀번호에 ' 가 있으면 DO 블록 파괴
        #   * psql -v app_password=…: 비밀번호가 프로세스 argv 에 노출된다 (ps /proc/PID/cmdline)
        #   * psql \set app_password …: 공백 포함 값이 분리 인자로 깨진다
        #   * DO $$ 블록: 본문이 달러인용이라 내부에 psql 변수 치환 불가 + 비밀번호 속
        #     '$$' 가 블록을 조기 종료시킨다
        # 그래서 셸에서 SQL 리터럴/식별자 이스케이프를 직접 하고(표준 SQL 에는
        # ' 와 " 각각의 두배화만이 유일한 이스케이프 규칙), 결과를 리터럴 SQL 로
        # stdin 에 흘린다 — argv 노출 없음, 전 문자셋 지원, DO 블록 불요(\gexec 분기).
        APP_USER_L="'$(printf '%s' "$APP_USER" | sed "s/'/''/g")'"
        APP_PASSWORD_L="'$(printf '%s' "$APP_PASSWORD" | sed "s/'/''/g")'"
        # shellcheck disable=SC2086  # PSQL_OPTS 단어분리 의도
        psql "$MASTER_URL_NO_PASS" $PSQL_OPTS <<EOSQL
$SET_LOCK_TIMEOUT_SQL
SELECT format('CREATE ROLE %I WITH LOGIN PASSWORD %L', $APP_USER_L, $APP_PASSWORD_L)
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = $APP_USER_L)
\gexec
SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', $APP_USER_L, $APP_PASSWORD_L)
 WHERE EXISTS (SELECT FROM pg_roles WHERE rolname = $APP_USER_L)
\gexec
EOSQL

        echo "[migration] granting privileges on all schemas to '$APP_USER'"
        # GRANT 도 같은 방식 — schema 는 고정 whitelist 라 리터럴 두배화 불필요,
        # APP_USER 만 식별자 이스케이프 한다 (" 포함 사용자명 방어).
        APP_USER_I="\"$(printf '%s' "$APP_USER" | sed 's/"/""/g')\""
        for schema in $SCHEMAS; do
            # shellcheck disable=SC2086  # PSQL_OPTS 단어분리 의도
            psql "$MASTER_URL_NO_PASS" $PSQL_OPTS <<EOSQL
$SET_LOCK_TIMEOUT_SQL
GRANT USAGE, CREATE ON SCHEMA "$schema" TO $APP_USER_I;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "$schema" TO $APP_USER_I;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA "$schema" TO $APP_USER_I;
ALTER DEFAULT PRIVILEGES IN SCHEMA "$schema" GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO $APP_USER_I;
ALTER DEFAULT PRIVILEGES IN SCHEMA "$schema" GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO $APP_USER_I;
EOSQL
        done
        echo "[migration]   privileges granted on schemas: $SCHEMAS"
    else
        echo "[migration] WARNING: APP_DB_USER/APP_DB_PASSWORD 없음 — application user 처리 건너뜀"
    fi
else
    echo "[migration] local mode — DB_MASTER_URL 없음, init SQL 은 postgres 컨테이너가 실행했을 것"
fi

echo "[migration] upgrading to head"
alembic upgrade head
echo "[migration] done."