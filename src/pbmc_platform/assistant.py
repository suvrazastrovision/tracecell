"""Answer a bounded set of questions from a pinned, committed knowledge bundle."""

import argparse
import json
from pathlib import Path
import re
import tempfile

from .publish_knowledge import BUNDLE_FILES, digest, prepare_bundle
from .question_routing import QUESTIONS, route_question
from .validate_knowledge import strict_json


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PIN = ROOT / "configs/assistant-package.json"
DEFAULT_BUNDLE = ROOT / "examples/verified-pbmc"


def validate_pin(pin):
    for field in ("release_id", "package_id", "publication_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", pin[field]):
            raise ValueError(f"Invalid pinned {field}")
    prefix = f"knowledge/releases/{pin['release_id']}/{pin['package_id']}"
    if pin["publication_uri"] != f"gs://{pin['bucket']}/{prefix}/publication.json":
        raise ValueError("Pinned publication URI differs from the selected package")
    if type(pin["publication_generation"]) is not int or pin["publication_generation"] <= 0:
        raise ValueError("Positive pinned publication generation required")
    return prefix


def load_package(directory, pin):
    """Check a local cache against an operator-trusted marker hash and bundle ID.

    This verifies preserved publication evidence, not a fresh source-data audit.
    The pin must come from a trusted successful pipeline run, never from a question.
    """
    prefix = validate_pin(pin)
    directory = Path(directory)
    marker_bytes = (directory / "publication.json").read_bytes()
    if digest(marker_bytes) != pin["publication_sha256"]:
        raise ValueError("Publication marker checksum differs from trusted pin")
    marker = strict_json(marker_bytes)
    if (marker.get("publication_schema_version") != 1 or marker.get("status") != "published"
            or marker.get("package_id") != pin["package_id"]
            or set(marker.get("objects", {})) != set(BUNDLE_FILES)):
        raise ValueError("Expected a committed publication marker for the pinned package")
    for name in BUNDLE_FILES:
        spec = marker["objects"][name]
        payload = (directory / name).read_bytes()
        if (spec["uri"] != f"gs://{pin['bucket']}/{prefix}/{name}"
                or type(spec["generation"]) is not int or spec["generation"] <= 0
                or len(payload) != spec["bytes"] or digest(payload) != spec["sha256"]):
            raise ValueError(f"Published file integrity check failed: {name}")
    with tempfile.TemporaryDirectory(prefix="pbmc-assistant-check-") as temp:
        plan = prepare_bundle(directory, pin["project"], pin["bucket"], pin["release_id"], temp)
    if plan["package_id"] != pin["package_id"] or plan["identity"] != marker["identity"]:
        raise ValueError("Bundle identity differs from the pinned publication")
    return strict_json((directory / "knowledge.json").read_bytes())


def fetch_package(directory, pin, client=None):
    """Read the exact pinned GCS generations; validate before replacing the cache."""
    prefix = validate_pin(pin)
    if client is None:
        from google.cloud import storage
        client = storage.Client(project=pin["project"])
    remote = client.bucket(pin["bucket"])

    def download(name, generation):
        return remote.blob(name, generation=generation).download_as_bytes(
            if_generation_match=generation, checksum="crc32c", timeout=120)

    with tempfile.TemporaryDirectory(prefix="pbmc-assistant-fetch-") as temp:
        root = Path(temp)
        payload = download(f"{prefix}/publication.json", pin["publication_generation"])
        if digest(payload) != pin["publication_sha256"]:
            raise ValueError("Downloaded publication marker differs from trusted pin")
        marker = strict_json(payload)
        (root / "publication.json").write_bytes(payload)
        if set(marker.get("objects", {})) != set(BUNDLE_FILES):
            raise ValueError("Incomplete publication marker")
        for name in BUNDLE_FILES:
            (root / name).write_bytes(download(f"{prefix}/{name}", marker["objects"][name]["generation"]))
        load_package(root, pin)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        for name in (*BUNDLE_FILES, "publication.json"):
            (directory / name).write_bytes((root / name).read_bytes())


def answer_question(package, pin, question):
    """Route one supported query; keep factual rendering bound to package fields."""
    topic, routing = route_question(question)
    metrics = package["results"]["metrics"]
    refs, facts = [], []

    def fact(text, path):
        facts.append({"text": text, "package_pointer": path})

    if topic == "summary":
        data = package["dataset"]
        fact(f"Dataset: {package['identity']['dataset_id']}; {data['organism']}, {data['tissue']}, "
             f"{data['donor_count']} donor(s), reference {data['reference_genome']}.", "/dataset")
        fact(f"The count matrix contains {metrics['cells_total']:,} cells and {metrics['features_total']:,} features.", "/results/metrics")
        refs = list(dict.fromkeys(package["dataset"]["evidence_refs"] + package["results"]["evidence_refs"]))
    elif topic == "qc":
        fact(f"{metrics['cells_pass']:,} of {metrics['cells_total']:,} cells passed QC "
             f"({metrics['qc_pass_fraction']:.2%}); {metrics['cells_fail']:,} failed.", "/results/metrics")
        refs = package["results"]["evidence_refs"]
    elif topic == "exclusions":
        counts = package["results"]["exclusions"]["counts"]
        fact("Recorded exclusion flags: " + "; ".join(f"{key}: {value:,}" for key, value in sorted(counts.items()))
             + ". Reasons can overlap; do not sum them to infer failed-cell totals.", "/results/exclusions")
        refs = package["results"]["exclusions"]["evidence_refs"]
    elif topic == "policy":
        policy = package["qc_policy"]
        fact(f"Passing requires detected genes >= {policy['min_genes_inclusive']} and < {policy['max_genes_exclusive']}, "
             f"mitochondrial counts < {policy['max_mito_pct_exclusive']}%, and nonzero total counts. "
             f"Mitochondrial gene prefix: {policy['mitochondrial_prefix']}.", "/qc_policy")
        refs = policy["evidence_refs"]
    elif topic == "dbt":
        dbt = package["dbt"]
        fact(f"dbt status: {dbt['status']}; {dbt['models_succeeded']} successful models "
             f"and {dbt['tests_passed']} passing tests. Invocation: {dbt['invocation_id']}.", "/dbt")
        refs = dbt["evidence_refs"]
    elif topic == "limitations":
        for index, limitation in enumerate(package["limitations"]):
            fact(limitation, f"/limitations/{index}")
        refs = package["dataset"]["evidence_refs"]
    return {
        "status": "answered" if topic else "unsupported",
        "question": question, "topic": topic,
        "routing": routing,
        "answer": "\n".join(item["text"] for item in facts) if topic else
            "This question is outside the supported evidence queries. Choose a listed question; "
            "try asking about dataset totals, QC results, exclusion reasons, thresholds, dbt tests or limitations. "
            "This version cannot infer cell types, compare diseases or answer arbitrary questions.",
        "facts": facts,
        "citations": [{"evidence_id": ref, **package["evidence"][ref]} for ref in refs],
        "package_id": pin["package_id"], "release_id": pin["release_id"],
        "publication_uri": pin["publication_uri"],
        "publication_generation": pin["publication_generation"],
        "publication_sha256": pin["publication_sha256"],
        "limitations": package["limitations"],
        "verification_scope": "Pinned published snapshot; original validation evidence checked locally, sources not re-queried.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pin", type=Path, default=DEFAULT_PIN)
    parser.add_argument("--bundle-dir", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--fetch", action="store_true", help="Read the pinned package from GCS using ADC")
    parser.add_argument("--question", help="A question about one supported evidence topic, or its topic key")
    parser.add_argument("--json", action="store_true", help="Print structured answer and citation evidence")
    args = parser.parse_args()
    try:
        pin = strict_json(args.pin.read_bytes())
        if args.fetch:
            fetch_package(args.bundle_dir, pin)
        package = load_package(args.bundle_dir, pin)
        if args.question is None:
            print("Verified package loaded. Supported questions:")
            for key, question in QUESTIONS.items():
                print(f"  {key}: {question}")
            return
        result = answer_question(package, pin, args.question)
        if args.json:
            print(json.dumps(result, indent=2, allow_nan=False))
        else:
            print(result["answer"])
            print(f"\nPackage: {result['package_id']}\nPublication: {result['publication_uri']}")
            for citation in result["citations"]:
                print(f"[{citation['evidence_id']}] {citation.get('uri', citation.get('table_id'))}")
            print("\nLimitations:")
            for limitation in result["limitations"]:
                print(f"- {limitation}")
    except Exception as error:
        parser.exit(1, f"Assistant could not verify or answer: {error}\n")


if __name__ == "__main__":
    main()
