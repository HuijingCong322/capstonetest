# Prosecution Data Acquisition Pipeline Design

## Purpose

Build a reproducible Python pipeline for the ProsecutionBench project that turns locally downloaded USPTO PatEx tables and, when credentials become available, USPTO Open Data Portal (ODP) Patent File Wrapper documents into auditable application timelines.

The first release must be fully testable without network access or an ODP API key. It must support live ODP acquisition later by reading a key from the `USPTO_API_KEY` environment variable. The pipeline is a research data-construction tool, not a source of legal conclusions.

## Scope

The pipeline will:

1. Read selected PatEx CSV files in chunks.
2. Identify eligible patent applications and create a deterministic, family-aware pilot sample.
3. Retrieve ODP document metadata and source files when an API key is available.
4. Store source files immutably with provenance and SHA-256 hashes.
5. Extract native PDF text and fall back to OCR when needed.
6. Normalize document and transaction events into application timelines.
7. Emit JSONL, Parquet, SQLite state, and an acquisition audit report.

The first release will not download the complete PatEx distribution automatically, assign substantive legal correctness labels, infer that a rejection was resolved, or use future events as inputs to earlier stages.

## Inputs

The supported PatEx 2022 inputs are:

- `application_data.csv`
- `transactions.csv`
- `cms_documents.csv`
- `cms_document_codes.csv`
- optional `continuity_parents.csv`
- optional `continuity_children.csv`

Column names will be mapped through an explicit schema layer. Required columns that are missing will produce a concise validation error naming the file and missing columns. Unknown additional columns will be ignored and preserved only when explicitly selected in configuration.

ODP access will use the current Patent File Wrapper document metadata and download endpoints behind a small client interface. Endpoint paths and response-field mappings will live in configuration or one adapter module so an upstream API migration does not affect sampling, extraction, or timeline logic.

## Architecture

The package will target Python 3.11 or newer and expose a command-line interface named `prosecution-data`. The implementation will be split into focused modules:

- `cli.py`: commands, arguments, and orchestration
- `config.py`: configuration, paths, and environment variables
- `patex.py`: chunked PatEx reads, validation, filtering, and sampling
- `odp.py`: ODP pagination, metadata retrieval, download, retry, and authentication
- `storage.py`: SQLite run state, file layout, hashes, and atomic writes
- `extract.py`: native PDF extraction, quality checks, and OCR fallback
- `timeline.py`: document classification, chronology, and candidate event links
- `schemas.py`: typed internal records and serialized output schemas

Network calls, OCR execution, and time will be passed through narrow interfaces so tests can use deterministic local implementations. Core filtering, classification, and timeline construction will remain pure functions where practical.

## Commands

The CLI will provide:

- `prosecution-data sample`: validate PatEx inputs and create the application and document manifests.
- `prosecution-data fetch`: obtain ODP metadata and source documents for a manifest.
- `prosecution-data extract`: extract text from already-downloaded documents.
- `prosecution-data build-timeline`: combine PatEx events, document metadata, and extracted text.
- `prosecution-data run`: execute the available stages in order and stop before `fetch` with a clear credential message when live acquisition is requested without a key.

Every command will support `--config` and explicit input/output overrides. Sampling will support sample size, date bounds, and a random seed. Fetching will support a conservative request rate, retry limit, and an application limit for pilot runs.

## Sampling and Eligibility

The default pilot cohort will:

- include only public U.S. applications with a normalized application number;
- require at least one Office Action document or corresponding transaction event;
- retain allowed, abandoned, and pending cases;
- avoid outcome-based inclusion criteria;
- mark response-generation eligibility only when an observed applicant response is present;
- allow event-forecasting eligibility without requiring an applicant response;
- select at most one representative application per reconstructed patent family by default;
- produce the same sample for the same inputs, filters, and random seed.

When continuity tables are absent, the pipeline will set `family_resolution` to `unavailable` and use the application number as a singleton family. It will not claim that these singleton groups are true patent families.

Document-code sets for Office Actions and applicant responses will be loaded from a versioned project mapping. Unrecognized document codes will be retained as `other`, counted in the audit report, and never silently discarded.

## Storage and Provenance

The default layout is:

```text
data/
  raw/odp/<application_number>/<document_id>.<ext>
  interim/text/<document_id>.json
  manifests/applications.jsonl
  manifests/documents.jsonl
  processed/timelines.jsonl
  processed/timelines.parquet
  reports/acquisition_summary.json
  state/prosecution_data.sqlite3
```

Application numbers and document identifiers will be normalized and validated before being used in paths. Unsafe or empty identifiers will be rejected rather than interpolated into filenames.

Each source-document record will include application number, document identifier, document code, recorded date, source URL or source identifier, retrieval timestamp, media type, byte length, SHA-256, and processing status. Raw files are append-only: an existing file with a matching hash is reused; a mismatch is quarantined and reported before a replacement is accepted.

Downloads will first write to a temporary file in the destination directory. The pipeline will validate the response and expected size when supplied, compute its hash, then atomically move it into place.

SQLite will track applications, documents, attempts, statuses, hashes, and errors. Document states are `pending`, `downloaded`, `failed`, `extracted`, and `extraction_failed`. A failed attempt records an error category and retry count without storing credentials or full authorization headers.

## ODP Authentication and Resilience

The ODP API key will be read only from `USPTO_API_KEY`. The key will never be accepted as a normal command-line argument, written to configuration files, logged, or stored in SQLite.

The client will paginate until the service indicates completion. Timeouts, HTTP 429 responses, and retryable 5xx responses will use bounded exponential backoff with jitter. Authentication and authorization failures will stop the fetch command immediately with instructions to configure the environment variable. Non-retryable document failures will be recorded and processing will continue with other documents unless the user selects fail-fast behavior.

The default automated test suite will never contact USPTO. A separately marked live smoke test may be run manually after a key is obtained.

## Text Extraction

PDF processing will prefer embedded text. A documented quality rule based on non-whitespace character count and printable-character ratio will decide whether native extraction is sufficient. A low-quality or empty result will trigger OCR when the configured OCR executable is available.

If OCR is unavailable, the document will be retained with `extraction_status: ocr_unavailable`; the pipeline will continue. Corrupt or unsupported files will similarly receive an explicit error status. Every extracted-text record will contain the extraction method, text, page count when available, quality measurements, extractor version, source hash, and error category.

The initial release will support PDF input. Other media types will be retained and reported as unsupported rather than passed to PDF tools.

## Timeline Construction

Timeline construction will combine normalized PatEx transaction events, document metadata, and extracted document records. Events will be ordered by:

1. recorded filing or mailing date;
2. a stable source-priority value documented in the event schema;
3. source identifier as a deterministic final tie-breaker.

Events with missing dates will be retained in an `undated_events` collection. Same-date ambiguity will be surfaced rather than resolved from content that became available later.

The first release will create candidate links from an Office Action to the next observed applicant response and from that response to the next recorded examination event, subject to configurable maximum intervals. These are chronological candidates, not determinations that an argument addressed or resolved a particular rejection.

Each application record will expose `generation_eligible`, `forecast_eligible`, and `censored`. Missing, unknown, not applicable, and observed absence will use distinct serialized values or status fields. A missing response document will not be treated as an argument-only response, and a missing downstream event will not automatically be treated as abandonment.

## Outputs

The canonical research output is newline-delimited JSON. Parquet is a tabular projection for analysis and will not replace nested JSON fields that cannot be represented without loss. The SQLite database is operational state, not the canonical released dataset.

The acquisition report will include:

- input row counts and validation failures;
- counts at each sampling filter;
- family-resolution coverage;
- document-code frequencies and unknown codes;
- download, retry, and failure counts by category;
- native extraction, OCR, and extraction-failure counts;
- missingness by document type and application outcome;
- task-eligibility and censoring counts.

## Testing Strategy

Development will follow test-driven development. Production behavior will be introduced only after a focused test fails for the expected reason.

The offline suite will cover:

- required-column validation and chunked PatEx filtering;
- deterministic seeded sampling and family-level deduplication;
- absent continuity tables and unrecognized document codes;
- ODP pagination, authentication failure, rate limiting, and retry exhaustion;
- safe path construction, atomic downloads, SHA-256 verification, and reruns;
- native PDF text extraction and OCR fallback through a local test adapter;
- corrupt PDFs, unsupported media, and unavailable OCR;
- same-date events, missing dates, incomplete histories, and deterministic ordering;
- CLI stage composition using small CSV, API-response, and PDF fixtures;
- protection against accidental network use in the default test run.

The live smoke test will be opt-in, require `USPTO_API_KEY`, request only a documented public application, and avoid assertions on fields likely to change upstream.

## Dependencies

Dependencies will be kept small. The planned categories are:

- CLI and configuration;
- HTTP client with explicit timeout support;
- typed validation models or dataclasses;
- chunked CSV and Parquet support;
- PDF text extraction;
- test runner and HTTP test transport.

OCR will use an optional system executable behind an adapter rather than bundle a large OCR runtime. Exact libraries and pinned minimum versions will be selected in the implementation plan after confirming availability in the workspace.

## Acceptance Criteria

The first release is complete when:

1. A clean environment can install the package and run the offline test suite without credentials or network access.
2. Fixture PatEx files can produce deterministic application and document manifests.
3. Fixture ODP responses and documents can pass through download, extraction, and timeline construction end to end.
4. Re-running the pipeline does not redownload or overwrite valid raw files.
5. Missing credentials, OCR, documents, and dates yield explicit statuses rather than silent loss or a whole-run crash.
6. JSONL, Parquet, SQLite state, and the acquisition report are produced with documented schemas.
7. Adding `USPTO_API_KEY` is the only credential change required to enable the live fetch path.

## Deferred Work

The following are deliberately outside the first release:

- automatic full PatEx bulk download and extraction;
- distributed execution or a workflow engine;
- semantic linking of rejection grounds to claim versions;
- automated legal-resolution labels;
- prior-art corpus expansion beyond documents already identified in the prosecution record;
- public release packaging or redistribution decisions for source documents.
