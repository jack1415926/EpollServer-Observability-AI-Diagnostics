-- Apply to the configured monitoring database with an administrator account.
-- The HTTP account only needs SELECT and INSERT on this table.
CREATE TABLE IF NOT EXISTS monitor_experiment (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    report JSON NOT NULL,
    created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX monitor_experiment_created (created_at, id)
);
