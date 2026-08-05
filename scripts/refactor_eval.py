"""Benchmark harness for RefactorGuard interview metrics.

The harness focuses on two claims:
1. Static candidates + LLM triage vs direct LLM repository search.
2. Single-agent vs multi-agent refactoring success, recorded from CLI artifacts.

It intentionally writes all experiment output under experiments/refactor_eval so
results can be inspected, summarized, and rerun without touching product code.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_ROOT = ROOT / "experiments" / "refactor_eval"
PROJECTS_ROOT = EXPERIMENT_ROOT / "projects"
RESULTS_ROOT = EXPERIMENT_ROOT / "results"
PROJECTS_FILE = EXPERIMENT_ROOT / "projects.json"
TASKS_FILE = EXPERIMENT_ROOT / "tasks.json"

DEFAULT_PROJECTS = [
    {
        "name": "commons-lang",
        "repo": "https://github.com/apache/commons-lang.git",
        "ref": "master",
        "build": "mvn -q -DskipTests compile",
        "test": "mvn -q test",
    },
    {
        "name": "commons-io",
        "repo": "https://github.com/apache/commons-io.git",
        "ref": "master",
        "build": "mvn -q -DskipTests compile",
        "test": "mvn -q test",
    },
    {
        "name": "gson",
        "repo": "https://github.com/google/gson.git",
        "ref": "main",
        "build": "mvn -q -DskipTests compile",
        "test": "mvn -q test",
    },
]


@dataclass
class LlmUsage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class BenchmarkRow:
    task_id: str
    project: str
    smell_type: str
    target_hint: str
    mode: str
    elapsed_sec: float
    static_scan_sec: float = 0.0
    llm_sec: float = 0.0
    project_task_count: int = 1
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    candidate_count: int = 0
    triage_candidate_count: int = 0
    accepted_count: int = 0
    found_count: int = 0
    evidence_valid_count: int = 0
    found: bool | None = None
    exit_code: int = 0
    error: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["details"] = json.dumps(self.details, ensure_ascii=False, sort_keys=True)
        return data


@dataclass
class StaticScanCache:
    candidates: list[Any] = field(default_factory=list)
    ast_analyses: tuple[Any, ...] = ()
    elapsed_sec: float = 0.0
    error: str = ""


class TracingLlmClient:
    """Small proxy that accumulates token usage from existing LLM clients."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.usage = LlmUsage()

    async def chat(self, messages: list[Any], tools: list[Any] | None = None, listener: Any | None = None) -> Any:
        response = await self.inner.chat(messages=messages, tools=tools, listener=listener)
        self.usage.calls += 1
        input_tokens = max(0, int(getattr(response, "input_tokens", 0) or 0))
        output_tokens = max(0, int(getattr(response, "output_tokens", 0) or 0))
        if input_tokens == 0:
            input_tokens = _estimate_tokens(
                {
                    "messages": [_message_payload(message) for message in messages],
                    "tools": tools,
                }
            )
        if output_tokens == 0:
            output_tokens = _estimate_tokens(getattr(response, "content", "") or "")
        self.usage.input_tokens += input_tokens
        self.usage.output_tokens += output_tokens
        self.usage.cached_input_tokens += max(0, int(getattr(response, "cached_input_tokens", 0) or 0))
        return response

    @property
    def model_name(self) -> str:
        return self.inner.model_name

    @property
    def provider_name(self) -> str:
        return self.inner.provider_name

    @property
    def max_context_window(self) -> int:
        return self.inner.max_context_window

    @property
    def supports_prompt_caching(self) -> bool:
        return self.inner.supports_prompt_caching

    @property
    def supports_tools(self) -> bool:
        return self.inner.supports_tools

    @property
    def supports_image_input(self) -> bool:
        return self.inner.supports_image_input

    @property
    def prompt_cache_mode(self) -> str:
        return self.inner.prompt_cache_mode


def main() -> int:
    parser = argparse.ArgumentParser(description="Run RefactorGuard evaluation experiments")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="create experiment config files")
    subparsers.add_parser("prepare", help="clone configured GitHub projects")
    subparsers.add_parser("smoke", help="run compile/test smoke checks for configured projects")

    scan_parser = subparsers.add_parser("scan-efficiency", help="run baseline vs static-candidate scan benchmark")
    scan_parser.add_argument("--limit", type=int, default=6, help="max tasks to run")
    scan_parser.add_argument("--task-id", help="run one configured task id, e.g. T002")
    scan_parser.add_argument("--project", help="run one project-level task, e.g. commons-lang")
    scan_parser.add_argument(
        "--all-smells",
        action="store_true",
        help="detect mixed smells for the selected project instead of one smell type",
    )
    scan_parser.add_argument(
        "--dry-run-candidates",
        action="store_true",
        help="write the static candidate selection plan without calling an LLM",
    )
    scan_parser.add_argument("--triage-limit", type=int, default=8, help="max static candidates sent to LLM")
    scan_parser.add_argument("--baseline-limit", type=int, default=20, help="max candidates baseline should find")
    scan_parser.add_argument("--baseline-iterations", type=int, default=12, help="max ReAct iterations for baseline")
    scan_parser.add_argument("--mode", choices=("baseline", "ours", "both"), default="both")
    scan_parser.add_argument("--model", help="override the configured freellmapi model for this experiment")

    triage_parser = subparsers.add_parser("triage-baseline", help="triage baseline candidates from a CSV result")
    triage_parser.add_argument(
        "--csv",
        required=True,
        type=Path,
        help="scan_efficiency CSV containing baseline details",
    )
    triage_parser.add_argument("--task-id", help="baseline task id to triage; defaults to the first baseline row")
    triage_parser.add_argument("--limit", type=int, default=5, help="max baseline candidates to triage")
    triage_parser.add_argument("--candidate-index", type=int, help="1-based candidate index to triage")
    triage_parser.add_argument("--model", help="override the configured freellmapi model for this triage run")

    plan_triage_parser = subparsers.add_parser(
        "triage-candidate-plan",
        help="triage static candidates from a candidate_plan JSON file",
    )
    plan_triage_parser.add_argument("--plan", required=True, type=Path, help="candidate_plan JSON from dry-run")
    plan_triage_parser.add_argument("--limit", type=int, default=5, help="max selected candidates to triage")
    plan_triage_parser.add_argument("--candidate-index", type=int, help="1-based selected candidate index to triage")
    plan_triage_parser.add_argument("--model", help="override the configured freellmapi model for this triage run")

    subparsers.add_parser("summarize", help="summarize CSV results into Markdown")

    args = parser.parse_args()
    if args.command == "init":
        return init_experiment()
    if args.command == "prepare":
        return prepare_projects()
    if args.command == "smoke":
        return smoke_projects()
    if args.command == "scan-efficiency":
        return scan_efficiency(
            limit=args.limit,
            task_id=args.task_id,
            project=args.project,
            all_smells=args.all_smells,
            dry_run_candidates=args.dry_run_candidates,
            triage_limit=args.triage_limit,
            baseline_limit=args.baseline_limit,
            baseline_iterations=args.baseline_iterations,
            mode=args.mode,
            model=args.model,
        )
    if args.command == "triage-baseline":
        return triage_baseline(
            csv_path=args.csv,
            task_id=args.task_id,
            limit=args.limit,
            candidate_index=args.candidate_index,
            model=args.model,
        )
    if args.command == "triage-candidate-plan":
        return triage_candidate_plan(
            plan_path=args.plan,
            limit=args.limit,
            candidate_index=args.candidate_index,
            model=args.model,
        )
    if args.command == "summarize":
        return summarize_results()
    return 2


def init_experiment() -> int:
    EXPERIMENT_ROOT.mkdir(parents=True, exist_ok=True)
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    if not PROJECTS_FILE.exists():
        PROJECTS_FILE.write_text(json.dumps(DEFAULT_PROJECTS, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not TASKS_FILE.exists():
        TASKS_FILE.write_text(json.dumps(_default_tasks(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    readme = EXPERIMENT_ROOT / "README.md"
    if not readme.exists():
        readme.write_text(_readme_text(), encoding="utf-8")
    print(f"initialized {EXPERIMENT_ROOT}")
    return 0


def prepare_projects() -> int:
    projects = _load_json(PROJECTS_FILE)
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    for project in projects:
        name = project["name"]
        destination = PROJECTS_ROOT / name
        if destination.exists():
            print(f"{name}: already exists")
            continue
        _run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                project.get("ref", "master"),
                project["repo"],
                str(destination),
            ],
            ROOT,
        )
        print(f"{name}: cloned")
    return 0


def smoke_projects() -> int:
    projects = _load_json(PROJECTS_FILE)
    rows: list[dict[str, Any]] = []
    for project in projects:
        root = PROJECTS_ROOT / project["name"]
        started = time.monotonic()
        if not root.exists():
            rows.append({"project": project["name"], "command": "missing", "exit_code": 127, "elapsed_sec": 0.0})
            continue
        command = project.get("build") or "mvn -q -DskipTests compile"
        result = _run_shell(command, root)
        rows.append(
            {
                "project": project["name"],
                "command": command,
                "exit_code": result.returncode,
                "elapsed_sec": round(time.monotonic() - started, 3),
                "stdout_tail": (result.stdout or "")[-1200:],
                "stderr_tail": (result.stderr or "")[-1200:],
            }
        )
        print(f"{project['name']}: {command} -> {result.returncode}")
    _write_json(RESULTS_ROOT / "smoke.json", rows)
    return 0 if all(row["exit_code"] == 0 for row in rows) else 1


def scan_efficiency(
    *,
    limit: int,
    task_id: str | None,
    project: str | None,
    all_smells: bool,
    dry_run_candidates: bool,
    triage_limit: int,
    baseline_limit: int,
    baseline_iterations: int,
    mode: str,
    model: str | None,
) -> int:
    _ensure_import_path()
    from suncli_py.config.config import PaiCliConfig
    from suncli_py.llm.factory import create_client_from_config
    from suncli_py.llm.providers.free_llm_api import FreeLlmApiClient
    from suncli_py.refactor_agent.analysis.scanner import JavaSmellScanner
    from suncli_py.refactor_agent.assistant.llm_assistant import RefactorLlmAssistant

    tasks = _load_json(TASKS_FILE)
    if all_smells:
        if not project:
            raise SystemExit("--all-smells requires --project")
        tasks = [_project_all_smells_task(project)]
    elif project:
        tasks = [task for task in tasks if str(task["project"]) == project]
        if not tasks:
            raise SystemExit(f"project not found in tasks.json: {project}")
        tasks = tasks[:limit]
    elif task_id:
        tasks = [task for task in tasks if str(task["id"]) == task_id]
        if not tasks:
            raise SystemExit(f"task id not found: {task_id}")
    else:
        tasks = tasks[:limit]
    task_counts_by_project = Counter(str(task["project"]) for task in tasks)
    client: Any | None = None
    if not dry_run_candidates:
        config = PaiCliConfig.load()
        if model:
            api_key = config.get_api_key("freellmapi")
            base_url = config.get_base_url("freellmapi")
            if not api_key or not base_url:
                raise SystemExit("freellmapi key/base_url is required when using --model")
            client = FreeLlmApiClient(api_key=api_key, model=model, base_url=base_url)
        else:
            client = create_client_from_config(config)
        if client is None:
            raise SystemExit("No LLM provider configured. Add a provider key to .env or ~/.paicli/config.json.")

    rows: list[BenchmarkRow] = []
    output = RESULTS_ROOT / f"scan_efficiency_{_timestamp()}.csv"

    def emit(row: BenchmarkRow) -> None:
        rows.append(row)
        _write_rows(output, rows)

    static_scan_cache: dict[str, StaticScanCache] = {}
    candidate_plans: list[dict[str, Any]] = []
    for task in tasks:
        project_root = PROJECTS_ROOT / task["project"]
        if not project_root.exists():
            if dry_run_candidates:
                candidate_plans.append({"task": task, "error": f"project not found: {project_root}"})
            else:
                emit(_error_row(task, mode, f"project not found: {project_root}"))
            continue
        if mode in {"baseline", "both"} and not dry_run_candidates:
            assert client is not None
            emit(
                _run_baseline_task(
                    task,
                    project_root,
                    client,
                    baseline_limit=baseline_limit,
                    baseline_iterations=baseline_iterations,
                )
            )
        if mode in {"ours", "both"} or dry_run_candidates:
            assert client is not None or dry_run_candidates
            traced = TracingLlmClient(client) if not dry_run_candidates else None
            assistant = RefactorLlmAssistant(traced) if traced is not None else None
            scan_cache = _static_scan_cache(
                static_scan_cache,
                scanner_type=JavaSmellScanner,
                project=task["project"],
                project_root=project_root,
            )
            if scan_cache.error:
                if dry_run_candidates:
                    candidate_plans.append({"task": task, "error": scan_cache.error})
                else:
                    assert traced is not None
                    emit(
                        _exception_row(
                            task,
                            "ours_static_candidates",
                            time.monotonic(),
                            traced.usage,
                            RuntimeError(scan_cache.error),
                            candidate_count=len(scan_cache.candidates),
                        )
                    )
                continue
            eligible_candidates = _candidate_subset_for_task(scan_cache.candidates, task)
            triage_candidates = eligible_candidates[:triage_limit]
            if dry_run_candidates:
                candidate_plans.append(
                    _candidate_plan(
                        task,
                        scan_cache,
                        eligible_candidates=eligible_candidates,
                        triage_candidates=triage_candidates,
                    )
                )
                continue
            started = time.monotonic()
            try:
                assert assistant is not None
                assert traced is not None
                assistant.bind_scan_analyses(scan_cache.ast_analyses)
                triage = assistant.triage_issues(project_root, triage_candidates, limit=triage_limit)
                llm_sec = round(time.monotonic() - started, 3)
                project_task_count = max(1, task_counts_by_project[str(task["project"])])
                fair_elapsed = round(llm_sec + scan_cache.elapsed_sec / project_task_count, 3)
                matched = _matches_expected(triage.issues, task)
                evidence_valid_count = sum(_issue_has_llm_source_evidence(issue) for issue in triage.issues)
                emit(
                    BenchmarkRow(
                        task_id=task["id"],
                        project=task["project"],
                        smell_type=task["smell_type"],
                        target_hint=task.get("target_hint", ""),
                        mode="ours_static_candidates",
                        elapsed_sec=fair_elapsed,
                        static_scan_sec=scan_cache.elapsed_sec,
                        llm_sec=llm_sec,
                        project_task_count=project_task_count,
                        llm_calls=traced.usage.calls,
                        input_tokens=traced.usage.input_tokens,
                        output_tokens=traced.usage.output_tokens,
                        cached_input_tokens=traced.usage.cached_input_tokens,
                        candidate_count=len(scan_cache.candidates),
                        triage_candidate_count=len(triage_candidates),
                        accepted_count=len(triage.issues),
                        found_count=len(triage.issues),
                        evidence_valid_count=evidence_valid_count,
                        found=matched,
                        details={
                            "accepted": [issue.to_dict() for issue in triage.issues[:10]],
                            "decisions": [decision.to_dict() for decision in triage.decisions[:20]],
                            "eligible_candidate_count": len(eligible_candidates),
                        },
                    )
                )
                print(f"{task['id']} ours -> found={matched} candidates={len(scan_cache.candidates)}")
            except Exception as err:
                assert traced is not None
                emit(
                    _exception_row(
                        task,
                        "ours_static_candidates",
                        started,
                        traced.usage,
                        err,
                        candidate_count=len(scan_cache.candidates),
                    )
                )

    if dry_run_candidates:
        output = RESULTS_ROOT / f"candidate_plan_{_timestamp()}.json"
        _write_json(output, candidate_plans)
        print(f"wrote {output}")
        return 0

    print(f"wrote {output}")
    return 0 if all(row.exit_code == 0 for row in rows) else 1


def _production_scanner(scanner_type: type[Any], project_root: Path) -> Any:
    class ProductionJavaSmellScanner(scanner_type):
        def _collect_java_files(self) -> list[Path]:
            source_root = self.root / "src" / "main" / "java"
            if not source_root.is_dir():
                return super()._collect_java_files()
            return sorted(source_root.rglob("*.java"))

    return ProductionJavaSmellScanner(project_root)


def _static_scan_cache(
    cache: dict[str, StaticScanCache],
    *,
    scanner_type: type[Any],
    project: str,
    project_root: Path,
) -> StaticScanCache:
    cached = cache.get(project)
    if cached is not None:
        return cached

    started = time.monotonic()
    try:
        scanner = _production_scanner(scanner_type, project_root)
        candidates = scanner.scan()
        cached = StaticScanCache(
            candidates=candidates,
            ast_analyses=tuple(scanner.ast_analyses),
            elapsed_sec=round(time.monotonic() - started, 3),
        )
    except Exception as err:
        cached = StaticScanCache(
            elapsed_sec=round(time.monotonic() - started, 3),
            error=f"{type(err).__name__}: {err}",
        )
    cache[project] = cached
    return cached


def _run_baseline_task(
    task: dict[str, Any],
    project_root: Path,
    client: Any,
    *,
    baseline_limit: int,
    baseline_iterations: int,
) -> BenchmarkRow:
    _ensure_import_path()
    from suncli_py.memory.manager import MemoryManager
    from suncli_py.refactor_agent.assistant.prompts import triage_system_prompt
    from suncli_py.refactor_agent.assistant.react import AgentBudget, ReactAgent
    from suncli_py.refactor_agent.assistant.toolbox import RefactorAgentToolbox, RefactorAgentToolRuntime

    traced = TracingLlmClient(client)
    started = time.monotonic()
    try:
        toolbox = RefactorAgentToolbox(project_root)
        tools = RefactorAgentToolRuntime(toolbox)
        smell_type = str(task["smell_type"])
        if smell_type == "all":
            smell_instruction = (
                "Requested smell_type: all supported Java code smells. Search for mixed high-confidence issues across "
                "long_method, large_class, complex_condition, duplicate_code, dead_code, feature_envy, and "
                "unclear_naming. Each candidate must include its smell_type.\n"
            )
        else:
            smell_instruction = f"Requested smell_type: {smell_type}\n"
        prompt = (
            "You are the baseline direct-repository-search detector. Search the repository with tools and find up "
            f"to {baseline_limit} trustworthy candidates for the requested Java code smell task. "
            "Return candidates in the same bounded issue shape used by static scanner output. Return JSON only: "
            '{"candidates":[{"id":"BASE-0001","type":"long_method","severity":"medium|high",'
            '"risk_level":"low|medium|high","file_path":"...","symbol":"...",'
            '"start_line":1,"end_line":80,"reason":"...",'
            '"evidence":[{"file_path":"...","start_line":1,"end_line":2,"reason":"..."}]}]}.\n'
            f"{smell_instruction}"
            f"Target hint: {task.get('target_hint', '')}\n"
            "Every candidate must include concrete file and line evidence. Use the candidate start_line/end_line as "
            "the smallest actionable source range, not an entire file or whole utility class. "
            "If a smell is class-wide, "
            "set start_line/end_line to the most representative 80-200 line region and put other evidence ranges in "
            "evidence. Avoid candidate ranges over 250 lines unless the source method itself is that large. Return "
            "fewer candidates if evidence is insufficient. Do not use static scanner candidates; this is a direct "
            "full-repository LLM search baseline."
        )
        result = ReactAgent(
            name="baseline-direct-search",
            client=traced,
            root=project_root,
            system_prompt=triage_system_prompt(),
            tools=tools,
            memory=MemoryManager(traced, project_root),
            budget_factory=lambda: AgentBudget(hard_max_iterations=baseline_iterations),
        ).run_json(prompt)
        candidates = _baseline_candidates_from_result(result.data or {})
        evidence_valid_count = sum(_candidate_has_valid_evidence(candidate) for candidate in candidates)
        elapsed_sec = round(time.monotonic() - started, 3)
        return BenchmarkRow(
            task_id=task["id"],
            project=task["project"],
            smell_type=task["smell_type"],
            target_hint=task.get("target_hint", ""),
            mode="baseline_direct_search",
            elapsed_sec=elapsed_sec,
            llm_sec=elapsed_sec,
            llm_calls=traced.usage.calls,
            input_tokens=traced.usage.input_tokens,
            output_tokens=traced.usage.output_tokens,
            cached_input_tokens=traced.usage.cached_input_tokens,
            found_count=len(candidates),
            evidence_valid_count=evidence_valid_count,
            found=bool(candidates),
            exit_code=0 if result.succeeded else 1,
            error=result.error,
            details={
                "result": result.data,
                "candidates": candidates,
                "traces": [trace.to_dict() for trace in result.traces],
            },
        )
    except Exception as err:
        return _exception_row(task, "baseline_direct_search", started, traced.usage, err)


def triage_baseline(
    *,
    csv_path: Path,
    task_id: str | None,
    limit: int,
    candidate_index: int | None,
    model: str | None,
) -> int:
    _ensure_import_path()

    csv.field_size_limit(sys.maxsize)
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8", newline="")))
    baseline = _select_baseline_row(rows, task_id)
    project_root = PROJECTS_ROOT / baseline["project"]
    if not project_root.exists():
        raise SystemExit(f"project not found: {project_root}")
    details = json.loads(baseline["details"])
    candidates = _baseline_candidates_from_result(details.get("result") or {})
    if not candidates:
        candidates = [dict(item) for item in details.get("candidates", []) if isinstance(item, dict)]
    issues = [_baseline_candidate_to_issue(candidate, index) for index, candidate in enumerate(candidates[:limit], 1)]
    selected = _select_issue_indices(issues, candidate_index)
    client = _create_eval_client(model)
    output = RESULTS_ROOT / f"baseline_triage_{_timestamp()}.jsonl"
    return _triage_issues_jsonl(
        project_root=project_root,
        issues=selected,
        client=client,
        output=output,
        source={
            "source_kind": "baseline",
            "source_csv": str(csv_path),
            "task_id": baseline["task_id"],
            "project": baseline["project"],
        },
        raw_candidates=candidates[:limit],
    )


def triage_candidate_plan(*, plan_path: Path, limit: int, candidate_index: int | None, model: str | None) -> int:
    _ensure_import_path()
    plans = json.loads(plan_path.read_text(encoding="utf-8"))
    if not plans:
        raise SystemExit(f"candidate plan is empty: {plan_path}")
    plan = plans[0]
    task = dict(plan["task"])
    project_root = PROJECTS_ROOT / task["project"]
    if not project_root.exists():
        raise SystemExit(f"project not found: {project_root}")
    selected_candidates = [dict(item) for item in plan.get("selected_candidates", [])[:limit]]
    issues = [_candidate_summary_to_issue(candidate, index) for index, candidate in enumerate(selected_candidates, 1)]
    selected = _select_issue_indices(issues, candidate_index)
    client = _create_eval_client(model)
    output = RESULTS_ROOT / f"candidate_plan_triage_{_timestamp()}.jsonl"
    return _triage_issues_jsonl(
        project_root=project_root,
        issues=selected,
        client=client,
        output=output,
        source={
            "source_kind": "candidate_plan",
            "source_plan": str(plan_path),
            "task_id": task["id"],
            "project": task["project"],
        },
        raw_candidates=selected_candidates,
    )


def _select_baseline_row(rows: list[dict[str, str]], task_id: str | None) -> dict[str, str]:
    for row in rows:
        if row.get("mode") != "baseline_direct_search":
            continue
        if task_id and row.get("task_id") != task_id:
            continue
        return row
    raise SystemExit(f"baseline row not found in CSV: task_id={task_id or '<first>'}")


def _create_eval_client(model: str | None) -> Any:
    from suncli_py.config.config import PaiCliConfig
    from suncli_py.llm.factory import create_client_from_config
    from suncli_py.llm.providers.free_llm_api import FreeLlmApiClient

    config = PaiCliConfig.load()
    if model:
        api_key = config.get_api_key("freellmapi")
        base_url = config.get_base_url("freellmapi")
        if not api_key or not base_url:
            raise SystemExit("freellmapi key/base_url is required when using --model")
        return FreeLlmApiClient(api_key=api_key, model=model, base_url=base_url)
    client = create_client_from_config(config)
    if client is None:
        raise SystemExit("No LLM provider configured. Add a provider key to .env or ~/.paicli/config.json.")
    return client


def _triage_issues_jsonl(
    *,
    project_root: Path,
    issues: list[Any],
    client: Any,
    output: Path,
    source: dict[str, Any],
    raw_candidates: list[dict[str, Any]],
) -> int:
    from suncli_py.refactor_agent.assistant.llm_assistant import RefactorLlmAssistant

    output.parent.mkdir(parents=True, exist_ok=True)
    completed = 0
    accepted = 0
    for issue in issues:
        traced = TracingLlmClient(client)
        assistant = RefactorLlmAssistant(traced)
        started = time.monotonic()
        record: dict[str, Any]
        try:
            triage = assistant.triage_issues(project_root, [issue], limit=1)
            elapsed_sec = round(time.monotonic() - started, 3)
            accepted_issues = [accepted_issue.to_dict() for accepted_issue in triage.issues]
            decisions = [decision.to_dict() for decision in triage.decisions]
            accepted += len(accepted_issues)
            record = {
                **source,
                "candidate_id": issue.id,
                "candidate_type": str(issue.type.value),
                "model": traced.model_name,
                "elapsed_sec": elapsed_sec,
                "input_tokens": traced.usage.input_tokens,
                "output_tokens": traced.usage.output_tokens,
                "accepted_count": len(accepted_issues),
                "decision_count": len(decisions),
                "candidate": issue.to_dict(),
                "accepted": accepted_issues,
                "decisions": decisions,
                "error": "",
            }
        except Exception as err:
            elapsed_sec = round(time.monotonic() - started, 3)
            record = {
                **source,
                "candidate_id": issue.id,
                "candidate_type": str(issue.type.value),
                "model": traced.model_name,
                "elapsed_sec": elapsed_sec,
                "input_tokens": traced.usage.input_tokens,
                "output_tokens": traced.usage.output_tokens,
                "accepted_count": 0,
                "decision_count": 0,
                "candidate": issue.to_dict(),
                "accepted": [],
                "decisions": [],
                "error": f"{type(err).__name__}: {err}",
            }
        record["raw_candidate"] = _raw_candidate_for_issue(issue, raw_candidates)
        _append_jsonl(output, record)
        completed += 1
        print(
            f"{issue.id} {issue.type.value} accepted={record['accepted_count']} "
            f"tokens={record['input_tokens'] + record['output_tokens']} "
            f"elapsed_sec={record['elapsed_sec']} error={bool(record['error'])}"
        )
    print(f"wrote {output}")
    print(f"completed={completed} accepted={accepted}")
    return 0


def _select_issue_indices(issues: list[Any], candidate_index: int | None) -> list[Any]:
    if candidate_index is None:
        return issues
    if candidate_index <= 0 or candidate_index > len(issues):
        raise SystemExit(f"candidate index out of range: {candidate_index}; available=1..{len(issues)}")
    return [issues[candidate_index - 1]]


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _raw_candidate_for_issue(issue: Any, raw_candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        index = int(str(issue.id).rsplit("-", 1)[-1]) - 1
    except ValueError:
        return None
    if 0 <= index < len(raw_candidates):
        return raw_candidates[index]
    return None


def summarize_results() -> int:
    files = sorted(RESULTS_ROOT.glob("scan_efficiency_*.csv"))
    if not files:
        print("no scan_efficiency CSV files found")
        return 1
    latest = files[-1]
    csv.field_size_limit(sys.maxsize)
    rows = list(csv.DictReader(latest.open(encoding="utf-8", newline="")))
    by_task: dict[str, dict[str, dict[str, str]]] = {}
    for row in rows:
        by_task.setdefault(row["task_id"], {})[row["mode"]] = row

    pairs = [
        pair
        for pair in by_task.values()
        if "baseline_direct_search" in pair and "ours_static_candidates" in pair
    ]
    time_drops: list[float] = []
    token_drops: list[float] = []
    for pair in pairs:
        baseline = pair["baseline_direct_search"]
        ours = pair["ours_static_candidates"]
        baseline_time = float(baseline["elapsed_sec"])
        ours_time = float(ours["elapsed_sec"])
        baseline_tokens = int(baseline["input_tokens"]) + int(baseline["output_tokens"])
        ours_tokens = int(ours["input_tokens"]) + int(ours["output_tokens"])
        if baseline_time > 0:
            time_drops.append((baseline_time - ours_time) / baseline_time)
        if baseline_tokens > 0:
            token_drops.append((baseline_tokens - ours_tokens) / baseline_tokens)

    lines = [
        "# RefactorGuard Evaluation Summary",
        "",
        f"- Source CSV: `{latest.name}`",
        f"- Completed rows: {len(rows)}",
        f"- Paired baseline/ours tasks: {len(pairs)}",
        f"- Mean fair elapsed reduction: {_pct(_mean(time_drops))}",
        f"- Mean token reduction: {_pct(_mean(token_drops))}",
        "- Ours elapsed = LLM triage time + project static scan time amortized by task count.",
        "",
        "## Rows",
        "",
        "| task | mode | elapsed_sec | static_scan_sec | llm_sec | triage_candidates | "
        "found_count | evidence_valid | tokens | found | exit |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |",
    ]
    for row in rows:
        tokens = int(row["input_tokens"]) + int(row["output_tokens"])
        lines.append(
            f"| {row['task_id']} | {row['mode']} | {row['elapsed_sec']} | "
            f"{row.get('static_scan_sec', '0.0')} | {row.get('llm_sec', row['elapsed_sec'])} | "
            f"{row.get('triage_candidate_count', '0')} | {row.get('found_count', '0')} | "
            f"{row.get('evidence_valid_count', '0')} | {tokens} | {row['found']} | {row['exit_code']} |"
        )
    output = RESULTS_ROOT / "summary.md"
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(output.read_text(encoding="utf-8"))
    return 0


def _default_tasks() -> list[dict[str, str]]:
    return [
        {
            "id": "T001",
            "project": "commons-lang",
            "smell_type": "long_method",
            "target_hint": "Find a method with high line count, branches, or nesting.",
        },
        {
            "id": "T002",
            "project": "commons-lang",
            "smell_type": "duplicate_code",
            "target_hint": "Find duplicated Java logic reported by CPD or obvious copy/paste.",
        },
        {
            "id": "T003",
            "project": "commons-io",
            "smell_type": "complex_condition",
            "target_hint": "Find a method or condition with nested or compound boolean logic.",
        },
        {
            "id": "T004",
            "project": "commons-io",
            "smell_type": "long_parameter_list",
            "target_hint": "Approximate with scanner-supported smells if the exact smell is not available.",
        },
        {
            "id": "T005",
            "project": "gson",
            "smell_type": "feature_envy",
            "target_hint": "Find a method that uses another project type more than its own class state.",
        },
        {
            "id": "T006",
            "project": "gson",
            "smell_type": "dead_code",
            "target_hint": "Find a private method that appears unused.",
        },
    ]


def _project_all_smells_task(project: str) -> dict[str, str]:
    return {
        "id": f"PROJECT-{project}-ALL",
        "project": project,
        "smell_type": "all",
        "target_hint": (
            "Find the most trustworthy mixed Java code smell issues across the repository-supported types: "
            "long_method, large_class, complex_condition, duplicate_code, dead_code, feature_envy, and unclear_naming."
        ),
    }


def _matches_expected(issues: list[Any], task: dict[str, Any]) -> bool:
    expected = str(task["smell_type"]).strip().lower()
    if expected == "all":
        return bool(issues)
    aliases = {"long_parameter_list": {"long_method", "complex_condition", "unclear_naming"}}
    acceptable = aliases.get(expected, {expected})
    return any(str(issue.type.value).lower() in acceptable for issue in issues)


def _baseline_candidates_from_result(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw_candidates = data.get("candidates")
    if isinstance(raw_candidates, list):
        return [dict(item) for item in raw_candidates if isinstance(item, dict)]
    if data.get("found"):
        return [data]
    return []


def _baseline_candidate_to_issue(candidate: dict[str, Any], index: int) -> Any:
    from suncli_py.refactor_agent.core.models import (
        Evidence,
        RefactorIssue,
        RiskLevel,
        Severity,
        SmellType,
    )

    smell_type = _baseline_smell_type(candidate)
    severity = Severity.HIGH if smell_type in {SmellType.LARGE_CLASS, SmellType.LONG_METHOD} else Severity.MEDIUM
    risk_level = RiskLevel.HIGH if smell_type == SmellType.LARGE_CLASS else RiskLevel.MEDIUM
    file_path = str(candidate.get("file_path") or "")
    original_start = _positive_int(candidate.get("start_line"), 1)
    original_end = max(_positive_int(candidate.get("end_line"), original_start), original_start)
    start_line, end_line = _bounded_baseline_range(candidate, original_start, original_end)
    return RefactorIssue(
        id=f"BASE-{index:04d}",
        type=smell_type,
        severity=severity,
        file_path=file_path,
        symbol=str(candidate.get("symbol") or "") or None,
        start_line=start_line,
        end_line=end_line,
        evidence=[
            Evidence(
                "Baseline direct-search candidate",
                {
                    "reason": str(candidate.get("reason") or ""),
                    "source": "baseline-direct-search",
                    "original_range": {
                        "file_path": file_path,
                        "start_line": original_start,
                        "end_line": original_end,
                    },
                    "source_evidence": candidate.get("evidence", []),
                },
            )
        ],
        impact=str(candidate.get("reason") or "Candidate found by baseline direct repository search."),
        recommendation="Validate with LLM triage before planning a refactor.",
        suggested_refactoring=_refactoring_for_smell(smell_type),
        risk_level=risk_level,
    )


def _candidate_summary_to_issue(candidate: dict[str, Any], index: int) -> Any:
    from suncli_py.refactor_agent.core.models import Evidence, RefactorIssue, RiskLevel, Severity, SmellType

    smell_type = SmellType(str(candidate["type"]))
    evidence = [
        Evidence(str(item.get("message") or "Static scanner evidence"), dict(item.get("metrics", {})))
        for item in candidate.get("evidence", [])
        if isinstance(item, dict)
    ]
    return RefactorIssue(
        id=str(candidate.get("id") or f"PLAN-{index:04d}"),
        type=smell_type,
        severity=Severity(str(candidate.get("severity") or "medium")),
        file_path=str(candidate["file_path"]),
        symbol=str(candidate.get("symbol") or "") or None,
        start_line=_positive_int(candidate.get("start_line"), 1),
        end_line=max(
            _positive_int(candidate.get("end_line"), _positive_int(candidate.get("start_line"), 1)),
            _positive_int(candidate.get("start_line"), 1),
        ),
        evidence=evidence,
        impact="Candidate selected from static scanner candidate plan.",
        recommendation="Validate with LLM triage before planning a refactor.",
        suggested_refactoring=_refactoring_for_smell(smell_type),
        risk_level=RiskLevel(str(candidate.get("risk_level") or "medium")),
    )


def _bounded_baseline_range(candidate: dict[str, Any], original_start: int, original_end: int) -> tuple[int, int]:
    max_span = 300
    if original_end - original_start + 1 <= max_span:
        return original_start, original_end
    evidence_ranges = _baseline_evidence_ranges(candidate)
    same_file = [
        (start, end)
        for file_path, start, end in evidence_ranges
        if not file_path or file_path == str(candidate.get("file_path") or "")
    ]
    meaningful = [item for item in same_file if item[1] - item[0] + 1 >= 5]
    if meaningful:
        start, end = min(meaningful, key=lambda item: (item[1] - item[0], item[0]))
        return start, min(end, start + max_span - 1)
    if same_file:
        start, end = same_file[0]
        return start, min(end, start + max_span - 1)
    return original_start, min(original_end, original_start + max_span - 1)


def _baseline_evidence_ranges(candidate: dict[str, Any]) -> list[tuple[str, int, int]]:
    ranges: list[tuple[str, int, int]] = []
    raw_evidence = candidate.get("evidence", [])
    if not isinstance(raw_evidence, list):
        return ranges
    for evidence in raw_evidence:
        if not isinstance(evidence, dict):
            continue
        start = _positive_int(evidence.get("start_line"), 0)
        if start <= 0:
            continue
        end = max(_positive_int(evidence.get("end_line"), start), start)
        ranges.append((str(evidence.get("file_path") or ""), start, end))
    return ranges


def _baseline_smell_type(candidate: dict[str, Any]) -> Any:
    from suncli_py.refactor_agent.core.models import SmellType

    raw = str(candidate.get("smell_type") or candidate.get("type") or "").strip().lower().replace("-", "_")
    aliases = {
        "god_class": "large_class",
        "data_class": "large_class",
        "long_parameter_list": "long_method",
        "speculative_generality": "dead_code",
    }
    raw = aliases.get(raw, raw)
    try:
        return SmellType(raw)
    except ValueError:
        return SmellType.LONG_METHOD


def _refactoring_for_smell(smell_type: Any) -> Any:
    from suncli_py.refactor_agent.core.models import RefactoringType, SmellType

    mapping = {
        SmellType.LONG_METHOD: RefactoringType.EXTRACT_METHOD,
        SmellType.LARGE_CLASS: RefactoringType.EXTRACT_CLASS,
        SmellType.COMPLEX_CONDITION: RefactoringType.INTRODUCE_EXPLAINING_VARIABLE,
        SmellType.DUPLICATE_CODE: RefactoringType.REPLACE_DUPLICATE_LOGIC,
        SmellType.DEAD_CODE: RefactoringType.REMOVE_DEAD_CODE,
        SmellType.FEATURE_ENVY: RefactoringType.MOVE_METHOD,
        SmellType.UNCLEAR_NAMING: RefactoringType.RENAME,
    }
    return mapping.get(smell_type, RefactoringType.EXTRACT_METHOD)


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _candidate_has_valid_evidence(candidate: dict[str, Any]) -> bool:
    if not candidate.get("file_path"):
        return False
    try:
        start_line = int(candidate.get("start_line", 0))
        end_line = int(candidate.get("end_line", start_line))
    except (TypeError, ValueError):
        return False
    evidence = candidate.get("evidence", [])
    return start_line > 0 and end_line >= start_line and isinstance(evidence, list) and bool(evidence)


def _issue_has_llm_source_evidence(issue: Any) -> bool:
    for evidence in getattr(issue, "evidence", []):
        metrics = getattr(evidence, "metrics", {})
        if isinstance(metrics, dict) and metrics.get("source_evidence"):
            return True
    return False


def _candidate_subset_for_task(candidates: list[Any], task: dict[str, Any]) -> list[Any]:
    expected = str(task["smell_type"]).strip().lower()
    if expected == "all":
        return _grouped_mixed_candidates(candidates)
    aliases = {"long_parameter_list": {"long_method", "complex_condition", "unclear_naming"}}
    acceptable = aliases.get(expected, {expected})
    matched = [candidate for candidate in candidates if str(candidate.type.value).lower() in acceptable]
    return _ranked_issues(matched or candidates)


def _grouped_mixed_candidates(candidates: list[Any]) -> list[Any]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for candidate in candidates:
        grouped[str(candidate.type.value)].append(candidate)

    smell_order = [
        "duplicate_code",
        "long_method",
        "large_class",
        "complex_condition",
        "dead_code",
        "feature_envy",
        "unclear_naming",
    ]
    ranked_by_smell = {smell: _ranked_issues(grouped.get(smell, [])) for smell in smell_order}
    selected: list[Any] = []
    seen: set[str] = set()
    depth = 0
    while True:
        added = False
        for smell in smell_order:
            ranked = ranked_by_smell[smell]
            if depth < len(ranked):
                issue = ranked[depth]
                selected.append(issue)
                seen.add(issue.id)
                added = True
        if not added:
            break
        depth += 1

    for issue in _ranked_issues(candidates):
        if issue.id not in seen:
            selected.append(issue)
    return selected


def _ranked_issues(candidates: list[Any]) -> list[Any]:
    from suncli_py.refactor_agent.analysis.candidate_ranker import rank_candidates

    return [candidate.issue for candidate in rank_candidates(candidates)]


def _candidate_plan(
    task: dict[str, Any],
    scan_cache: StaticScanCache,
    *,
    eligible_candidates: list[Any],
    triage_candidates: list[Any],
) -> dict[str, Any]:
    from suncli_py.refactor_agent.analysis.candidate_ranker import rank_candidates

    ranked_by_id = {ranked.issue.id: ranked for ranked in rank_candidates(eligible_candidates)}
    return {
        "task": task,
        "static_scan_sec": scan_cache.elapsed_sec,
        "candidate_count": len(scan_cache.candidates),
        "eligible_candidate_count": len(eligible_candidates),
        "selected_candidate_count": len(triage_candidates),
        "candidate_counts_by_smell": dict(
            sorted(Counter(str(candidate.type.value) for candidate in scan_cache.candidates).items())
        ),
        "selected_counts_by_smell": dict(
            sorted(Counter(str(candidate.type.value) for candidate in triage_candidates).items())
        ),
        "selected_candidates": [
            _candidate_summary(candidate, ranked=ranked_by_id.get(candidate.id))
            for candidate in triage_candidates
        ],
    }


def _candidate_summary(candidate: Any, *, ranked: Any | None = None) -> dict[str, Any]:
    summary = {
        "id": candidate.id,
        "type": str(candidate.type.value),
        "severity": str(candidate.severity.value),
        "risk_level": str(candidate.risk_level.value),
        "file_path": candidate.file_path,
        "symbol": candidate.symbol,
        "start_line": candidate.start_line,
        "end_line": candidate.end_line,
        "evidence": [
            {
                "message": evidence.message,
                "metrics": evidence.metrics,
            }
            for evidence in candidate.evidence
        ],
    }
    if ranked is not None:
        summary["rank_score"] = ranked.score
        summary["rank_reasons"] = ranked.reasons
    return summary


def _task_candidate_sort_key(candidate: Any, expected: str) -> tuple[float, float, float]:
    metrics = _merged_evidence_metrics(candidate)
    span = max(1, int(getattr(candidate, "end_line", 1)) - int(getattr(candidate, "start_line", 1)) + 1)
    severity_weight = {"high": 3.0, "medium": 2.0, "low": 1.0}.get(str(candidate.severity.value), 0.0)
    if expected == "long_method":
        return (
            -float(metrics.get("lines", span) or span),
            -float(metrics.get("branches", 0) or 0),
            -float(metrics.get("max_nesting", 0) or 0),
        )
    if expected == "duplicate_code":
        return (-span, -severity_weight, 0.0)
    return (-severity_weight, -span, 0.0)


def _merged_evidence_metrics(candidate: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for evidence in getattr(candidate, "evidence", []):
        metrics = getattr(evidence, "metrics", None)
        if isinstance(metrics, dict):
            merged.update(metrics)
    return merged


def _message_payload(message: Any) -> dict[str, Any]:
    return {
        "role": getattr(message, "role", ""),
        "content": getattr(message, "content", ""),
        "tool_calls": bool(getattr(message, "tool_calls", None)),
    }


def _estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, round(ascii_chars / 4 + non_ascii_chars * 0.8))


def _readme_text() -> str:
    return """# RefactorGuard Evaluation

This folder contains a lightweight, reproducible experiment setup for interview
metrics. It uses runnable Java Maven repositories rather than isolated snippets.

Commands:

```powershell
uv run python scripts/refactor_eval.py init
uv run python scripts/refactor_eval.py prepare
uv run python scripts/refactor_eval.py smoke
uv run python scripts/refactor_eval.py scan-efficiency --limit 6 --mode both --model claude-4-sonnet
uv run python scripts/refactor_eval.py scan-efficiency --project commons-lang --all-smells `
  --triage-limit 10 --baseline-limit 10 --baseline-iterations 50 --mode both --model claude-4-sonnet
uv run python scripts/refactor_eval.py summarize
```

Metric wording:

- Baseline: direct LLM repository search with read-only tools.
- Ours: JavaParser / Symbol Solver / PMD CPD candidate generation, then LLM semantic triage.
- Project-level mixed mode: `--project <name> --all-smells` runs one task that asks both systems for mixed Top-K issues.
- Token reduction: `(baseline_tokens - ours_tokens) / baseline_tokens`.
- Time reduction: `(baseline_elapsed - ours_fair_elapsed) / baseline_elapsed`.
- Ours fair elapsed: `llm_triage_sec + static_scan_sec / same_project_task_count`.

The second claim, single-agent vs multi-agent refactoring success, should reuse
the same `tasks.json` rows and record CLI artifacts from `scan`, `plan`, and
`apply --max-repair-attempts N`. Keep the success definition fixed: Maven
verification passes, target smell is removed, public API changes are expected,
and diff/path guards do not reject the patch.
"""


def _error_row(task: dict[str, Any], mode: str, error: str) -> BenchmarkRow:
    return BenchmarkRow(
        task_id=task["id"],
        project=task["project"],
        smell_type=task["smell_type"],
        target_hint=task.get("target_hint", ""),
        mode=mode,
        elapsed_sec=0.0,
        exit_code=1,
        error=error,
    )


def _exception_row(
    task: dict[str, Any],
    mode: str,
    started: float,
    usage: LlmUsage,
    err: Exception,
    *,
    candidate_count: int = 0,
) -> BenchmarkRow:
    return BenchmarkRow(
        task_id=task["id"],
        project=task["project"],
        smell_type=task["smell_type"],
        target_hint=task.get("target_hint", ""),
        mode=mode,
        elapsed_sec=round(time.monotonic() - started, 3),
        llm_calls=usage.calls,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        candidate_count=candidate_count,
        exit_code=1,
        error=f"{type(err).__name__}: {err}",
    )


def _write_rows(path: Path, rows: list[BenchmarkRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(BenchmarkRow("", "", "", "", "", 0.0).to_dict().keys())
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(row.to_dict() for row in rows)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_json(path: Path) -> Any:
    if not path.exists():
        raise SystemExit(f"missing {path}; run `uv run python scripts/refactor_eval.py init` first")
    return json.loads(path.read_text(encoding="utf-8"))


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    executable = shutil.which(command[0]) or command[0]
    result = subprocess.run(
        [executable, *command[1:]],
        cwd=str(cwd),
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr or result.stdout or f"{command} failed")
    return result


def _run_shell(command: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=str(cwd), shell=True, capture_output=True, encoding="utf-8", errors="replace")


def _timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _ensure_import_path() -> None:
    root_text = str(ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)


if __name__ == "__main__":
    raise SystemExit(main())
