"""本地 Java 坏味道扫描器（阶段二 MVP）。

这个文件负责「找问题」，不负责「改代码」。

整体流程（可以先记住这一条）：
1. 收集项目里的 .java 文件
2. 用 JavaParser 做 AST（抽象语法树）分析，得到类/方法结构
3. 用一组确定性规则（阈值）扫描常见坏味道
4. 重复代码交给 Maven 的 PMD CPD 插件检测
5. 把结果整理成 RefactorIssue 列表，并编号成 RA-0001、RA-0002...

后面 CLI 的 plan / apply 都会基于这里产出的 issue 继续工作。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from suncli_py.refactor_agent.analysis.java_ast import (
    AstFieldAccess,
    AstFileAnalysis,
    AstMethodCall,
    JavaAstError,
    JavaParserAnalyzer,
)
from suncli_py.refactor_agent.analysis.project_detector import CommandRunner
from suncli_py.refactor_agent.core.models import (
    Evidence,
    RefactoringType,
    RefactorIssue,
    RiskLevel,
    Severity,
    SmellType,
)

# 扫描时跳过这些目录，避免扫到构建产物、依赖或工具缓存。
IGNORED_DIRS = {".git", ".paicli", "target", "build", ".gradle", "node_modules"}

# 「命名不清晰」规则用的黑名单：这些名字本身几乎不表达业务含义。
UNCLEAR_LOCAL_NAMES = {"tmp", "temp", "data", "obj", "x", "y", "z", "foo", "bar"}
UNCLEAR_METHOD_NAMES = {"handle", "process", "doIt", "doit", "run", "execute"}
UNCLEAR_CLASS_SUFFIXES = ("Manager", "Helper", "Util")


class CpdError(Exception):
    """PMD CPD（复制粘贴检测）跑不起来时抛出。

    CPD = Copy/Paste Detector，用来找重复代码片段。
    """


@dataclass(frozen=True)
class JavaMethod:
    """扫描器用的「方法摘要」：从 AST 结果里挑出规则真正需要的字段。"""

    name: str
    start_line: int
    end_line: int
    body_lines: list[str]  # 去掉注释/字符串后的方法体，方便做正则启发式
    signature: str
    declaring_type: str  # 这个方法属于哪个类
    resolved_signature: str  # Symbol Solver 解析出的完整签名（可能为空）
    symbol_resolved: bool  # 符号是否解析成功
    is_private: bool
    is_public: bool
    is_static: bool
    branch_count: int  # if/switch/循环等分支数量
    max_control_nesting: int  # 最深嵌套层数（if 里再套 if）
    method_calls: list[AstMethodCall]
    field_accesses: list[AstFieldAccess]


@dataclass(frozen=True)
class JavaClass:
    """扫描器用的「类摘要」。"""

    name: str
    start_line: int
    end_line: int
    body_lines: list[str]
    kind: str  # 例如 class / interface / enum
    field_count: int
    method_count: int
    public_method_count: int


@dataclass(frozen=True)
class JavaFileAnalysis:
    """单个 Java 文件的分析结果，供后续各条坏味道规则复用。"""

    path: Path
    relative_path: str  # 相对项目根目录的路径，写进 issue 时用这个
    lines: list[str]  # 原始源码行
    sanitized_lines: list[str]  # 去掉注释和字符串内容后的行
    methods: list[JavaMethod]
    classes: list[JavaClass]


def _default_command_runner(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """默认的外部命令执行器（例如跑 `mvn pmd:cpd-check`）。

    用 shutil.which 找可执行文件，方便在 Windows/Linux 上都能定位到 mvn。
    """
    executable = shutil.which(command[0]) or command[0]
    return subprocess.run(
        [executable, *command[1:]],
        cwd=str(cwd),
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=90,
    )


class JavaSmellScanner:
    """用本地确定性启发式规则扫描 Java 坏味道。

    「确定性」意思是：同样输入，规则阈值固定，结果可复现。
    LLM 的 triage（二次筛选）在更上层的 commands.py 里做，不在这里。
    """

    def __init__(
        self,
        root: str | Path,
        command_runner: CommandRunner | None = None,
        ast_command_runner: CommandRunner | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        # 可注入 command_runner，方便单元测试时 fake 掉 mvn 调用。
        self._run = command_runner or _default_command_runner
        self._ast_analyzer = JavaParserAnalyzer(self.root, ast_command_runner)
        # scan() 结束后，上层（如 LLM）可能还要复用原始 AST 结果。
        self.ast_analyses: tuple[AstFileAnalysis, ...] = ()
        self.warnings: list[str] = []

    def scan(self) -> list[RefactorIssue]:
        """主入口：扫描整个项目，返回编号后的 issue 列表。"""
        self.warnings = []
        self.ast_analyses = ()
        java_files = self._collect_java_files()
        analyses = self._analyze_files(java_files)
        issues: list[RefactorIssue] = []

        # 下面每一条规则各自产出一批候选 issue，最后统一编号。
        issues.extend(self._scan_long_methods(analyses))
        issues.extend(self._scan_large_classes(analyses))
        issues.extend(self._scan_complex_conditions(analyses))
        issues.extend(self._scan_unclear_naming(analyses))
        issues.extend(self._scan_dead_code(analyses))
        issues.extend(self._scan_feature_envy(analyses))
        issues.extend(self._scan_duplicate_code(analyses))

        # 先按文件路径 + 行号排序，再统一分配 RA-0001 这类稳定编号。
        sorted_issues = sorted(issues, key=lambda issue: (issue.file_path, issue.start_line, issue.type.value))
        return [
            RefactorIssue(
                id=f"RA-{index:04d}",
                type=issue.type,
                severity=issue.severity,
                file_path=issue.file_path,
                symbol=issue.symbol,
                start_line=issue.start_line,
                end_line=issue.end_line,
                evidence=issue.evidence,
                impact=issue.impact,
                recommendation=issue.recommendation,
                suggested_refactoring=issue.suggested_refactoring,
                risk_level=issue.risk_level,
            )
            for index, issue in enumerate(sorted_issues, start=1)
        ]

    def _collect_java_files(self) -> list[Path]:
        """递归收集项目下所有 .java，并跳过构建/依赖目录。"""
        files: list[Path] = []
        for path in self.root.rglob("*.java"):
            if any(part in IGNORED_DIRS for part in path.relative_to(self.root).parts):
                continue
            files.append(path)
        return sorted(files)

    def _analyze_files(self, paths: list[Path]) -> list[JavaFileAnalysis]:
        """调用 JavaParser，把每个文件转成扫描规则能直接用的结构。"""
        ast_analyses = self._ast_analyzer.analyze_files(paths)
        ast_by_path = {analysis.relative_path: analysis for analysis in ast_analyses}
        analyses: list[JavaFileAnalysis] = []
        for path in paths:
            relative_path = path.relative_to(self.root).as_posix()
            ast_analysis = ast_by_path.get(relative_path)
            # 有文件没解析到 AST，说明解析链路坏了，直接失败比静默跳过更安全。
            if ast_analysis is None:
                raise JavaAstError(f"JavaParser returned no AST for {relative_path}")
            analyses.append(self._analysis_from_ast(ast_analysis))
        self.ast_analyses = tuple(ast_analyses)
        return analyses

    def _analysis_from_ast(self, ast_analysis: AstFileAnalysis) -> JavaFileAnalysis:
        """把底层 AstFileAnalysis 适配成 scanner 内部使用的 JavaFileAnalysis。

        关键动作：
        - 读源码行
        - 去掉注释/字符串（避免正则误伤）
        - 把每个方法关联上「落在方法行号范围内」的调用和字段访问
        """
        lines = ast_analysis.path.read_text(encoding="utf-8", errors="replace").splitlines()
        sanitized = _strip_comments_and_strings(lines)
        methods = [
            JavaMethod(
                name=method.name,
                start_line=method.start_line,
                end_line=method.end_line,
                # Python 切片是左闭右开，行号从 1 开始，所以要 start_line-1。
                body_lines=sanitized[method.start_line - 1 : method.end_line],
                signature=method.signature,
                declaring_type=method.declaring_type,
                resolved_signature=method.resolved_signature,
                symbol_resolved=method.symbol_resolved,
                is_private=method.is_private,
                is_public=method.is_public,
                is_static=method.is_static,
                branch_count=method.branch_count,
                max_control_nesting=method.max_control_nesting,
                method_calls=[
                    call
                    for call in ast_analysis.method_calls
                    if method.start_line <= call.start_line <= method.end_line
                ],
                field_accesses=[
                    access
                    for access in ast_analysis.field_accesses
                    if method.start_line <= access.start_line <= method.end_line
                ],
            )
            for method in ast_analysis.methods
        ]
        classes = [
            JavaClass(
                name=class_info.name,
                start_line=class_info.start_line,
                end_line=class_info.end_line,
                body_lines=sanitized[class_info.start_line - 1 : class_info.end_line],
                kind=class_info.kind,
                field_count=class_info.field_count,
                method_count=class_info.method_count,
                public_method_count=class_info.public_method_count,
            )
            for class_info in ast_analysis.classes
        ]
        return JavaFileAnalysis(
            path=ast_analysis.path,
            relative_path=ast_analysis.relative_path,
            lines=lines,
            sanitized_lines=sanitized,
            methods=methods,
            classes=classes,
        )

    def _scan_long_methods(self, analyses: Iterable[JavaFileAnalysis]) -> list[RefactorIssue]:
        """规则：长方法（Long Method）。

        触发条件（任一超标即可）：
        - 行数 > 80
        - 分支数 > 12
        - 最大嵌套深度 > 4

        更夸张时升为 HIGH（行数 > 160 / 分支 > 20 / 嵌套 > 6）。
        """
        issues: list[RefactorIssue] = []
        for analysis in analyses:
            for method in analysis.methods:
                line_count = method.end_line - method.start_line + 1
                branch_count = method.branch_count
                nesting_depth = method.max_control_nesting
                if line_count <= 80 and branch_count <= 12 and nesting_depth <= 4:
                    continue

                severity = (
                    Severity.HIGH
                    if line_count > 160 or branch_count > 20 or nesting_depth > 6
                    else Severity.MEDIUM
                )
                issues.append(
                    _issue(
                        SmellType.LONG_METHOD,
                        severity,
                        analysis.relative_path,
                        method.name,
                        method.start_line,
                        method.end_line,
                        [
                            Evidence(
                                "方法规模或控制流复杂度超过阈值。",
                                {"lines": line_count, "branches": branch_count, "max_nesting": nesting_depth},
                            )
                        ],
                        "长方法会让职责边界模糊，增加理解、测试和安全重构成本。",
                        "优先识别连续的业务步骤并使用 Extract Method 小步拆分。",
                        RefactoringType.EXTRACT_METHOD,
                        risk_level=RiskLevel.MEDIUM if severity == Severity.MEDIUM else RiskLevel.HIGH,
                    )
                )
        return issues

    def _scan_large_classes(self, analyses: Iterable[JavaFileAnalysis]) -> list[RefactorIssue]:
        """规则：过大类（Large Class / God Class 的轻量版）。

        触发条件（任一超标即可）：
        - 行数 > 500
        - 方法数 > 20
        - 字段数 > 20
        - public 方法数 > 15
        """
        issues: list[RefactorIssue] = []
        for analysis in analyses:
            for class_info in analysis.classes:
                loc = class_info.end_line - class_info.start_line + 1
                if (
                    loc <= 500
                    and class_info.method_count <= 20
                    and class_info.field_count <= 20
                    and class_info.public_method_count <= 15
                ):
                    continue

                severity = (
                    Severity.HIGH
                    if loc > 1000 or class_info.method_count > 40 or class_info.field_count > 40
                    else Severity.MEDIUM
                )
                issues.append(
                    _issue(
                        SmellType.LARGE_CLASS,
                        severity,
                        analysis.relative_path,
                        class_info.name,
                        class_info.start_line,
                        class_info.end_line,
                        [
                            Evidence(
                                "类的规模或成员数量超过阈值。",
                                {
                                    "lines": loc,
                                    "methods": class_info.method_count,
                                    "fields": class_info.field_count,
                                    "public_methods": class_info.public_method_count,
                                },
                            )
                        ],
                        "过大类通常承担多种职责，会放大修改影响面并降低内聚性。",
                        "优先生成 Extract Class / Move Method 计划，并在用户确认后按计划执行。",
                        RefactoringType.EXTRACT_CLASS,
                        risk_level=RiskLevel.HIGH,
                    )
                )
        return issues

    def _scan_complex_conditions(self, analyses: Iterable[JavaFileAnalysis]) -> list[RefactorIssue]:
        """规则：复杂条件（Complex Condition）。

        两条路径：
        1. 方法整体嵌套太深（> 4）→ 直接报整个方法
        2. 单行 if/while/for 条件里 && / || 太多（>= 3）→ 报那一行
        """
        issues: list[RefactorIssue] = []
        condition_pattern = re.compile(r"\b(if|while|for)\s*\((.*)\)")
        for analysis in analyses:
            for method in analysis.methods:
                nesting_depth = method.max_control_nesting
                if nesting_depth > 4:
                    issues.append(
                        _issue(
                            SmellType.COMPLEX_CONDITION,
                            Severity.MEDIUM,
                            analysis.relative_path,
                            method.name,
                            method.start_line,
                            method.end_line,
                            [Evidence("控制流嵌套较深。", {"max_nesting": nesting_depth})],
                            "深层嵌套会隐藏边界条件，增加遗漏分支和回归风险。",
                            "优先使用 Guard Clauses 或 Extract Method 分解条件逻辑。",
                            RefactoringType.INTRODUCE_EXPLAINING_VARIABLE,
                            risk_level=RiskLevel.MEDIUM,
                        )
                    )
                    # 已经按「整方法嵌套过深」报过了，就不再逐行扫条件，避免重复刷屏。
                    continue

                for offset, line in enumerate(method.body_lines, start=method.start_line):
                    match = condition_pattern.search(line)
                    if not match:
                        continue
                    condition = match.group(2)
                    operator_count = condition.count("&&") + condition.count("||")
                    if operator_count < 3:
                        continue
                    severity = Severity.MEDIUM if operator_count >= 4 else Severity.LOW
                    issues.append(
                        _issue(
                            SmellType.COMPLEX_CONDITION,
                            severity,
                            analysis.relative_path,
                            method.name,
                            offset,
                            offset,
                            [Evidence("布尔表达式包含较多逻辑操作符。", {"boolean_operators": operator_count})],
                            "复杂条件降低可读性，也让测试用例更难覆盖所有组合。",
                            "提取解释性变量或小方法，给关键业务判断命名。",
                            RefactoringType.INTRODUCE_EXPLAINING_VARIABLE,
                            risk_level=RiskLevel.LOW if severity == Severity.LOW else RiskLevel.MEDIUM,
                        )
                    )
        return issues

    def _scan_unclear_naming(self, analyses: Iterable[JavaFileAnalysis]) -> list[RefactorIssue]:
        """规则：命名不清晰（Unclear Naming）。

        检查三类名字：
        - 类名以 Manager / Helper / Util 结尾
        - 方法名落在 handle/process/run 这类泛化词里
        - 局部变量名是 tmp/data/foo 等
        """
        issues: list[RefactorIssue] = []
        # 很粗的「局部变量声明」正则：类型 + 变量名 + =/;/,
        # 不是完整 Java 语法解析，只做启发式。
        local_pattern = re.compile(
            r"\b(?:String|int|long|double|float|boolean|char|byte|short|var|[A-Z][A-Za-z0-9_<>]*)\s+"
            r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:=|;|,)"
        )
        for analysis in analyses:
            for class_info in analysis.classes:
                if class_info.name.endswith(UNCLEAR_CLASS_SUFFIXES):
                    issues.append(
                        self._naming_issue(
                            analysis.relative_path,
                            class_info.name,
                            class_info.start_line,
                            f"类名 {class_info.name} 过于泛化。",
                            "类名应表达主要职责，避免 Manager/Helper/Util 泛化命名。",
                        )
                    )
            for method in analysis.methods:
                if method.name in UNCLEAR_METHOD_NAMES:
                    issues.append(
                        self._naming_issue(
                            analysis.relative_path,
                            method.name,
                            method.start_line,
                            f"方法名 {method.name} 含义泛化。",
                            "方法名应描述具体业务动作，降低调用方理解成本。",
                        )
                    )
                for offset, line in enumerate(method.body_lines, start=method.start_line):
                    for name in local_pattern.findall(line):
                        if name in UNCLEAR_LOCAL_NAMES:
                            issues.append(
                                self._naming_issue(
                                    analysis.relative_path,
                                    name,
                                    offset,
                                    f"局部变量名 {name} 含义不清晰。",
                                    "局部变量名应表达其业务含义或中间结果含义。",
                                )
                            )
        return issues

    def _naming_issue(
        self,
        file_path: str,
        symbol: str,
        line: int,
        evidence_message: str,
        recommendation: str,
    ) -> RefactorIssue:
        """命名类问题的统一打包：严重度/风险都偏低，建议重构方式是 Rename。"""
        return _issue(
            SmellType.UNCLEAR_NAMING,
            Severity.LOW,
            file_path,
            symbol,
            line,
            line,
            [Evidence(evidence_message)],
            "命名不清晰会让代码意图依赖上下文猜测，增加维护和 Review 成本。",
            recommendation,
            RefactoringType.RENAME,
            risk_level=RiskLevel.LOW,
        )

    def _scan_dead_code(self, analyses: list[JavaFileAnalysis]) -> list[RefactorIssue]:
        """规则：疑似死代码（目前主要盯 private 方法）。

        判断策略（两级）：
        1. 优先用 Symbol Solver 的解析签名：如果没有任何调用点引用它 → 疑似死代码
        2. 解析失败时退化为「全项目标识符出现次数」：名字只出现 1 次（定义本身）→ 疑似死代码

        注意：反射、配置驱动调用、某些框架回调可能造成误报，所以建议是 LOW 风险。
        """
        # 把所有清洗后的源码拼起来，做「名字出现次数」统计（fallback 用）。
        source_text = "\n".join("\n".join(analysis.sanitized_lines) for analysis in analyses)
        name_counts = Counter(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", source_text))
        # 收集全项目里「已被解析出来的方法调用签名」。
        resolved_calls = {
            call.resolved_signature
            for analysis in analyses
            for method in analysis.methods
            for call in method.method_calls
            if call.symbol_resolved and call.resolved_signature
        }
        issues: list[RefactorIssue] = []
        for analysis in analyses:
            for method in analysis.methods:
                # 只检查 private；main 即使 private 也不当死代码。
                if not method.is_private or method.name in {"main"}:
                    continue
                has_symbol_data = bool(method.symbol_resolved and method.resolved_signature)
                # 有人调用过这个签名 → 不是死代码。
                if has_symbol_data and method.resolved_signature in resolved_calls:
                    continue
                # fallback：名字出现超过 1 次，说明别处至少提到过它。
                if not has_symbol_data and name_counts[method.name] > 1:
                    continue
                source = "symbol-solver" if has_symbol_data else "identifier-count-fallback"
                issues.append(
                    _issue(
                        SmellType.DEAD_CODE,
                        Severity.LOW,
                        analysis.relative_path,
                        method.name,
                        method.start_line,
                        method.end_line,
                        [
                            Evidence(
                                "private 方法未发现其它引用。",
                                {
                                    "identifier_occurrences": name_counts[method.name],
                                    "source": source,
                                    "resolved_signature": method.resolved_signature,
                                },
                            )
                        ],
                        "无用 private 代码会增加阅读负担，也可能误导后续重构判断。",
                        "确认无反射或框架调用后使用 Remove Dead Code。",
                        RefactoringType.REMOVE_DEAD_CODE,
                        risk_level=RiskLevel.LOW,
                    )
                )
        return issues

    def _scan_feature_envy(self, analyses: Iterable[JavaFileAnalysis]) -> list[RefactorIssue]:
        """规则：依恋情节（Feature Envy）。

        直觉：一个方法「更关心别人家的数据/方法」，而不是自己类的成员。
        这时往往更适合 Move Method，把逻辑挪到它真正依赖的那个类里。

        触发条件（同时满足）：
        - 外部成员使用次数够多（>= 5）
        - 外部使用明显多于本地（至少是本地的 2 倍，且下限 3）
        - 外部访问主要集中在某一个类型上（该类型至少被用 4 次）
        """
        issues: list[RefactorIssue] = []
        for analysis in analyses:
            for method in analysis.methods:
                # private / 符号解析失败 / 不知道所属类 → 证据不足，跳过。
                if method.is_private or not method.symbol_resolved or not method.declaring_type:
                    continue
                # 外部：别的业务类型上的方法调用 / 字段访问（排除 java.* 标准库）。
                external_calls = [
                    call
                    for call in method.method_calls
                    if call.symbol_resolved
                    and call.declaring_type
                    and not _same_or_nested_type(call.declaring_type, method.declaring_type)
                    and not call.declaring_type.startswith("java.")
                ]
                external_fields = [
                    access
                    for access in method.field_accesses
                    if access.symbol_resolved
                    and access.declaring_type
                    and not _same_or_nested_type(access.declaring_type, method.declaring_type)
                    and not access.declaring_type.startswith("java.")
                ]
                # 本地：本类或嵌套类上的成员使用。
                local_calls = [
                    call
                    for call in method.method_calls
                    if call.symbol_resolved and _same_or_nested_type(call.declaring_type, method.declaring_type)
                ]
                local_fields = [
                    access
                    for access in method.field_accesses
                    if access.symbol_resolved and _same_or_nested_type(access.declaring_type, method.declaring_type)
                ]
                external_total = len(external_calls) + len(external_fields)
                local_total = len(local_calls) + len(local_fields)
                if external_total < 5 or external_total < max(local_total * 2, 3):
                    continue

                # 找出「最被依恋」的那个外部类型。
                external_types = Counter(
                    item.declaring_type for item in [*external_calls, *external_fields] if item.declaring_type
                )
                dominant_type, dominant_count = external_types.most_common(1)[0]
                if dominant_count < 4:
                    continue

                issues.append(
                    _issue(
                        SmellType.FEATURE_ENVY,
                        Severity.MEDIUM,
                        analysis.relative_path,
                        method.name,
                        method.start_line,
                        method.end_line,
                        [
                            Evidence(
                                "方法访问外部类型成员明显多于本类成员。",
                                {
                                    "source": "javaparser-symbol-solver",
                                    "declaring_type": method.declaring_type,
                                    "dominant_external_type": dominant_type,
                                    "external_member_uses": external_total,
                                    "local_member_uses": local_total,
                                    "external_method_calls": len(external_calls),
                                    "external_field_accesses": len(external_fields),
                                },
                            )
                        ],
                        "Feature Envy 通常说明方法逻辑更依赖其它类型的数据或行为，可能导致职责放错位置。",
                        "优先评估 Move Method、Extract Method 或把外部数据访问封装到目标类型内。",
                        RefactoringType.MOVE_METHOD,
                        risk_level=RiskLevel.MEDIUM,
                    )
                )
        return issues

    def _scan_duplicate_code(self, analyses: list[JavaFileAnalysis]) -> list[RefactorIssue]:
        """规则：重复代码。当前实现不自己比文本，直接委托给 PMD CPD。"""
        del analyses  # 参数保留是为了和其他 _scan_* 方法签名一致。
        return self._scan_duplicate_code_with_cpd()

    def _scan_duplicate_code_with_cpd(self) -> list[RefactorIssue]:
        """调用 `mvn pmd:cpd-check`，解析输出里的 duplication 片段。

        约定：
        - returncode == 0：没发现重复，返回空列表
        - 非 0 且能解析出 duplication：正常产出 issue
        - 非 0 且解析不出：当成 CPD 执行失败，抛 CpdError
        """
        try:
            result = self._run(["mvn", "-q", "pmd:cpd-check"], self.root)
        except (FileNotFoundError, subprocess.SubprocessError, OSError) as err:
            raise CpdError(f"无法执行 PMD CPD: {err}") from err

        output = "\n".join(part for part in [result.stdout, result.stderr] if part)
        if result.returncode == 0:
            return []
        parsed = _parse_cpd_output(output, self.root)
        if not parsed:
            raise CpdError(f"PMD CPD 执行失败且未返回可解析结果: {output[-1000:]}")
        return parsed


def _issue(
    smell_type: SmellType,
    severity: Severity,
    file_path: str,
    symbol: str | None,
    start_line: int,
    end_line: int,
    evidence: list[Evidence],
    impact: str,
    recommendation: str,
    suggested_refactoring: RefactoringType,
    *,
    risk_level: RiskLevel,
) -> RefactorIssue:
    """构造临时 issue；id 先留空，最后在 scan() 里统一编成 RA-xxxx。"""
    return RefactorIssue(
        id="",
        type=smell_type,
        severity=severity,
        file_path=file_path,
        symbol=symbol,
        start_line=start_line,
        end_line=end_line,
        evidence=evidence,
        impact=impact,
        recommendation=recommendation,
        suggested_refactoring=suggested_refactoring,
        risk_level=risk_level,
    )


def _strip_comments_and_strings(lines: list[str]) -> list[str]:
    """去掉注释和字符串字面量内容，保留代码骨架。

    为什么要做这一步？
    注释/字符串里也可能出现 if、tmp、方法名等词，直接正则会误报。
    字符串内容统一替换成空格，尽量保持列位置大致可用。
    """
    sanitized: list[str] = []
    in_block_comment = False
    for line in lines:
        index = 0
        output = []
        in_string: str | None = None
        while index < len(line):
            current = line[index]
            next_char = line[index + 1] if index + 1 < len(line) else ""
            if in_block_comment:
                if current == "*" and next_char == "/":
                    in_block_comment = False
                    index += 2
                else:
                    index += 1
                continue
            if in_string:
                # 跳过转义字符（如 \"），避免提前结束字符串。
                if current == "\\":
                    index += 2
                    continue
                if current == in_string:
                    in_string = None
                output.append(" ")
                index += 1
                continue
            # 单行注释：后面整行丢掉。
            if current == "/" and next_char == "/":
                break
            # 块注释开始。
            if current == "/" and next_char == "*":
                in_block_comment = True
                index += 2
                continue
            # 进入字符串/字符字面量。
            if current in {'"', "'"}:
                in_string = current
                output.append(" ")
                index += 1
                continue
            output.append(current)
            index += 1
        sanitized.append("".join(output))
    return sanitized


def _same_or_nested_type(candidate: str, owner: str) -> bool:
    """判断 candidate 是否就是 owner，或是它的嵌套类型（如 Outer.Inner）。"""
    return bool(candidate and owner and (candidate == owner or candidate.startswith(owner + ".")))


def _parse_cpd_output(output: str, root: Path) -> list[RefactorIssue]:
    """把 PMD CPD 文本输出解析成 issue。

    CPD 输出大致长这样：
      Found a 12 line (xxx tokens) duplication...
      Starting at line 40 of D:\\proj\\A.java
      Starting at line 88 of D:\\proj\\B.java
    我们按「一段 duplication + 若干 location」聚合成一个 issue。
    """
    issues: list[RefactorIssue] = []
    current_lines = 0
    current_locations: list[tuple[str, int]] = []
    duplication_pattern = re.compile(r"Found a\s+(\d+)\s+line.*duplication", re.IGNORECASE)
    location_pattern = re.compile(r"Starting at line\s+(\d+)\s+of\s+(.+)", re.IGNORECASE)

    for raw_line in output.splitlines():
        line = raw_line.strip()
        duplication_match = duplication_pattern.search(line)
        if duplication_match:
            # 遇到下一段 duplication，先把上一段落盘。
            if current_locations:
                issues.append(_cpd_issue(current_lines, current_locations))
            current_lines = int(duplication_match.group(1))
            current_locations = []
            continue

        location_match = location_pattern.search(line)
        if location_match:
            file_path = Path(location_match.group(2).strip())
            try:
                relative = file_path.resolve().relative_to(root).as_posix()
            except (OSError, ValueError):
                # 解析相对路径失败时，退回原路径字符串，至少不丢这条证据。
                relative = file_path.as_posix()
            current_locations.append((relative, int(location_match.group(1))))

    if current_locations:
        issues.append(_cpd_issue(current_lines, current_locations))
    return issues


def _cpd_issue(line_count: int, locations: list[tuple[str, int]]) -> RefactorIssue:
    """把一处「重复代码块」变成 RefactorIssue；主定位取第一个出现位置。"""
    first_file, first_line = locations[0]
    return _issue(
        SmellType.DUPLICATE_CODE,
        Severity.MEDIUM if line_count < 40 else Severity.HIGH,
        first_file,
        None,
        first_line,
        first_line + max(line_count - 1, 0),
        [
            Evidence(
                "PMD CPD 发现重复代码。",
                {
                    "lines": line_count,
                    "duplicate_locations": [{"file": file_path, "start_line": line} for file_path, line in locations],
                    "source": "pmd-cpd",
                },
            )
        ],
        "重复代码会让缺陷修复和规则调整需要多处同步，容易出现行为漂移。",
        "优先考虑 Extract Method 或提取共享逻辑。",
        RefactoringType.REPLACE_DUPLICATE_LOGIC,
        risk_level=RiskLevel.MEDIUM,
    )
