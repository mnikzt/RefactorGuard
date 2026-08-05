"""JavaParser-backed AST extraction for Java source files."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

CommandRunner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]

MAX_EXEC_ARGS_CHARS = 2_000


class JavaAstError(Exception):
    """Raised when JavaParser AST extraction is unavailable or invalid."""


@dataclass(frozen=True)
class AstMethod:
    name: str
    start_line: int
    end_line: int
    signature: str
    declaring_type: str
    resolved_signature: str
    symbol_resolved: bool
    is_private: bool
    is_public: bool
    is_static: bool
    branch_count: int
    max_control_nesting: int


@dataclass(frozen=True)
class AstMethodCall:
    name: str
    start_line: int
    end_line: int
    scope: str
    declaring_type: str
    resolved_signature: str
    return_type: str
    symbol_resolved: bool
    error: str


@dataclass(frozen=True)
class AstFieldAccess:
    name: str
    start_line: int
    end_line: int
    scope: str
    declaring_type: str
    type: str
    symbol_resolved: bool
    error: str


@dataclass(frozen=True)
class AstClass:
    name: str
    start_line: int
    end_line: int
    kind: str
    field_count: int
    method_count: int
    public_method_count: int


@dataclass(frozen=True)
class AstFileAnalysis:
    path: Path
    relative_path: str
    methods: list[AstMethod]
    classes: list[AstClass]
    method_calls: list[AstMethodCall]
    field_accesses: list[AstFieldAccess]


def _default_command_runner(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    executable = shutil.which(command[0]) or command[0]
    return subprocess.run(
        [executable, *command[1:]],
        cwd=str(cwd),
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=180,
    )


class JavaParserAnalyzer:
    """Run the bundled JavaParser helper and return source ranges from a real Java AST."""

    def __init__(self, root: str | Path, command_runner: CommandRunner | None = None) -> None:
        self.root = Path(root).resolve()
        self._run = command_runner or _default_command_runner
        self.helper_dir = Path(__file__).resolve().parent / "java_ast_helper"

    def analyze_files(self, paths: list[Path]) -> list[AstFileAnalysis]:
        if not paths:
            return []
        relative_paths = [self._relative_path(path) for path in paths]
        analyses: list[AstFileAnalysis] = []
        current_batch: list[str] = []
        current_size = len(str(self.root)) + 3
        for relative_path in relative_paths:
            quoted_size = len(_quote_arg(relative_path)) + 1
            if current_batch and current_size + quoted_size > MAX_EXEC_ARGS_CHARS:
                analyses.extend(self._analyze_relative_paths(current_batch))
                current_batch = []
                current_size = len(str(self.root)) + 3
            current_batch.append(relative_path)
            current_size += quoted_size
        if current_batch:
            analyses.extend(self._analyze_relative_paths(current_batch))
        return analyses

    def _analyze_relative_paths(self, relative_paths: list[str]) -> list[AstFileAnalysis]:
        args = " ".join([_quote_arg(str(self.root)), *[_quote_arg(path) for path in relative_paths]])
        command = [
            "mvn",
            "-q",
            "-f",
            str(self.helper_dir / "pom.xml"),
            "compile",
            "exec:java",
            f"-Dexec.args={args}",
        ]
        try:
            result = self._run(command, self.root)
        except (FileNotFoundError, OSError, subprocess.SubprocessError) as err:
            raise JavaAstError(f"JavaParser helper failed to start: {err}") from err
        if result.returncode != 0:
            output = (result.stderr or result.stdout or "").strip()
            raise JavaAstError(f"JavaParser helper failed: {output[-1000:]}")
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as err:
            raise JavaAstError("JavaParser helper returned invalid JSON.") from err
        return [_ast_file_from_dict(item, self.root) for item in data.get("files", [])]

    def _relative_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError as err:
            raise JavaAstError(f"Java source path is outside project root: {path}") from err


def _ast_file_from_dict(data: dict, root: Path) -> AstFileAnalysis:
    relative_path = str(data["path"]).replace("\\", "/")
    return AstFileAnalysis(
        path=(root / relative_path).resolve(),
        relative_path=relative_path,
        methods=[
            AstMethod(
                name=item["name"],
                start_line=int(item["start_line"]),
                end_line=int(item["end_line"]),
                signature=item.get("signature", ""),
                declaring_type=item.get("declaring_type", ""),
                resolved_signature=item.get("resolved_signature", ""),
                symbol_resolved=bool(item.get("symbol_resolved", False)),
                is_private=bool(item.get("is_private", False)),
                is_public=bool(item.get("is_public", False)),
                is_static=bool(item.get("is_static", False)),
                branch_count=int(item.get("branch_count", 0)),
                max_control_nesting=int(item.get("max_control_nesting", 0)),
            )
            for item in data.get("methods", [])
        ],
        classes=[
            AstClass(
                name=item["name"],
                start_line=int(item["start_line"]),
                end_line=int(item["end_line"]),
                kind=item.get("kind", "class"),
                field_count=int(item.get("field_count", 0)),
                method_count=int(item.get("method_count", 0)),
                public_method_count=int(item.get("public_method_count", 0)),
            )
            for item in data.get("classes", [])
        ],
        method_calls=[
            AstMethodCall(
                name=item.get("name", ""),
                start_line=int(item.get("start_line", 1)),
                end_line=int(item.get("end_line", item.get("start_line", 1))),
                scope=item.get("scope", ""),
                declaring_type=item.get("declaring_type", ""),
                resolved_signature=item.get("resolved_signature", ""),
                return_type=item.get("return_type", ""),
                symbol_resolved=bool(item.get("symbol_resolved", False)),
                error=item.get("error", ""),
            )
            for item in data.get("method_calls", [])
        ],
        field_accesses=[
            AstFieldAccess(
                name=item.get("name", ""),
                start_line=int(item.get("start_line", 1)),
                end_line=int(item.get("end_line", item.get("start_line", 1))),
                scope=item.get("scope", ""),
                declaring_type=item.get("declaring_type", ""),
                type=item.get("type", ""),
                symbol_resolved=bool(item.get("symbol_resolved", False)),
                error=item.get("error", ""),
            )
            for item in data.get("field_accesses", [])
        ],
    )


def _quote_arg(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
