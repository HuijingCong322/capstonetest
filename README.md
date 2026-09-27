# ProsecutionBench Data Acquisition

This package builds an auditable pilot corpus from local USPTO PatEx CSV files and USPTO Open Data Portal (ODP) Patent File Wrapper documents.

The default test suite and the `sample`, `extract`, and `build-timeline` stages work without network access. Live ODP acquisition requires an API key supplied only through the `USPTO_API_KEY` environment variable.

## Installation

Python 3.11 or newer is required.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -m 'not live' -q
```

Optional OCR requires both `pdftoppm` (Poppler) and `tesseract` on `PATH`. Native-text PDFs do not require OCR.

## PatEx inputs

Download and unpack the PatEx CSV distribution yourself. The pipeline expects:

- `application_data.csv`
- `transactions.csv`
- `cms_documents.csv`
- `cms_document_codes.csv`
- optionally `continuity_parents.csv` and `continuity_children.csv`

Official sources:

- [USPTO Patent Examination Research Dataset (PatEx)](https://www.uspto.gov/ip-policy/economic-research/research-datasets/patent-examination-research-dataset-public-pair)
- [USPTO Patent File Wrapper API](https://data.uspto.gov/apis/patent-file-wrapper/documents)

## Offline pilot

```bash
.venv/bin/prosecution-data \
  --config config/example.toml \
  sample \
  --application-data /path/to/application_data.csv \
  --transactions /path/to/transactions.csv \
  --cms-documents /path/to/cms_documents.csv \
  --cms-document-codes /path/to/cms_document_codes.csv \
  --continuity-parents /path/to/continuity_parents.csv \
  --continuity-children /path/to/continuity_children.csv \
  --sample-size 100 \
  --seed 42
```

Sampling is deterministic for the same input files, filters, sample size, and seed. By default only one representative application per reconstructed family is retained. Use `--all-family-members` to disable that rule.

## Live ODP fetch

Create an ODP key through your USPTO.gov account, then export it in the shell. Do not put it in TOML, command history, or source control.

```bash
export USPTO_API_KEY='your-key'
.venv/bin/prosecution-data \
  --config config/example.toml \
  fetch \
  --applications-manifest data/manifests/applications.jsonl
```

Downloads use temporary sibling files, size checks when the service supplies a size, SHA-256 hashes, and atomic replacement. A valid matching file is reused. Mismatched existing content is quarantined rather than overwritten silently.

To run the opt-in metadata smoke test:

```bash
RUN_USPTO_LIVE_TESTS=1 .venv/bin/python -m pytest tests/test_odp_live.py -m live -q
```

## Extraction and timelines

```bash
.venv/bin/prosecution-data --config config/example.toml extract
.venv/bin/prosecution-data --config config/example.toml build-timeline
```

`extract` reads downloaded files recorded in SQLite, uses embedded PDF text first, and attempts OCR only when native text quality is low. Missing OCR tools, corrupt PDFs, and unsupported media become explicit per-document statuses.

`build-timeline` combines PatEx events, source-document metadata, and extracted text. Office Action–response–next-event links are chronological candidates only; they are not findings that a rejection was resolved or that an argument was legally adequate.

For a single offline command that samples and builds outputs from any already-available raw documents:

```bash
.venv/bin/prosecution-data --config config/example.toml run --offline \
  --application-data /path/to/application_data.csv \
  --transactions /path/to/transactions.csv \
  --cms-documents /path/to/cms_documents.csv \
  --cms-document-codes /path/to/cms_document_codes.csv
```

## Output layout

```text
data/
  raw/odp/<application_number>/<document_id>.pdf
  interim/text/<document_id>.json
  manifests/applications.jsonl
  manifests/documents.jsonl
  manifests/transactions.jsonl
  manifests/odp_documents.jsonl
  manifests/extracted_text.jsonl
  processed/timelines.jsonl
  processed/timelines.parquet
  reports/sampling_counters.json
  reports/acquisition_summary.json
  state/prosecution_data.sqlite3
```

JSONL is the canonical nested research output. Parquet is a flattened analysis projection whose nested fields are JSON strings. SQLite is resumable operational state, not the released dataset.

## Data-use cautions

- PatEx metadata is used for discovery and linkage; original prosecution documents remain the source evidence.
- Missing documents, unknown values, not-applicable fields, and observed absence are kept distinct.
- Pending cases without an observed follow-up are treated as censored rather than negative outcomes.
- Review USPTO access and redistribution terms before releasing source documents.
