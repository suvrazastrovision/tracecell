# Stage 6.3: typed questions and evaluated routing

The browser now accepts typed questions about six evidence topics. A conservative
English grammar routes supported phrasings to the existing deterministic answer
templates. The CLI uses the same router. No language model, API key, cloud write
or additional dependency is involved.

## Try it

Start the app from the repository root, then open http://127.0.0.1:8765:

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m pbmc_platform.assistant_web
```

Type **How many cells failed quality control?** and click **Show answer**. The
answer includes the full QC totals: 2,638 passed, 62 failed, 2,700 total and 97.70%
passing. The sources remain the selected dataset QC table and release manifest.
You can also try:

- What percentage of cells passed QC?
- Give me an overview of the dataset
- Why did some cells fail QC?
- Explain the QC policy
- Was the dbt build successful?
- Explain the scientific limitations

Example links and autocomplete suggestions help you choose a supported query.
The text you entered stays in the input after submission. Select **View answer
as JSON** to inspect the routing method, reason, version, source citations and
publication identity.

CLI example:

```powershell
.\.venv\Scripts\python.exe -m pbmc_platform.assistant `
  --question "How many cells failed quality control?" --json
```

## Routing and abstention

The router first checks listed questions/topic keys, then bounded whole-question
patterns. Case, extra whitespace, trailing punctuation and a small set of polite
wrappers are tolerated. Only one topic may match. There is no substring search,
wildcard suffix, fuzzy fallback or conversation memory. Input is limited to 500
characters. Unknown phrasing returns `unsupported` with no factual answer or
citations, even when it concerns a supported topic.

Whole-question matching prevents a recognized prefix from concealing a second
request. For example, **How many cells passed QC? Also infer disease status.**
must abstain. So must a different donor/dataset qualifier, a hypothetical QC
threshold, a request to suppress sources or a request to change the selected
package. The router never selects cloud resources or modifies thresholds.

The six topics remain dataset summary, QC totals, exclusion flags, QC policy,
dbt results and limitations. Routing does not expand the scientific evidence.
There are no inferred cell types, disease comparisons, clinical conclusions,
new calculations for hypothetical policies or live/latest-run claims.

## Reproduce the evaluation

[The regression corpus](../evaluations/assistant-questions-v1.json) has **60
hand-authored cases**: 30 answerable phrasings across all six topics and 30
unsupported, ambiguous, mixed or adversarial requests. It is a regression set
developed with this grammar, not a held-out test of general language understanding.

```powershell
.\.venv\Scripts\python.exe -m pbmc_platform.evaluate_assistant
.\.venv\Scripts\python.exe -m pytest tests/test_assistant.py tests/test_assistant_web.py tests/test_question_routing.py -q
```

On 2026-11-04, **60/60 evaluation cases passed**: all 30 supported questions routed
to their expected topics and all 30 unsupported requests abstained. In addition
to routing, the evaluator checks the selected package ID, unchanged limitations,
expected evidence references, exact citation records, resolvable package pointers
and specified answer values. It is not an independent semantic check of every
word in an answer.

**133 focused tests passed**, including corruption detection, HTTP/CLI parity,
typed-question preservation, HTML escaping and 30 generated refusal tests that
append unsupported qualifiers/instructions to listed questions. No new visual
layout inspection is claimed; the browser access host was unavailable during
the previous UI increment, and this increment is checked over local HTTP.

The [archived evaluation](../examples/assistant-evaluation-v1.json) records each
case and hashes of the corpus, router, answer engine and evaluator. A fresh local
report is written to the ignored `artifacts/assistant-evaluation.json`; evaluation
failure exits nonzero and retains the mismatches for inspection. Hashes identify
the evaluated files; this is not a signed attestation.

Stage 6.3 is complete. [Stage 6.4](020-cloud-assistant.md) now deploys the same
bounded interface to authenticated Cloud Run. Any future model-based language
interface remains a separate increment.
