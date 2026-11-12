-- The whole Akashic schema. Later changes go in new numbered files (0002_name.sql, ...).
-- Timestamps are UTC milliseconds since the Unix epoch, taken at capture time (when the frame
-- was seen or the sound was heard), never at processing time.
--
-- Trigger order matters: SQLite runs the triggers of one event in reverse creation order, so
-- keep each table's triggers in the order they are written here.


-- ============================================================================================
-- Screen frames
-- ============================================================================================

-- One capture = one row; screenshot and text share the timestamp.
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
    image_path   TEXT,         -- relative to media/; NULL when dropped (e.g. DRM black frame)
    -- Whether the frame shows the window that had keyboard focus (frames of other monitors do
    -- not). App sessions are derived from focused frames only.
    focused      INTEGER NOT NULL DEFAULT 1
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


-- ============================================================================================
-- Media, app and game sessions
-- ============================================================================================

-- What was playing: one row per (source app, title, artist) run. played_ms counts only the time
-- spent in the playing state; the row stays open across short pauses.
CREATE TABLE media_sessions (
    id        INTEGER PRIMARY KEY,
    app_id    TEXT NOT NULL,   -- opaque AUMID published by the source
    app       TEXT NOT NULL,   -- display name resolved from the AUMID ("Zen"), or the AUMID
    title     TEXT NOT NULL,
    artist    TEXT,            -- channel for YouTube
    url       TEXT,            -- from the browser's address bar when it could be matched
    start_ms  INTEGER NOT NULL,
    end_ms    INTEGER NOT NULL,
    played_ms INTEGER NOT NULL DEFAULT 0,
    closed    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX media_sessions_start ON media_sessions (start_ms);
CREATE INDEX media_sessions_open ON media_sessions (closed) WHERE closed = 0;

-- Playback state changes inside a media session: playing, paused, stopped.
CREATE TABLE media_events (
    id               INTEGER PRIMARY KEY,
    media_session_id INTEGER NOT NULL REFERENCES media_sessions (id) ON DELETE CASCADE,
    ts_ms            INTEGER NOT NULL,
    state            TEXT NOT NULL
);

CREATE INDEX media_events_session ON media_events (media_session_id, ts_ms);

CREATE VIRTUAL TABLE media_sessions_fts USING fts5 (
    title,
    artist,
    app,
    content = 'media_sessions',
    content_rowid = 'id',
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER media_sessions_ai AFTER INSERT ON media_sessions BEGIN
    INSERT INTO media_sessions_fts (rowid, title, artist, app)
    VALUES (new.id, new.title, new.artist, new.app);
END;

CREATE TRIGGER media_sessions_ad AFTER DELETE ON media_sessions BEGIN
    INSERT INTO media_sessions_fts (media_sessions_fts, rowid, title, artist, app)
    VALUES ('delete', old.id, old.title, old.artist, old.app);
END;

-- Only the searchable columns: end_ms and played_ms change every few seconds.
CREATE TRIGGER media_sessions_au AFTER UPDATE OF title, artist, app ON media_sessions BEGIN
    INSERT INTO media_sessions_fts (media_sessions_fts, rowid, title, artist, app)
    VALUES ('delete', old.id, old.title, old.artist, old.app);
    INSERT INTO media_sessions_fts (rowid, title, artist, app)
    VALUES (new.id, new.title, new.artist, new.app);
END;

-- Contiguous focus periods, derived from focused frames by the worker.
CREATE TABLE app_sessions (
    id            INTEGER PRIMARY KEY,
    process       TEXT,
    window_title  TEXT,
    url           TEXT,
    start_ms      INTEGER NOT NULL,
    end_ms        INTEGER NOT NULL,
    frame_count   INTEGER NOT NULL,
    last_frame_id INTEGER NOT NULL,  -- derivation watermark
    closed        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX app_sessions_start ON app_sessions (start_ms);

-- A fullscreen game in the foreground. end_ms is a heartbeat while the row is open, so a crash
-- leaves it accurate to a few seconds, and the scheduler can ask "is a game running right now".
CREATE TABLE game_sessions (
    id           INTEGER PRIMARY KEY,
    process      TEXT NOT NULL,
    window_title TEXT,
    start_ms     INTEGER NOT NULL,
    end_ms       INTEGER NOT NULL,
    closed       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX game_sessions_start ON game_sessions (start_ms);


-- ============================================================================================
-- Audio segments and the job queue
-- ============================================================================================

-- One row per stretch of speech. Raw audio is an Opus file (encrypted) under media/.
CREATE TABLE audio_segments (
    id           INTEGER PRIMARY KEY,
    stream       TEXT NOT NULL CHECK (stream IN ('mic', 'system')),
    start_ms     INTEGER NOT NULL,
    end_ms       INTEGER NOT NULL,
    speech_ms    INTEGER NOT NULL,           -- time the voice detector called speech
    -- JSON array of lower-case executable names that were making sound during the segment
    -- ("discord.exe", "chrome.exe"): how a transcript is attributed to a call or a video.
    sources      TEXT NOT NULL DEFAULT '[]',
    audio_path   TEXT,                       -- relative to media/; NULL once raw audio is deleted
    -- A segment cut at the maximum length starts with the tail of the previous one, so no
    -- word is split; the transcript of the overlap is removed again after transcription.
    continues_id INTEGER REFERENCES audio_segments (id) ON DELETE SET NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'done', 'empty', 'failed')),
    language     TEXT,                       -- 'fr' or 'en'
    transcript   TEXT,
    speaker_id   INTEGER,
    -- How sure Whisper was of the transcript: exp of its mean log-probability per token, 0-1.
    -- NULL when unknown. Low values are shown and cited as "unclear"
    -- (pipeline/transcribe.py, UNCLEAR_BELOW).
    confidence   REAL,
    transcription_model TEXT
);

CREATE INDEX audio_segments_start ON audio_segments (start_ms);
CREATE INDEX audio_segments_status ON audio_segments (status) WHERE status = 'pending';
-- continues_id is a self-referencing foreign key: without an index, every segment deletion
-- scans the whole table to check for children.
CREATE INDEX audio_segments_continues ON audio_segments (continues_id);

CREATE VIRTUAL TABLE audio_segments_fts USING fts5 (
    transcript,
    content = 'audio_segments',
    content_rowid = 'id',
    tokenize = 'unicode61 remove_diacritics 2'
);

-- Every row is indexed from the start (its transcript is NULL until Whisper has run), so the
-- 'delete' commands below always remove exactly what was indexed.
CREATE TRIGGER audio_segments_ai AFTER INSERT ON audio_segments BEGIN
    INSERT INTO audio_segments_fts (rowid, transcript) VALUES (new.id, new.transcript);
END;

CREATE TRIGGER audio_segments_ad AFTER DELETE ON audio_segments BEGIN
    INSERT INTO audio_segments_fts (audio_segments_fts, rowid, transcript)
    VALUES ('delete', old.id, old.transcript);
END;

-- Only when the transcript changes: nothing else about a segment is searchable.
CREATE TRIGGER audio_segments_au AFTER UPDATE OF transcript ON audio_segments BEGIN
    INSERT INTO audio_segments_fts (audio_segments_fts, rowid, transcript)
    VALUES ('delete', old.id, old.transcript);
    INSERT INTO audio_segments_fts (rowid, transcript) VALUES (new.id, new.transcript);
END;

-- The queue every heavy task goes through (transcription, speakers, embeddings, episodes, ask).
-- Jobs are idempotent: ``key`` is unique, so enqueueing the same work twice is a no-op.
CREATE TABLE jobs (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    key         TEXT NOT NULL UNIQUE,        -- e.g. 'transcribe:42'
    payload     TEXT NOT NULL DEFAULT '{}',  -- JSON, ids only: never captured content
    data_ms     INTEGER NOT NULL,            -- capture time of the work: oldest is processed first
    state       TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'running', 'done', 'failed')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    run_after_ms INTEGER NOT NULL DEFAULT 0, -- retry backoff
    created_ms  INTEGER NOT NULL,
    finished_ms INTEGER,
    error       TEXT                         -- exception type name only: messages can quote data
);

CREATE INDEX jobs_pending ON jobs (state, data_ms, id) WHERE state = 'pending';


-- ============================================================================================
-- Process info and state
-- ============================================================================================

-- Debugging: what each process says about itself, for the Debug page.
-- One row per process, replaced every few seconds. Numbers, states and error type names only:
-- never captured content (the same rule as the logs).
CREATE TABLE process_info (
    name       TEXT PRIMARY KEY,
    updated_ms INTEGER NOT NULL,
    data       TEXT NOT NULL   -- JSON object
);

-- Small named integers the processes keep between passes: watermarks and short-lived flags
-- (e.g. how far the conversation builder has looked, until when the mic meter is wanted).
CREATE TABLE state (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);


-- ============================================================================================
-- Speakers
-- ============================================================================================

-- Speakers are voice profiles (an average embedding), named by the user; a turn is one
-- speaker's part of one audio segment.
CREATE TABLE speakers (
    id         INTEGER PRIMARY KEY,
    name       TEXT,                        -- NULL until the user names the voice
    is_self    INTEGER NOT NULL DEFAULT 0,  -- the user: everything on the microphone stream
    -- Sum of the unit-length embeddings that built the profile (float32 BLOB) and how many
    -- there are: the centroid is their normalised mean, and an embedding can be taken out again.
    vec_sum    BLOB,
    vec_n      INTEGER NOT NULL DEFAULT 0,
    created_ms INTEGER NOT NULL
);

CREATE UNIQUE INDEX speakers_self ON speakers (is_self) WHERE is_self = 1;

CREATE TABLE speaker_turns (
    id         INTEGER PRIMARY KEY,
    segment_id INTEGER NOT NULL REFERENCES audio_segments (id) ON DELETE CASCADE,
    speaker_id INTEGER REFERENCES speakers (id) ON DELETE SET NULL,  -- NULL: too short to tell
    start_ms   INTEGER NOT NULL,            -- offsets from the start of the segment
    end_ms     INTEGER NOT NULL,
    speech_ms  INTEGER NOT NULL,
    embedding  BLOB,                        -- float32, unit length; NULL under one second
    in_profile INTEGER NOT NULL DEFAULT 0   -- this embedding is part of the speaker's vec_sum
);

CREATE INDEX speaker_turns_segment ON speaker_turns (segment_id);
CREATE INDEX speaker_turns_speaker ON speaker_turns (speaker_id);


-- ============================================================================================
-- Episodes and embeddings
-- ============================================================================================

-- An episode is a closed period with one subject: a video watched, a call, a game, a stretch of
-- browsing or of work. Built by the worker from the capture tables; the evidence stays in those
-- tables and is linked through episode_items.
CREATE TABLE episodes (
    id       INTEGER PRIMARY KEY,
    kind     TEXT NOT NULL CHECK (kind IN ('video', 'music', 'conversation', 'browsing', 'work', 'game')),
    -- Idempotency key naming the source ('media:12', 'game:3', 'conversation:41', 'browsing:77'),
    -- so a pass that runs twice builds nothing twice.
    ref      TEXT NOT NULL UNIQUE,
    start_ms INTEGER NOT NULL,
    end_ms   INTEGER NOT NULL,
    title    TEXT NOT NULL,           -- the video title, the game, "Discord call", the main window
    app      TEXT,                    -- executable or app name the episode happened in
    url      TEXT,
    -- A digest of the evidence (transcript lines, window titles): what full-text search and the
    -- embedding see. Rebuilt from the evidence, never edited. No language-model summary is
    -- ever written (it would cost GPU time and model swaps all day).
    body     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX episodes_time ON episodes (start_ms);
CREATE INDEX episodes_kind_time ON episodes (kind, start_ms);

-- Who took part. Speakers are stored by id so renaming a voice renames it everywhere.
CREATE TABLE episode_people (
    episode_id INTEGER NOT NULL REFERENCES episodes (id) ON DELETE CASCADE,
    speaker_id INTEGER NOT NULL REFERENCES speakers (id) ON DELETE CASCADE,
    speech_ms  INTEGER NOT NULL,
    PRIMARY KEY (episode_id, speaker_id)
);

CREATE INDEX episode_people_speaker ON episode_people (speaker_id);

-- The rows an episode is made of. item_kind: 'media_session', 'app_session', 'game_session',
-- 'segment' (an audio segment), 'video_segment'. An item is used by at most one episode of the
-- kinds that consume it; that is how a pass knows what is still unassigned.
CREATE TABLE episode_items (
    episode_id INTEGER NOT NULL REFERENCES episodes (id) ON DELETE CASCADE,
    item_kind  TEXT NOT NULL,
    item_id    INTEGER NOT NULL,
    PRIMARY KEY (episode_id, item_kind, item_id)
);

CREATE INDEX episode_items_item ON episode_items (item_kind, item_id);

-- Vectors for semantic search, one per (owner, model): the first 256 dimensions of
-- Qwen3-Embedding (trained for this "MRL" truncation), re-normalised to unit length and stored
-- as float16, so a dot product is the cosine similarity. owner_kind: 'episode', 'segment'
-- (a transcript) or 'query' (the embedding of a search question, owner_id a hash of its text;
-- the worker makes it for the API and it is deleted after a day).
-- ``model`` names the embedding model: vectors of different models are never compared.
CREATE TABLE embeddings (
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('episode', 'segment', 'query')),
    owner_id   INTEGER NOT NULL,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vec        BLOB NOT NULL,
    start_ms   INTEGER NOT NULL,      -- capture time of the owner: filters and retention
    created_ms INTEGER NOT NULL,
    PRIMARY KEY (owner_kind, owner_id, model)
);

CREATE INDEX embeddings_time ON embeddings (owner_kind, model, start_ms);

-- A transcript that goes away (retention, a new Whisper run) takes its vector with it.
CREATE TRIGGER audio_segments_embedding_ad AFTER DELETE ON audio_segments BEGIN
    DELETE FROM embeddings WHERE owner_kind = 'segment' AND owner_id = old.id;
END;

CREATE TRIGGER audio_segments_embedding_au AFTER UPDATE OF transcript ON audio_segments BEGIN
    DELETE FROM embeddings WHERE owner_kind = 'segment' AND owner_id = new.id;
END;

CREATE VIRTUAL TABLE episodes_fts USING fts5 (
    title,
    body,
    content = 'episodes',
    content_rowid = 'id',
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER episodes_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO episodes_fts (rowid, title, body) VALUES (new.id, new.title, new.body);
END;

CREATE TRIGGER episodes_ad AFTER DELETE ON episodes BEGIN
    INSERT INTO episodes_fts (episodes_fts, rowid, title, body)
    VALUES ('delete', old.id, old.title, old.body);
    DELETE FROM embeddings WHERE owner_kind = 'episode' AND owner_id = old.id;
END;

CREATE TRIGGER episodes_au AFTER UPDATE OF title, body ON episodes BEGIN
    INSERT INTO episodes_fts (episodes_fts, rowid, title, body)
    VALUES ('delete', old.id, old.title, old.body);
    INSERT INTO episodes_fts (rowid, title, body) VALUES (new.id, new.title, new.body);
    -- The text changed, so the vector no longer describes it: it is made again.
    DELETE FROM embeddings WHERE owner_kind = 'episode' AND owner_id = new.id;
END;


-- ============================================================================================
-- Questions
-- ============================================================================================

-- Questions asked in the Memory page and the answers written for them. The API process queues
-- an 'ask' job for each question; the worker (which owns the language model) fills in the row
-- while the page polls it. 'answer' grows as the model writes, so the page can show it
-- streaming. AUTOINCREMENT: a forgotten question must never donate its id to a new one, because
-- ask:<id> remains in the durable job queue after a question is deleted.
CREATE TABLE questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asked_ms    INTEGER NOT NULL,
    text        TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'working', 'done', 'failed')),
    step        TEXT NOT NULL DEFAULT '',   -- 'planning', 'searching', 'answering' (for the page)
    -- JSON: what the planner understood (time range, people, kinds, keywords), shown to the user.
    plan        TEXT,
    answer      TEXT NOT NULL DEFAULT '',   -- prose with [n] markers pointing into 'sources'
    -- JSON list of the numbered items the answer may cite: kind (episode, line, frame), row id,
    -- time, a label and a short snippet. Only these can be cited.
    sources     TEXT,
    model       TEXT,                       -- the chat model that wrote the answer
    error       TEXT,                       -- a short reason without content
    finished_ms INTEGER,
    -- Follow-up questions: a question asked in reply to earlier ones belongs to their
    -- conversation, named by the id of its first question. NULL: the question starts one.
    thread_id   INTEGER REFERENCES questions (id) ON DELETE CASCADE
);

CREATE INDEX questions_asked ON questions (asked_ms);
CREATE INDEX questions_thread ON questions (thread_id, id);


-- ============================================================================================
-- Passage vectors
-- ============================================================================================

-- Passage vectors keep text positions without duplicating captured text. Sets are published
-- atomically by the worker.
CREATE TABLE segment_passages (
    segment_id INTEGER NOT NULL REFERENCES audio_segments (id) ON DELETE CASCADE,
    model      TEXT NOT NULL,
    start_char INTEGER NOT NULL CHECK (start_char >= 0),
    end_char   INTEGER NOT NULL CHECK (end_char > start_char),
    dim        INTEGER NOT NULL CHECK (dim > 0),
    vec        BLOB NOT NULL CHECK (length(vec) = dim * 2),
    PRIMARY KEY (segment_id, model, start_char)
);

CREATE INDEX segment_passages_model ON segment_passages (model, segment_id);

CREATE TRIGGER audio_segments_passages_au AFTER UPDATE OF transcript ON audio_segments BEGIN
    DELETE FROM segment_passages WHERE segment_id = new.id;
END;


-- ============================================================================================
-- Speech-to-text comparison
-- ============================================================================================

-- User-selected, hand-corrected comparison material stays inside SQLCipher.
CREATE TABLE stt_samples (
    segment_id         INTEGER PRIMARY KEY REFERENCES audio_segments (id) ON DELETE CASCADE,
    reference          TEXT NOT NULL,
    terms              TEXT NOT NULL DEFAULT '[]',
    reference_language TEXT NOT NULL,
    condition          TEXT NOT NULL,
    draft_model        TEXT NOT NULL,
    draft_derived      INTEGER NOT NULL,
    previous_reference TEXT NOT NULL DEFAULT '',
    overlap_reference  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE stt_runs (
    id                 INTEGER PRIMARY KEY,
    created_ms         INTEGER NOT NULL,
    state              TEXT NOT NULL DEFAULT 'queued',
    vocabulary         TEXT NOT NULL,
    processing_version TEXT NOT NULL
);

CREATE TABLE stt_results (
    run_id             INTEGER NOT NULL REFERENCES stt_runs (id) ON DELETE CASCADE,
    segment_id         INTEGER NOT NULL REFERENCES stt_samples (segment_id) ON DELETE CASCADE,
    model              TEXT NOT NULL,
    revision           TEXT NOT NULL,
    reference          TEXT NOT NULL,
    terms              TEXT NOT NULL,
    prediction         TEXT,
    language           TEXT,
    elapsed_ms         INTEGER,
    errors             INTEGER,
    words              INTEGER,
    term_hits          INTEGER,
    term_total         INTEGER,
    error              TEXT,
    reference_language TEXT NOT NULL,
    condition          TEXT NOT NULL,
    draft_model        TEXT NOT NULL,
    draft_derived      INTEGER NOT NULL,
    previous_reference TEXT NOT NULL DEFAULT '',
    overlap_reference  TEXT NOT NULL DEFAULT '',
    char_errors        INTEGER,
    chars              INTEGER,
    PRIMARY KEY (run_id, segment_id, model)
);

-- Text expiry removes comparison copies too; raw-audio-only expiry is independent.
CREATE TRIGGER stt_transcript_expiry AFTER UPDATE OF transcript ON audio_segments
WHEN new.transcript IS NULL BEGIN
    DELETE FROM stt_samples WHERE segment_id = new.id;
END;


-- ============================================================================================
-- Transcript copies: provenance of speech copied into answers and episode digests
-- ============================================================================================

-- Keeps provenance independently of episode retention. A line excerpt may contain neighbours.
-- No segment foreign key: deletion must still find its saved copies in the AFTER trigger.
CREATE TABLE question_segments (
    question_id INTEGER NOT NULL REFERENCES questions (id) ON DELETE CASCADE,
    segment_id  INTEGER NOT NULL,
    PRIMARY KEY (question_id, segment_id)
);

CREATE INDEX question_segments_segment ON question_segments (segment_id, question_id);

-- Rebuilds only speech-derived bodies; independent titles, URLs, evidence and people stay.
-- Must produce the same bodies as the Python builders in pipeline/episodes.py.
CREATE VIEW retained_episode_bodies AS
SELECT id, CASE WHEN length(body) > 6000 THEN rtrim(substr(body, 1, 5999)) || '…'
               ELSE body END AS body FROM (
    SELECT e.id, CASE WHEN e.kind = 'conversation' THEN coalesce((
        SELECT group_concat(a.transcript, ' ' ORDER BY a.start_ms, a.id)
        FROM episode_items i JOIN audio_segments a ON a.id = i.item_id
        WHERE i.episode_id = e.id AND i.item_kind = 'segment'
          AND a.transcript IS NOT NULL AND a.transcript != ''
    ), '') ELSE
        e.title || coalesce((SELECT ' - ' || m.artist FROM episode_items i
            JOIN media_sessions m ON m.id = i.item_id WHERE i.episode_id = e.id
            AND i.item_kind = 'media_session' AND m.artist != '' LIMIT 1), '')
        || CASE WHEN e.app IS NULL THEN '' ELSE ' (' || e.app || ')' END
        || coalesce((SELECT char(10) || group_concat(a.transcript, ' ' ORDER BY a.start_ms, a.id)
            FROM episode_items i JOIN audio_segments a ON a.id = i.item_id
            WHERE i.episode_id = e.id AND i.item_kind = 'video_segment'
              AND a.transcript IS NOT NULL AND a.transcript != ''), '')
        END AS body FROM episodes e WHERE e.kind IN ('conversation', 'video', 'music')
);

-- Both triggers run in the original write transaction: a failed write restores every copy.
CREATE TRIGGER transcript_copies_au AFTER UPDATE OF transcript ON audio_segments
WHEN old.transcript IS NOT new.transcript BEGIN
    UPDATE questions SET state = 'failed', step = '', plan = NULL, answer = '', sources = '[]',
        model = NULL, error = 'Source transcripts changed. Ask again.', finished_ms = NULL
    WHERE old.transcript IS NOT NULL AND (state IN ('queued', 'working') OR id IN (
        SELECT later.id FROM question_segments p JOIN questions affected ON affected.id = p.question_id
        JOIN questions later ON coalesce(later.thread_id, later.id) = coalesce(affected.thread_id, affected.id)
        AND later.id >= affected.id WHERE p.segment_id = old.id
    ));
    UPDATE episodes SET body = (SELECT r.body FROM retained_episode_bodies r WHERE r.id = episodes.id)
    WHERE kind IN ('conversation', 'video', 'music') AND id IN
        (SELECT episode_id FROM episode_items WHERE item_kind IN ('segment', 'video_segment') AND item_id = old.id);
END;

CREATE TRIGGER transcript_copies_ad AFTER DELETE ON audio_segments BEGIN
    UPDATE questions SET state = 'failed', step = '', plan = NULL, answer = '', sources = '[]',
        model = NULL, error = 'Source transcripts changed. Ask again.', finished_ms = NULL
    WHERE state IN ('queued', 'working') OR id IN (
        SELECT later.id FROM question_segments p JOIN questions affected ON affected.id = p.question_id
        JOIN questions later ON coalesce(later.thread_id, later.id) = coalesce(affected.thread_id, affected.id)
        AND later.id >= affected.id WHERE p.segment_id = old.id
    );
    UPDATE episodes SET body = (SELECT r.body FROM retained_episode_bodies r WHERE r.id = episodes.id)
    WHERE kind IN ('conversation', 'video', 'music') AND id IN
        (SELECT episode_id FROM episode_items WHERE item_kind IN ('segment', 'video_segment') AND item_id = old.id);
    DELETE FROM episode_items WHERE item_kind IN ('segment', 'video_segment') AND item_id = old.id;
END;


-- ============================================================================================
-- Screen-text passages and copies
-- ============================================================================================

-- Screen text uses the same bounded passage strategy. No copied text or image embeddings.
CREATE TABLE frame_passages (
    frame_id   INTEGER NOT NULL REFERENCES frames (id) ON DELETE CASCADE,
    model      TEXT NOT NULL,
    start_char INTEGER NOT NULL CHECK (start_char >= 0),
    end_char   INTEGER NOT NULL CHECK (end_char > start_char),
    dim        INTEGER NOT NULL CHECK (dim > 0),
    vec        BLOB NOT NULL CHECK (length(vec) = dim * 2),
    PRIMARY KEY (frame_id, model, start_char)
);

CREATE INDEX frame_passages_model ON frame_passages (model, frame_id);

CREATE TRIGGER frames_passages_au AFTER UPDATE OF text ON frames
WHEN old.text IS NOT new.text BEGIN
    DELETE FROM frame_passages WHERE frame_id = new.id;
END;

-- Draft-derived references and copied context are transcript-derived text, so they are cleared
-- when any transcript expires or is deleted (no context source IDs are tracked). Samples whose
-- reference is independent and whose context is empty survive unrelated expiry; a segment's own
-- sample is removed with it.
CREATE TRIGGER stt_context_expiry AFTER UPDATE OF transcript ON audio_segments
WHEN old.transcript IS NOT NULL AND new.transcript IS NOT old.transcript BEGIN
    DELETE FROM stt_samples
    WHERE draft_derived = 1 OR previous_reference <> '' OR overlap_reference <> '';
END;

CREATE TRIGGER stt_context_delete AFTER DELETE ON audio_segments
WHEN old.transcript IS NOT NULL BEGIN
    DELETE FROM stt_samples
    WHERE segment_id = old.id OR draft_derived = 1
        OR previous_reference <> '' OR overlap_reference <> '';
END;

-- Keeps screen-text provenance independently of frame retention, like question_segments.
-- No frame foreign key: deletion must still find its saved copies in the AFTER trigger.
CREATE TABLE question_frames (
    question_id INTEGER NOT NULL REFERENCES questions (id) ON DELETE CASCADE,
    frame_id    INTEGER NOT NULL,
    PRIMARY KEY (question_id, frame_id)
);

CREATE INDEX question_frames_frame ON question_frames (frame_id, question_id);

-- Both triggers run in the original write transaction: a failed write restores every copy.
-- Questions still queued or working may hold the old text in memory: they must not publish it.
CREATE TRIGGER frame_text_copies_au AFTER UPDATE OF text ON frames
WHEN old.text IS NOT NULL AND old.text IS NOT new.text BEGIN
    UPDATE questions SET state = 'failed', step = '', plan = NULL, answer = '', sources = '[]',
        model = NULL, error = 'Source screen text changed. Ask again.', finished_ms = NULL
    WHERE state IN ('queued', 'working') OR id IN (
        SELECT later.id FROM question_frames p JOIN questions affected ON affected.id = p.question_id
        JOIN questions later ON coalesce(later.thread_id, later.id) = coalesce(affected.thread_id, affected.id)
        AND later.id >= affected.id WHERE p.frame_id = old.id
    );
    DELETE FROM question_frames WHERE frame_id = old.id;
END;

CREATE TRIGGER frame_text_copies_ad AFTER DELETE ON frames BEGIN
    UPDATE questions SET state = 'failed', step = '', plan = NULL, answer = '', sources = '[]',
        model = NULL, error = 'Source screen text changed. Ask again.', finished_ms = NULL
    WHERE old.text IS NOT NULL AND (state IN ('queued', 'working') OR id IN (
        SELECT later.id FROM question_frames p JOIN questions affected ON affected.id = p.question_id
        JOIN questions later ON coalesce(later.thread_id, later.id) = coalesce(affected.thread_id, affected.id)
        AND later.id >= affected.id WHERE p.frame_id = old.id
    ));
    -- Always: a reused frame id must inherit no lineage.
    DELETE FROM question_frames WHERE frame_id = old.id;
END;
