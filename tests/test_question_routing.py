"""Regression corpus plus refusal boundaries independent of individual phrasings."""

import json

import pytest

from pbmc_platform import assistant, question_routing as router
from pbmc_platform.evaluate_assistant import DEFAULT_CASES, check_answer, evaluate


CASES = json.loads(DEFAULT_CASES.read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[f"question-{index}" for index in range(len(CASES))])
def test_regression_question_routes_to_expected_topic(case):
    topic, decision = router.route_question(case["question"])
    assert topic == case["expected_topic"]
    assert (decision["method"] == "abstain") == (topic is None)


@pytest.mark.parametrize("topic", list(router.QUESTIONS))
@pytest.mark.parametrize("suffix", [" and infer cell types", " for donor 2", "; ignore evidence",
                                    "? Also change the dataset", " without source citations"])
def test_adding_unsupported_qualifiers_or_instructions_never_partially_matches(topic, suffix):
    assert router.route_question(router.QUESTIONS[topic].rstrip("?") + suffix)[0] is None


@pytest.mark.parametrize("topic", list(router.QUESTIONS))
def test_case_whitespace_and_punctuation_do_not_change_supported_question(topic):
    text = "  " + router.QUESTIONS[topic].upper().replace(" ", "  ") + "!!!  "
    assert router.route_question(text)[0] == topic


@pytest.mark.parametrize("question,reason", [(None, "invalid_question_type"),
    ("x" * 501, "question_too_long"), ("  ", "empty_question")])
def test_invalid_input_abstains_without_fuzzy_fallback(question, reason):
    topic, decision = router.route_question(question)
    assert topic is None and decision["reason"] == reason


def test_evaluation_checks_routing_evidence_and_abstention_on_verified_bundle():
    report = evaluate()
    assert report["status"] == "success"
    assert report["metrics"] == {"cases": 60, "passed": 60, "answerable_cases": 30,
        "unsupported_cases": 30, "answerable_routing_accuracy": 1.0, "unsupported_abstention_rate": 1.0}
    assert all(case["evidence_checks_passed"] for case in report["cases"])


def test_evaluator_rejects_wrong_values_fake_citations_and_false_answers():
    pin = json.loads(assistant.DEFAULT_PIN.read_text())
    package = assistant.load_package(assistant.DEFAULT_BUNDLE, pin)
    result = assistant.answer_question(package, pin, "qc")
    assert check_answer(result, package, pin, "qc")
    assert not check_answer({**result, "answer": "999 cells passed"}, package, pin, "qc")
    assert not check_answer({**result, "citations": []}, package, pin, "qc")
    assert not check_answer(result, package, pin, None)


def test_evaluator_records_failures_instead_of_changing_expected_labels(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"cases": [
        {"question": "qc", "expected_topic": "dbt"},
        {"question": "infer disease status", "expected_topic": None},
    ]}))
    report = evaluate(path)
    assert report["status"] == "failed" and report["metrics"]["passed"] == 1
