"""Rank scanner candidates before spending LLM triage budget."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from suncli_py.refactor_agent.core.models import RefactorIssue, Severity, SmellType


@dataclass(frozen=True)
class RankedCandidate:
    issue: RefactorIssue
    score: float
    issue_score: float
    hotspot_score: float
    reasons: list[str]


_SEVERITY_WEIGHT = {
    Severity.HIGH: 40.0,
    Severity.MEDIUM: 24.0,
    Severity.LOW: 8.0,
}

_SMELL_WEIGHT = {
    SmellType.DUPLICATE_CODE: 28.0,
    SmellType.LONG_METHOD: 24.0,
    SmellType.LARGE_CLASS: 22.0,
    SmellType.FEATURE_ENVY: 20.0,
    SmellType.DEAD_CODE: 18.0,
    SmellType.COMPLEX_CONDITION: 14.0,
    SmellType.UNCLEAR_NAMING: 4.0,
}

_EVIDENCE_SOURCE_WEIGHT = {
    "pmd-cpd": 18.0,
    "javaparser-symbol-solver": 16.0,
    "symbol-solver": 14.0,
    "identifier-count-fallback": 5.0,
}

# These are conservative signals for library-shaped code.  They lower triage
# priority but never remove a candidate: a lookup table or public API can
# still contain a real refactoring opportunity.
_STATIC_GUARD_PENALTIES = {
    "flat_branch_lookup": 10.0,
    "public_api_dense": 8.0,
    "primitive_overload_family": 10.0,
    "symmetric_exception_api": 10.0,
}

_PUBLIC_API_CLASS_SUFFIXES = ("Style", "Utils", "Builder")


def rank_candidates(issues: Iterable[RefactorIssue]) -> list[RankedCandidate]:
    """Rank candidates by local value signals and file-level hotspots.

    A file is allowed to dominate when it has diverse, well-evidenced issues.
    Only repetitive low-value smells, especially unclear naming, are dampened.
    """
    issue_list = list(issues)
    file_hotspots = _file_hotspot_scores(issue_list)
    repeated_type_counts: dict[tuple[str, SmellType], int] = defaultdict(int)
    ranked: list[RankedCandidate] = []

    for issue in issue_list:
        repeated_type_counts[(issue.file_path, issue.type)] += 1
        issue_score, reasons = _issue_score(issue)
        hotspot_score = file_hotspots[issue.file_path]
        duplicate_penalty = _same_file_same_type_penalty(issue, repeated_type_counts[(issue.file_path, issue.type)])
        if duplicate_penalty:
            reasons.append(f"same-file {issue.type.value} repetition penalty -{duplicate_penalty:g}")
        guard_penalty, guard_reasons = _static_guard_adjustment(issue)
        score = issue_score + hotspot_score - duplicate_penalty - guard_penalty
        ranked.append(
            RankedCandidate(
                issue=issue,
                score=score,
                issue_score=issue_score,
                hotspot_score=hotspot_score,
                reasons=[*reasons, *guard_reasons, f"file hotspot +{hotspot_score:g}"],
            )
        )

    original_index = {id(issue): index for index, issue in enumerate(issue_list)}
    return sorted(
        ranked,
        key=lambda candidate: (
            -candidate.score,
            original_index[id(candidate.issue)],
        ),
    )


def select_triage_candidates(
    issues: list[RefactorIssue],
    *,
    limit: int,
) -> tuple[list[RankedCandidate], list[RankedCandidate]]:
    """Split candidates into selected and deferred groups for LLM triage."""
    if limit <= 0:
        return [], rank_candidates(issues)
    ranked = rank_candidates(issues)
    return ranked[:limit], ranked[limit:]


def _issue_score(issue: RefactorIssue) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    severity_score = _SEVERITY_WEIGHT.get(issue.severity, 0.0)
    score += severity_score
    reasons.append(f"{issue.severity.value} severity +{severity_score:g}")

    smell_score = _SMELL_WEIGHT.get(issue.type, 0.0)
    score += smell_score
    reasons.append(f"{issue.type.value} type +{smell_score:g}")

    evidence_score = _evidence_score(issue)
    score += evidence_score
    if evidence_score:
        reasons.append(f"evidence strength +{evidence_score:g}")

    span = max(1, issue.end_line - issue.start_line + 1)
    span_score = min(12.0, span / 20.0)
    score += span_score
    if span_score >= 1.0:
        reasons.append(f"code span +{span_score:g}")

    if issue.type == SmellType.DEAD_CODE and issue.risk_level.value == "low":
        score += 6.0
        reasons.append("low-risk dead-code refactorability +6")
    if issue.type == SmellType.UNCLEAR_NAMING:
        score -= 8.0
        reasons.append("low-value naming noise -8")

    return score, reasons


def _file_hotspot_scores(issues: list[RefactorIssue]) -> dict[str, float]:
    by_file: dict[str, list[RefactorIssue]] = defaultdict(list)
    for issue in issues:
        by_file[issue.file_path].append(issue)

    scores: dict[str, float] = {}
    for file_path, file_issues in by_file.items():
        type_counts = Counter(issue.type for issue in file_issues)
        meaningful = [issue for issue in file_issues if issue.type != SmellType.UNCLEAR_NAMING]
        high_or_medium = [issue for issue in file_issues if issue.severity in {Severity.HIGH, Severity.MEDIUM}]
        diversity = len({issue.type for issue in meaningful})
        evidence_total = sum(_evidence_score(issue) for issue in file_issues)
        naming_noise = max(0, type_counts[SmellType.UNCLEAR_NAMING] - 2)
        score = (
            min(18.0, len(high_or_medium) * 3.0)
            + min(14.0, diversity * 3.5)
            + min(10.0, evidence_total / 4.0)
            - min(12.0, naming_noise * 2.0)
        )
        scores[file_path] = max(0.0, score)
    return scores


def _evidence_score(issue: RefactorIssue) -> float:
    score = 0.0
    for evidence in issue.evidence:
        source = str(evidence.metrics.get("source") or "").strip()
        score += _EVIDENCE_SOURCE_WEIGHT.get(source, 0.0)
        if evidence.metrics.get("resolved_signature"):
            score += 4.0
        if evidence.metrics.get("duplicate_locations"):
            score += 4.0
        if {"lines", "branches", "max_nesting"} & set(evidence.metrics):
            score += 4.0
    return min(24.0, score)


def _same_file_same_type_penalty(issue: RefactorIssue, occurrence: int) -> float:
    if occurrence <= 1:
        return 0.0
    if issue.type == SmellType.UNCLEAR_NAMING:
        return min(40.0, (occurrence - 1) * 8.0)
    if issue.type == SmellType.COMPLEX_CONDITION:
        return min(16.0, (occurrence - 1) * 3.0)
    return min(8.0, (occurrence - 1) * 1.5)


def _static_guard_adjustment(issue: RefactorIssue) -> tuple[float, list[str]]:
    """Apply explainable, non-blocking guards for common library structures.

    Rule examples:
    - ``hexDigitMsb0ToBinary``: 44 lines, 24 branches, nesting 1 -> likely a
      flat lookup/switch table, so triage later.
    - ``ToStringStyle``: many public methods and a large public surface ->
      could be an intentional extension/template API, so triage later.

    The scanner evidence remains authoritative and the issue is never filtered.
    """
    penalties: list[tuple[str, float]] = []
    for evidence in issue.evidence:
        metrics = evidence.metrics
        if issue.type == SmellType.LONG_METHOD:
            lines = _numeric_metric(metrics, "lines")
            branches = _numeric_metric(metrics, "branches")
            nesting = _numeric_metric(metrics, "max_nesting")
            if lines <= 80 and branches >= 13 and nesting <= 1:
                penalties.append(("flat_branch_lookup", _STATIC_GUARD_PENALTIES["flat_branch_lookup"]))

        if issue.type == SmellType.LARGE_CLASS:
            methods = _numeric_metric(metrics, "methods")
            public_methods = _numeric_metric(metrics, "public_methods")
            api_shape = str(issue.symbol or "")
            if public_methods >= 20 and (
                (methods > 0 and public_methods / methods >= 0.5)
                or api_shape.endswith(_PUBLIC_API_CLASS_SUFFIXES)
            ):
                penalties.append(("public_api_dense", _STATIC_GUARD_PENALTIES["public_api_dense"]))

        if issue.type == SmellType.DUPLICATE_CODE and metrics.get("overload_family"):
            penalties.append(("primitive_overload_family", _STATIC_GUARD_PENALTIES["primitive_overload_family"]))

        if issue.type == SmellType.DUPLICATE_CODE and metrics.get("api_symmetry"):
            penalties.append(("symmetric_exception_api", _STATIC_GUARD_PENALTIES["symmetric_exception_api"]))

    total = sum(penalty for _, penalty in penalties)
    reasons = [f"static guard {label} -{penalty:g} (defer, not reject)" for label, penalty in penalties]
    return total, reasons


def _numeric_metric(metrics: dict[str, object], key: str) -> float:
    value = metrics.get(key, 0)
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0
