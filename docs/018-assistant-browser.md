# Stage 6.2: local browser interface

The assistant now has a local browser interface that uses the same checked package
and deterministic answers as the CLI. It shows dataset totals, a question
input, the answer, evidence sources and scientific limitations. Expand
publication details to inspect the exact package, release, generation and SHA-256.
The JSON link exposes the structured answer and its supporting package pointers.

For the authenticated cloud deployment, use [the Stage 6.4 guide](020-cloud-assistant.md).
The instructions below run the separate local Python application.

## Open it

In VS Code PowerShell, from the repository root:

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m pbmc_platform.assistant_web
```

Keep that terminal running. Open **http://127.0.0.1:8765** in your browser.
Type a question or choose an autocomplete example, then click **Show answer**.
The [Stage 6.3 routing guide](019-assistant-question-routing.md) lists supported
phrasings and evaluation evidence. The default question is
"How many cells passed QC?" and its answer is:

```text
2,638 of 2,700 cells passed QC (97.70%); 62 failed.
```

Press **Ctrl+C** in the terminal to stop the interface. To reopen it after closing
VS Code or restarting the computer, open the repository folder and run the same
two commands. No AI API key or GCP login is required for the archived package.

If port 8765 is in use, choose another port:

```powershell
.\.venv\Scripts\python.exe -m pbmc_platform.assistant_web --port 8766
```

Then open `http://127.0.0.1:8766`. The server prints the actual address on startup.

## Verification and boundaries

The local command binds only `127.0.0.1`. The separate Cloud Run mode and its
IAM boundary are documented in the cloud deployment guide.
Package selection comes from startup arguments, never from browser input. A
missing or invalid bundle prevents startup. The saved publication and files are
checked again for every answer and health request. If a file is changed while the
server runs, requests return HTTP 503 without showing a stale answer or raw local
exception details. Restore the trusted bundle before continuing.

Responses are marked `no-store`; the page uses local CSS, escaped text and no
JavaScript or external assets. Foreign Host headers are rejected. Unrecognized
questions abstain through the existing answer engine. Evidence URIs and table
names are displayed for inspection; their presence does not grant cloud access.
Source data is not fetched or revalidated by page requests.

Thirty focused tests passed across the CLI foundation and browser interface,
including real loopback HTTP requests for all six questions, JSON parity with
the CLI engine, corruption detection after startup, unsupported input, invalid
requests, foreign hosts and startup refusal. The running demo's HTML, JSON and
health routes were also checked. Automated visual inspection could not run
because the computer/browser access host was unavailable; responsive CSS and
semantic form controls are implemented but no screenshot-based layout check is
claimed.

Stage 6.2 is complete. Stage 6.3 adds evaluated grammar routing for typed questions.
Stage 6.4 adds the authenticated cloud service. Any model-based interface remains
future work. Answers still use six fixed evidence queries, without a language model.
