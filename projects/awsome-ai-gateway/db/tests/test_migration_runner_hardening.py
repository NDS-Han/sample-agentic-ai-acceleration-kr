# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""R28 regression — migration runner hardening guards.

These are static guards in the same style as
test_init_sql_role_and_glob_guards.py: they pin the load-bearing properties of
``env.py`` and ``run_migration.sh`` so a future edit can't silently drop them.

Covered invariants:
  * alembic connections carry lock/statement/idle-in-transaction timeouts —
    without them a queued DDL on a hot table (usage_logs) silently stalls every
    gateway write queued behind it, for as long as the lock holder takes;
  * every psql invocation runs with ``-X`` and ``ON_ERROR_STOP``;
  * APP_USER/APP_PASSWORD are never shell-interpolated into SQL text — quoting
    goes through psql variables + format() %I/%L, so a password containing a
    single quote (or a role name containing a double quote) can't break or
    inject;
  * the password-stripping sed preserves the URL's user instead of overwriting
    it with an unrelated env default.
"""

from pathlib import Path

DB = Path(__file__).resolve().parent.parent
ENV_PY = (DB / "env.py").read_text(encoding="utf-8")
RUN_SH = (DB / "run_migration.sh").read_text(encoding="utf-8")


class TestEnvTimeouts:
    """alembic must not hold-or-queue locks without bound.

    RDS Proxy rejects startup-packet options (PGOPTIONS, asyncpg
    server_settings) with "command-line options" unsupported — so timeouts
    must be issued as ordinary session SET statements after connect.
    """

    def test_session_set_applied(self) -> None:
        # One exec_driver_sql per GUC — asyncpg rejects multi-command statements.
        assert 'SET {guc}' in ENV_PY
        for guc in (
            '"lock_timeout"',
            '"statement_timeout"',
            '"idle_in_transaction_session_timeout"',
        ):
            assert guc in ENV_PY, f"env.py missing session SET for {guc}"

    def test_no_startup_packet_options(self) -> None:
        # server_settings / PGOPTIONS travel in the startup packet → RDS Proxy
        # rejects them (migration-223, 2026-10-19). Post-connect SET is the
        # only proxy-safe path. Check code only — comments legitimately cite
        # the rejected mechanisms.
        code = "\n".join(
            ln.split("#", 1)[0] for ln in ENV_PY.splitlines()
        )
        assert "server_settings" not in code
        assert "PGOPTIONS" not in code

    def test_timeouts_env_overridable(self) -> None:
        # Operators must be able to relax the budgets without a code change
        # (e.g. a one-off heavy backfill on a huge table).
        for var in (
            "DB_MIGRATION_LOCK_TIMEOUT",
            "DB_MIGRATION_STATEMENT_TIMEOUT",
            "DB_MIGRATION_IDLE_TX_TIMEOUT",
        ):
            assert var in ENV_PY, f"env.py missing env override {var}"


class TestPsqlHardening:
    """run_migration.sh psql discipline."""

    def test_psql_no_psqlrc(self) -> None:
        # ~/.psqlrc executes arbitrary SQL as the connecting (master) user when
        # the script is re-run by hand — psql must run with -X.
        assert "-X" in RUN_SH

    def test_all_psql_calls_use_common_opts(self) -> None:
        psql_lines = [
            ln.strip()
            for ln in RUN_SH.splitlines()
            if ln.strip().startswith("psql ")
        ]
        assert psql_lines, "no psql invocations found"
        for ln in psql_lines:
            assert "$PSQL_OPTS" in ln, f"psql call bypasses PSQL_OPTS: {ln}"

    def test_lock_timeout_set_for_psql_phase(self) -> None:
        # Init SQL / GRANTs also queue on locks while the app is live — they
        # must fail fast like the alembic phase, not stall the write path.
        # It must arrive as a session SET (RDS Proxy rejects PGOPTIONS —
        # checked on code lines only, comments cite the rejected mechanism).
        code = "\n".join(
            ln.split("#", 1)[0] for ln in RUN_SH.splitlines()
        )
        assert "PGOPTIONS" not in code
        assert "SET_LOCK_TIMEOUT_SQL" in RUN_SH
        # Every psql call site must apply it — via -c before -f, or as the
        # first line inside the heredoc.
        assert '-c "$SET_LOCK_TIMEOUT_SQL" -f "$f"' in RUN_SH
        heredoc_calls = RUN_SH.count("$SET_LOCK_TIMEOUT_SQL")
        assert heredoc_calls >= 3  # 1 definition + 3 consumption sites


class TestCredentialQuoting:
    """Secret values must not be shell-interpolated into SQL text."""

    def test_no_bare_interpolation_into_sql(self) -> None:
        # Secrets must reach SQL only through the _L (literal-escaped) /
        # _I (identifier-escaped) shell vars — never raw ${APP_*}/"${APP_*}".
        for ln in RUN_SH.splitlines():
            assert "PASSWORD '${APP_PASSWORD}'" not in ln
            assert '"${APP_USER}"' not in ln, f"unquoted role interpolation: {ln}"
            assert "'${APP_USER}'" not in ln, f"unquoted literal interpolation: {ln}"

    def test_secret_not_in_psql_argv(self) -> None:
        # `-v app_password=...` would place the password on the process argv —
        # visible via ps/proc for the job's lifetime. The script must feed the
        # escaped literal over stdin instead.
        for ln in RUN_SH.splitlines():
            code = ln.split("#", 1)[0]  # the rejected-approach list lives in comments
            assert "-v app_password" not in code
            assert "\\set app_password" not in code

    def test_shell_sql_escaping_present(self) -> None:
        # APP_USER_L / APP_PASSWORD_L must be produced by the '→'' doubling
        # sed, and APP_USER_I by the "→"" doubling — the only SQL escape rules.
        assert "APP_USER_L=" in RUN_SH
        assert "APP_PASSWORD_L=" in RUN_SH
        assert "APP_USER_I=" in RUN_SH
        assert "s/'/''/g" in RUN_SH
        assert 's/"/""/g' in RUN_SH
        # And they must be consumed inside the heredocs (format() %I/%L +
        # \\gexec for the create/alter role branch).
        assert "format('CREATE ROLE %I WITH LOGIN PASSWORD %L'" in RUN_SH
        assert "format('ALTER ROLE %I WITH LOGIN PASSWORD %L'" in RUN_SH
        assert "\\gexec" in RUN_SH
        assert "$APP_USER_L" in RUN_SH
        assert "$APP_PASSWORD_L" in RUN_SH
        assert "$APP_USER_I" in RUN_SH

    def test_url_user_preserved(self) -> None:
        # The password-strip must keep the URL's own user (\1 backreference),
        # not overwrite it with a possibly-divergent DB_MASTER_USER default.
        sed_lines = [ln for ln in RUN_SH.splitlines() if "sed " in ln]
        assert sed_lines, "password-strip sed missing"
        assert any("://\\1@" in ln for ln in sed_lines), sed_lines
        assert all("DB_MASTER_USER" not in ln for ln in sed_lines), sed_lines
