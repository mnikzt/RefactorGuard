"""Data models for the Java refactor-agent MVP."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MavenModule:
    """Detected Maven module metadata."""

    name: str
    path: str
    has_main_java: bool
    has_test_java: bool


@dataclass(frozen=True)
class ProjectProfile:
    """Project profile produced by the phase-one detector."""

    root: Path
    is_git_repo: bool
    is_maven_project: bool
    has_main_java: bool
    has_test_java: bool
    is_git_clean: bool
    has_pmd_cpd_plugin: bool = False
    maven_version: str | None = None
    java_version: str | None = None
    modules: list[MavenModule] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["root"] = str(self.root)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectProfile:
        return cls(
            root=Path(data["root"]),
            is_git_repo=bool(data["is_git_repo"]),
            is_maven_project=bool(data["is_maven_project"]),
            has_main_java=bool(data["has_main_java"]),
            has_test_java=bool(data["has_test_java"]),
            is_git_clean=bool(data["is_git_clean"]),
            has_pmd_cpd_plugin=bool(data.get("has_pmd_cpd_plugin", False)),
            maven_version=data.get("maven_version"),
            java_version=data.get("java_version"),
            modules=[MavenModule(**module) for module in data.get("modules", [])],
            warnings=list(data.get("warnings", [])),
        )


class SmellType(StrEnum):
    LONG_METHOD = "long_method"
    LARGE_CLASS = "large_class"
    COMPLEX_CONDITION = "complex_condition"
    DUPLICATE_CODE = "duplicate_code"
    DEAD_CODE = "dead_code"
    FEATURE_ENVY = "feature_envy"
    UNCLEAR_NAMING = "unclear_naming"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RefactoringType(StrEnum):
    EXTRACT_METHOD = "Extract Method"
    EXTRACT_CLASS = "Extract Class"
    INTRODUCE_EXPLAINING_VARIABLE = "Introduce Explaining Variable"
    MOVE_METHOD = "Move Method"
    REPLACE_DUPLICATE_LOGIC = "Replace Duplicate Logic With Shared Method"
    REMOVE_DEAD_CODE = "Remove Dead Code"
    RENAME = "Rename Variable / Method / Class"


class DecisionStatus(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class Evidence:
    message: str
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RefactorIssue:
    id: str
    type: SmellType
    severity: Severity
    file_path: str
    symbol: str | None
    start_line: int
    end_line: int
    evidence: list[Evidence]
    impact: str
    recommendation: str
    suggested_refactoring: RefactoringType
    risk_level: RiskLevel

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RefactorIssue:
        return cls(
            id=data["id"],
            type=SmellType(data["type"]),
            severity=Severity(data["severity"]),
            file_path=data["file_path"],
            symbol=data.get("symbol"),
            start_line=int(data["start_line"]),
            end_line=int(data["end_line"]),
            evidence=[
                Evidence(message=evidence["message"], metrics=dict(evidence.get("metrics", {})))
                for evidence in data.get("evidence", [])
            ],
            impact=data["impact"],
            recommendation=data["recommendation"],
            suggested_refactoring=RefactoringType(data["suggested_refactoring"]),
            risk_level=RiskLevel(data["risk_level"]),
        )


@dataclass(frozen=True)
class CandidateDecision:
    candidate_id: str
    status: DecisionStatus
    confidence: float
    reason: str
    source_evidence: list[dict[str, Any]] = field(default_factory=list)
    root_cause: str = ""
    proposed_solution: str = ""
    verification_strategy: list[str] = field(default_factory=list)
    rank_score: float | None = None
    rank_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CandidateDecision:
        return cls(
            candidate_id=str(data["candidate_id"]),
            status=DecisionStatus(data["status"]),
            confidence=float(data.get("confidence", 0.0)),
            reason=str(data.get("reason", "")),
            source_evidence=[dict(item) for item in data.get("source_evidence", [])],
            root_cause=str(data.get("root_cause", "")),
            proposed_solution=str(data.get("proposed_solution", "")),
            verification_strategy=[str(item) for item in data.get("verification_strategy", [])],
            rank_score=float(data["rank_score"]) if data.get("rank_score") is not None else None,
            rank_reasons=[str(item) for item in data.get("rank_reasons", [])],
        )


@dataclass(frozen=True)
class TriageResult:
    issues: list[RefactorIssue]
    decisions: list[CandidateDecision]


@dataclass(frozen=True)
class ScanResult:
    profile: ProjectProfile
    issues: list[RefactorIssue]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile.to_dict(),
            "issues": [issue.to_dict() for issue in self.issues],
            "warnings": self.warnings,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScanResult:
        return cls(
            profile=ProjectProfile.from_dict(data["profile"]),
            issues=[RefactorIssue.from_dict(issue) for issue in data.get("issues", [])],
            warnings=list(data.get("warnings", [])),
        )


@dataclass(frozen=True)
class JavaContext:
    issue_id: str
    target_file: str
    target_symbol: str | None
    source_excerpt: str
    related_tests: list[str]
    direct_callers: list[str]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JavaContext:
        return cls(
            issue_id=data["issue_id"],
            target_file=data["target_file"],
            target_symbol=data.get("target_symbol"),
            source_excerpt=data.get("source_excerpt", ""),
            related_tests=list(data.get("related_tests", [])),
            direct_callers=list(data.get("direct_callers", [])),
            warnings=list(data.get("warnings", [])),
        )


@dataclass(frozen=True)
class CoverageAssessment:
    has_related_test_class: bool
    related_tests: list[str]
    confidence: str
    needs_characterization_test: bool
    recommendation: str
    jacoco_report_found: bool = False
    changed_lines_total: int = 0
    changed_lines_covered: int = 0
    coverage_ratio: float = 0.0
    generated_tests: list[str] = field(default_factory=list)
    target_file_lines_total: int = 0
    target_file_lines_covered: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CoverageAssessment:
        return cls(
            has_related_test_class=bool(data["has_related_test_class"]),
            related_tests=list(data.get("related_tests", [])),
            confidence=data["confidence"],
            needs_characterization_test=bool(data["needs_characterization_test"]),
            recommendation=data["recommendation"],
            jacoco_report_found=bool(data.get("jacoco_report_found", False)),
            changed_lines_total=int(data.get("changed_lines_total", 0)),
            changed_lines_covered=int(data.get("changed_lines_covered", 0)),
            coverage_ratio=float(data.get("coverage_ratio", 0.0)),
            generated_tests=list(data.get("generated_tests", [])),
            target_file_lines_total=int(data.get("target_file_lines_total", 0)),
            target_file_lines_covered=int(data.get("target_file_lines_covered", 0)),
        )


@dataclass(frozen=True)
class RefactorPlan:
    task_id: str
    issue_id: str
    goal: str
    refactoring_type: RefactoringType
    files_to_modify: list[str]
    expected_changes: list[str]
    out_of_scope: list[str]
    risk_level: RiskLevel
    risk_reasons: list[str]
    verification_commands: list[str]
    rollback_strategy: str
    coverage_assessment: CoverageAssessment
    context: JavaContext
    planning_source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RefactorPlan:
        return cls(
            task_id=data["task_id"],
            issue_id=data["issue_id"],
            goal=data["goal"],
            refactoring_type=RefactoringType(data["refactoring_type"]),
            files_to_modify=list(data.get("files_to_modify", [])),
            expected_changes=list(data.get("expected_changes", [])),
            out_of_scope=list(data.get("out_of_scope", [])),
            risk_level=RiskLevel(data["risk_level"]),
            risk_reasons=list(data.get("risk_reasons", [])),
            verification_commands=list(data.get("verification_commands", [])),
            rollback_strategy=data["rollback_strategy"],
            coverage_assessment=CoverageAssessment.from_dict(data["coverage_assessment"]),
            context=JavaContext.from_dict(data["context"]),
            planning_source=data["planning_source"],
        )


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CommandResult:
        return cls(
            command=data["command"],
            exit_code=int(data["exit_code"]),
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
        )


@dataclass(frozen=True)
class VerificationResult:
    status: str
    commands: list[CommandResult]
    coverage: CoverageAssessment
    static_findings: list[str] = field(default_factory=list)
    diff_summary: str = ""
    message: str = ""
    approved: bool | None = None
    decision_source: str = "deterministic"
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    attempt: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "commands": [command.to_dict() for command in self.commands],
            "coverage": self.coverage.to_dict(),
            "static_findings": self.static_findings,
            "diff_summary": self.diff_summary,
            "message": self.message,
            "approved": self.approved if self.approved is not None else self.status in {"passed", "warning"},
            "decision_source": self.decision_source,
            "issues": self.issues,
            "suggestions": self.suggestions,
            "attempt": self.attempt,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VerificationResult:
        return cls(
            status=data["status"],
            commands=[CommandResult.from_dict(command) for command in data.get("commands", [])],
            coverage=CoverageAssessment.from_dict(data["coverage"]),
            static_findings=list(data.get("static_findings", [])),
            diff_summary=data.get("diff_summary", ""),
            message=data.get("message", ""),
            approved=bool(data["approved"]) if "approved" in data else data.get("status") in {"passed", "warning"},
            decision_source=str(data.get("decision_source", "deterministic")),
            issues=list(data.get("issues", [])),
            suggestions=list(data.get("suggestions", [])),
            attempt=max(1, int(data.get("attempt", 1))),
        )


@dataclass(frozen=True)
class PreModificationResult:
    status: str
    commands: list[CommandResult]
    coverage: CoverageAssessment
    requires_test_generation: bool
    message: str
    infrastructure_error: str = ""
    generated_tests: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "commands": [command.to_dict() for command in self.commands],
            "coverage": self.coverage.to_dict(),
            "requires_test_generation": self.requires_test_generation,
            "message": self.message,
            "infrastructure_error": self.infrastructure_error,
            "generated_tests": self.generated_tests,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PreModificationResult:
        return cls(
            status=str(data["status"]),
            commands=[CommandResult.from_dict(command) for command in data.get("commands", [])],
            coverage=CoverageAssessment.from_dict(data["coverage"]),
            requires_test_generation=bool(data.get("requires_test_generation", False)),
            message=str(data.get("message", "")),
            infrastructure_error=str(data.get("infrastructure_error", "")),
            generated_tests=list(data.get("generated_tests", [])),
        )


@dataclass(frozen=True)
class CharacterizationTestPlan:
    issue_id: str
    target_class: str
    target_methods: list[str]
    test_framework: str
    destination_file: str
    assertion_intent: list[str]
    content: str
    user_confirmed: bool = False
    pre_refactor_test_result: CommandResult | None = None
    usable_as_refactor_guard: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["pre_refactor_test_result"] = (
            self.pre_refactor_test_result.to_dict() if self.pre_refactor_test_result else None
        )
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CharacterizationTestPlan:
        precheck = data.get("pre_refactor_test_result")
        return cls(
            issue_id=data["issue_id"],
            target_class=data["target_class"],
            target_methods=list(data.get("target_methods", [])),
            test_framework=data["test_framework"],
            destination_file=data["destination_file"],
            assertion_intent=list(data.get("assertion_intent", [])),
            content=data.get("content", ""),
            user_confirmed=bool(data.get("user_confirmed", False)),
            pre_refactor_test_result=CommandResult.from_dict(precheck) if precheck else None,
            usable_as_refactor_guard=bool(data.get("usable_as_refactor_guard", False)),
        )


@dataclass(frozen=True)
class RollbackResult:
    status: str
    task_id: str
    restored_files: list[str]
    conflicts: list[str] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RollbackResult:
        return cls(
            status=data["status"],
            task_id=data["task_id"],
            restored_files=list(data.get("restored_files", [])),
            conflicts=list(data.get("conflicts", [])),
            message=data.get("message", ""),
        )
