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

-- Independent AI audit history. These records are deliberately separate from
-- questions/transcripts/audio human-review state and from review_records.
CREATE TABLE IF NOT EXISTS ai_audit_source_snapshots (
    snapshot_sha256 TEXT PRIMARY KEY CHECK (length(snapshot_sha256) = 64),
    exam_id TEXT NOT NULL REFERENCES exams(id),
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_audit_runs (
    id TEXT PRIMARY KEY,
    exam_id TEXT NOT NULL REFERENCES exams(id),
    snapshot_sha256 TEXT NOT NULL REFERENCES ai_audit_source_snapshots(snapshot_sha256),
    contract_version TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_audit_passes (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES ai_audit_runs(id),
    pass_number INTEGER NOT NULL CHECK (pass_number >= 1),
    auditor_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    perspective TEXT NOT NULL,
    blind INTEGER NOT NULL DEFAULT 1 CHECK (blind = 1),
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
    input_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, pass_number),
    UNIQUE (run_id, auditor_id)
);

CREATE TABLE IF NOT EXISTS ai_audit_checkpoints (
    id INTEGER PRIMARY KEY,
    pass_id TEXT NOT NULL REFERENCES ai_audit_passes(id),
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    checkpoint_sha256 TEXT NOT NULL UNIQUE CHECK (length(checkpoint_sha256) = 64),
    completed_subject_ids_json TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (pass_id, sequence)
);

CREATE TABLE IF NOT EXISTS ai_audit_results (
    id INTEGER PRIMARY KEY,
    pass_id TEXT NOT NULL UNIQUE REFERENCES ai_audit_passes(id),
    result_sha256 TEXT NOT NULL UNIQUE CHECK (length(result_sha256) = 64),
    completed_subject_ids_json TEXT NOT NULL,
    notes_json TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_audit_attempts (
    id INTEGER PRIMARY KEY,
    pass_id TEXT NOT NULL REFERENCES ai_audit_passes(id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    status TEXT NOT NULL CHECK (status IN ('succeeded','failed','timed_out','invalid')),
    result_id INTEGER REFERENCES ai_audit_results(id),
    response_sha256 TEXT CHECK (response_sha256 IS NULL OR length(response_sha256) = 64),
    raw_response_json TEXT,
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (pass_id, attempt_number),
    CHECK ((response_sha256 IS NULL AND raw_response_json IS NULL) OR
           (response_sha256 IS NOT NULL AND raw_response_json IS NOT NULL)),
    CHECK ((status = 'succeeded' AND result_id IS NOT NULL) OR
           (status <> 'succeeded' AND result_id IS NULL))
);

CREATE TABLE IF NOT EXISTS ai_audit_findings (
    fingerprint TEXT PRIMARY KEY CHECK (length(fingerprint) = 64),
    subject_id TEXT NOT NULL,
    category TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_audit_finding_occurrences (
    id INTEGER PRIMARY KEY,
    result_id INTEGER NOT NULL REFERENCES ai_audit_results(id),
    pass_id TEXT NOT NULL REFERENCES ai_audit_passes(id),
    fingerprint TEXT NOT NULL REFERENCES ai_audit_findings(fingerprint),
    severity TEXT NOT NULL CHECK (severity IN ('low','medium','high','critical')),
    summary TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (pass_id, fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_ai_audit_passes_run
    ON ai_audit_passes(run_id, pass_number);
CREATE INDEX IF NOT EXISTS idx_ai_audit_occurrences_fingerprint
    ON ai_audit_finding_occurrences(fingerprint);
CREATE INDEX IF NOT EXISTS idx_ai_audit_attempts_pass
    ON ai_audit_attempts(pass_id, attempt_number);

-- AI audit rows are evidence, not mutable workflow state. Checkpoints model
-- progress by adding rows, so every audit table can remain append-only.
CREATE TRIGGER IF NOT EXISTS ai_audit_source_snapshots_no_update
BEFORE UPDATE ON ai_audit_source_snapshots BEGIN
    SELECT RAISE(ABORT, 'ai audit tables are append-only');
END;
CREATE TRIGGER IF NOT EXISTS ai_audit_source_snapshots_no_delete
BEFORE DELETE ON ai_audit_source_snapshots BEGIN
    SELECT RAISE(ABORT, 'ai audit tables are append-only');
END;
CREATE TRIGGER IF NOT EXISTS ai_audit_runs_no_update
BEFORE UPDATE ON ai_audit_runs BEGIN
    SELECT RAISE(ABORT, 'ai audit tables are append-only');
END;
CREATE TRIGGER IF NOT EXISTS ai_audit_runs_no_delete
BEFORE DELETE ON ai_audit_runs BEGIN
    SELECT RAISE(ABORT, 'ai audit tables are append-only');
END;
CREATE TRIGGER IF NOT EXISTS ai_audit_passes_no_update
BEFORE UPDATE ON ai_audit_passes BEGIN
    SELECT RAISE(ABORT, 'ai audit tables are append-only');
END;
CREATE TRIGGER IF NOT EXISTS ai_audit_passes_no_delete
BEFORE DELETE ON ai_audit_passes BEGIN
    SELECT RAISE(ABORT, 'ai audit tables are append-only');
END;
CREATE TRIGGER IF NOT EXISTS ai_audit_checkpoints_no_update
BEFORE UPDATE ON ai_audit_checkpoints BEGIN
    SELECT RAISE(ABORT, 'ai audit tables are append-only');
END;
CREATE TRIGGER IF NOT EXISTS ai_audit_checkpoints_no_delete
BEFORE DELETE ON ai_audit_checkpoints BEGIN
    SELECT RAISE(ABORT, 'ai audit tables are append-only');
END;
CREATE TRIGGER IF NOT EXISTS ai_audit_results_no_update
BEFORE UPDATE ON ai_audit_results BEGIN
    SELECT RAISE(ABORT, 'ai audit tables are append-only');
END;
CREATE TRIGGER IF NOT EXISTS ai_audit_results_no_delete
BEFORE DELETE ON ai_audit_results BEGIN
    SELECT RAISE(ABORT, 'ai audit tables are append-only');
END;
CREATE TRIGGER IF NOT EXISTS ai_audit_attempts_no_update
BEFORE UPDATE ON ai_audit_attempts BEGIN
    SELECT RAISE(ABORT, 'ai audit tables are append-only');
END;
CREATE TRIGGER IF NOT EXISTS ai_audit_attempts_no_delete
BEFORE DELETE ON ai_audit_attempts BEGIN
    SELECT RAISE(ABORT, 'ai audit tables are append-only');
END;
CREATE TRIGGER IF NOT EXISTS ai_audit_findings_no_update
BEFORE UPDATE ON ai_audit_findings BEGIN
    SELECT RAISE(ABORT, 'ai audit tables are append-only');
END;
CREATE TRIGGER IF NOT EXISTS ai_audit_findings_no_delete
BEFORE DELETE ON ai_audit_findings BEGIN
    SELECT RAISE(ABORT, 'ai audit tables are append-only');
END;
CREATE TRIGGER IF NOT EXISTS ai_audit_finding_occurrences_no_update
BEFORE UPDATE ON ai_audit_finding_occurrences BEGIN
    SELECT RAISE(ABORT, 'ai audit tables are append-only');
END;
CREATE TRIGGER IF NOT EXISTS ai_audit_finding_occurrences_no_delete
BEFORE DELETE ON ai_audit_finding_occurrences BEGIN
    SELECT RAISE(ABORT, 'ai audit tables are append-only');
END;
