"""Exercise the browser interface over real loopback HTTP with the archived bundle."""

from http.client import HTTPConnection
import json
import shutil
from threading import Thread
from urllib.parse import urlencode

import pytest

from pbmc_platform import assistant, assistant_web


@pytest.fixture
def web_server(tmp_path):
    bundle = tmp_path / "bundle"
    shutil.copytree(assistant.DEFAULT_BUNDLE, bundle)
    server = assistant_web.create_server(0, bundle_dir=bundle)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, bundle
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def get(server, path="/", headers=None):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode("utf-8")
    finally:
        connection.close()


def test_default_page_contains_verified_answer_and_citations(web_server):
    server, _ = web_server
    status, headers, html = get(server)
    assert status == 200
    assert "2,638 of 2,700 cells passed QC (97.70%); 62 failed." in html
    assert "mart_dataset_qc" in html and "one healthy donor" in html
    assert "Original sources are not queried again" in " ".join(html.split())
    assert "<label for='question'>" in html and "type='text'" in html
    assert "value='How many cells passed QC?'" in html
    assert headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


@pytest.mark.parametrize("topic", list(assistant.QUESTIONS))
def test_each_question_matches_cli_engine_and_json_export(web_server, topic):
    server, _ = web_server
    pin = json.loads(assistant.DEFAULT_PIN.read_text())
    package = assistant.load_package(assistant.DEFAULT_BUNDLE, pin)
    expected = assistant.answer_question(package, pin, topic)
    status, headers, body = get(server, "/answer.json?" + urlencode({"question": topic}))
    assert status == 200 and headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == expected
    assert get(server, "/?" + urlencode({"question": topic}))[0] == 200


def test_unsupported_input_is_escaped_and_abstains(web_server):
    server, _ = web_server
    status, _, html = get(server, "/?" + urlencode({"question": '<script>alert("test")</script>'}))
    assert status == 200 and "outside the supported evidence queries" in html
    assert "<script>" not in html and "No source citations" in html
    assert "&lt;script&gt;" in html
    status, _, payload = get(server, "/answer.json?question=infer-cell-types")
    assert status == 200 and json.loads(payload)["facts"] == []


def test_rechecks_bundle_before_every_answer_and_health_check(web_server):
    server, bundle = web_server
    assert get(server, "/ready")[0] == 200
    (bundle / "knowledge.json").write_bytes(b"altered")
    for path in ("/", "/answer.json?question=qc", "/ready"):
        status, _, body = get(server, path)
        assert status == 503 and "No answer was generated" in body
        assert "2,638" not in body and str(bundle) not in body


@pytest.mark.parametrize("path,status", [
    ("/../../configs/assistant-package.json", 404), ("/missing", 404),
    ("/?pin=untrusted", 400), ("/?question=qc&question=dbt", 400),
    ("/?question=" + "x" * 2050, 414),
])
def test_invalid_requests_cannot_change_package_selection(web_server, path, status):
    assert get(web_server[0], path)[0] == status


def test_server_is_loopback_only_and_rejects_foreign_host(web_server):
    server, _ = web_server
    assert server.server_address[0] == "127.0.0.1"
    assert get(server, headers={"Host": "unrelated.example"})[0] == 403


def test_refuses_to_start_without_valid_publication(tmp_path):
    with pytest.raises(FileNotFoundError):
        assistant_web.create_server(0, bundle_dir=tmp_path)


def test_typed_paraphrase_is_preserved_and_routes_to_cited_answer(web_server):
    query = "How many cells failed quality control?"
    status, _, html = get(web_server[0], "/?" + urlencode({"question": query}))
    assert status == 200 and f"value='{query}'" in html
    assert "2,638 of 2,700" in html and "mart_dataset_qc" in html
    status, _, payload = get(web_server[0], "/answer.json?" + urlencode({"question": query}))
    result = json.loads(payload)
    assert status == 200 and result["topic"] == "qc" and result["routing"]["method"] == "grammar"


def test_cloud_mode_requires_expected_service_environment(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    with pytest.raises(ValueError, match="Cloud Run service"):
        assistant_web.create_server(cloud_run=True)


@pytest.mark.parametrize("port", ["", "0", "65536", "not-a-port"])
def test_cloud_mode_rejects_invalid_injected_port(monkeypatch, port):
    monkeypatch.setenv("K_SERVICE", "pbmc-assistant")
    monkeypatch.setenv("PORT", port)
    with pytest.raises(ValueError, match="valid PORT"):
        assistant_web.create_server(cloud_run=True)


def test_cloud_mode_uses_injected_port_and_platform_routing(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "pbmc-assistant")
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setattr(assistant_web, "ThreadingHTTPServer", lambda address, handler: address)
    assert assistant_web.create_server(cloud_run=True) == ("0.0.0.0", 8080)
    assert assistant_web.create_server() == ("127.0.0.1", 8765)
    assert "Authenticated cloud demo" in assistant_web.document("test", cloud_run=True)
