-- Pilot schema: original copyrighted content stays in an ignored local database.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS exams (
    id TEXT PRIMARY KEY,
    session INTEGER NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('I','II')),
    booklet TEXT NOT NULL,
    UNIQUE (session, level, booklet)
);

CREATE TABLE IF NOT EXISTS source_files (
    id INTEGER PRIMARY KEY,
    relative_path TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size > 0),
    source_url TEXT,
    source_page TEXT,
    CHECK (length(sha256) = 64)
);

CREATE TABLE IF NOT EXISTS sections (
    id TEXT PRIMARY KEY,
    exam_id TEXT NOT NULL REFERENCES exams(id),
    name TEXT NOT NULL CHECK (name IN ('listening','reading','writing')),
    first_exam_number INTEGER NOT NULL,
    last_exam_number INTEGER NOT NULL,
    answer_key_offset INTEGER NOT NULL DEFAULT 0,
    UNIQUE (exam_id, name),
    CHECK (first_exam_number <= last_exam_number)
);

CREATE TABLE IF NOT EXISTS question_groups (
    id TEXT PRIMARY KEY,
    section_id TEXT NOT NULL REFERENCES sections(id),
    first_exam_number INTEGER NOT NULL,
    last_exam_number INTEGER NOT NULL,
    instruction TEXT NOT NULL DEFAULT '',
    passage_text TEXT NOT NULL DEFAULT '',
    points_each INTEGER,
    passage_image_key TEXT,
    CHECK (first_exam_number <= last_exam_number)
);

CREATE TABLE IF NOT EXISTS questions (
    id TEXT PRIMARY KEY,
    section_id TEXT NOT NULL REFERENCES sections(id),
    group_id TEXT REFERENCES question_groups(id),
    source_file_id INTEGER NOT NULL REFERENCES source_files(id),
    exam_number INTEGER NOT NULL,
    answer_key_number INTEGER NOT NULL,
    source_pdf_page INTEGER NOT NULL CHECK (source_pdf_page > 0),
    printed_page INTEGER,
    points INTEGER NOT NULL CHECK (points > 0),
    stem TEXT NOT NULL DEFAULT '',
    raw_question_text TEXT NOT NULL DEFAULT '',
    passage_id TEXT,
    requires_image INTEGER NOT NULL DEFAULT 0 CHECK (requires_image IN (0,1)),
    review_status TEXT NOT NULL DEFAULT 'needs_manual_review'
        CHECK (review_status IN ('needs_manual_review','verified','rejected')),
    extraction_origin TEXT NOT NULL,
    preview_flags_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE (section_id, exam_number),
    UNIQUE (section_id, answer_key_number)
);

CREATE TABLE IF NOT EXISTS choices (
    question_id TEXT NOT NULL REFERENCES questions(id),
    number INTEGER NOT NULL CHECK (number BETWEEN 1 AND 4),
    text TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (question_id, number)
);

CREATE TABLE IF NOT EXISTS answers (
    question_id TEXT PRIMARY KEY REFERENCES questions(id),
    choice_number INTEGER NOT NULL CHECK (choice_number BETWEEN 1 AND 4),
    source_file_id INTEGER NOT NULL REFERENCES source_files(id),
    source_pdf_page INTEGER NOT NULL,
    preview_and_pdf_agree INTEGER NOT NULL CHECK (preview_and_pdf_agree IN (0,1))
);

CREATE TABLE IF NOT EXISTS images (
    key TEXT PRIMARY KEY,
    mime_type TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    bytes BLOB NOT NULL,
    source_file_id INTEGER NOT NULL REFERENCES source_files(id)
);

CREATE TABLE IF NOT EXISTS question_images (
    question_id TEXT NOT NULL REFERENCES questions(id),
    image_key TEXT NOT NULL REFERENCES images(key),
    PRIMARY KEY (question_id, image_key)
);

CREATE TABLE IF NOT EXISTS audio_assets (
    id TEXT PRIMARY KEY,
    section_id TEXT NOT NULL REFERENCES sections(id),
    source_file_id INTEGER NOT NULL REFERENCES source_files(id),
    duration_seconds REAL CHECK (duration_seconds > 0),
    -- Per-question timings are intentionally not guessed during the pilot.
    timing_status TEXT NOT NULL DEFAULT 'not_segmented'
);

-- Independently reviewed audio boundaries. Candidate timestamps never imply
-- that dialogue and question announcements have been verified by listening.
CREATE TABLE IF NOT EXISTS audio_segments (
    question_id TEXT PRIMARY KEY REFERENCES questions(id),
    audio_asset_id TEXT NOT NULL REFERENCES audio_assets(id),
    start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms INTEGER NOT NULL CHECK (end_ms > start_ms),
    status TEXT NOT NULL CHECK (status IN ('candidate','verified')),
    version INTEGER NOT NULL CHECK (version >= 1),
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
    updated_at TEXT NOT NULL,
    clip_relative_path TEXT,
    clip_sha256 TEXT,
    CHECK ((clip_relative_path IS NULL AND clip_sha256 IS NULL) OR
           (clip_relative_path IS NOT NULL AND clip_sha256 IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS transcripts (
    question_id TEXT PRIMARY KEY REFERENCES questions(id),
    source_file_id INTEGER NOT NULL REFERENCES source_files(id),
    source_pdf_page INTEGER NOT NULL CHECK (source_pdf_page > 0),
    dialogue_text TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'needs_manual_review',
    warnings_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS review_records (
    id INTEGER PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    status TEXT NOT NULL,
    reviewer TEXT,
    scope TEXT NOT NULL,
    evidence TEXT NOT NULL,
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS import_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_questions_section_number
    ON questions(section_id, exam_number);
CREATE INDEX IF NOT EXISTS idx_review_status ON questions(review_status);
