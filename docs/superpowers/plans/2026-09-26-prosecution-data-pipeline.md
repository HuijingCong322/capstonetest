# Prosecution Data Acquisition Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an offline-testable CLI that samples PatEx records, acquires and verifies ODP file-wrapper documents when credentials exist, extracts text, and emits auditable prosecution timelines.

**Architecture:** A Python package exposes five composable CLI stages over typed records. Pure sampling and timeline functions are separated from injected HTTP, filesystem, clock, and OCR adapters; SQLite records operational state while JSONL remains the canonical nested output and Parquet provides an analysis projection.

**Tech Stack:** Python 3.11+, stdlib `argparse`/`dataclasses`/`sqlite3`, `httpx>=0.27`, `pandas>=2.2`, `pyarrow>=17`, `pypdf>=5`, `pytest>=8`, and `reportlab>=4` as a test-only PDF-fixture dependency; optional system `pdftoppm` and `tesseract` for OCR.

**Spec:** `docs/superpowers/specs/2026-09-26-prosecution-data-pipeline-design.md`

## Global Constraints

- All default tests run without network access, an API key, Tesseract, or pre-downloaded PatEx data.
- `USPTO_API_KEY` is the only credential input; never accept it as a CLI flag or persist it.
- Raw source files are append-only and are accepted only after validation and SHA-256 calculation.
- JSONL is canonical; Parquet is a loss-aware analysis projection; SQLite is operational state.
- Missing, unknown, not applicable, and observed absence remain distinct.
- Candidate event links are chronological heuristics, never legal-resolution labels.
- The workspace is not currently a Git repository; do not initialize one or claim commits without explicit user direction.

## Review Focus

- PatEx identifiers represented as numbers, strings with punctuation, or scientific notation must never silently identify the wrong application; Task 2 adds normalization and rejection tests.
- ODP may return pagination or document fields under alternate wrapper names; Task 4 pins supported shapes and rejects unknown shapes with an actionable schema error.
- Interrupted or mismatched downloads must leave no apparently valid raw file; Task 3 and Task 4 test temporary-file cleanup and quarantine behavior.
- A low-text PDF without OCR installed must remain traceable and must not crash the run; Task 5 tests `ocr_unavailable` output.
- Same-day and undated prosecution events must be deterministic without inventing chronology; Task 6 tests stable ordering and separate undated storage.

---

### Task 1: Package foundation, configuration, and shared schemas

**Files:**
- Create: `pyproject.toml`
- Create: `src/prosecution_data/__init__.py`
- Create: `src/prosecution_data/config.py`
- Create: `src/prosecution_data/schemas.py`
- Create: `tests/test_config.py`
- Create: `tests/test_schemas.py`

**Interfaces:**
- Consumes: environment mapping and optional TOML configuration path.
- Produces: `AppConfig`, `PatExInputs`, `SampleOptions`, `ApplicationRecord`, `DocumentRecord`, `TransactionEvent`, `ExtractedText`, `TimelineRecord`, `normalize_application_number(value: object) -> str`, and JSON-safe `to_dict()` methods.

- [ ] **Step 1: Add package metadata and create the isolated environment**

  Add only `pyproject.toml` and an empty `src/prosecution_data/__init__.py`, declaring the `src` layout, console entry point, runtime dependencies, test dependency group, and `live` pytest marker. Then run `python3 -m venv .venv` and `.venv/bin/python -m pip install -e '.[test]'`.

- [ ] **Step 2: Write failing configuration and schema tests**

  Add tests proving that `load_config(path, env)` reads paths and non-secret settings from TOML, reads the key only from `env["USPTO_API_KEY"]`, redacts it from `repr`, and leaves it unset offline. Add normalization tests for slash/dash-separated strings, leading zeros, empty values, floats, and scientific-notation strings; unsafe or ambiguous inputs must raise `IdentifierError`.

- [ ] **Step 3: Run the focused tests and verify RED**

  Run: `.venv/bin/python -m pytest tests/test_config.py tests/test_schemas.py -q`

  Expected: collection fails because `prosecution_data` does not exist.

- [ ] **Step 4: Add minimal package implementations**

  Implement frozen dataclasses, JSON serialization, identifier validation, and secret-safe configuration.

- [ ] **Step 5: Run the focused tests and verify GREEN**

  Run: `.venv/bin/python -m pytest tests/test_config.py tests/test_schemas.py -q`

  Expected: all tests pass with no warnings.

- [ ] **Step 6: Run the suite checkpoint**

  Run: `.venv/bin/python -m pytest -q`

  Expected: all tests pass.

### Task 2: Chunked PatEx ingestion and deterministic sampling

**Files:**
- Create: `src/prosecution_data/patex.py`
- Create: `src/prosecution_data/resources/patex_2022.json`
- Create: `tests/fixtures/patex/application_data.csv`
- Create: `tests/fixtures/patex/transactions.csv`
- Create: `tests/fixtures/patex/cms_documents.csv`
- Create: `tests/fixtures/patex/cms_document_codes.csv`
- Create: `tests/fixtures/patex/continuity_parents.csv`
- Create: `tests/test_patex.py`

**Interfaces:**
- Consumes: `PatExInputs`, `SampleOptions`, and the versioned alias/code mapping.
- Produces: `validate_patex_inputs(inputs) -> None` and `build_manifests(inputs, options) -> ManifestBundle`, where `ManifestBundle` contains application records, document records, transaction events, and filter counters.

- [ ] **Step 1: Write failing input-validation tests**

  Test missing required files, missing canonical columns, accepted aliases, ignored extra columns, and errors that name both the source file and missing columns.

- [ ] **Step 2: Verify validation tests fail for the missing implementation**

  Run: `.venv/bin/python -m pytest tests/test_patex.py -k validation -q`

  Expected: FAIL because `validate_patex_inputs` is unavailable.

- [ ] **Step 3: Implement schema mapping and chunk readers**

  Use `pandas.read_csv(..., chunksize=...)` with string dtypes for identifiers. Put PatEx 2022 aliases and versioned Office Action/response document-code categories in `patex_2022.json`; unknown codes map to `other` and remain counted.

- [ ] **Step 4: Verify validation tests pass**

  Run: `.venv/bin/python -m pytest tests/test_patex.py -k validation -q`

  Expected: PASS.

- [ ] **Step 5: Write failing cohort and sampling tests**

  Cover Office-Action eligibility through either documents or transactions, retention of allowed/abandoned/pending cases, response-generation versus forecasting flags, seeded determinism, date bounds, one representative per reconstructed family, missing continuity files, unknown document codes, and the identifier cases listed in Review Focus.

- [ ] **Step 6: Verify cohort tests fail for the expected missing behavior**

  Run: `.venv/bin/python -m pytest tests/test_patex.py -k 'sample or family or eligibility or identifier' -q`

  Expected: FAIL in `build_manifests` assertions.

- [ ] **Step 7: Implement filtering, union-find family reconstruction, and sampling**

  Keep family selection stable by sorting normalized application numbers before seeded sampling. When continuity data are absent, emit singleton IDs with `family_resolution="unavailable"`; do not label them resolved families.

- [ ] **Step 8: Verify Task 2 and the full suite are GREEN**

  Run: `.venv/bin/python -m pytest tests/test_patex.py -q`

  Run: `.venv/bin/python -m pytest -q`

  Expected: all tests pass.

### Task 3: Durable storage, SQLite state, hashing, and manifests

**Files:**
- Create: `src/prosecution_data/storage.py`
- Create: `tests/test_storage.py`

**Interfaces:**
- Consumes: `AppConfig`, application/document records, byte streams, and expected metadata.
- Produces: `StateStore`, `safe_document_path(root, application_number, document_id, suffix) -> Path`, `store_download(...) -> StoredFile`, `write_jsonl(...)`, and `read_jsonl(...)`.

- [ ] **Step 1: Write failing safe-path and state-transition tests**

  Cover empty/path-traversal identifiers, creation of the SQLite schema, valid state transitions, attempt/error recording, and confirmation that secrets and authorization headers never appear in rows or logs.

- [ ] **Step 2: Verify RED**

  Run: `.venv/bin/python -m pytest tests/test_storage.py -k 'path or state or secret' -q`

  Expected: FAIL because storage interfaces do not exist.

- [ ] **Step 3: Implement path validation and `StateStore`**

  Create tables for applications, documents, and attempts. Make migrations idempotent and expose context-manager cleanup.

- [ ] **Step 4: Verify the first storage slice is GREEN**

  Run: `.venv/bin/python -m pytest tests/test_storage.py -k 'path or state or secret' -q`

  Expected: PASS.

- [ ] **Step 5: Write failing atomic-write, hash, rerun, and quarantine tests**

  Assert that interrupted writes leave no destination, valid matching files are reused, mismatches move to a timestamp-free deterministic quarantine name containing the observed hash, and canonical JSONL is stable and newline-delimited.

- [ ] **Step 6: Verify RED for file persistence**

  Run: `.venv/bin/python -m pytest tests/test_storage.py -k 'atomic or hash or quarantine or jsonl' -q`

  Expected: FAIL in the unimplemented persistence behavior.

- [ ] **Step 7: Implement atomic storage and manifest helpers**

  Stream into a temporary sibling file, validate byte length when supplied, calculate SHA-256, `fsync`, and use `os.replace`. Never overwrite a mismatched raw file silently.

- [ ] **Step 8: Verify Task 3 and the full suite are GREEN**

  Run: `.venv/bin/python -m pytest tests/test_storage.py -q`

  Run: `.venv/bin/python -m pytest -q`

  Expected: all tests pass.

### Task 4: ODP metadata and document acquisition

**Files:**
- Create: `src/prosecution_data/odp.py`
- Create: `tests/fixtures/odp/documents_page_1.json`
- Create: `tests/fixtures/odp/documents_page_2.json`
- Create: `tests/test_odp.py`
- Create: `tests/test_odp_live.py`

**Interfaces:**
- Consumes: injected `httpx.Client`, `OdpSettings`, `StateStore`, clock/sleep/random callables, and application manifest records.
- Produces: `OdpClient.list_documents(application_number) -> list[DocumentRecord]`, `OdpClient.download_document(record) -> StoredFile`, `fetch_manifest(...) -> FetchSummary`, and typed `AuthenticationError`, `RateLimitError`, `OdpSchemaError`, and `RetryExhausted`.

- [ ] **Step 1: Write failing authentication, header, and schema tests**

  Assert missing keys fail before any request; requests use only `X-API-KEY`; key material is absent from exceptions; both documented response-envelope variants in fixtures parse; an unknown envelope raises `OdpSchemaError` with top-level field names but no response secrets.

- [ ] **Step 2: Verify RED**

  Run: `.venv/bin/python -m pytest tests/test_odp.py -k 'auth or header or schema' -q`

  Expected: FAIL because `OdpClient` does not exist.

- [ ] **Step 3: Implement the client adapter and response mapping**

  Default to `https://api.uspto.gov`, document listing `/api/v1/patent/applications/{applicationNumber}/documents`, and PDF download `/api/v1/download/applications/{applicationNumber}/{documentId}.pdf`. Keep templates configurable and URL-encode validated identifiers.

- [ ] **Step 4: Verify authentication and schema tests are GREEN**

  Run: `.venv/bin/python -m pytest tests/test_odp.py -k 'auth or header or schema' -q`

  Expected: PASS.

- [ ] **Step 5: Write failing pagination and resilience tests**

  Cover multiple pages, empty final pages, HTTP 401/403 immediate stop, retry of timeout/429/5xx, `Retry-After`, bounded exponential backoff with deterministic injected jitter, retry exhaustion, non-retryable 4xx, and cleanup after interrupted or size-mismatched downloads.

- [ ] **Step 6: Verify RED for acquisition behavior**

  Run: `.venv/bin/python -m pytest tests/test_odp.py -k 'page or retry or download or interrupt' -q`

  Expected: FAIL in pagination/retry/download assertions.

- [ ] **Step 7: Implement pagination, retry policy, rate limiting, and fetch orchestration**

  Stream downloads into `storage.store_download`, update `StateStore` around each attempt, skip hash-verified files, continue past per-document non-retryable failures unless fail-fast is enabled, and return aggregate counts.

- [ ] **Step 8: Add the opt-in live smoke-test shell**

  Mark `tests/test_odp_live.py` with `pytest.mark.live`, skip unless both `RUN_USPTO_LIVE_TESTS=1` and `USPTO_API_KEY` exist, and request metadata only for one documented public application by default.

- [ ] **Step 9: Verify Task 4 and offline isolation**

  Run: `.venv/bin/python -m pytest tests/test_odp.py -q`

  Run: `.venv/bin/python -m pytest -m 'not live' -q`

  Expected: all offline tests pass and no network call escapes the injected test transport.

### Task 5: PDF extraction and optional OCR fallback

**Files:**
- Create: `src/prosecution_data/extract.py`
- Create: `tests/test_extract.py`

**Interfaces:**
- Consumes: source PDF path/hash, `ExtractionSettings`, and injected `OcrEngine`/command runner.
- Produces: `assess_text_quality(text) -> TextQuality`, `extract_document(path, source_hash, settings, ocr_engine) -> ExtractedText`, `SystemOcrEngine`, and per-document JSON records.

- [ ] **Step 1: Write failing native extraction and quality tests**

  Generate a tiny text PDF in the test using ReportLab. Assert text, page count, source hash, extractor version, printable ratio, and `method="native_pdf"`; separately test exact threshold boundaries.

- [ ] **Step 2: Verify RED**

  Run: `.venv/bin/python -m pytest tests/test_extract.py -k 'native or quality' -q`

  Expected: FAIL because extraction functions do not exist.

- [ ] **Step 3: Implement native PDF extraction and quality scoring**

  Use `pypdf.PdfReader`; make minimum non-whitespace characters and printable ratio explicit `ExtractionSettings` fields.

- [ ] **Step 4: Verify native extraction tests are GREEN**

  Run: `.venv/bin/python -m pytest tests/test_extract.py -k 'native or quality' -q`

  Expected: PASS.

- [ ] **Step 5: Write failing OCR and error-status tests**

  Use an injected fake OCR engine to test fallback output. Cover unavailable OCR, corrupt PDF, unsupported media type, low-quality native text, per-document continuation, and JSON output with `ocr_unavailable`, `corrupt`, or `unsupported_media` categories.

- [ ] **Step 6: Verify RED for fallback behavior**

  Run: `.venv/bin/python -m pytest tests/test_extract.py -k 'ocr or corrupt or unsupported or fallback' -q`

  Expected: FAIL in fallback/status assertions.

- [ ] **Step 7: Implement OCR adapter and resilient extraction orchestration**

  `SystemOcrEngine` checks for both `pdftoppm` and `tesseract`, renders pages in a temporary directory, invokes Tesseract without a shell, joins page text in order, and cleans up. Missing executables produce status data, not a process-wide exception.

- [ ] **Step 8: Verify Task 5 and the full offline suite are GREEN**

  Run: `.venv/bin/python -m pytest tests/test_extract.py -q`

  Run: `.venv/bin/python -m pytest -m 'not live' -q`

  Expected: all tests pass without requiring Tesseract.

### Task 6: Deterministic timeline construction and eligibility

**Files:**
- Create: `src/prosecution_data/timeline.py`
- Create: `tests/test_timeline.py`

**Interfaces:**
- Consumes: one application's `ApplicationRecord`, document records, transaction events, extracted-text records, and `TimelineSettings` maximum intervals/source priorities.
- Produces: `build_timeline(...) -> TimelineRecord` with dated events, undated events, candidate links, eligibility flags, censoring status, and reason codes.

- [ ] **Step 1: Write failing ordering and candidate-link tests**

  Cover date ordering, configured source priority, identifier tie-breaks, same-date ambiguity flags, Office Action to next response to next examination-event links, maximum intervals, and no link across applications.

- [ ] **Step 2: Verify RED**

  Run: `.venv/bin/python -m pytest tests/test_timeline.py -k 'order or candidate or interval' -q`

  Expected: FAIL because `build_timeline` does not exist.

- [ ] **Step 3: Implement deterministic ordering and chronological candidate links**

  Keep link records explicitly typed as `chronological_candidate`; never emit `resolved`, `accepted`, or equivalent conclusions.

- [ ] **Step 4: Verify ordering/link tests are GREEN**

  Run: `.venv/bin/python -m pytest tests/test_timeline.py -k 'order or candidate or interval' -q`

  Expected: PASS.

- [ ] **Step 5: Write failing missingness, censoring, and eligibility tests**

  Cover separate undated events, missing response documents, pending cases with no downstream event, observed abandonment, generation versus forecasting eligibility, and distinct missing/unknown/not-applicable/observed-absence serialization.

- [ ] **Step 6: Verify RED for status semantics**

  Run: `.venv/bin/python -m pytest tests/test_timeline.py -k 'missing or censor or eligibility or undated' -q`

  Expected: FAIL in status assertions.

- [ ] **Step 7: Implement timeline status and eligibility rules**

  Emit reason codes alongside booleans so cohort decisions remain auditable. Treat pending/no-follow-up as censored unless an observed terminal event establishes otherwise.

- [ ] **Step 8: Verify Task 6 and the full suite are GREEN**

  Run: `.venv/bin/python -m pytest tests/test_timeline.py -q`

  Run: `.venv/bin/python -m pytest -m 'not live' -q`

  Expected: all tests pass.

### Task 7: CLI orchestration, outputs, audit report, and user documentation

**Files:**
- Create: `src/prosecution_data/cli.py`
- Create: `src/prosecution_data/report.py`
- Create: `tests/test_cli.py`
- Create: `tests/test_report.py`
- Create: `README.md`
- Create: `config/example.toml`
- Create: `.gitignore`

**Interfaces:**
- Consumes: all prior task interfaces and filesystem command arguments.
- Produces: console commands `sample`, `fetch`, `extract`, `build-timeline`, and `run`; `build_acquisition_report(...) -> dict`; canonical JSONL, Parquet projection, SQLite state, and summary JSON.

- [ ] **Step 1: Write failing command parsing and credential-boundary tests**

  Call `main(argv, env)` directly. Assert command help, explicit/config path precedence, absence of an API-key flag, successful offline commands without a key, and a concise nonzero `fetch` result before any network call when the key is missing.

- [ ] **Step 2: Verify RED**

  Run: `.venv/bin/python -m pytest tests/test_cli.py -k 'help or config or credential or offline' -q`

  Expected: FAIL because CLI commands do not exist.

- [ ] **Step 3: Implement command parsing and stage orchestration**

  Use stdlib `argparse`. `run` executes `sample`, then fetch only when requested and credentialed, followed by extraction and timeline construction for available raw files; each stage returns structured counts and a process exit code.

- [ ] **Step 4: Verify command-boundary tests are GREEN**

  Run: `.venv/bin/python -m pytest tests/test_cli.py -k 'help or config or credential or offline' -q`

  Expected: PASS.

- [ ] **Step 5: Write failing end-to-end output and report tests**

  Run the fixture cohort through `sample`, an injected fixture fetch transport, `extract`, and `build-timeline`. Assert canonical JSONL, a readable Parquet table, SQLite states, stable rerun behavior, counts at every filter, unknown-code counts, download/extraction outcomes, family coverage, eligibility, censoring, and no secret leakage.

- [ ] **Step 6: Verify RED for end-to-end behavior**

  Run: `.venv/bin/python -m pytest tests/test_cli.py tests/test_report.py -k 'end_to_end or output or report or rerun' -q`

  Expected: FAIL in missing orchestration/report/output assertions.

- [ ] **Step 7: Implement output writers and acquisition reporting**

  Preserve nested values in JSONL. Define a documented flattened Parquet schema with JSON-encoded nested columns only where necessary, and report every requested audit category from the design spec.

- [ ] **Step 8: Write installation and operating documentation**

  Document environment creation, dependency installation, PatEx file placement, offline pilot commands, output layout, API-key setup by environment variable, optional OCR prerequisites, live smoke-test opt-in, resumability, and data-use cautions. Link official USPTO PatEx and ODP documentation.

- [ ] **Step 9: Run final verification**

  Run: `.venv/bin/python -m pytest -m 'not live' -q`

  Run: `.venv/bin/python -m prosecution_data.cli --help`

  Expected: tests pass without warnings; help lists all five commands; the end-to-end pytest fixture run produces manifests, extracted text, timelines, Parquet, SQLite state, and the audit report without network access.

- [ ] **Step 10: Inspect deliverables and working tree**

  Run: `find src tests config docs -maxdepth 4 -type f | sort`

  Run: `git status --short` only if the user has initialized a Git repository by this point.

  Expected: all planned files exist; no credential, raw downloaded document, SQLite database, generated data, or temporary file is tracked.
