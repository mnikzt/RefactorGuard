from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest
from loguru import logger

from suncli_py.llm.models import ChatResponse, ContentPart, ToolCall, _Function
from suncli_py.refactor_agent.analysis.candidate_ranker import rank_candidates
from suncli_py.refactor_agent.analysis.java_ast import AstFileAnalysis
from suncli_py.refactor_agent.assistant.llm_assistant import (
    RefactorLlmAssistant,
    RefactorLlmError,
    _reset_sync_loop_for_tests,
    _run_async,
    _sync_loop_id_for_tests,
)
from suncli_py.refactor_agent.assistant.toolbox import RefactorAgentToolbox, RefactorAgentToolRuntime
from suncli_py.refactor_agent.core.models import (
    CoverageAssessment,
    DecisionStatus,
    Evidence,
    JavaContext,
    ProjectProfile,
    RefactoringType,
    RefactorIssue,
    RefactorPlan,
    RiskLevel,
    ScanResult,
    Severity,
    SmellType,
)
from suncli_py.refactor_agent.core.storage import RefactorAgentStorage
from suncli_py.refactor_agent.interface.commands import run_plan, run_scan


def test_content_part_defaults_and_factories_do_not_shadow_fields() -> None:
    empty = ContentPart(type="text")
    assert empty.text is None
    assert empty.image_base64 is None
    assert empty.image_url is None

    text_part = ContentPart.from_text("hello")
    image_part = ContentPart.from_image_base64("aGVsbG8=", "image/png")
    url_part = ContentPart.from_image_url("https://example.com/image.png")

    assert text_part.text == "hello"
    assert image_part.image_base64 == "aGVsbG8="
    assert image_part.mime_type == "image/png"
    assert url_part.image_url == "https://example.com/image.png"


def test_llm_assistant_explains_issues_and_enhances_plan(tmp_path: Path) -> None:
    source_path = _write_dead_code_java_file(tmp_path)
    issue = _dead_code_issue(source_path)
    plan = _plan(issue)
    assistant = RefactorLlmAssistant(
        _FakeLlmClient(
            [
                (
                    '{"impact":"LLM explains maintainability impact",'
                    '"recommendation":"LLM recommends guarded removal",'
                    '"risk_notes":["check reflection"],"confidence":"high"}'
                ),
                (
                    '{"goal":"LLM refined goal","expected_changes":["delete private method only"],'
                    '"out_of_scope":["public API changes"],"risk_reasons":["LLM risk"],'
                    '"verification_commands":["mvn -q -DskipTests compile","mvn test"]}'
                ),
            ]
        )
    )

    explained = assistant.explain_issues(tmp_path, [issue])
    enhanced = assistant.enhance_plan(plan, issue)

    assert explained[0].impact == "LLM explains maintainability impact"
    assert explained[0].recommendation == "LLM recommends guarded removal"
    assert any(evidence.message == "LLM risk notes" for evidence in explained[0].evidence)
    assert enhanced.goal == "LLM refined goal"
    assert enhanced.planning_source == "llm-enhanced"
    assert "public API changes" in enhanced.out_of_scope


def test_refactor_llm_sync_bridge_reuses_event_loop() -> None:
    _reset_sync_loop_for_tests()
    try:
        first_loop_id = _run_async(_current_loop_id())
        second_loop_id = _run_async(_current_loop_id())

        assert first_loop_id == second_loop_id
        assert first_loop_id == _sync_loop_id_for_tests()
    finally:
        _reset_sync_loop_for_tests()


def test_llm_provider_failure_is_wrapped_as_refactor_error(tmp_path: Path) -> None:
    source_path = _write_dead_code_java_file(tmp_path)
    assistant = RefactorLlmAssistant(_FailingLlmClient(RuntimeError("Event loop is closed")))

    with pytest.raises(RefactorLlmError, match="LLM request failed"):
        assistant.explain_issues(tmp_path, [_dead_code_issue(source_path)])


def test_scan_lets_llm_triage_rule_and_ast_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = _write_scan_long_method_java_file(tmp_path)
    monkeypatch.chdir(tmp_path)

    assistant = _FakeDecisionAssistant(
        {
            "RA-0001": {
                "priority": 1,
                "severity": "high",
                "risk_level": "medium",
                "suggested_refactoring": "Extract Method",
                "impact": "LLM decided the method is too hard to safely maintain",
                "recommendation": "Extract a cohesive helper after checking tests",
                "decision_reason": "AST metrics and source excerpt show repeated accumulator steps",
            }
        }
    )
    exit_code = run_scan(output_format="json", llm_assistant=assistant)

    assert exit_code == 0
    assert assistant.bound_analysis_paths == [source_path.relative_to(tmp_path).as_posix()]
    issue = RefactorAgentStorage(tmp_path).load_scan_result().issues[0]
    assert issue.file_path == source_path.relative_to(tmp_path).as_posix()
    assert issue.severity == Severity.HIGH
    assert issue.risk_level == RiskLevel.MEDIUM
    assert issue.impact == "LLM decided the method is too hard to safely maintain"
    assert any(evidence.message == "LLM triage decision" for evidence in issue.evidence)


def test_llm_triage_rejects_false_positive_after_source_review(tmp_path: Path) -> None:
    source_path = _write_dead_code_java_file(tmp_path)
    assistant = RefactorLlmAssistant(
        _FakeLlmClient(
            [
                (
                    '{"candidate_id":"RA-0001","decision":"reject","confidence":0.92,'
                    '"reason":"The private method is invoked reflectively by a framework registration",'
                    '"source_evidence":[{"file_path":"src/main/java/demo/OrderService.java",'
                    '"start_line":4,"end_line":6,"reason":"Framework entry point"}]}'
                )
            ]
        )
    )

    result = assistant.triage_issues(tmp_path, [_dead_code_issue(source_path)])

    assert result.issues == []
    assert result.decisions[0].status == DecisionStatus.REJECT
    assert result.decisions[0].confidence == pytest.approx(0.92)


def test_llm_triage_requires_evidence_before_accepting_candidate(tmp_path: Path) -> None:
    source_path = _write_dead_code_java_file(tmp_path)
    assistant = RefactorLlmAssistant(
        _FakeLlmClient(
            ['{"candidate_id":"RA-0001","decision":"accept","confidence":0.8,"reason":"Looks unused"}']
        )
    )

    result = assistant.triage_issues(tmp_path, [_dead_code_issue(source_path)])

    assert result.issues == []
    assert result.decisions[0].status == DecisionStatus.UNCERTAIN


def test_candidate_ranker_allows_real_hotspot_file_to_dominate(tmp_path: Path) -> None:
    noisy_file = _write_java_lines(tmp_path, "src/main/java/demo/Names.java", line_count=20)
    hotspot_file = _write_java_lines(tmp_path, "src/main/java/demo/Hotspot.java", line_count=140)
    issues = [
        _simple_issue(
            "RA-0001",
            SmellType.UNCLEAR_NAMING,
            Severity.LOW,
            noisy_file,
            4,
            4,
            Evidence("unclear local name"),
        ),
        _simple_issue(
            "RA-0002",
            SmellType.LONG_METHOD,
            Severity.HIGH,
            hotspot_file,
            10,
            120,
            Evidence("large method", {"lines": 111, "branches": 14, "max_nesting": 3}),
        ),
        _simple_issue(
            "RA-0003",
            SmellType.FEATURE_ENVY,
            Severity.MEDIUM,
            hotspot_file,
            20,
            70,
            Evidence("external member use", {"source": "javaparser-symbol-solver"}),
        ),
    ]

    ranked = rank_candidates(issues)

    assert [candidate.issue.id for candidate in ranked[:2]] == ["RA-0002", "RA-0003"]
    assert ranked[0].hotspot_score > 0
    assert any("file hotspot" in reason for reason in ranked[1].reasons)


def test_candidate_ranker_downranks_flat_lookup_without_filtering(tmp_path: Path) -> None:
    source = _write_java_lines(tmp_path, "src/main/java/demo/Lookup.java", line_count=60)
    issue = _simple_issue(
        "RA-0100",
        SmellType.LONG_METHOD,
        Severity.HIGH,
        source,
        10,
        53,
        Evidence("branch-heavy method", {"lines": 44, "branches": 24, "max_nesting": 1}),
    )

    ranked = rank_candidates([issue])[0]

    assert ranked.issue.id == "RA-0100"
    assert any("flat_branch_lookup" in reason for reason in ranked.reasons)
    assert any("defer, not reject" in reason for reason in ranked.reasons)


def test_candidate_ranker_downranks_dense_public_api_without_filtering(tmp_path: Path) -> None:
    source = _write_java_lines(tmp_path, "src/main/java/demo/ToStringStyle.java", line_count=2400)
    issue = _simple_issue(
        "RA-0101",
        SmellType.LARGE_CLASS,
        Severity.HIGH,
        source,
        1,
        2300,
        Evidence("large public surface", {"lines": 2300, "methods": 40, "public_methods": 30}),
    )

    ranked = rank_candidates([issue])[0]

    assert ranked.issue.id == "RA-0101"
    assert any("public_api_dense" in reason for reason in ranked.reasons)


def test_candidate_ranker_downranks_primitive_overload_family_without_filtering(tmp_path: Path) -> None:
    source = _write_java_lines(tmp_path, "src/main/java/demo/ArrayUtils.java", line_count=220)
    issue = _simple_issue(
        "RA-0102",
        SmellType.DUPLICATE_CODE,
        Severity.HIGH,
        source,
        20,
        80,
        Evidence(
            "CPD duplicate",
            {
                "source": "pmd-cpd",
                "overload_family": [
                    {
                        "declaring_type": "demo.ArrayUtils",
                        "method_name": "contains",
                        "signatures": ["contains(int[] array, int value)", "contains(long[] array, long value)"],
                    }
                ],
            },
        ),
    )

    ranked = rank_candidates([issue])[0]

    assert ranked.issue.id == "RA-0102"
    assert any("primitive_overload_family" in reason for reason in ranked.reasons)


def test_llm_triage_uses_hotspot_aware_schedule_before_deferring(tmp_path: Path) -> None:
    noisy_file = _write_java_lines(tmp_path, "src/main/java/demo/Names.java", line_count=20)
    hotspot_file = _write_java_lines(tmp_path, "src/main/java/demo/Hotspot.java", line_count=140)
    issues = [
        _simple_issue(
            "RA-0001",
            SmellType.UNCLEAR_NAMING,
            Severity.LOW,
            noisy_file,
            4,
            4,
            Evidence("unclear local name"),
        ),
        _simple_issue(
            "RA-0002",
            SmellType.LONG_METHOD,
            Severity.HIGH,
            hotspot_file,
            10,
            120,
            Evidence("large method", {"lines": 111, "branches": 14, "max_nesting": 3}),
        ),
        _simple_issue(
            "RA-0003",
            SmellType.FEATURE_ENVY,
            Severity.MEDIUM,
            hotspot_file,
            20,
            70,
            Evidence("external member use", {"source": "javaparser-symbol-solver"}),
        ),
    ]
    assistant = RefactorLlmAssistant(
        _FakeLlmClient(
            [
                (
                    '{"candidate_id":"RA-0002","decision":"reject","confidence":0.7,'
                    '"reason":"source review does not show a cohesive extractable block",'
                    '"source_evidence":[{"file_path":"src/main/java/demo/Hotspot.java",'
                    '"start_line":10,"end_line":12,"reason":"reviewed hotspot"}]}'
                ),
                (
                    '{"candidate_id":"RA-0003","decision":"reject","confidence":0.7,'
                    '"reason":"external calls are acceptable orchestration",'
                    '"source_evidence":[{"file_path":"src/main/java/demo/Hotspot.java",'
                    '"start_line":20,"end_line":22,"reason":"reviewed hotspot"}]}'
                ),
            ]
        )
    )

    result = assistant.triage_issues(tmp_path, issues, limit=2)

    assert [decision.candidate_id for decision in result.decisions] == ["RA-0001", "RA-0002", "RA-0003"]
    assert result.decisions[0].status == DecisionStatus.UNCERTAIN
    assert "deferred by hotspot-aware triage scheduling" in result.decisions[0].reason
    assert result.decisions[1].status == DecisionStatus.REJECT
    assert result.decisions[2].status == DecisionStatus.REJECT
    assert result.decisions[1].rank_score is not None
    assert result.decisions[2].rank_score is not None


def test_plan_is_generated_by_llm_from_tool_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = _write_dead_code_java_file(tmp_path)
    issue = _dead_code_issue(source_path)
    RefactorAgentStorage(tmp_path).save_scan_result(
        ScanResult(
            profile=ProjectProfile(
                root=tmp_path,
                is_git_repo=True,
                is_maven_project=True,
                has_main_java=True,
                has_test_java=False,
                is_git_clean=True,
            ),
            issues=[issue],
        )
    )
    monkeypatch.chdir(tmp_path)

    exit_code = run_plan(
        issue_id=issue.id,
        llm_assistant=_FakePlanAssistant(
            {
                "goal": "LLM authored plan to remove unreachable private method",
                "refactoring_type": "Remove Dead Code",
                "files_to_modify": [issue.file_path],
                "expected_changes": ["delete only unusedPrivate"],
                "out_of_scope": ["do not touch public createOrder"],
                "risk_level": "low",
                "risk_reasons": ["private method has no direct callers"],
                "verification_commands": ["mvn test"],
                "rollback_strategy": "restore the task snapshot",
            }
        ),
    )

    assert exit_code == 0
    task_dir = next((tmp_path / ".paicli" / "refactor-agent" / "tasks").iterdir())
    plan = RefactorAgentStorage(tmp_path).load_task_plan(task_dir)[0]
    assert plan.goal == "LLM authored plan to remove unreachable private method"
    assert plan.planning_source == "llm-primary"
    assert plan.expected_changes == ["delete only unusedPrivate"]


def test_llm_plan_can_call_readonly_tools_before_final_json(tmp_path: Path) -> None:
    source_path = _write_dead_code_java_file(tmp_path)
    issue = _dead_code_issue(source_path)
    plan = _plan(issue)
    client = _FakeLlmClient(
        [
            ChatResponse(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id="tool-1",
                        function=_Function(
                            name="read_file",
                            arguments='{"file_path":"src/main/java/demo/OrderService.java","start_line":1,"end_line":12}',
                        ),
                    )
                ],
            ),
            '{"goal":"LLM used tool context before planning",'
            '"expected_changes":["delete unusedPrivate after reading exact source"],'
            '"out_of_scope":["do not change createOrder"],'
            '"risk_reasons":["tool read confirmed target method boundaries"],'
            '"verification_commands":["mvn test"]}',
        ]
    )
    assistant = RefactorLlmAssistant(client)

    planned = assistant.generate_plan(tmp_path, plan, issue)

    assert planned.goal == "LLM used tool context before planning"
    assert planned.planning_source == "llm-primary"
    assert client.tool_schema_seen is True
    assert any(message.role == "tool" and "unusedPrivate" in message.content for message in client.seen_messages)


def test_issue_context_prefetches_paths_without_related_file_contents(tmp_path: Path) -> None:
    source_path = _write_dead_code_java_file(tmp_path)

    context = RefactorAgentToolbox(tmp_path, ast_analyses=[]).issue_context(_dead_code_issue(source_path))

    assert "unusedPrivate" in context["source_excerpt"]
    assert "related_tests" in context
    assert "direct_callers" in context
    assert "related_test_excerpts" not in context
    assert "direct_caller_excerpts" not in context


def test_llm_executes_multiple_tool_calls_in_parallel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = _write_dead_code_java_file(tmp_path)
    issue = _dead_code_issue(source_path)
    plan = _plan(issue)
    client = _FakeLlmClient(
        [
            ChatResponse(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id="tool-read",
                        function=_Function(name="read_file", arguments='{"file_path":"first.java"}'),
                    ),
                    ToolCall(
                        id="tool-search",
                        function=_Function(name="search_code", arguments='{"query":"unusedPrivate"}'),
                    ),
                ],
            ),
            '{"goal":"parallel evidence collection",'
            '"expected_changes":["delete unusedPrivate"],'
            '"out_of_scope":["public API changes"],'
            '"risk_reasons":["reflection"],'
            '"verification_commands":["mvn test"]}',
        ]
    )
    barrier = threading.Barrier(2, timeout=2)
    worker_threads: set[int] = set()
    lock = threading.Lock()

    def execute_in_parallel(self, name, arguments):
        del self, arguments
        with lock:
            worker_threads.add(threading.get_ident())
        barrier.wait()
        return f'{{"tool":"{name}"}}'

    monkeypatch.setattr(RefactorAgentToolRuntime, "execute", execute_in_parallel)
    log_messages: list[str] = []
    sink_id = logger.add(lambda message: log_messages.append(str(message)), level="INFO")
    try:
        planned = RefactorLlmAssistant(client).generate_plan(tmp_path, plan, issue)
    finally:
        logger.remove(sink_id)

    assert planned.goal == "parallel evidence collection"
    assert len(worker_threads) == 2
    tool_messages = [message for message in client.seen_messages if message.role == "tool"]
    assert [message.tool_call_id for message in tool_messages] == ["tool-read", "tool-search"]
    log_text = "".join(log_messages)
    assert "Executing 2 independent tools in parallel" in log_text
    assert "Completed 2 tool(s)" in log_text


class _FakeLlmClient:
    def __init__(self, responses: list[str | ChatResponse]) -> None:
        self._responses = responses
        self.tool_schema_seen = False
        self.seen_messages = []

    async def chat(self, *, messages, tools=None) -> ChatResponse:
        self.seen_messages = list(messages)
        self.tool_schema_seen = self.tool_schema_seen or bool(tools)
        response = self._responses.pop(0)
        if isinstance(response, ChatResponse):
            return response
        return ChatResponse(role="assistant", content=response)


class _FailingLlmClient:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def chat(self, *, messages, tools=None) -> ChatResponse:
        del messages, tools
        raise self.error


class _FakeDecisionAssistant:
    def __init__(self, decisions: dict[str, dict]) -> None:
        self.decisions = decisions
        self.bound_analysis_paths: list[str] = []

    def bind_scan_analyses(self, analyses: Sequence[AstFileAnalysis]) -> None:
        self.bound_analysis_paths = [analysis.relative_path for analysis in analyses]

    def triage_issues(self, root: Path, issues: list[RefactorIssue]) -> list[RefactorIssue]:
        del root
        from dataclasses import replace

        updated: list[RefactorIssue] = []
        for issue in issues:
            decision = self.decisions.get(issue.id, {})
            updated.append(
                replace(
                    issue,
                    severity=Severity(decision.get("severity", issue.severity)),
                    risk_level=RiskLevel(decision.get("risk_level", issue.risk_level)),
                    impact=decision.get("impact", issue.impact),
                    recommendation=decision.get("recommendation", issue.recommendation),
                    evidence=[
                        *issue.evidence,
                        Evidence("LLM triage decision", {"reason": decision.get("decision_reason", "")}),
                    ],
                )
            )
        return updated


class _FakePlanAssistant:
    def __init__(self, plan_data: dict) -> None:
        self.plan_data = plan_data

    def generate_plan(self, root: Path, plan: RefactorPlan, issue: RefactorIssue) -> RefactorPlan:
        del root, issue
        from dataclasses import replace

        return replace(
            plan,
            goal=self.plan_data["goal"],
            expected_changes=list(self.plan_data["expected_changes"]),
            out_of_scope=list(self.plan_data["out_of_scope"]),
            risk_reasons=list(self.plan_data["risk_reasons"]),
            verification_commands=list(self.plan_data["verification_commands"]),
            rollback_strategy=self.plan_data["rollback_strategy"],
            planning_source="llm-primary",
        )


class _FailOnceRunner:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, command, cwd):
        import subprocess

        del cwd
        self.calls += 1
        if self.calls == 1:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="compile failed")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")


def _write_dead_code_java_file(root: Path) -> Path:
    _write_minimal_repo(root)
    source_dir = root / "src" / "main" / "java" / "demo"
    source_dir.mkdir(parents=True, exist_ok=True)
    source_path = source_dir / "OrderService.java"
    source_path.write_text(
        """
package demo;

public class OrderService {
    private void unusedPrivate() {
        System.out.println("unused");
    }

    public int createOrder(int input) {
        return input + 1;
    }
}
""".lstrip(),
        encoding="utf-8",
    )
    return source_path


def _write_long_method_java_file(root: Path) -> Path:
    _write_minimal_repo(root)
    source_dir = root / "src" / "main" / "java" / "demo"
    source_dir.mkdir(parents=True, exist_ok=True)
    source_path = source_dir / "MathService.java"
    source_path.write_text(
        """
package demo;

public class MathService {
    public int huge(int input) {
        int total = input;
        total += 1;
        total += 2;
        total += 3;
        total += 4;
        total += 5;
        total += 6;
        return total;
    }
}
""".lstrip(),
        encoding="utf-8",
    )
    return source_path


def _write_scan_long_method_java_file(root: Path) -> Path:
    _write_minimal_repo(root)
    source_dir = root / "src" / "main" / "java" / "demo"
    source_dir.mkdir(parents=True, exist_ok=True)
    source_path = source_dir / "LargeMathService.java"
    body = "\n".join(f"        total += {index};" for index in range(90))
    source_path.write_text(
        f"""
package demo;

public class LargeMathService {{
    public int huge(int input) {{
        int total = input;
{body}
        return total;
    }}
}}
""".lstrip(),
        encoding="utf-8",
    )
    return source_path


def _write_java_lines(root: Path, relative_path: str, *, line_count: int) -> Path:
    source_path = root / relative_path
    source_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["package demo;", "", "public class Sample {"]
    lines.extend(f"    // line {index}" for index in range(4, line_count))
    lines.append("}")
    source_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return source_path


def _write_minimal_repo(root: Path) -> None:
    (root / ".git").mkdir(exist_ok=True)
    (root / "pom.xml").write_text(
        """
<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>demo</groupId>
  <artifactId>sample</artifactId>
  <version>1.0.0</version>
  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-pmd-plugin</artifactId>
        <version>3.28.0</version>
      </plugin>
    </plugins>
  </build>
</project>
""".strip(),
        encoding="utf-8",
    )


def _dead_code_issue(source_path: Path) -> RefactorIssue:
    lines = source_path.read_text(encoding="utf-8").splitlines()
    start_line = next(index for index, line in enumerate(lines, start=1) if "unusedPrivate" in line)
    return RefactorIssue(
        id="RA-0001",
        type=SmellType.DEAD_CODE,
        severity=Severity.LOW,
        file_path="src/main/java/demo/OrderService.java",
        symbol="unusedPrivate",
        start_line=start_line,
        end_line=start_line + 2,
        evidence=[Evidence("private method has no references", {"identifier_occurrences": 1})],
        impact="dead private code adds noise",
        recommendation="remove dead code",
        suggested_refactoring=RefactoringType.REMOVE_DEAD_CODE,
        risk_level=RiskLevel.LOW,
    )


def _simple_issue(
    issue_id: str,
    smell_type: SmellType,
    severity: Severity,
    source_path: Path,
    start_line: int,
    end_line: int,
    evidence: Evidence,
) -> RefactorIssue:
    return RefactorIssue(
        id=issue_id,
        type=smell_type,
        severity=severity,
        file_path=source_path.relative_to(source_path.parents[4]).as_posix(),
        symbol="sample",
        start_line=start_line,
        end_line=end_line,
        evidence=[evidence],
        impact="test impact",
        recommendation="test recommendation",
        suggested_refactoring=RefactoringType.EXTRACT_METHOD
        if smell_type == SmellType.LONG_METHOD
        else RefactoringType.MOVE_METHOD
        if smell_type == SmellType.FEATURE_ENVY
        else RefactoringType.RENAME,
        risk_level=RiskLevel.MEDIUM if severity != Severity.LOW else RiskLevel.LOW,
    )


def _long_method_issue(source_path: Path) -> RefactorIssue:
    lines = source_path.read_text(encoding="utf-8").splitlines()
    start_line = next(index for index, line in enumerate(lines, start=1) if "public int huge" in line)
    end_line = next(index for index, line in enumerate(lines, start=1) if index > start_line and line == "    }")
    return RefactorIssue(
        id="RA-0002",
        type=SmellType.LONG_METHOD,
        severity=Severity.MEDIUM,
        file_path="src/main/java/demo/MathService.java",
        symbol="huge",
        start_line=start_line,
        end_line=end_line,
        evidence=[Evidence("method is long enough for extract-method demo", {"lines": end_line - start_line + 1})],
        impact="long method hides steps",
        recommendation="extract a cohesive accumulator block",
        suggested_refactoring=RefactoringType.EXTRACT_METHOD,
        risk_level=RiskLevel.MEDIUM,
    )


def _plan(
    issue: RefactorIssue,
    *,
    risk_level: RiskLevel = RiskLevel.LOW,
    refactoring_type: RefactoringType | None = None,
) -> RefactorPlan:
    return RefactorPlan(
        task_id=f"{issue.id.lower()}-test",
        issue_id=issue.id,
        goal="small safe refactor",
        refactoring_type=refactoring_type or issue.suggested_refactoring,
        files_to_modify=[issue.file_path],
        expected_changes=["modify only the target code"],
        out_of_scope=["do not change public API"],
        risk_level=risk_level,
        risk_reasons=["test risk"],
        verification_commands=["mvn test"],
        rollback_strategy="restore planned files from snapshot",
        coverage_assessment=CoverageAssessment(
            has_related_test_class=False,
            related_tests=[],
            confidence="low",
            needs_characterization_test=False,
            recommendation="run tests",
        ),
        context=JavaContext(
            issue_id=issue.id,
            target_file=issue.file_path,
            target_symbol=issue.symbol,
            source_excerpt="",
            related_tests=[],
            direct_callers=[],
        ),
        planning_source="test",
    )


def _save_plan(tmp_path: Path, plan: RefactorPlan, issue: RefactorIssue) -> None:
    RefactorAgentStorage(tmp_path).save_plan(plan, issue)


async def _current_loop_id() -> int:
    return id(asyncio.get_running_loop())
