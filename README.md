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
- `event_codes.csv` (recommended for transaction descriptions)
- optionally `cms_documents.csv` and `cms_document_codes.csv` together
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

## PatEx 2022 metadata-only pilot

For the five downloaded CSV files, CMS files are not required:

```bash
cd /Users/c/Desktop/capstone
export PYTHONPATH="$PWD/src"
.venv/bin/python .venv/bin/prosecution-data --output-root data/patex_2022/pilot sample \
  --application-data data/patex_2022/raw_csv/application_data.csv \
  --transactions data/patex_2022/raw_csv/transactions.csv \
  --event-codes data/patex_2022/raw_csv/event_codes.csv \
  --continuity-parents data/patex_2022/raw_csv/continuity_parents.csv \
  --continuity-children data/patex_2022/raw_csv/continuity_children.csv \
  --date-from 2008-01-01 --date-to 2015-12-31 --sample-size 50 --seed 42
.venv/bin/python .venv/bin/prosecution-data --output-root data/patex_2022/pilot build-timeline
```

This scans large CSVs in chunks and selects only required columns. It can take
substantial time; it is not a three-record smoke test. Public-record candidates
are inferred from publication or patent identifiers when public_indicator is
absent. This is a discovery filter, not proof of public document availability.
The current sampling command does not enforce utility type or technology-center
strata; confirm these before adopting its output as the shared evaluation sample.

Transaction codes preserve punctuation: MN/=. is the mailed allowance notice;
MCTNF/MCTFR are mailing events; A... and A/RR are response candidates. Legacy
aliases remain for compatibility with existing inputs. Transactions remain
separate from document codes and are not deduplicated into verified OA rounds.

Metadata-only output contains no fabricated PDF document entries and is not
eligible for generation. response_observed flags candidate reply events, while
requires_document_verification marks records needing ODP/source review. Timelines
are chronological candidates, not labels.csv or validated t=1 labels. Review
same-day order, reply completeness, duplicate processing/mailing events, and
snapshot dates with A before scoring. The default timeline window is 365 days;
absence of a link is not proof of a negative outcome. RCE/appeal events stop a
candidate next-event link rather than being skipped to a later OA.
