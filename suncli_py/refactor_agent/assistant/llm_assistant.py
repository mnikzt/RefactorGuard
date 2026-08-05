"""LLM decision and planning stages for refactor-agent."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from suncli_py.config.config import PaiCliConfig
from suncli_py.llm.client import LlmClient
from suncli_py.llm.factory import create_client_from_config
from suncli_py.memory.manager import MemoryManager
from suncli_py.refactor_agent.analysis.candidate_ranker import RankedCandidate, select_triage_candidates
from suncli_py.refactor_agent.analysis.java_ast import AstFileAnalysis
from suncli_py.refactor_agent.assistant.prompts import (
    explanation_system_prompt,
    planning_system_prompt,
    triage_system_prompt,
)
from suncli_py.refactor_agent.assistant.react import (
    ReactAgent,
    _reset_sync_loop_for_tests,
    _run_async,
    _sync_loop_id_for_tests,
)
from suncli_py.refactor_agent.assistant.toolbox import RefactorAgentToolbox, RefactorAgentToolRuntime
from suncli_py.refactor_agent.core.models import (
    CandidateDecision,
    DecisionStatus,
    Evidence,
    RefactoringType,
    RefactorIssue,
    RefactorPlan,
    RiskLevel,
    Severity,
    TriageResult,
)

__all__ = [
    "RefactorLlmAssistant",
    "RefactorLlmError",
    "_reset_sync_loop_for_tests",
    "_run_async",
    "_sync_loop_id_for_tests",
]


class RefactorLlmAssistant:
    """LLM brain for the Java refactor agent.

    Static rules, JavaParser AST data, Symbol Solver calls, tests, and file
    excerpts are treated as tool observations. The LLM decides priority,
    planning, patch operations, and repair attempts; the rest of the pipeline
    validates and applies those decisions.
    """

    def __init__(self, client: LlmClient) -> None:
        self.client = client
        self._scan_ast_analyses: tuple[AstFileAnalysis, ...] | None = None
        self._memory_managers: dict[Path, MemoryManager] = {}

    def bind_scan_analyses(self, analyses: Sequence[AstFileAnalysis]) -> None:
        """Reuse the AST snapshot produced by the current scan."""
        self._scan_ast_analyses = tuple(analyses)

    @classmethod
    def from_config(cls) -> RefactorLlmAssistant | None:
        client = create_client_from_config(PaiCliConfig.load())
        return cls(client) if client else None

    def triage_issues(self, root: Path, issues: list[RefactorIssue], *, limit: int = 20) -> TriageResult:
        """Investigate candidates individually and accept, reject, or defer them."""
        if not issues:
            return TriageResult(issues=[], decisions=[])

        toolbox = RefactorAgentToolbox(root, self._scan_ast_analyses)
        tools = RefactorAgentToolRuntime(toolbox, issues=issues)
        selected, deferred = select_triage_candidates(issues, limit=limit)
        decisions_by_id: dict[str, CandidateDecision] = {}
        ranked: list[tuple[int, RefactorIssue]] = []
        original_index_by_id = {issue.id: index for index, issue in enumerate(issues)}

        for candidate in selected:
            issue = candidate.issue
            index = original_index_by_id[issue.id]

            data = self._chat_json(
                system=triage_system_prompt(),
                user=(
                    "Investigate this single scanner candidate and make the final semantic decision. Return JSON: "
                    '{"candidate_id":"RA-0001","decision":"accept|reject|uncertain",'
                    '"confidence":0.0,"priority":1,"reason":"...","root_cause":"...",'
                    '"source_evidence":[{"file_path":"...","start_line":1,"end_line":2,"reason":"..."}],'
                    '"severity":"low|medium|high","risk_level":"low|medium|high",'
                    '"suggested_refactoring":"Extract Method|Extract Class|Introduce Explaining Variable|'
                    'Move Method|Replace Duplicate Logic With Shared Method|Remove Dead Code|'
                    'Rename Variable / Method / Class",'
                    '"impact":"...","recommendation":"...","proposed_solution":"...",'
                    '"verification_strategy":["..."]}. '
                    "Do not accept a candidate without concrete source_evidence.\n"
                    + toolbox.as_json(toolbox.issue_context(issue))
                ),
                tools=tools,
                root=root,
            )
            decision = _candidate_decision(root, issue, data)
            decision = _decision_with_rank(decision, candidate)
            decisions_by_id[issue.id] = decision
            if decision.status != DecisionStatus.ACCEPT:
                continue

            priority = _int_value(data.get("priority"), index + 1)
            ranked.append(
                (
                    priority,
                    replace(
                        issue,
                        severity=_enum_value(Severity, data.get("severity"), issue.severity),
                        risk_level=_enum_value(RiskLevel, data.get("risk_level"), issue.risk_level),
                        suggested_refactoring=_enum_value(
                            RefactoringType, data.get("suggested_refactoring"), issue.suggested_refactoring
                        ),
                        impact=str(data.get("impact") or issue.impact),
                        recommendation=str(
                            data.get("recommendation") or decision.proposed_solution or issue.recommendation
                        ),
                        evidence=[
                            *issue.evidence,
                            Evidence(
                                "LLM triage decision",
                                {
                                    "decision": decision.status.value,
                                    "confidence": decision.confidence,
                                    "priority": priority,
                                    "reason": decision.reason,
                                    "root_cause": decision.root_cause,
                                    "source_evidence": decision.source_evidence,
                                    "verification_strategy": decision.verification_strategy,
                                },
                            ),
                        ],
                    ),
                )
            )
        for candidate in deferred:
            decisions_by_id[candidate.issue.id] = CandidateDecision(
                candidate_id=candidate.issue.id,
                status=DecisionStatus.UNCERTAIN,
                confidence=0.0,
                reason=(
                    f"Candidate was deferred by hotspot-aware triage scheduling because the LLM budget is {limit}."
                ),
                rank_score=candidate.score,
                rank_reasons=candidate.reasons,
            )
        return TriageResult(
            issues=[issue for _, issue in sorted(ranked, key=lambda item: item[0])],
            decisions=[
                decisions_by_id[issue.id]
                for issue in issues
                if issue.id in decisions_by_id
            ],
        )

    def _legacy_triage_issues(self, root: Path, issues: list[RefactorIssue], *, limit: int = 20) -> list[RefactorIssue]:
        """Let the LLM decide which rule/AST candidates are worth fixing."""
        if not issues:
            return []

        toolbox = RefactorAgentToolbox(root)
        tools = RefactorAgentToolRuntime(toolbox, issues=issues)
        data = self._chat_json(
            system=triage_system_prompt(),
            user=(
                "Triage these candidate code issues. Return JSON: "
                '{"issues":[{"id":"RA-0001","priority":1,'
                '"severity":"low|medium|high","risk_level":"low|medium|high",'
                '"suggested_refactoring":"Extract Method|Extract Class|Introduce Explaining Variable|'
                'Move Method|Replace Duplicate Logic With Shared Method|Remove Dead Code|'
                'Rename Variable / Method / Class",'
                '"impact":"...","recommendation":"...","decision_reason":"..."}]}.\n'
                + toolbox.as_json(toolbox.issue_candidates(issues, limit=limit))
            ),
            tools=tools,
            root=root,
        )

        decisions = {
            str(item.get("id", "")).upper(): item
            for item in data.get("issues", [])
            if isinstance(item, dict) and item.get("id")
        }
        ranked: list[tuple[int, RefactorIssue]] = []
        for index, issue in enumerate(issues):
            decision = decisions.get(issue.id.upper())
            if not decision:
                ranked.append((index + 10_000, issue))
                continue

            priority = _int_value(decision.get("priority"), index + 1)
            ranked.append(
                (
                    priority,
                    replace(
                        issue,
                        severity=_enum_value(Severity, decision.get("severity"), issue.severity),
                        risk_level=_enum_value(RiskLevel, decision.get("risk_level"), issue.risk_level),
                        suggested_refactoring=_enum_value(
                            RefactoringType,
                            decision.get("suggested_refactoring"),
                            issue.suggested_refactoring,
                        ),
                        impact=str(decision.get("impact") or issue.impact),
                        recommendation=str(decision.get("recommendation") or issue.recommendation),
                        evidence=[
                            *issue.evidence,
                            Evidence(
                                "LLM triage decision",
                                {
                                    "priority": priority,
                                    "reason": str(decision.get("decision_reason") or "").strip(),
                                },
                            ),
                        ],
                    ),
                )
            )
        return [issue for _, issue in sorted(ranked, key=lambda item: item[0])]

    def explain_issues(self, root: Path, issues: list[RefactorIssue], *, limit: int = 5) -> list[RefactorIssue]:
        """Backward-compatible issue explanation step."""
        updated: list[RefactorIssue] = []
        toolbox = RefactorAgentToolbox(root)
        for index, issue in enumerate(issues):
            if index >= limit:
                updated.append(issue)
                continue
            data = self._chat_json(
                system=explanation_system_prompt(),
                user=(
                    "Explain this Java issue and give a safe refactoring recommendation. Return JSON: "
                    '{"impact":"...","recommendation":"...","risk_notes":["..."],"confidence":"low|medium|high"}.\n'
                    + toolbox.as_json(toolbox.issue_context(issue))
                ),
                tools=RefactorAgentToolRuntime(toolbox, issue=issue),
                root=root,
            )
            risk_notes = [str(item) for item in data.get("risk_notes", []) if str(item).strip()]
            evidence = issue.evidence
            if risk_notes:
                evidence = [*issue.evidence, Evidence("LLM risk notes", {"notes": risk_notes})]
            updated.append(
                replace(
                    issue,
                    impact=str(data.get("impact") or issue.impact),
                    recommendation=str(data.get("recommendation") or issue.recommendation),
                    evidence=evidence,
                )
            )
        return updated

    def generate_plan(self, root: Path, plan: RefactorPlan, issue: RefactorIssue) -> RefactorPlan:
        """Let the LLM generate the user-confirmed plan from a safe scaffold."""
        toolbox = RefactorAgentToolbox(root)
        tools = RefactorAgentToolRuntime(toolbox, plan=plan, issue=issue)
        data = self._chat_json(
            system=planning_system_prompt(),
            user=(
                "Generate the refactoring plan. Return JSON: "
                '{"goal":"...","refactoring_type":"...","files_to_modify":["..."],'
                '"expected_changes":["..."],"out_of_scope":["..."],'
                '"risk_level":"low|medium|high","risk_reasons":["..."],'
                '"verification_commands":["..."],"rollback_strategy":"..."}.\n'
                + toolbox.as_json(toolbox.plan_context(plan, issue))
            ),
            tools=tools,
            root=root,
        )
        return replace(
            plan,
            goal=str(data.get("goal") or plan.goal),
            refactoring_type=_enum_value(RefactoringType, data.get("refactoring_type"), plan.refactoring_type),
            files_to_modify=_string_list(data.get("files_to_modify"), plan.files_to_modify),
            expected_changes=_string_list(data.get("expected_changes"), plan.expected_changes),
            out_of_scope=_string_list(data.get("out_of_scope"), plan.out_of_scope),
            risk_level=_enum_value(RiskLevel, data.get("risk_level"), plan.risk_level),
            risk_reasons=_string_list(data.get("risk_reasons"), plan.risk_reasons),
            verification_commands=_string_list(data.get("verification_commands"), plan.verification_commands),
            rollback_strategy=str(data.get("rollback_strategy") or plan.rollback_strategy),
            planning_source="llm-primary",
        )

    def enhance_plan(self, plan: RefactorPlan, issue: RefactorIssue) -> RefactorPlan:
        """Backward-compatible plan enhancement API used by older tests/callers."""
        payload = {
            "issue": issue.to_dict(),
            "plan": plan.to_dict(),
            "source_excerpt": plan.context.source_excerpt[:8000],
        }
        data = self._chat_json(
            system=planning_system_prompt(),
            user=(
                "Enhance this plan. Return JSON: "
                '{"goal":"...","expected_changes":["..."],"out_of_scope":["..."],'
                '"risk_reasons":["..."],"verification_commands":["..."]}.\n'
                + json.dumps(payload, ensure_ascii=False)
            ),
            root=Path(".").resolve(),
        )
        return replace(
            plan,
            goal=str(data.get("goal") or plan.goal),
            expected_changes=_string_list(data.get("expected_changes"), plan.expected_changes),
            out_of_scope=_string_list(data.get("out_of_scope"), plan.out_of_scope),
            risk_reasons=_string_list(data.get("risk_reasons"), plan.risk_reasons),
            verification_commands=_string_list(data.get("verification_commands"), plan.verification_commands),
            planning_source="llm-enhanced",
        )

    def _chat_json(
        self,
        *,
        system: str,
        user: str,
        tools: RefactorAgentToolRuntime | None = None,
        root: Path | None = None,
    ) -> dict[str, Any]:
        resolved_root = (root or Path(".")).resolve()
        memory = self._memory_managers.get(resolved_root)
        if memory is None:
            memory = MemoryManager(self.client, resolved_root)
            self._memory_managers[resolved_root] = memory
        result = ReactAgent(
            name="refactor-stage",
            client=self.client,
            root=resolved_root,
            system_prompt=system,
            tools=tools,
            memory=memory,
        ).run_json(user)
        if result.error:
            raise RefactorLlmError(result.error)
        return result.data or {}


class RefactorLlmError(Exception):
    """Raised when the LLM provider call fails."""


def _string_list(value: Any, fallback: list[str]) -> list[str]:
    if not isinstance(value, list):
        return fallback
    result = [str(item).strip() for item in value if str(item).strip()]
    return result or fallback


def _enum_value(enum_type, value: Any, fallback):
    if value is None:
        return fallback
    text = str(value).strip()
    if not text:
        return fallback
    for item in enum_type:
        if text.lower() in {item.value.lower(), item.name.lower()}:
            return item
    return fallback


def _int_value(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _candidate_decision(root: Path, issue: RefactorIssue, data: dict[str, Any]) -> CandidateDecision:
    status = _enum_value(DecisionStatus, data.get("decision"), DecisionStatus.UNCERTAIN)
    reason = str(data.get("reason") or "").strip()
    raw_evidence = data.get("source_evidence")
    source_evidence = _validated_source_evidence(root, raw_evidence)
    if status in {DecisionStatus.ACCEPT, DecisionStatus.REJECT} and (not reason or not source_evidence):
        status = DecisionStatus.UNCERTAIN
        reason = reason or "The model did not provide enough repository evidence for a final decision."
    try:
        confidence = min(1.0, max(0.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    return CandidateDecision(
        candidate_id=issue.id,
        status=status,
        confidence=confidence,
        reason=reason or "The model returned no supported decision.",
        source_evidence=source_evidence,
        root_cause=str(data.get("root_cause") or "").strip(),
        proposed_solution=str(data.get("proposed_solution") or data.get("recommendation") or "").strip(),
        verification_strategy=[
            str(item).strip()
            for item in data.get("verification_strategy", [])
            if str(item).strip()
        ]
        if isinstance(data.get("verification_strategy"), list)
        else [],
    )


def _decision_with_rank(decision: CandidateDecision, candidate: RankedCandidate) -> CandidateDecision:
    return replace(
        decision,
        rank_score=candidate.score,
        rank_reasons=candidate.reasons,
    )


def _validated_source_evidence(root: Path, value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    validated: list[dict[str, Any]] = []
    resolved_root = root.resolve()
    for item in value:
        if not isinstance(item, dict):
            continue
        file_path = str(item.get("file_path") or "").replace("\\", "/").lstrip("/")
        try:
            path = (resolved_root / file_path).resolve()
            path.relative_to(resolved_root)
            start_line = int(item.get("start_line", 0))
            end_line = int(item.get("end_line", start_line))
        except (OSError, TypeError, ValueError):
            continue
        if not path.is_file() or start_line < 1 or end_line < start_line:
            continue
        line_count = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        if end_line > line_count:
            continue
        validated.append(
            {
                "file_path": file_path,
                "start_line": start_line,
                "end_line": end_line,
                "reason": str(item.get("reason") or "").strip(),
            }
        )
    return validated
