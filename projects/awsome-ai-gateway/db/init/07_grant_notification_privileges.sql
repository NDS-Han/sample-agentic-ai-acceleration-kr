-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- 07_grant_notification_privileges.sql
-- Must run AFTER 05_create_notification_schema.sql creates the tables
-- and AFTER 04_create_users.sql creates the notification_worker_user role

-- notification: SELECT + INSERT + UPDATE (NotificationConfig read, NotificationLog write)
GRANT USAGE ON SCHEMA notification TO notification_worker_user;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA notification TO notification_worker_user;

-- migration 0069: business Slack alert checks read-only usage inputs. No INSERT/UPDATE here.
GRANT USAGE ON SCHEMA usage TO notification_worker_user;
GRANT SELECT (status, mismatch_count, report_date, period, started_at, finished_at, checks)
    ON usage.reconciliation_runs TO notification_worker_user;
GRANT SELECT (reason, created_at) ON usage.cost_dead_letters TO notification_worker_user;
GRANT SELECT (model_alias, input_tokens, output_tokens, requested_at, pricing_missing)
    ON usage.usage_logs TO notification_worker_user;
