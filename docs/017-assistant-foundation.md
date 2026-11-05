# Stage 6.1: answers from a selected verified package

The first assistant increment is a local, deterministic question interface.
It answers six supported questions with package fields and evidence citations.
It uses no model, API key or BigQuery query. A [local browser interface](018-assistant-browser.md)
is available in Stage 6.2, with [authenticated cloud deployment](020-cloud-assistant.md)
in Stage 6.4. A language model remains future work.

## Try one answer

From the repository root in your VS Code PowerShell terminal, using the project
Python environment:

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m pbmc_platform.assistant --question "How many cells passed QC?"
```

Expected answer for the selected production package:

```text
2,638 of 2,700 cells passed QC (97.70%); 62 failed.
```

The output also lists the publication URI, original evidence locations and
scientific limitations. Append `--json` to inspect structured facts, their JSON
pointers into the package, citations and the pinned publication generation/hash.

Run without `--question` to list the six questions. Topic keys also work:

| Key | Question |
| --- | --- |
| `summary` | Summarize the dataset |
| `qc` | How many cells passed QC? |
| `exclusions` | Why were cells excluded? |
| `policy` | What QC thresholds were used? |
| `dbt` | Did the dbt tests pass? |
| `limitations` | What are the limitations? |

The original interface matched listed questions. [Stage 6.3](019-assistant-question-routing.md)
now accepts bounded everyday phrasings, ignoring case, surrounding whitespace and
final punctuation. Unrecognized wording, mixed requests, cell-type inference and disease comparisons return
`unsupported` with no factual answer or evidence citations. This deliberately
bounded interface does not pretend to understand arbitrary natural language.

## Which knowledge is trusted?

[The selection file](../configs/assistant-package.json) pins the actual successful
Stage 5 production publication: release ID, package ID, publication generation,
URI and SHA-256. The default local bundle under
[`examples/verified-pbmc/`](../examples/verified-pbmc/) contains the four unchanged
files read back from that publication. It is real public-data output, unlike the
separate illustrative `examples/pbmc-knowledge-v1.json` fixture. GCP resource names
and a temporary producer path in its audit are evidence metadata, not credentials.

Before answering, the reader checks:

1. The publication marker matches the operator-selected SHA-256 and package ID.
2. All three files have the marker's exact lengths and SHA-256 values; references
   remain under the selected bucket/release/package and have positive generations.
3. The existing publication contract accepts the bundle: successful validation,
   current validator/schema hashes, complete validation checks, reproducible
   report, consistent metrics, limitations and source evidence bindings.
4. The recomputed package identity and ID match the marker and trusted selection.

The pin is an operator-controlled trust root. Changing both the pin and artifacts
is not prevented by a signature. Offline checks verify a preserved published
snapshot, not current GCS availability or a new calculation from source data.
Source data and BigQuery are not re-queried when answering. Preserve exact archived
bytes; `.gitattributes` prevents line-ending conversion of the bundle and fixes
the hashed validator/schema sources to LF.

To re-download the exact pinned generations, use your ADC and read access:

```powershell
.\.venv\Scripts\python.exe -m pbmc_platform.assistant `
  --fetch --bundle-dir artifacts/assistant-cache --question qc
```

Downloads are checked in a temporary directory before updating the selected cache.
An interrupted local cache update fails integrity checks on the next read; rerun
the download. There is no latest-package lookup, SQL tool, external web retrieval
or cloud write permission requirement. A future model must consume this checked
context and cannot choose its own trust pin through a question.

## Verification and next increment

Fourteen focused tests cover supported answers and citations, unsupported/mixed
questions, changed files, missing completion marker, wrong package selection,
generation-pinned fetching, preservation on corrupt downloads and consistency
of the archived production bundle. The first live readback and local `qc` answer
were verified on 2026-11-04. The prior full Stage 5 suite passed 198 tests.

Stage 6.1 is complete, followed by the local browser interface in Stage 6.2.
Stage 6.3 adds evaluated grammar routing with abstention and citation checks.
Stage 6 as a whole is still in progress; there is no model-based interface yet.
