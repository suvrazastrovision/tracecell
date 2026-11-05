"""Evaluate routing, abstention and specified evidence checks on a regression set."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from . import assistant, question_routing
from .ingestion import sha256_file


DEFAULT_CASES = assistant.ROOT / "evaluations/assistant-questions-v1.json"
EXPECTED_CITATIONS = {
    "summary": {"release", "dataset_qc"}, "qc": {"dataset_qc", "release"},
    "exclusions": {"qc_exclusions", "release"}, "policy": {"release"},
    "dbt": {"dbt_audit", "dbt_manifest", "dbt_results"}, "limitations": {"release"},
}


def check_answer(result, package, pin, expected_topic):
    """Check references and selected result values; this is not a semantic model judge."""
    if result["topic"] != expected_topic:
        return False
    if result["package_id"] != pin["package_id"] or result["limitations"] != package["limitations"]:
        return False
    if expected_topic is None:
        return result["status"] == "unsupported" and not result["facts"] and not result["citations"]
    if result["status"] != "answered" or not result["facts"]:
        return False
    if {source["evidence_id"] for source in result["citations"]} != EXPECTED_CITATIONS[expected_topic]:
        return False
    for source in result["citations"]:
        if source != {"evidence_id": source["evidence_id"], **package["evidence"][source["evidence_id"]]}:
            return False
    try:
        for fact in result["facts"]:
            node = package
            for part in fact["package_pointer"].strip("/").split("/"):
                node = node[int(part)] if isinstance(node, list) else node[part]
    except (KeyError, ValueError, IndexError, TypeError):
        return False
    m = package["results"]["metrics"]
    expected_values = {
        "summary": [f"{m['cells_total']:,}", f"{m['features_total']:,}", package["identity"]["dataset_id"]],
        "qc": [f"{m['cells_pass']:,}", f"{m['cells_fail']:,}", f"{m['cells_total']:,}", f"{m['qc_pass_fraction']:.2%}"],
        "exclusions": [f"{key}: {value:,}" for key, value in package["results"]["exclusions"]["counts"].items()] + ["overlap"],
        "policy": [f">= {package['qc_policy']['min_genes_inclusive']}", f"< {package['qc_policy']['max_genes_exclusive']}",
                   f"< {package['qc_policy']['max_mito_pct_exclusive']}%"],
        "dbt": [str(package["dbt"]["models_succeeded"]), str(package["dbt"]["tests_passed"]), package["dbt"]["invocation_id"]],
        "limitations": package["limitations"],
    }
    return all(value in result["answer"] for value in expected_values[expected_topic])


def evaluate(cases_path=DEFAULT_CASES, pin_path=assistant.DEFAULT_PIN, bundle_dir=assistant.DEFAULT_BUNDLE):
    pin = json.loads(Path(pin_path).read_text())
    package = assistant.load_package(bundle_dir, pin)
    cases = json.loads(Path(cases_path).read_text())["cases"]
    if not cases or not any(row["expected_topic"] is None for row in cases):
        raise ValueError("Evaluation requires answerable and unsupported cases")
    records = []
    for case in cases:
        result = assistant.answer_question(package, pin, case["question"])
        records.append({**case, "actual_topic": result["topic"], "status": result["status"],
                        "routing": result["routing"],
                        "routing_correct": result["topic"] == case["expected_topic"],
                        "evidence_checks_passed": check_answer(result, package, pin, case["expected_topic"])})
    supported = [row for row in records if row["expected_topic"] is not None]
    unsupported = [row for row in records if row["expected_topic"] is None]
    if not supported:
        raise ValueError("Evaluation requires answerable cases")
    passed = sum(row["routing_correct"] and row["evidence_checks_passed"] for row in records)
    return {"schema_version": 1, "status": "success" if passed == len(records) else "failed",
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "scope": "Hand-authored regression set, not a held-out benchmark or independent semantic grounding proof.",
            "package_id": pin["package_id"], "release_id": pin["release_id"],
            "cases_sha256": sha256_file(cases_path), "router_sha256": sha256_file(question_routing.__file__),
            "answer_engine_sha256": sha256_file(assistant.__file__), "evaluator_sha256": sha256_file(__file__),
            "metrics": {"cases": len(records), "passed": passed,
                        "answerable_cases": len(supported), "unsupported_cases": len(unsupported),
                        "answerable_routing_accuracy": sum(row["routing_correct"] for row in supported) / len(supported),
                        "unsupported_abstention_rate": sum(row["status"] == "unsupported" for row in unsupported) / len(unsupported)},
            "cases": records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-path", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=Path("artifacts/assistant-evaluation.json"))
    args = parser.parse_args()
    report = evaluate(args.cases_path)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "metrics": report["metrics"], "output": str(args.output)}, indent=2))
    if report["status"] != "success":
        parser.exit(1, "Assistant regression evaluation failed; inspect the report.\n")


if __name__ == "__main__":
    main()
