-- Today's frame schema, copied verbatim from src/akashic/storage/migrations (frames-related
-- statements only, in migration order). Generated for the identical-frame-text-reuse invent run.
--
-- Connection pragmas (src/akashic/storage/db.py connect(), applied by ref_today.connect):
--   PRAGMA key = "x'<64 hex>'";          -- raw 256-bit SQLCipher key, no PBKDF2
--   PRAGMA busy_timeout = 5000;
--   PRAGMA journal_mode = WAL;
--   PRAGMA synchronous = NORMAL;
--   PRAGMA foreign_keys = ON;
--   PRAGMA temp_store = MEMORY;
-- Writes use BEGIN IMMEDIATE ... COMMIT (db.transaction); isolation_level=None.
-- Not copied: tables of other kinds (audio_segments, embeddings, episodes, jobs, ...).

-- ===== 0001_frames.sql =====
-- Screen frames: one capture = one row; screenshot and text share the timestamp.
-- Timestamps are UTC milliseconds since the Unix epoch (capture time, never processing time).
CREATE TABLE frames (
    id           INTEGER PRIMARY KEY,
    ts_ms        INTEGER NOT NULL,
    monitor      INTEGER NOT NULL,
    process      TEXT,
    window_title TEXT,
    url          TEXT,
    text         TEXT,
    text_source  TEXT CHECK (text_source IN ('uia', 'ocr')),
    trigger      TEXT NOT NULL,
    image_path   TEXT          -- relative to media/; NULL when dropped (e.g. DRM black frame)
);

CREATE INDEX frames_ts ON frames (ts_ms);

-- remove_diacritics 2: "cafe" matches "café" (French OCR and typing).
CREATE VIRTUAL TABLE frames_fts USING fts5 (
    text,
    window_title,
    url,
    content = 'frames',
    content_rowid = 'id',
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER frames_ai AFTER INSERT ON frames BEGIN
    INSERT INTO frames_fts (rowid, text, window_title, url)
    VALUES (new.id, new.text, new.window_title, new.url);
END;

CREATE TRIGGER frames_ad AFTER DELETE ON frames BEGIN
    INSERT INTO frames_fts (frames_fts, rowid, text, window_title, url)
    VALUES ('delete', old.id, old.text, old.window_title, old.url);
END;

CREATE TRIGGER frames_au AFTER UPDATE ON frames BEGIN
    INSERT INTO frames_fts (frames_fts, rowid, text, window_title, url)
    VALUES ('delete', old.id, old.text, old.window_title, old.url);
    INSERT INTO frames_fts (rowid, text, window_title, url)
    VALUES (new.id, new.text, new.window_title, new.url);
END;

-- ===== 0002_sessions.sql (frames statement only) =====
-- Whether the frame shows the window that had keyboard focus (frames of other monitors do not).
-- App sessions are derived from focused frames only.
ALTER TABLE frames ADD COLUMN focused INTEGER NOT NULL DEFAULT 1;

-- ===== 0015_window_frames.sql =====
-- Existing screenshots are monitor views, not proof of selected-window ownership.
ALTER TABLE frames ADD COLUMN image_scope TEXT NOT NULL DEFAULT 'monitor'
    CHECK (image_scope IN ('monitor', 'window'));

-- ===== 0018_frame_passages.sql =====
-- Screen text uses the existing bounded passage strategy. No copied text or image embeddings.
CREATE TABLE frame_passages (
    frame_id INTEGER NOT NULL REFERENCES frames(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    start_char INTEGER NOT NULL CHECK (start_char >= 0),
    end_char INTEGER NOT NULL CHECK (end_char > start_char),
    dim INTEGER NOT NULL CHECK (dim > 0),
    vec BLOB NOT NULL CHECK (length(vec) = dim * 2),
    PRIMARY KEY (frame_id, model, start_char)
);
CREATE INDEX frame_passages_model ON frame_passages(model, frame_id);
CREATE TRIGGER frames_passages_au AFTER UPDATE OF text ON frames
WHEN old.text IS NOT new.text BEGIN
    DELETE FROM frame_passages WHERE frame_id = new.id;
END;
