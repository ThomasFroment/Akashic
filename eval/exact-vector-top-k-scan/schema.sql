-- Disposable copy of the tables the exact vector top-k scans read, as of migration 0018.
-- Sources: src/akashic/storage/migrations/
--   0001_frames.sql (frames, frames_ts), 0002_sessions.sql (frames.focused),
--   0003_audio.sql (audio_segments, audio_segments_start), 0009_confidence.sql (confidence),
--   0006_episodes.sql (embeddings, embeddings_time), 0013_segment_passages.sql,
--   0015_window_frames.sql (frames.image_scope), 0018_frame_passages.sql.
-- Omitted on purpose (never read by passages.top / embeddings.top, and the app runs no
-- ANALYZE, so the planner sees the same indexes): FTS tables and every trigger,
-- the partial status index, jobs, speakers, episodes and their side tables.

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
    image_path   TEXT,
    focused      INTEGER NOT NULL DEFAULT 1,
    image_scope  TEXT NOT NULL DEFAULT 'monitor' CHECK (image_scope IN ('monitor', 'window'))
);

CREATE INDEX frames_ts ON frames (ts_ms);

CREATE TABLE audio_segments (
    id           INTEGER PRIMARY KEY,
    stream       TEXT NOT NULL CHECK (stream IN ('mic', 'system')),
    start_ms     INTEGER NOT NULL,
    end_ms       INTEGER NOT NULL,
    speech_ms    INTEGER NOT NULL,
    sources      TEXT NOT NULL DEFAULT '[]',
    audio_path   TEXT,
    continues_id INTEGER REFERENCES audio_segments (id) ON DELETE SET NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'done', 'empty', 'failed')),
    language     TEXT,
    transcript   TEXT,
    speaker_id   INTEGER,
    confidence   REAL
);

CREATE INDEX audio_segments_start ON audio_segments (start_ms);

CREATE TABLE embeddings (
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('episode', 'segment', 'query')),
    owner_id   INTEGER NOT NULL,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vec        BLOB NOT NULL,
    start_ms   INTEGER NOT NULL,
    created_ms INTEGER NOT NULL,
    PRIMARY KEY (owner_kind, owner_id, model)
);

CREATE INDEX embeddings_time ON embeddings (owner_kind, model, start_ms);

CREATE TABLE segment_passages (
    segment_id INTEGER NOT NULL REFERENCES audio_segments(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    start_char INTEGER NOT NULL CHECK (start_char >= 0),
    end_char INTEGER NOT NULL CHECK (end_char > start_char),
    dim INTEGER NOT NULL CHECK (dim > 0),
    vec BLOB NOT NULL CHECK (length(vec) = dim * 2),
    PRIMARY KEY (segment_id, model, start_char)
);
CREATE INDEX segment_passages_model ON segment_passages(model, segment_id);

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
