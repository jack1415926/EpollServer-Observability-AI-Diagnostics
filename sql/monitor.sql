-- Apply to the configured monitoring database with an administrator account.
-- The HTTP account only needs SELECT and INSERT on this table.
CREATE TABLE IF NOT EXISTS monitor_experiment (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    report JSON NOT NULL,
    created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX monitor_experiment_created (created_at, id)
);

-- Event writer needs SELECT, INSERT and UPDATE on this independent table.
CREATE TABLE IF NOT EXISTS monitor_event (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    instance_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    triggered_at_unix_ms BIGINT NOT NULL,
    report JSON NOT NULL,
    INDEX monitor_event_recent (triggered_at_unix_ms, id),
    INDEX monitor_event_active (instance_id, status)
);

-- Independent Python diagnosis service: SELECT/INSERT/UPDATE/DELETE on these two tables only.
CREATE TABLE IF NOT EXISTS diagnosis_task (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    question TEXT NOT NULL,
    status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    error_code VARCHAR(96) CHARACTER SET ascii COLLATE ascii_bin NULL,
    created_at_unix_ms BIGINT NOT NULL,
    updated_at_unix_ms BIGINT NOT NULL,
    expires_at_unix_ms BIGINT NOT NULL,
    INDEX diagnosis_task_recent (created_at_unix_ms, id),
    INDEX diagnosis_task_expiry (expires_at_unix_ms)
);
CREATE TABLE IF NOT EXISTS diagnosis_report (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    report JSON NOT NULL,
    expires_at_unix_ms BIGINT NOT NULL,
    INDEX diagnosis_report_expiry (expires_at_unix_ms)
);
