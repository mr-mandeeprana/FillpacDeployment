from __future__ import annotations

import threading

from .connection import database_connection


# ==========================================================
# SQL SERVER SCHEMA
# ==========================================================

SCHEMA_STATEMENTS = [

    # ======================================================
    # SYSTEM STATE
    # ======================================================

    """
    IF OBJECT_ID('dbo.system_state', 'U') IS NULL
    BEGIN
        CREATE TABLE dbo.system_state (
            id INT NOT NULL PRIMARY KEY,
            system_status NVARCHAR(50),
            model_loaded BIT NOT NULL DEFAULT 0,
            inference_manager_running BIT NOT NULL DEFAULT 0,
            elasticsearch_connected BIT NOT NULL DEFAULT 0,
            dashboard_enabled BIT NOT NULL DEFAULT 1,
            updated_at DATETIMEOFFSET NOT NULL
        );
    END
    """,

    # ======================================================
    # CAMERA STATUS
    # ======================================================

    """
    IF OBJECT_ID('dbo.camera_status', 'U') IS NULL
    BEGIN
        CREATE TABLE dbo.camera_status (
            id INT IDENTITY(1,1) PRIMARY KEY,
            camera_id NVARCHAR(100) NOT NULL UNIQUE,
            status NVARCHAR(50),
            fps FLOAT,
            frame_count BIGINT NOT NULL DEFAULT 0,
            last_seen DATETIMEOFFSET,
            updated_at DATETIMEOFFSET NOT NULL
        );
    END
    """,

    # ======================================================
    # PRODUCTION EVENTS
    # ======================================================

    """
    IF OBJECT_ID('dbo.production_events', 'U') IS NULL
    BEGIN
        CREATE TABLE dbo.production_events (
            id BIGINT IDENTITY(1,1) PRIMARY KEY,
            camera_id NVARCHAR(100) NOT NULL,
            timestamp DATETIMEOFFSET NOT NULL,
            bag_count INT NOT NULL DEFAULT 0,
            printed_count INT NOT NULL DEFAULT 0,
            unprinted_count INT NOT NULL DEFAULT 0,
            line_count INT NOT NULL DEFAULT 0,
            frame_roi_count INT NOT NULL DEFAULT 0,
            bags_inside_roi INT NOT NULL DEFAULT 0,
            metadata_json NVARCHAR(MAX),
            event_key NVARCHAR(255) NULL
        );
    END
    """,

    # ======================================================
    # SYSTEM STATE / CAMERA STATUS -- LIVE PAYLOAD COLUMN
    #
    # DashboardState used to publish its full in-memory
    # snapshot to dashboard/backend/state.json. That file is
    # being removed; the same snapshot now lives in SQL
    # Server instead. Rather than redesigning the dashboard
    # payload into many narrow columns (which would require
    # rewriting every dashboard consumer at the same time),
    # a `state_json` column holds the full snapshot for each
    # side, while the existing structured columns above stay
    # populated for simple/fast queries.
    # ======================================================

    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('dbo.system_state')
          AND name = 'state_json'
    )
    BEGIN
        ALTER TABLE dbo.system_state
        ADD state_json NVARCHAR(MAX) NULL;
    END
    """,

    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('dbo.camera_status')
          AND name = 'state_json'
    )
    BEGIN
        ALTER TABLE dbo.camera_status
        ADD state_json NVARCHAR(MAX) NULL;
    END
    """,

    # ======================================================
    # PRINT EVENTS
    # ======================================================

    """
    IF OBJECT_ID('dbo.print_events', 'U') IS NULL
    BEGIN
        CREATE TABLE dbo.print_events (
            id BIGINT IDENTITY(1,1) PRIMARY KEY,
            camera_id NVARCHAR(100) NOT NULL,
            timestamp DATETIMEOFFSET NOT NULL,
            track_id BIGINT,
            result NVARCHAR(50),
            confidence FLOAT,
            metadata_json NVARCHAR(MAX),
            event_key NVARCHAR(255) NULL
        );
    END
    """,

    # ======================================================
    # PRINT EVENTS -- COUNT COLUMNS
    #
    # print_count     : always 1 for every logged print result
    # total_count     : running number of print results for the
    #                   camera, continues across restarts
    # total_printed   : running number of 'printed' results
    # total_missing   : running number of 'missing' results
    #
    # One guarded ALTER per column (same reason as jam_events).
    # ======================================================

    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.print_events') AND name = 'print_count')
    BEGIN ALTER TABLE dbo.print_events ADD print_count INT NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.print_events') AND name = 'total_count')
    BEGIN ALTER TABLE dbo.print_events ADD total_count INT NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.print_events') AND name = 'total_printed')
    BEGIN ALTER TABLE dbo.print_events ADD total_printed INT NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.print_events') AND name = 'total_missing')
    BEGIN ALTER TABLE dbo.print_events ADD total_missing INT NULL; END
    """,

    # Tiny filtered index so the backfill check below is a seek,
    # not a table scan, on every schema initialization.
    """
    IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'idx_print_total_null' AND object_id = OBJECT_ID('dbo.print_events'))
    BEGIN
        CREATE INDEX idx_print_total_null
        ON dbo.print_events(id)
        WHERE total_count IS NULL;
    END
    """,

    # Backfill OLD rows once: print_count = 1 and running totals
    # per camera in id order. Idempotent (only rows still NULL).
    """
    IF EXISTS (SELECT 1 FROM dbo.print_events WHERE total_count IS NULL)
    BEGIN
        ;WITH t AS (
            SELECT
                id,
                ROW_NUMBER() OVER (PARTITION BY camera_id ORDER BY id) AS rn,
                SUM(CASE WHEN result = 'printed' THEN 1 ELSE 0 END)
                    OVER (PARTITION BY camera_id ORDER BY id ROWS UNBOUNDED PRECEDING) AS tp,
                SUM(CASE WHEN result = 'missing' THEN 1 ELSE 0 END)
                    OVER (PARTITION BY camera_id ORDER BY id ROWS UNBOUNDED PRECEDING) AS tm
            FROM dbo.print_events
        )
        UPDATE p
        SET p.print_count   = 1,
            p.total_count   = t.rn,
            p.total_printed = t.tp,
            p.total_missing = t.tm
        FROM dbo.print_events p
        JOIN t ON t.id = p.id
        WHERE p.total_count IS NULL;
    END
    """,

    """
    IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'idx_print_camera_total' AND object_id = OBJECT_ID('dbo.print_events'))
    BEGIN
        CREATE INDEX idx_print_camera_total
        ON dbo.print_events(camera_id, total_count DESC)
        INCLUDE (total_printed, total_missing);
    END
    """,

    # ======================================================
    # ROI SNAPSHOTS
    # ======================================================

    """
    IF OBJECT_ID('dbo.roi_snapshots', 'U') IS NULL
    BEGIN
        CREATE TABLE dbo.roi_snapshots (
            id BIGINT IDENTITY(1,1) PRIMARY KEY,
            camera_id NVARCHAR(100) NOT NULL,
            timestamp DATETIMEOFFSET NOT NULL,
            event_type NVARCHAR(100),
            image_path NVARCHAR(1000) NOT NULL,
            file_size BIGINT,
            width INT,
            height INT,
            sha256 NVARCHAR(128),
            metadata_json NVARCHAR(MAX),
            created_at DATETIMEOFFSET NOT NULL,
            event_key NVARCHAR(255) NULL
        );
    END
    """,

    # ======================================================
    # JAM EVENTS
    #
    # CONDITION A = MOVEMENT JAM
    # CONDITION B = BAG SPACING JAM
    # CONDITION C = ROI OCCUPANCY JAM
    #
    # All three conditions are stored in one table and
    # distinguished by condition_code / condition_name.
    # ======================================================

    """
    IF OBJECT_ID('dbo.jam_events', 'U') IS NULL
    BEGIN
        CREATE TABLE dbo.jam_events (
            id BIGINT IDENTITY(1,1) PRIMARY KEY,
            camera_id NVARCHAR(100) NOT NULL,
            start_time DATETIMEOFFSET NOT NULL,
            end_time DATETIMEOFFSET,
            duration_seconds FLOAT,
            jam_type NVARCHAR(100),
            condition_code NVARCHAR(10),
            condition_name NVARCHAR(50),
            status NVARCHAR(50),
            track_ids NVARCHAR(MAX),
            reason NVARCHAR(1000),
            roi_snapshot_id BIGINT,
            metadata_json NVARCHAR(MAX),
            created_at DATETIMEOFFSET NOT NULL,
            event_key NVARCHAR(255) NULL,

            CONSTRAINT FK_jam_roi_snapshot
                FOREIGN KEY (roi_snapshot_id)
                REFERENCES dbo.roi_snapshots(id)
        );
    END
    """,

    # ======================================================
    # JAM EVENTS -- condition_name column
    #
    # Added after jam_events already existed in some
    # deployments (condition_name was only ever written into
    # metadata_json, never as its own column). This ALTER
    # brings existing tables up to date the same way the
    # state_json columns above were added.
    # ======================================================

    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('dbo.jam_events')
          AND name = 'condition_name'
    )
    BEGIN
        ALTER TABLE dbo.jam_events
        ADD condition_name NVARCHAR(50) NULL;
    END
    """,

    # ======================================================
    # JAM EVENTS -- backfill condition_name on old rows
    #
    # Rows written before the column above existed have
    # condition_name = NULL. This backfills them from
    # condition_code every time the schema initializes --
    # idempotent (WHERE condition_name IS NULL), so it's a
    # no-op once everything is backfilled.
    # ======================================================

    """
    IF OBJECT_ID('dbo.jam_events', 'U') IS NOT NULL
    BEGIN
        UPDATE dbo.jam_events
        SET condition_name =
            CASE condition_code
                WHEN 'A' THEN 'MOVEMENT_JAM'
                WHEN 'B' THEN 'BAG_SPACING_JAM'
                WHEN 'C' THEN 'ROI_OCCUPANCY_JAM'
                ELSE condition_name
            END
        WHERE condition_name IS NULL
          AND condition_code IN ('A', 'B', 'C');
    END
    """,

    # ======================================================
    # JAM EVENTS -- normalized condition B/C detail columns
    #
    # Previously the only place these values existed was
    # inside metadata_json (spacing_result / condition_c_result),
    # which meant every dashboard/report query had to parse
    # JSON to read something as simple as bag_count or
    # minimum_gap_mm. These columns are additive (existing rows
    # simply have NULL until re-written) and metadata_json is
    # left in place unchanged for full-fidelity/audit purposes.
    #
    # IMPORTANT: each column gets its OWN guarded ALTER
    # statement here, rather than one ALTER ... ADD col1, col2,
    # ... bundle guarded by a single "does bag_count exist"
    # check. Some deployments already had a handful of these
    # columns (e.g. bag_count) from an earlier manual fix; a
    # single combined guard skipped the ENTIRE statement in
    # that case, silently leaving the *other* new columns
    # (pair_count, roi_x1, etc.) missing even though the code
    # now inserts into them. One guard per column makes this
    # idempotent no matter which subset already exists.
    # ======================================================
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.jam_events') AND name = 'bag_count')
    BEGIN ALTER TABLE dbo.jam_events ADD bag_count INT NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.jam_events') AND name = 'pair_count')
    BEGIN ALTER TABLE dbo.jam_events ADD pair_count INT NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.jam_events') AND name = 'active_jam_count')
    BEGIN ALTER TABLE dbo.jam_events ADD active_jam_count INT NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.jam_events') AND name = 'minimum_gap_mm')
    BEGIN ALTER TABLE dbo.jam_events ADD minimum_gap_mm DECIMAL(12,3) NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.jam_events') AND name = 'average_gap_mm')
    BEGIN ALTER TABLE dbo.jam_events ADD average_gap_mm DECIMAL(12,3) NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.jam_events') AND name = 'threshold_mm')
    BEGIN ALTER TABLE dbo.jam_events ADD threshold_mm DECIMAL(12,3) NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.jam_events') AND name = 'minimum_safe_gap_mm')
    BEGIN ALTER TABLE dbo.jam_events ADD minimum_safe_gap_mm DECIMAL(12,3) NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.jam_events') AND name = 'measurement_margin_mm')
    BEGIN ALTER TABLE dbo.jam_events ADD measurement_margin_mm DECIMAL(12,3) NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.jam_events') AND name = 'max_allowed_bags')
    BEGIN ALTER TABLE dbo.jam_events ADD max_allowed_bags INT NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.jam_events') AND name = 'occupancy_percent')
    BEGIN ALTER TABLE dbo.jam_events ADD occupancy_percent DECIMAL(10,3) NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.jam_events') AND name = 'jam_duration')
    BEGIN ALTER TABLE dbo.jam_events ADD jam_duration DECIMAL(12,3) NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.jam_events') AND name = 'direction')
    BEGIN ALTER TABLE dbo.jam_events ADD direction NVARCHAR(20) NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.jam_events') AND name = 'calibrated')
    BEGIN ALTER TABLE dbo.jam_events ADD calibrated BIT NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.jam_events') AND name = 'roi_x1')
    BEGIN ALTER TABLE dbo.jam_events ADD roi_x1 INT NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.jam_events') AND name = 'roi_y1')
    BEGIN ALTER TABLE dbo.jam_events ADD roi_y1 INT NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.jam_events') AND name = 'roi_x2')
    BEGIN ALTER TABLE dbo.jam_events ADD roi_x2 INT NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.jam_events') AND name = 'roi_y2')
    BEGIN ALTER TABLE dbo.jam_events ADD roi_y2 INT NULL; END
    """,

    # ======================================================
    # ROI SNAPSHOTS -- normalized JAM-snapshot detail columns
    #
    # Condition C snapshots previously carried this same
    # information only inside metadata_json
    # (condition_c_result). Kept in sync with the jam_events
    # columns above so both tables can be queried the same way.
    # Same one-guard-per-column reasoning as jam_events above.
    # ======================================================
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.roi_snapshots') AND name = 'source_event_id')
    BEGIN ALTER TABLE dbo.roi_snapshots ADD source_event_id NVARCHAR(100) NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.roi_snapshots') AND name = 'condition_code')
    BEGIN ALTER TABLE dbo.roi_snapshots ADD condition_code NVARCHAR(20) NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.roi_snapshots') AND name = 'condition_name')
    BEGIN ALTER TABLE dbo.roi_snapshots ADD condition_name NVARCHAR(100) NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.roi_snapshots') AND name = 'jam_type')
    BEGIN ALTER TABLE dbo.roi_snapshots ADD jam_type NVARCHAR(50) NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.roi_snapshots') AND name = 'jam_status')
    BEGIN ALTER TABLE dbo.roi_snapshots ADD jam_status NVARCHAR(30) NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.roi_snapshots') AND name = 'jam_detected')
    BEGIN ALTER TABLE dbo.roi_snapshots ADD jam_detected BIT NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.roi_snapshots') AND name = 'bag_count')
    BEGIN ALTER TABLE dbo.roi_snapshots ADD bag_count INT NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.roi_snapshots') AND name = 'minimum_gap_mm')
    BEGIN ALTER TABLE dbo.roi_snapshots ADD minimum_gap_mm DECIMAL(12,3) NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.roi_snapshots') AND name = 'average_gap_mm')
    BEGIN ALTER TABLE dbo.roi_snapshots ADD average_gap_mm DECIMAL(12,3) NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.roi_snapshots') AND name = 'threshold_mm')
    BEGIN ALTER TABLE dbo.roi_snapshots ADD threshold_mm DECIMAL(12,3) NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.roi_snapshots') AND name = 'max_allowed_bags')
    BEGIN ALTER TABLE dbo.roi_snapshots ADD max_allowed_bags INT NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.roi_snapshots') AND name = 'occupancy_percent')
    BEGIN ALTER TABLE dbo.roi_snapshots ADD occupancy_percent DECIMAL(10,3) NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.roi_snapshots') AND name = 'jam_duration')
    BEGIN ALTER TABLE dbo.roi_snapshots ADD jam_duration DECIMAL(12,3) NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.roi_snapshots') AND name = 'track_count')
    BEGIN ALTER TABLE dbo.roi_snapshots ADD track_count INT NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.roi_snapshots') AND name = 'track_ids_json')
    BEGIN ALTER TABLE dbo.roi_snapshots ADD track_ids_json NVARCHAR(MAX) NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.roi_snapshots') AND name = 'roi_x1')
    BEGIN ALTER TABLE dbo.roi_snapshots ADD roi_x1 INT NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.roi_snapshots') AND name = 'roi_y1')
    BEGIN ALTER TABLE dbo.roi_snapshots ADD roi_y1 INT NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.roi_snapshots') AND name = 'roi_x2')
    BEGIN ALTER TABLE dbo.roi_snapshots ADD roi_x2 INT NULL; END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.roi_snapshots') AND name = 'roi_y2')
    BEGIN ALTER TABLE dbo.roi_snapshots ADD roi_y2 INT NULL; END
    """,

    # ======================================================
    # JAM PAIR MEASUREMENTS
    #
    # Individual bag-to-bag spacing measurements (Condition B
    # `distances`/`pairs`/`jam_pairs`, Condition C `distances`).
    # These are per-event arrays and do not belong flattened
    # into jam_events/roi_snapshots columns -- one child row
    # per measured pair instead.
    # ======================================================

    """
    IF OBJECT_ID('dbo.jam_pair_measurements', 'U') IS NULL
    BEGIN
        CREATE TABLE dbo.jam_pair_measurements (
            id BIGINT IDENTITY(1,1) PRIMARY KEY,
            jam_event_id BIGINT NULL,
            snapshot_id BIGINT NULL,
            camera_id NVARCHAR(100) NOT NULL,
            front_track_id BIGINT,
            rear_track_id BIGINT,
            distance_mm DECIMAL(12,3),
            distance_px DECIMAL(12,3),
            threshold_mm DECIMAL(12,3),
            minimum_safe_gap_mm DECIMAL(12,3),
            measurement_margin_mm DECIMAL(12,3),
            jam_detected BIT,
            status NVARCHAR(30),
            front_center_x DECIMAL(12,3),
            front_center_y DECIMAL(12,3),
            rear_center_x DECIMAL(12,3),
            rear_center_y DECIMAL(12,3),
            front_bbox_json NVARCHAR(MAX),
            rear_bbox_json NVARCHAR(MAX),
            created_at DATETIMEOFFSET NOT NULL,

            CONSTRAINT FK_pair_jam_event
                FOREIGN KEY (jam_event_id)
                REFERENCES dbo.jam_events(id),

            CONSTRAINT FK_pair_snapshot
                FOREIGN KEY (snapshot_id)
                REFERENCES dbo.roi_snapshots(id)
        );
    END
    """,

    """
    IF OBJECT_ID('dbo.jam_pair_measurements', 'U') IS NOT NULL
       AND NOT EXISTS (
            SELECT 1 FROM sys.indexes
            WHERE name = 'idx_pair_jam_event'
              AND object_id = OBJECT_ID('dbo.jam_pair_measurements')
       )
    BEGIN
        CREATE INDEX idx_pair_jam_event
        ON dbo.jam_pair_measurements(jam_event_id);
    END
    """,

    # ======================================================
    # APPLICATION LOGS
    # ======================================================

    """
    IF OBJECT_ID('dbo.application_logs', 'U') IS NULL
    BEGIN
        CREATE TABLE dbo.application_logs (
            id BIGINT IDENTITY(1,1) PRIMARY KEY,
            timestamp DATETIMEOFFSET NOT NULL,
            level NVARCHAR(50),
            logger NVARCHAR(200),
            event_type NVARCHAR(100),
            camera_id NVARCHAR(100),
            message NVARCHAR(MAX),
            exception NVARCHAR(MAX),
            metadata_json NVARCHAR(MAX)
        );
    END
    """,

    # ======================================================
    # IDEMPOTENCY / DUPLICATE-PREVENTION KEYS
    #
    # Existing deployments are upgraded safely. New event writes
    # populate event_key and filtered UNIQUE indexes guarantee that
    # retries/replays cannot create a second row. NULL legacy rows
    # are intentionally excluded from the unique indexes.
    # ======================================================

    """
    IF OBJECT_ID('dbo.production_events', 'U') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.production_events') AND name = 'event_key')
    BEGIN
        ALTER TABLE dbo.production_events ADD event_key NVARCHAR(255) NULL;
    END
    """,

    """
    IF OBJECT_ID('dbo.print_events', 'U') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.print_events') AND name = 'event_key')
    BEGIN
        ALTER TABLE dbo.print_events ADD event_key NVARCHAR(255) NULL;
    END
    """,

    """
    IF OBJECT_ID('dbo.roi_snapshots', 'U') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.roi_snapshots') AND name = 'event_key')
    BEGIN
        ALTER TABLE dbo.roi_snapshots ADD event_key NVARCHAR(255) NULL;
    END
    """,

    """
    IF OBJECT_ID('dbo.jam_events', 'U') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.jam_events') AND name = 'event_key')
    BEGIN
        ALTER TABLE dbo.jam_events ADD event_key NVARCHAR(255) NULL;
    END
    """,

    """
    IF OBJECT_ID('dbo.production_events', 'U') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_production_event_key' AND object_id = OBJECT_ID('dbo.production_events'))
    BEGIN
        CREATE UNIQUE INDEX UX_production_event_key
        ON dbo.production_events(event_key)
        WHERE event_key IS NOT NULL;
    END
    """,

    """
    IF OBJECT_ID('dbo.print_events', 'U') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_print_event_key' AND object_id = OBJECT_ID('dbo.print_events'))
    BEGIN
        CREATE UNIQUE INDEX UX_print_event_key
        ON dbo.print_events(event_key)
        WHERE event_key IS NOT NULL;
    END
    """,

    """
    IF OBJECT_ID('dbo.roi_snapshots', 'U') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_roi_snapshot_event_key' AND object_id = OBJECT_ID('dbo.roi_snapshots'))
    BEGIN
        CREATE UNIQUE INDEX UX_roi_snapshot_event_key
        ON dbo.roi_snapshots(event_key)
        WHERE event_key IS NOT NULL;
    END
    """,

    """
    IF OBJECT_ID('dbo.jam_events', 'U') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_jam_event_key' AND object_id = OBJECT_ID('dbo.jam_events'))
    BEGIN
        CREATE UNIQUE INDEX UX_jam_event_key
        ON dbo.jam_events(event_key)
        WHERE event_key IS NOT NULL;
    END
    """,

    # ======================================================
    # INDEXES
    # ======================================================

    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.indexes
        WHERE name = 'idx_production_camera_time'
          AND object_id = OBJECT_ID('dbo.production_events')
    )
    BEGIN
        CREATE INDEX idx_production_camera_time
        ON dbo.production_events(camera_id, timestamp);
    END
    """,

    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.indexes
        WHERE name = 'idx_print_camera_time'
          AND object_id = OBJECT_ID('dbo.print_events')
    )
    BEGIN
        CREATE INDEX idx_print_camera_time
        ON dbo.print_events(camera_id, timestamp);
    END
    """,

    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.indexes
        WHERE name = 'idx_jam_camera_time'
          AND object_id = OBJECT_ID('dbo.jam_events')
    )
    BEGIN
        CREATE INDEX idx_jam_camera_time
        ON dbo.jam_events(camera_id, start_time);
    END
    """,

    """
    IF OBJECT_ID('dbo.jam_events', 'U') IS NOT NULL
       AND NOT EXISTS (
            SELECT 1
            FROM sys.indexes
            WHERE name = 'idx_jam_condition'
              AND object_id = OBJECT_ID('dbo.jam_events')
       )
    BEGIN
        CREATE INDEX idx_jam_condition
        ON dbo.jam_events(condition_code);
    END
    """,

    """
    IF OBJECT_ID('dbo.jam_events', 'U') IS NOT NULL
       AND NOT EXISTS (
            SELECT 1
            FROM sys.indexes
            WHERE name = 'idx_jam_camera_condition_time'
              AND object_id = OBJECT_ID('dbo.jam_events')
       )
    BEGIN
        CREATE INDEX idx_jam_camera_condition_time
        ON dbo.jam_events(camera_id, condition_code, start_time);
    END
    """,

    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.indexes
        WHERE name = 'idx_jam_status'
          AND object_id = OBJECT_ID('dbo.jam_events')
    )
    BEGIN
        CREATE INDEX idx_jam_status
        ON dbo.jam_events(status);
    END
    """,

    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.indexes
        WHERE name = 'idx_snapshot_camera_time'
          AND object_id = OBJECT_ID('dbo.roi_snapshots')
    )
    BEGIN
        CREATE INDEX idx_snapshot_camera_time
        ON dbo.roi_snapshots(camera_id, timestamp);
    END
    """,

    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.indexes
        WHERE name = 'idx_logs_time'
          AND object_id = OBJECT_ID('dbo.application_logs')
    )
    BEGIN
        CREATE INDEX idx_logs_time
        ON dbo.application_logs(timestamp);
    END
    """,

    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.indexes
        WHERE name = 'idx_camera_status'
          AND object_id = OBJECT_ID('dbo.camera_status')
    )
    BEGIN
        CREATE INDEX idx_camera_status
        ON dbo.camera_status(status);
    END
    """,

    # ======================================================
    # BATCH COUNTER (dashboard "set a limit and count to it")
    #
    # One row (id = 1). The counted bags are NOT stored here:
    # they are SUM(bag_count) over production_events since
    # started_at, so the counter is restart-safe and separate
    # from the all-time totals.
    # ======================================================

    """
    IF OBJECT_ID('dbo.batch_counter', 'U') IS NULL
    BEGIN
        CREATE TABLE dbo.batch_counter (
            id INT NOT NULL PRIMARY KEY,
            target INT NOT NULL,
            camera_id NVARCHAR(100) NULL,
            started_at DATETIMEOFFSET NOT NULL,
            is_active BIT NOT NULL DEFAULT 1,
            updated_at DATETIMEOFFSET NOT NULL
        );
    END
    """,

    # ======================================================
    # USERS (LOGIN)
    # ======================================================

    """
    IF OBJECT_ID('dbo.users', 'U') IS NULL
    BEGIN
        CREATE TABLE dbo.users (
            id INT IDENTITY(1,1) PRIMARY KEY,
            username NVARCHAR(100) NOT NULL UNIQUE,
            password_hash NVARCHAR(255) NOT NULL,
            password_salt NVARCHAR(64) NOT NULL,
            role NVARCHAR(50) NOT NULL DEFAULT 'operator',
            is_active BIT NOT NULL DEFAULT 1,
            created_at DATETIMEOFFSET NOT NULL,
            last_login_at DATETIMEOFFSET NULL
        );
    END
    """,

    # ======================================================
    # AUTH SESSIONS (LOGIN TOKENS)
    #
    # A logged-in browser holds one of these tokens (sent as
    # `Authorization: Bearer <token>`). Verifying a request is
    # just a lookup here instead of decoding a signed token, so
    # login/logout takes effect immediately and doesn't need any
    # extra crypto dependency beyond the Python standard library.
    # ======================================================

    """
    IF OBJECT_ID('dbo.auth_sessions', 'U') IS NULL
    BEGIN
        CREATE TABLE dbo.auth_sessions (
            token NVARCHAR(128) NOT NULL PRIMARY KEY,
            user_id INT NOT NULL,
            created_at DATETIMEOFFSET NOT NULL,
            expires_at DATETIMEOFFSET NOT NULL,
            last_seen_at DATETIMEOFFSET NOT NULL,
            CONSTRAINT fk_auth_sessions_user
                FOREIGN KEY (user_id) REFERENCES dbo.users(id)
                ON DELETE CASCADE
        );
    END
    """,

    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.indexes
        WHERE name = 'idx_auth_sessions_user'
          AND object_id = OBJECT_ID('dbo.auth_sessions')
    )
    BEGIN
        CREATE INDEX idx_auth_sessions_user
        ON dbo.auth_sessions(user_id);
    END
    """,

    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.indexes
        WHERE name = 'idx_auth_sessions_expires'
          AND object_id = OBJECT_ID('dbo.auth_sessions')
    )
    BEGIN
        CREATE INDEX idx_auth_sessions_expires
        ON dbo.auth_sessions(expires_at);
    END
    """,
]


# ==========================================================
# INITIALIZE SCHEMA
# ==========================================================

_schema_lock = threading.Lock()
_schema_ready = False


def initialize_schema() -> None:
    """
    Create/upgrade the schema.

    Called at the top of every repository function, so it must be
    cheap: the ~70 DDL statements run ONCE per process (guarded by
    a lock so two cameras starting together cannot run them at the
    same time and deadlock). If it fails, it is retried on the next
    call.
    """

    global _schema_ready

    if _schema_ready:
        return

    with _schema_lock:

        if _schema_ready:
            return

        with database_connection() as conn:

            cursor = conn.cursor()

            for statement in SCHEMA_STATEMENTS:
                cursor.execute(statement)

            conn.commit()

        _schema_ready = True