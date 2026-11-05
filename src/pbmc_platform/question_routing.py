"""Conservative whole-question grammar for six evidence queries, without a model."""

import re


QUESTIONS = {
    "summary": "Summarize the dataset",
    "qc": "How many cells passed QC?",
    "exclusions": "Why were cells excluded?",
    "policy": "What QC thresholds were used?",
    "dbt": "Did the dbt tests pass?",
    "limitations": "What are the limitations?",
}
ROUTER_VERSION = "1"
MAX_QUESTION_LENGTH = 500

# Every expression must match the entire normalized question. No wildcard suffix
# can silently discard a second request, instruction or unsupported qualifier.
PATTERNS = {
    "summary": (
        r"(?:summari[sz]e|describe|tell me about) (?:this |the )?dataset",
        r"(?:give|show) me (?:a summary|an overview) of (?:this |the )?dataset",
        r"what (?:is this dataset|dataset are you using)",
        r"how many (?:cells|features) (?:are|were) in (?:this |the )?dataset",
        r"how many features were measured",
    ),
    "qc": (
        r"how many cells (?:passed|failed|pass|fail) (?:qc|quality control)",
        r"how many cells (?:were retained|were kept|remain|are left) after (?:qc|quality control)",
        r"what (?:percentage|fraction|proportion) of cells (?:passed|pass) (?:qc|quality control)",
        r"(?:show|give) me (?:the )?(?:qc|quality control) (?:results|totals|summary)",
    ),
    "exclusions": (
        r"why (?:were|are) (?:some |the )?cells (?:excluded|filtered out|removed|flagged)",
        r"why did (?:some |the )?cells fail (?:qc|quality control)",
        r"what (?:are|were) the (?:qc )?exclusion reasons",
        r"(?:show|explain|list) (?:the )?(?:qc )?exclusion (?:reasons|counts|flags)",
    ),
    "policy": (
        r"what (?:qc|quality control) (?:thresholds|cutoffs|rules|policy) (?:were|was) used",
        r"what (?:are|were) the (?:qc|quality control) (?:thresholds|cutoffs|rules)",
        r"(?:show|explain|describe) (?:the )?(?:qc|quality control) (?:policy|thresholds|rules)",
        r"how (?:is|was) (?:a |the )?cell (?:marked|classified) as (?:passing|failing) (?:qc|quality control)",
    ),
    "dbt": (
        r"did (?:all |the )?dbt tests pass",
        r"(?:what is|show me) (?:the )?dbt (?:build|test) (?:status|results)",
        r"how many dbt (?:tests passed|models succeeded)",
        r"was (?:the )?dbt build successful",
    ),
    "limitations": (
        r"what (?:are|were) (?:the )?(?:dataset |scientific )?limitations",
        r"(?:show|explain|list|describe) (?:the )?(?:dataset |scientific )?limitations",
        r"what (?:can this dataset not tell us|conclusions are unsupported)",
    ),
}


def normalize(text):
    return " ".join(text.casefold().strip().rstrip("?.!").split())


def route_question(question):
    """Return a topic and an inspectable decision; never use partial matches."""
    decision = {"router_version": ROUTER_VERSION, "method": "abstain", "reason": "unsupported_phrasing"}
    if not isinstance(question, str):
        return None, {**decision, "reason": "invalid_question_type"}
    if len(question) > MAX_QUESTION_LENGTH:
        return None, {**decision, "reason": "question_too_long"}
    text = normalize(question)
    if not text:
        return None, {**decision, "reason": "empty_question"}
    exact = {normalize(value): key for key, value in QUESTIONS.items()}
    exact.update({key: key for key in QUESTIONS})
    if text in exact:
        return exact[text], {**decision, "method": "exact", "reason": "listed_question_or_topic"}
    # Polite wrappers are bounded and apply only at the edges of one question.
    text = re.sub(r"^(?:(?:can|could) you |please )", "", text)
    text = re.sub(r",? please$", "", text)
    matches = {topic for topic, patterns in PATTERNS.items()
               if any(re.fullmatch(pattern, text) for pattern in patterns)}
    if len(matches) == 1:
        return matches.pop(), {**decision, "method": "grammar", "reason": "one_supported_query"}
    return None, {**decision, "reason": "ambiguous_query" if matches else "unsupported_phrasing"}
