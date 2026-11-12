-- Disposable copy of the schema the episode vector step reads, as of migration 0018.
-- Sources: src/akashic/storage/migrations/0005_speakers.sql (speakers, for episode_people's FK),
--          0006_episodes.sql (episodes, episode_people, embeddings, indexes),
--          0011_drop_episode_summary.sql (episodes.summary dropped).
-- Later migrations (0012-0018) do not change these tables or indexes.
-- Omitted on purpose (the vector step never reads them): episodes_fts and its triggers,
-- episode_items, speaker_turns, the audio_segments triggers on embeddings.

CREATE TABLE speakers (
    id         INTEGER PRIMARY KEY,
    name       TEXT,                        -- NULL until the user names the voice
    is_self    INTEGER NOT NULL DEFAULT 0,  -- the user: everything on the microphone stream
    vec_sum    BLOB,
    vec_n      INTEGER NOT NULL DEFAULT 0,
    created_ms INTEGER NOT NULL
);

CREATE UNIQUE INDEX speakers_self ON speakers (is_self) WHERE is_self = 1;

-- 0006 + 0011 (summary column dropped).
CREATE TABLE episodes (
    id       INTEGER PRIMARY KEY,
    kind     TEXT NOT NULL CHECK (kind IN ('video', 'music', 'conversation', 'browsing', 'work', 'game')),
    ref      TEXT NOT NULL UNIQUE,
    start_ms INTEGER NOT NULL,
    end_ms   INTEGER NOT NULL,
    title    TEXT NOT NULL,
    app      TEXT,
    url      TEXT,
    body     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX episodes_time ON episodes (start_ms);
CREATE INDEX episodes_kind_time ON episodes (kind, start_ms);

CREATE TABLE episode_people (
    episode_id INTEGER NOT NULL REFERENCES episodes (id) ON DELETE CASCADE,
    speaker_id INTEGER NOT NULL REFERENCES speakers (id) ON DELETE CASCADE,
    speech_ms  INTEGER NOT NULL,
    PRIMARY KEY (episode_id, speaker_id)
);

CREATE INDEX episode_people_speaker ON episode_people (speaker_id);

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
