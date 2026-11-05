"""Serve the verified-package question interface on this computer only."""

import argparse
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from .assistant import DEFAULT_BUNDLE, DEFAULT_PIN, QUESTIONS, answer_question, load_package
from .validate_knowledge import strict_json


CSS = """
:root{color-scheme:light;--ink:#18363c;--muted:#526970;--line:#dbe5e5;--green:#176b56}
*{box-sizing:border-box}body{margin:0;background:#f4f7f6;color:var(--ink);font:16px/1.6 system-ui,sans-serif}
header{background:#fff;border-bottom:1px solid var(--line)}.top{max-width:1180px;margin:auto;padding:18px 32px;display:flex;gap:16px;justify-content:space-between;align-items:center}
.brand{font-weight:750;letter-spacing:-.4px}.badge{font-size:12px;font-weight:650;padding:5px 11px;border-radius:30px;background:#e1f2ea;color:#225c49}
main{max-width:1180px;margin:36px auto;padding:0 32px 42px}.eyebrow{text-transform:uppercase;letter-spacing:1.8px;font-size:11px;font-weight:750;color:var(--green)}
h1{font-size:38px;line-height:1.2;letter-spacing:-1.3px;margin:9px 0 12px}h2{font-size:19px;line-height:1.4;margin:0 0 12px}h3{font-size:15px;margin:16px 0 6px}
p{margin:8px 0 16px}.intro{color:var(--muted);max-width:690px}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:26px 0}
.stat,.card{background:white;border:1px solid var(--line);border-radius:14px}.stat{padding:18px 22px}.number{font-size:29px;line-height:1.3;font-weight:700;letter-spacing:-.7px}.label{font-size:13px;color:var(--muted)}
.layout{display:grid;grid-template-columns:minmax(0,1.8fr) minmax(280px,1fr);gap:22px;align-items:start}.card{padding:26px}.stack{display:grid;gap:20px}
label{display:block;font-size:13px;font-weight:650;margin:16px 0 7px}input{width:100%;border:1px solid #a7bcbb;border-radius:8px;padding:12px;color:var(--ink);background:white;font:inherit}
button{margin:14px 0 0;border:0;border-radius:8px;padding:12px 21px;background:var(--green);color:white;font:650 14px system-ui;cursor:pointer}
button:hover{background:#105642}a{color:var(--green);text-underline-offset:3px}a:hover{text-decoration-thickness:2px}button:focus-visible,input:focus-visible,a:focus-visible,summary:focus-visible{outline:3px solid #c28432;outline-offset:4px}
.answer{background:#f0f7f3;border-left:3px solid #39957a;padding:18px 20px;border-radius:0 10px 10px 0;margin:22px 0}.answer p{font-size:19px;line-height:1.6;margin:0;white-space:pre-line}
.caption,.meta{font-size:12px;color:var(--muted)}.meta{margin-top:10px}.sources{padding:0;list-style:none}.sources li{padding:13px 0;border-top:1px solid var(--line)}code{font:12px/1.6 ui-monospace,Consolas,monospace;overflow-wrap:anywhere}
.source-name{font-size:13px;font-weight:700}.source-kind{font-size:11px;color:var(--muted);margin-left:7px}.limits{padding-left:20px;font-size:14px}.limits li{margin:8px 0}details{border-top:1px solid var(--line);margin-top:16px;padding-top:12px}summary{cursor:pointer;font-size:13px;font-weight:650}
.identity{font-size:12px;overflow-wrap:anywhere}.identity dt{font-weight:700;margin-top:12px}.identity dd{margin:3px 0 0}.foot{font-size:12px;color:var(--muted);margin-top:25px}.error{max-width:650px;margin:70px auto}
@media(max-width:760px){.layout{grid-template-columns:1fr}main{padding:0 18px;margin-top:25px}.top{padding:16px 18px}.card{padding:20px}.stat{padding:14px}.number{font-size:23px}h1{font-size:30px}.stats{gap:8px}.badge{font-size:10px}}
"""


def document(content, cloud_run=False):
    label = "Authenticated cloud demo" if cloud_run else "Local portfolio demo"
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>PBMC | Evidence assistant</title>"
            f"<style>{CSS}</style></head><body><header><div class='top'>"
            f"<div class='brand'>PBMC / Knowledge platform</div><span class='badge'>{label}</span>"
            f"</div></header>{content}</body></html>")


def render_page(package, pin, result, cloud_run=False):
    e = escape
    metrics = package["results"]["metrics"]
    options = "".join(f"<option value='{e(question, quote=True)}'></option>" for question in QUESTIONS.values())
    question_text = QUESTIONS.get(result['question'], result['question'])
    suggestions = " &middot; ".join(
        f"<a href='/?{urlencode({'question': key})}'>{e(key)}</a>" for key in QUESTIONS)
    sources = "".join(
        f"<li><span class='source-name'>{e(c['evidence_id'])}</span>"
        f"<span class='source-kind'>{e(c['kind'])}</span><br>"
        f"<code>{e(c.get('uri', c.get('table_id', '')))}</code>"
        + (f"<details><summary>Source checksum</summary><code>{e(c['sha256'])}</code></details>" if 'sha256' in c else "")
        + "</li>" for c in result["citations"])
    if not sources:
        sources = "<li>No source citations: this request is outside the supported queries.</li>"
    limitations = "".join(f"<li>{e(item)}</li>" for item in result["limitations"])
    pointers = "".join(f"<li><code>{e(fact['package_pointer'])}</code></li>" for fact in result["facts"])
    export = "/answer.json?" + urlencode({"question": result["question"]})
    return document(f"""
    <main>
      <div class='eyebrow'>Scientist assistant / PBMC3k</div>
      <h1>Explore the evidence.</h1>
      <p class='intro'>Ask a question about the dataset, quality checks or build results.
      Answers come from one verified knowledge package.</p>
      <div class='stats' aria-label='Dataset overview'>
        <div class='stat'><div class='number'>{metrics['cells_total']:,}</div><div class='label'>Cells in the dataset</div></div>
        <div class='stat'><div class='number'>{metrics['cells_pass']:,}</div><div class='label'>Cells passing QC</div></div>
        <div class='stat'><div class='number'>{metrics['features_total']:,}</div><div class='label'>Features measured</div></div>
      </div>
      <div class='layout'>
        <section class='card'>
          <h2>Ask about this dataset</h2>
          <p class='caption'>Ask about one of six evidence topics. Use your own wording or try an example.</p>
          <form method='get' action='/'>
            <label for='question'>Your question</label>
            <input id='question' name='question' type='text' list='examples' maxlength='500'
              value='{e(question_text, quote=True)}' placeholder='How many cells failed quality control?' required>
            <datalist id='examples'>{options}</datalist>
            <button type='submit'>Show answer &rarr;</button>
          </form>
          <p class='caption'>Try: {suggestions}</p>
          <div class='answer' role='region' aria-label='Answer'><p>{e(result['answer'])}</p></div>
          <a class='caption' href='{e(export, quote=True)}'>View answer as JSON</a>
          <h3>Evidence sources</h3><ul class='sources'>{sources}</ul>
          {f"<details><summary>Fields used in this answer</summary><ul>{pointers}</ul></details>" if pointers else ''}
        </section>
        <aside class='stack'>
          <section class='card'><span class='badge'>Verified package</span>
            <h2 style='margin-top:16px'>Know the boundaries</h2><ul class='limits'>{limitations}</ul>
            <p class='caption'>These are dataset QC findings. They do not establish clinical conclusions.</p>
          </section>
          <section class='card'><h2>Where this answer comes from</h2>
            <p class='caption'>The saved publication and file checksums are checked before each answer.
            Original sources are not queried again.</p>
            <div class='label'>Dataset</div><strong>{e(package['identity']['dataset_id'])}</strong>
            <details><summary>Publication details</summary><dl class='identity'>
              <dt>Package ID</dt><dd><code>{e(pin['package_id'])}</code></dd>
              <dt>Release ID</dt><dd><code>{e(pin['release_id'])}</code></dd>
              <dt>Publication</dt><dd><code>{e(pin['publication_uri'])}</code></dd>
              <dt>Publication generation</dt><dd><code>{pin['publication_generation']}</code></dd>
              <dt>Publication SHA-256</dt><dd><code>{e(pin['publication_sha256'])}</code></dd>
            </dl></details>
          </section>
        </aside>
      </div>
      <p class='foot'>Public dataset: {e(package['dataset']['attribution'])} &middot; {e(package['dataset']['license'])}.
      Project-defined knowledge format. Grammar routing, six evidence topics, no language model.</p>
    </main>""", cloud_run=cloud_run)


def create_server(port=8765, bundle_dir=DEFAULT_BUNDLE, pin_path=DEFAULT_PIN, cloud_run=False):
    """Use loopback locally; explicit Cloud Run mode uses PORT behind platform IAM."""
    host = "127.0.0.1"
    if cloud_run:
        if os.environ.get("K_SERVICE") != "pbmc-assistant":
            raise ValueError("Cloud mode requires the pbmc-assistant Cloud Run service")
        raw_port = os.environ.get("PORT", "")
        if not raw_port.isascii() or not raw_port.isdigit() or not 1 <= int(raw_port) <= 65535:
            raise ValueError("Cloud mode requires a valid PORT")
        port, host = int(raw_port), "0.0.0.0"
    def verified_context():
        pin = strict_json(Path(pin_path).read_bytes())
        return load_package(bundle_dir, pin), pin

    verified_context()  # Refuse to start with an invalid package.

    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, payload, content_type="text/html; charset=utf-8"):
            data = payload.encode("utf-8")
            self.send_response(status)
            for key, value in {
                "Content-Type": content_type, "Content-Length": str(len(data)),
                "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            }.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            # Cloud Run handles routing and authentication before container access.
            # Locally reject browser requests redirected by an unrelated site's DNS.
            allowed_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if not cloud_run and self.headers.get("Host") not in allowed_hosts:
                self.respond(403, "Local access only.", "text/plain; charset=utf-8")
                return
            if len(self.path) > 2048:
                self.respond(414, "Question is too long.", "text/plain; charset=utf-8")
                return
            url = urlsplit(self.path)
            if url.path not in ("/", "/answer.json", "/ready"):
                self.respond(404, "Not found.", "text/plain; charset=utf-8")
                return
            params = parse_qs(url.query, keep_blank_values=True)
            if set(params) - {"question"} or len(params.get("question", [])) > 1:
                self.respond(400, "Choose one question.", "text/plain; charset=utf-8")
                return
            try:
                package, pin = verified_context()
            except Exception:
                self.respond(503, document("<main class='error card'><h1>Knowledge package unavailable</h1>"
                    "<p>The saved package could not be verified. No answer was generated.</p>"
                    "<p>Restore the selected verified bundle and reload this page.</p></main>", cloud_run=cloud_run))
                return
            if url.path == "/ready":
                self.respond(200, '{"status":"ready"}', "application/json; charset=utf-8")
                return
            result = answer_question(package, pin, params.get("question", ["qc"])[0])
            if url.path == "/answer.json":
                self.respond(200, json.dumps(result, indent=2, allow_nan=False), "application/json; charset=utf-8")
            else:
                self.respond(200, render_page(package, pin, result, cloud_run=cloud_run))

        def log_message(self, fmt, *args):
            # Log status only; questions or paths need not be written to the terminal.
            print(f"Assistant HTTP status: {args[1] if len(args) > 1 else 'unknown'}", flush=True)

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--cloud-run", action="store_true", help="Use Cloud Run's injected PORT and ingress; requires K_SERVICE")
    parser.add_argument("--bundle-dir", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--pin", type=Path, default=DEFAULT_PIN)
    args = parser.parse_args()
    try:
        server = create_server(args.port, args.bundle_dir, args.pin, args.cloud_run)
    except Exception as error:
        parser.exit(1, f"Assistant could not start: {error}\n")
    print(f"PBMC assistant listening on {server.server_address[0]}:{server.server_port}\nPress Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
