## 亮点 1 代码映射

### 端到端调用链

```
scan 命令
  └─ commands.run_scan()
       ├─ ProjectDetector          # 校验 Maven / 可选安装 PMD plugin
       ├─ JavaSmellScanner.scan()  # 静态候选（高召回）
       │    ├─ JavaParserAnalyzer  # 调 JavaAstDump（AST + Symbol Solver）
       │    ├─ 七类规则 _scan_*
       │    └─ mvn pmd:cpd-check   # 重复代码
       ├─ bind_scan_analyses()     # 把 AST 快照交给 LLM 工具箱复用
       ├─ RefactorLlmAssistant.triage_issues()  # 语义精排
       │    ├─ toolbox.issue_context()
       │    ├─ 只读工具 read/search
       │    └─ accept / reject / uncertain
       └─ storage.save_scan_result / save_scan_audit
```

### 文件 ↔ 职责

| 环节 | 路径 | 关键符号 | 面试一句话 |
|---|---|---|---|
| 入口 | `suncli_py/refactor_agent/interface/commands.py` | `run_scan()` | 串起检测、PMD 安装确认、扫描、triage、落盘 |
| CLI | `suncli_py/refactor_agent/interface/cli.py` | `scan` subparser | 用户触发点 |
| 项目门禁 | `suncli_py/refactor_agent/analysis/project_detector.py` | `detect()` / `install_pmd_cpd_plugin()` | 确认 Git+Maven，必要时装 PMD CPD |
| 候选扫描 | `suncli_py/refactor_agent/analysis/scanner.py` | `JavaSmellScanner.scan()` | 七类坏味道规则，输出 `RefactorIssue` 列表 |
| AST 桥接 | `suncli_py/refactor_agent/analysis/java_ast.py` | `JavaParserAnalyzer.analyze_files()` | Python 调 Java helper，拿结构化 AST JSON |
| Java Helper | `.../java_ast_helper/.../JavaAstDump.java` | `main` / Symbol Solver 配置 | 真正跑 JavaParser + Symbol Solver |
| 上下文收集 | `suncli_py/refactor_agent/analysis/java_context.py` | `JavaContextCollector.collect()` | 给 LLM 局部源码、测试、调用方 |
| 工具箱 | `suncli_py/refactor_agent/assistant/toolbox.py` | `issue_context()` / `read_file` / `search_code` | triage 阶段只读工具 |
| Prompt | `suncli_py/refactor_agent/assistant/prompts.py` | `triage_system_prompt()` | 规定 accept 必须有源码证据 |
| LLM 精排 | `suncli_py/refactor_agent/assistant/llm_assistant.py` | `triage_issues()` / `bind_scan_analyses()` | 逐候选决策；默认 limit=20；仅 accept 进入结果 |
| 数据模型 | `suncli_py/refactor_agent/core/models.py` | `RefactorIssue` / `CandidateDecision` / `TriageResult` / `SmellType` | 候选与决策的结构化契约 |
| 落盘审计 | `suncli_py/refactor_agent/core/storage.py` | `save_scan_result()` / `save_scan_audit()` | `issues.json` + `candidates.json` + `decisions.json` |

### 七种坏味道 → scanner 方法

| 坏味道 | 方法 | 主要信号来源 |
|---|---|---|
| Long Method | `_scan_long_methods` | AST：行数 / branches / nesting（阈值约 >80 行或分支>12 或嵌套>4） |
| Large Class | `_scan_large_classes` | AST：类 LOC / method_count / field_count |
| Complex Condition | `_scan_complex_conditions` | AST 嵌套深度 + 条件表达式启发式 |
| Unclear Naming | `_scan_unclear_naming` | 弱命名启发式（如 tmp/data/handle/Manager） |
| Dead Code | `_scan_dead_code` | Symbol Solver 调用图；失败则 identifier-count fallback |
| Feature Envy | `_scan_feature_envy` | Symbol Solver：外部成员访问 vs 本类；要求 `symbol_resolved` |
| Duplicate Code | `_scan_duplicate_code_with_cpd` | `mvn -q pmd:cpd-check`，解析 CPD 输出 |

### 产出文件（scan 后）

| 文件 | 含义 |
|---|---|
| `.paicli/refactor-agent/candidates.json` | 静态阶段全部候选 |
| `.paicli/refactor-agent/decisions.json` | LLM 对每个候选的 accept/reject/uncertain |
| `.paicli/refactor-agent/issues.json` | 仅 accept 后的最终 issue（供后续 plan/apply） |

### 建议对照测试

| 测试文件 | 证明点 |
|---|---|
| `tests/test_refactor_agent_java_ast.py` | AST 行号/类指标；Feature Envy 依赖 Symbol Solver |
| `tests/test_refactor_agent_phase_two.py` | 七类候选都能被 scanner 打出 |
| `tests/test_refactor_agent_llm_agent.py` | triage accept/reject；accept 必须有 evidence |
| `tests/test_refactor_agent_java_context.py` | triage 复用 scan 阶段 AST 快照，避免重复解析 |
| `tests/test_refactor_agent_phase_one.py` | 非 Maven / PMD plugin 安装确认等门禁 |

### 面试指文件顺序（2 分钟）

1. `commands.py` → `run_scan`：先讲流水线  
2. `scanner.py` → `scan` + `_scan_feature_envy` / `_scan_duplicate_code_with_cpd`：讲静态召回  
3. `JavaAstDump.java`：讲 AST 与 Symbol Solver 在哪落地  
4. `llm_assistant.py` → `triage_issues`：讲精排、limit=20、只留 accept  
5. `storage.py`：讲 candidates/decisions/issues 三份审计产物  

---

## 亮点 2 代码映射

> 对应表述：拆分测试、修改和验证 Agent，隔离提示词与对话；修改前通过 JaCoCo 检查覆盖，必要时生成行为锁定测试；验证失败反馈修改 Agent 重试。

### 端到端调用链

```
apply 命令
  └─ commands.run_apply()
       └─ RefactorAgentOrchestrator.run()            # 串行编排，Agent 不共享会话
            ├─ ensure_initial_snapshot()             # 生产代码修改前先保存不可变快照
            ├─ PreModificationVerifier.verify()
            │    ├─ mvn compile
            │    ├─ JaCoCo prepare-agent test + report
            │    └─ CoverageAnalyzer.assess()        # 判断目标文件/目标行是否有覆盖
            ├─ [覆盖不足] TestGeneratorAgent.run()
            │    ├─ 独立 ReactAgent + test-generator prompt
            │    ├─ inspect_test_conventions
            │    ├─ apply_test_edits                 # 只能新建白名单测试文件
            │    └─ run_generated_test_precheck
            │         ├─ test-compile
            │         ├─ mvn test
            │         ├─ JaCoCo test + report
            │         └─ 要求目标源码确实产生覆盖
            └─ attempt 1..N
                 ├─ ModifierAgent.run()
                 │    └─ 独立 ReactAgent + modifier prompt
                 ├─ VerifierAgent.run()
                 │    └─ 每轮新建 ReactAgent + verifier prompt
                 ├─ approved → 成功
                 └─ rejected
                      ├─ VerificationResult 作为下一轮 modifier 的 feedback
                      ├─ 先恢复生产文件，保留已验证的行为锁定测试
                      └─ 进入下一轮修改
```

### 文件 ↔ 职责

| 环节 | 路径 | 关键符号 | 面试一句话 |
|---|---|---|---|
| apply 入口 | `suncli_py/refactor_agent/interface/commands.py` | `run_apply()` | 加载计划、用户确认，然后进入多 Agent 闭环 |
| 总编排 | `suncli_py/refactor_agent/assistant/orchestrator.py` | `RefactorAgentOrchestrator.run()` | 固定执行 preflight → test-generator → modifier → verifier，失败有界重试 |
| 修改前门禁 | `suncli_py/refactor_agent/execution/verifier.py` | `PreModificationVerifier.verify()` | 原代码编译/测试不通过就禁止修改；JaCoCo 覆盖不足则要求补测试 |
| 覆盖分析 | `suncli_py/refactor_agent/analysis/coverage.py` | `CoverageAnalyzer.assess()` | 读取 JaCoCo XML，计算目标文件和目标区域覆盖 |
| 测试 Agent | `suncli_py/refactor_agent/assistant/test_agent.py` | `TestGeneratorAgent` / `TestGeneratorToolRuntime` | 只负责生成行为锁定测试并强制预检，不碰生产代码 |
| 测试文件门禁 | `suncli_py/refactor_agent/execution/test_generator.py` | `GeneratedTestFileManager` | 限制新测试路径、拒绝覆盖已有文件、记录测试哈希 |
| 修改 Agent | `suncli_py/refactor_agent/assistant/agents.py` | `ModifierAgent` / `ModifierToolRuntime` | 读取计划和验证反馈，通过 `apply_edits` 做受控修改 |
| 验证 Agent | `suncli_py/refactor_agent/assistant/agents.py` | `VerifierAgent` / `VerifierToolRuntime` | 独立读取真实 Diff、命令结果、覆盖和工作区证据 |
| Prompt 隔离 | `suncli_py/refactor_agent/assistant/prompts.py` | `test_generator_agent_system_prompt()` / `modifier_agent_system_prompt()` / `verifier_agent_system_prompt()` | 三个角色有不同职责、工具权限和系统提示词 |
| ReAct 会话 | `suncli_py/refactor_agent/assistant/react.py` | `ReactAgent` | 每个 Agent 实例维护自己的 history；验证 Agent 每轮重新创建 |
| 结果契约 | `suncli_py/refactor_agent/core/models.py` | `PreModificationResult` / `VerificationResult` | Agent 之间传结构化结果，不共享自由对话 |

### Agent 隔离具体落点

| Agent | 可写范围 | 成功硬条件 |
|---|---|---|
| Test Generator | 仅 `allowed_new_test_files`，不能覆盖已有测试或生产文件 | 最新测试版本编译、测试、JaCoCo 均成功且目标文件有覆盖 |
| Modifier | 仅 `plan.files_to_modify` | 必须成功调用一次 `apply_edits` |
| Verifier | 不允许编辑；仅验证命令会产生构建产物 | compile/test、真实 Diff、覆盖检查都已执行，且无硬失败 |

### 产出文件（apply 任务目录）

| 文件 | 含义 |
|---|---|
| `pre_modification.json` | 修改前编译、测试和覆盖基线 |
| `preflight/test_generator.json` | 测试 Agent 决策、工具轨迹、命令和覆盖证据 |
| `generated_test_files.json` | 自动生成测试的路径和哈希，后续作为 guard |
| `attempts/NN/modifier.json` | 第 N 轮修改 Agent 的决策和工具轨迹 |
| `attempts/NN/verifier.json` | 第 N 轮验证 Agent 的决策和工具轨迹 |
| `attempts/NN/feedback.json` | 验证失败后反馈给下一轮修改 Agent 的结构化消息 |
| `agent_messages.jsonl` | 跨 Agent 的任务/结果/反馈审计日志 |

### 建议对照测试

| 测试文件 | 证明点 |
|---|---|
| `tests/test_refactor_agent_pre_modification.py` | 覆盖不足触发测试生成；测试 guard 防篡改；修复轮次保留生成测试 |
| `tests/test_refactor_agent_multi_agent.py` | modifier/verifier 分工、验证反馈进入下一轮、重试耗尽回滚 |
| `tests/test_refactor_agent_phase_five_six.py` | Maven + JaCoCo 验证、真实 Diff 和工作区检查 |

### 表述边界

- “测试、修改和验证 Agent”在代码中分别是 `TestGeneratorAgent`、`ModifierAgent`、`VerifierAgent`。
- 测试 Agent 不是每次都运行：仅修改前覆盖不足时触发。
- 首次 JaCoCo 基线由机器层 `PreModificationVerifier` 执行；测试 Agent 只在覆盖不足时启动，并在生成测试后再次执行 JaCoCo 预检。
- 三个角色之间不共享完整 ReAct 对话，但同一个 `ModifierAgent` 会跨 repair 轮次复用自己的 history；当前没有调用 `reset_history()`，因此不应表述为“所有轮次的对话记录完全隔离”。
- preflight 或验证基础设施错误会直接回滚，不进入修复重试；只有正常的 verifier rejection 才反馈给 Modifier。
- “Pass@1 75.0% → 88.3%”是项目内 benchmark 数据，代码只实现机制；仓库中没有独立 benchmark 脚本或原始结果文件可复算该数字。

### 面试指文件顺序（2 分钟）

1. `orchestrator.py` → `run`：先画三 Agent 串行闭环  
2. `verifier.py` → `PreModificationVerifier.verify`：讲修改前覆盖门禁  
3. `test_agent.py` → `_run_precheck`：讲测试生成后的强制验证  
4. `agents.py` → `ModifierAgent.run` / `VerifierAgent.run`：讲提示词、工具和 history 隔离  
5. `orchestrator.py` → verifier rejection 分支：讲反馈、恢复、重试  

---

## 亮点 3 代码映射

> 对应表述：LLM 只输出结构化行级编辑；应用前校验白名单、路径和行号；应用后通过 JavaParser 检查语法结构及公开 API，失败立即恢复。

### 端到端调用链

```
ModifierAgent.run()
  └─ ReactAgent（modifier prompt）
       └─ apply_edits({
            edits: [{
              file_path,
              start_line,
              end_line,
              replacement
            }],
            explanation
          })
            └─ ModifierToolRuntime.execute()
                 ├─ RefactorPatcher.generate_changes()
                 │    ├─ _validate_plan()             # issue 文件必须属于计划白名单
                 │    └─ _changes_from_llm_edits()
                 │         ├─ 文件必须在 files_to_modify
                 │         ├─ 路径必须位于项目根目录
                 │         └─ 行号必须落在当前文件范围内
                 └─ RefactorPatcher.apply_changes()
                      ├─ ensure_initial_snapshot()
                      ├─ 写入全部计划内变更
                      ├─ 回读内容一致性检查
                      ├─ AstPatchValidator.validate()
                      │    ├─ 修改前后都重新跑 JavaParser
                      │    ├─ 比较类声明集合
                      │    └─ 比较非 private 方法签名集合
                      └─ 任一步失败 → 写回 before_text
```

### 文件 ↔ 职责

| 环节 | 路径 | 关键符号 | 面试一句话 |
|---|---|---|---|
| 结构化工具协议 | `suncli_py/refactor_agent/assistant/agents.py` | `ModifierToolRuntime.schemas()` | `apply_edits` schema 强制每项包含路径、起止行和 replacement |
| Prompt 约束 | `suncli_py/refactor_agent/assistant/prompts.py` | `BASE_AGENT_RULES` / `modifier_agent_system_prompt()` | 禁止直接写文件，要求最小修改、白名单和 JSON 输出 |
| 修改适配 | `suncli_py/refactor_agent/assistant/agents.py` | `ModifierToolRuntime.execute()` | LLM 只能通过受控工具把编辑计划交给 patcher |
| 计划白名单 | `suncli_py/refactor_agent/execution/patcher.py` | `_validate_plan()` / `_changes_from_llm_edits()` | issue 和每条 edit 都必须属于 `plan.files_to_modify` |
| 路径边界 | `suncli_py/refactor_agent/execution/patcher.py` | `_resolve_allowed_file()` | 拒绝绝对路径、忽略目录、越出 root 和不存在文件 |
| 行号边界 | `suncli_py/refactor_agent/execution/patcher.py` | `_changes_from_llm_edits()` | 校验 `1 <= start <= end <= 文件总行数` |
| 事务式应用 | `suncli_py/refactor_agent/execution/patcher.py` | `apply_changes()` | 先快照、写入、回读、AST 验证；异常写回原文 |
| AST/API 门禁 | `suncli_py/refactor_agent/execution/patch_validator.py` | `AstPatchValidator.validate()` / `_compare_ast_shape()` | JavaParser 验证可解析，并拒绝意外类声明或非 private 签名变化 |
| AST 实现 | `suncli_py/refactor_agent/analysis/java_ast.py` | `JavaParserAnalyzer.analyze_files()` | Python 调 Java helper，获取真实 AST 结构 |

### 三层防线

| 防线 | 阻止的问题 | 代码依据 |
|---|---|---|
| Schema | LLM 返回自由文本补丁、缺少必要字段 | `apply_edits` JSON tool schema |
| Patcher | 越白名单、路径穿越、错误行号 | `_validate_plan` / `_resolve_allowed_file` / `_changes_from_llm_edits` |
| AST Validator | 写出不可解析 Java、意外改类声明或外部可见方法签名 | `AstPatchValidator` |

### 产出文件

| 文件 | 含义 |
|---|---|
| `snapshot.json` + `before/` | 修改前不可变快照与 SHA-256 |
| `attempts/NN/patch.diff` | 本轮受控编辑生成的真实 unified diff |
| `after/` + `after_state.json` | 成功应用后的文件副本与 SHA-256 |
| `patch.diff` / `diff_summary.txt` | 任务级最终 Diff |

### 建议对照测试

| 测试文件 | 证明点 |
|---|---|
| `tests/test_refactor_agent_phase_four.py` | LLM 修改计划外文件会被拒绝；受控 patch 应用 |
| `tests/test_refactor_agent_multi_agent.py` | Modifier 必须实际调用 `apply_edits`；失败进入回滚 |
| `tests/test_refactor_agent_pre_modification.py` | 生成测试的独立写入白名单和防篡改 |

### 表述边界

- “公开 API”在当前实现中准确说是“**非 private 方法签名**”；它比 Java 的 `public` API 范围更宽，会同时保护 package-private 和 protected 方法。
- AST 门禁比较类声明和非 private 方法签名，不是完整二进制兼容性分析；字段、注解、异常声明等并未全部纳入 API diff。
- `apply_changes()` 在单次补丁失败时用内存里的 `before_text` 立即恢复已写文件；整个任务最终失败则由 `TaskRollbacker` 从快照恢复。

### 面试指文件顺序（2 分钟）

1. `agents.py` → `ModifierToolRuntime.schemas`：先展示结构化行级编辑契约  
2. `patcher.py` → `_changes_from_llm_edits`：讲白名单、路径、行号  
3. `patcher.py` → `apply_changes`：讲事务式写入与失败恢复  
4. `patch_validator.py` → `_compare_ast_shape`：讲 AST 和非 private 签名门禁  

---

## 亮点 4 代码映射

> 对应表述：验证 Agent 结合重构计划与真实 Diff，执行 Maven/JaCoCo 和工作区哈希检查；失败反馈修改 Agent，多轮仍失败则恢复全部文件。

### 端到端调用链

```
ModifierAgent 完成受控编辑
  └─ Orchestrator._publish_combined_diff()
       └─ VerifierAgent.run(plan, issue, task_dir, attempt)
            ├─ run_verification_command()
            │    ├─ mvn -q -DskipTests compile
            │    ├─ jacoco:prepare-agent test
            │    └─ jacoco:report
            ├─ inspect_diff()
            │    └─ 从 snapshot/before 与当前工作区重建真实 Diff
            ├─ get_coverage_assessment()
            │    └─ 读取 JaCoCo 覆盖
            ├─ inspect_workspace()
            │    └─ 对比修改前后全工作区 SHA-256 manifest
            └─ LLM 基于 plan + issue + 真实证据给 approved/status
                 ├─ approved → 返回成功
                 └─ rejected
                      ├─ 生成 feedback.json
                      ├─ 下一轮前回滚生产文件
                      ├─ ModifierAgent 接收 verification_feedback 重修
                      └─ 重试耗尽 → _final_rollback() 恢复生产文件并删除生成测试
```

### 文件 ↔ 职责

| 环节 | 路径 | 关键符号 | 面试一句话 |
|---|---|---|---|
| 验证 Agent | `suncli_py/refactor_agent/assistant/agents.py` | `VerifierAgent.run()` | 把计划、issue 和强制证据工具交给独立 LLM 判断 |
| 证据门禁 | `suncli_py/refactor_agent/assistant/agents.py` | `VerifierToolRuntime.evidence_error()` | 没跑完必需命令、Diff 或覆盖检查，最终 JSON 不会被接受 |
| 硬失败规则 | `suncli_py/refactor_agent/assistant/agents.py` | `_hard_verification_issues()` | compile/test 失败或静态检查问题不能被 LLM 强行批准 |
| Maven 验证 | `suncli_py/refactor_agent/execution/verifier.py` | `DEFAULT_VERIFICATION_COMMANDS` / `run_command()` | 不经过 shell，执行白名单 Maven 命令并记录输出 |
| 真实 Diff | `suncli_py/refactor_agent/execution/verifier.py` | `_actual_workspace_diff()` | 不信 modifier 摘要，按初始快照重建当前工作区 Diff |
| 工作区完整性 | `suncli_py/refactor_agent/execution/workspace.py` | `capture_workspace_manifest()` | 对非生成目录文件逐个计算 SHA-256 |
| 越界检查 | `suncli_py/refactor_agent/execution/verifier.py` | `_workspace_findings()` | 修改前后 manifest 对比，计划外变化直接形成 finding |
| 闭环编排 | `suncli_py/refactor_agent/assistant/orchestrator.py` | `run()` | verifier rejection 写成 feedback，恢复后交给 modifier 重试 |
| 最终回滚 | `suncli_py/refactor_agent/assistant/orchestrator.py` | `_final_rollback()` | 重试耗尽或基础设施错误时调用任务级回滚 |
| 快照回滚 | `suncli_py/refactor_agent/execution/rollback.py` | `TaskRollbacker.rollback()` | 校验冲突后恢复 before 副本，并删除本任务生成的测试 |

### 验证证据与裁决关系

| 证据 | 是否强制 | 失败影响 |
|---|---|---|
| Maven compile | 是 | 硬失败，不可批准 |
| JaCoCo instrumented test | 是 | 硬失败，不可批准 |
| JaCoCo report | 是 | 命令需执行；报告失败通常形成 warning/覆盖问题 |
| 真实 Diff | 是 | 无实际计划内变化或快照异常形成静态 finding |
| 工作区 SHA-256 | 最终裁决前自动检查 | 计划外变化形成硬问题 |
| LLM 语义判断 | 是 | 判断坏味道是否消除、计划是否满足，并给修复建议 |

### 产出文件

| 文件 | 含义 |
|---|---|
| `snapshot.json` | 初始 Git 状态、计划文件哈希、全工作区 manifest |
| `attempts/NN/verification.json` | 每轮完整验证结果 |
| `attempts/NN/feedback.json` | 拒绝原因和下一轮修复依据 |
| `verification.json` | 最新一轮验证结果 |
| `rollback.json` | 最终回滚状态、恢复文件和冲突 |
| `reports/latest.md` | 汇总计划、验证、回滚、测试 guard 的最终报告 |

### 建议对照测试

| 测试文件 | 证明点 |
|---|---|
| `tests/test_refactor_agent_phase_five_six.py` | Maven/JaCoCo、真实 Diff、计划外工作区修改拒绝、回滚冲突 |
| `tests/test_refactor_agent_multi_agent.py` | verifier 不能批准越界变化；反馈重试；耗尽后恢复不可变快照 |
| `tests/test_refactor_agent_pre_modification.py` | 最终拒绝同时回滚生产代码和生成测试 |

### 表述边界

- 默认验证不是单独的 `mvn test` 字符串，而是 `compile`、`JaCoCo prepare-agent test`、`JaCoCo report` 三步；其中第二步实际执行测试。
- “坏味道是否消除”主要由 Verifier Agent 基于计划和 Diff 语义判断，当前代码没有在 apply 后重新跑完整七类 scanner 做机械对比。

### 面试指文件顺序（2 分钟）

1. `agents.py` → `VerifierToolRuntime.schemas/evidence_error`：讲强制证据  
2. `verifier.py` → `_actual_workspace_diff`：讲为什么不信 modifier 摘要  
3. `workspace.py` → `capture_workspace_manifest`：讲全工作区哈希  
4. `orchestrator.py` → rejection/feedback 分支：讲循环修复  
5. `rollback.py` → `rollback`：讲多轮失败后的完整恢复  

---

## 亮点 5 代码映射

> 对应表述：PAI.md 项目记忆、项目/全局长期记忆和会话历史；按当前问题检索注入上下文，并压缩早期对话。

### 端到端调用链

```
refactor-agent chat
  └─ RefactorChatSession._run_assistant(user_input)
       ├─ MemoryManager.add_user_message()           # 短期会话记忆
       ├─ MemoryManager.prompt_context(query)
       │    ├─ ProjectMemoryLoader.load_for_prompt()
       │    │    ├─ ~/.paicli-py/PAI.md
       │    │    ├─ <repo>/PAI.md
       │    │    ├─ <repo>/.paicli/PAI.md
       │    │    └─ PAI.local.md 及安全 @import
       │    └─ MemoryRetriever.build_context()
       │         └─ 从长期记忆按 query 词匹配 + 时间衰减取 Top 10
       ├─ 把 PAI.md + 相关长期记忆拼入 system prompt
       ├─ compact_short_term_if_needed()
       ├─ compact_history_if_needed(history)
       │    └─ 压缩旧轮次，保留最近 3 个用户轮次
       ├─ _chat_with_tools()
       └─ add_assistant_message()

scan / plan / modifier / verifier / test-generator 等 ReAct 任务
  └─ ReactAgent.run_json(task)
       ├─ MemoryManager.prompt_context(task)
       │    ├─ 全量加载 PAI.md
       │    └─ 按任务 JSON 中的关键词检索长期记忆
       ├─ 把记忆追加到该 Agent 的 system prompt
       ├─ compact_history_if_needed(history)
       └─ 执行独立 ReAct 会话
```

### 文件 ↔ 职责

| 环节 | 路径 | 关键符号 | 面试一句话 |
|---|---|---|---|
| Chat 入口 | `suncli_py/refactor_agent/interface/chat.py` | `RefactorChatSession._run_assistant()` | 每轮先检索记忆、组装 system prompt，再压缩和调用 LLM |
| 统一门面 | `suncli_py/memory/manager.py` | `MemoryManager` | 组合短期、长期、PAI.md、检索和压缩 |
| 查询检索 | `suncli_py/memory/manager.py` | `MemoryRetriever` | 中英文 token 匹配、项目可见性过滤、简单时间衰减和预算截断 |
| PAI.md | `suncli_py/memory/project.py` | `ProjectMemoryLoader` | 按固定优先级加载全局/项目/本地 PAI.md，限制导入深度和路径 |
| PAI 初始化 | `suncli_py/memory/project.py` | `ProjectMemoryInitializer` | `/init` 生成项目记忆模板，默认不覆盖 |
| 会话短期记忆 | `suncli_py/memory/storage.py` | `ConversationMemory` | 内存保存本会话消息和截断后的工具结果 |
| 长期持久化 | `suncli_py/memory/storage.py` | `LongTermMemory` | JSON 持久化，RLock 保护，区分 global/project scope |
| 压缩 | `suncli_py/memory/compression.py` | `ContextCompressor` / `ConversationHistoryCompactor` | 摘要保留目标、关键操作、结论和未解决问题 |
| 用户命令 | `suncli_py/refactor_agent/interface/chat.py` | `/memory` / `/save` / `/init` / `/compact` / `/clear` | 显式管理记忆和会话历史 |
| 独立 CLI | `suncli_py/memory/commands.py` | `run_memory()` / `run_save()` / `run_init()` | 非 chat 模式也可查看和维护长期记忆 |
| ReAct 注入 | `suncli_py/refactor_agent/assistant/react.py` | `ReactAgent.run_json()` | triage/plan/modifier/verifier/test-generator 启动时也注入 PAI.md 和相关长期记忆 |
| LLM 阶段桥接 | `suncli_py/refactor_agent/assistant/llm_assistant.py` | `_chat_json()` / `_memory_managers` | 为 triage、plan、explain 等任务复用按项目缓存的 MemoryManager |

### 三层记忆

| 层 | 生命周期 | 存储位置 | 注入方式 |
|---|---|---|---|
| PAI.md 项目记忆 | 跟随项目文件长期存在 | 仓库/用户目录中的 `PAI*.md` | 每轮完整加载，受 24,000 字符预算限制 |
| 长期记忆 | 跨会话 | `~/.paicli-py/memory/long_term_memory.json`，或 `PAICLI_PY_MEMORY_DIR` | 按当前 query 检索后注入 |
| 会话 history | 当前进程会话 | Chat/Agent 内存中的 `list[Message]` | 直接作为 LLM messages；达到阈值后摘要旧轮次 |
| 短期影子缓冲 | 当前进程会话 | `ConversationMemory` | 记录 user/assistant/tool 并供 `ContextCompressor` 摘要，但摘要结果不会由 `prompt_context()` 注入模型 |

### 关键安全与预算规则

| 规则 | 实现 |
|---|---|
| 项目记忆 import 防越界 | 绝对路径和 `..` 拒绝，最多递归 3 层，检测循环 |
| 项目/全局隔离 | project scope 记录绝对项目 key；读取时只返回当前项目 + global |
| 工具结果防膨胀 | 单条只保留最多 500 字符进入短期记忆 |
| 历史压缩 | 超 token 阈值自动压缩旧消息，保留最近 3 个用户轮次 |
| 摘要约束 | Prompt 明确保留目标、操作、结论、待办/未解决问题，不新增事实 |

### 建议对照测试

| 测试文件 | 证明点 |
|---|---|
| `tests/test_memory.py` | 长期记忆持久化与 scope、相关记忆注入、PAI import 安全、历史压缩边界 |
| `tests/test_refactor_agent_chat.py` | Chat 命令、自然语言工具调用和 memory 管理入口 |

当前测试缺口：没有直接覆盖 `ContextCompressor` / `compact_short_term_if_needed()`，也没有覆盖 ReAct 路径的 PAI.md + 长期记忆端到端注入。

### 表述边界

- 更准确的说法是“**PAI.md + 带项目/全局 scope 的长期记忆 + 会话 history**”。`ConversationMemory` 是辅助压缩的影子缓冲，不是额外注入模型的知识源。
- 检索是词项匹配和简单时间衰减，不是向量数据库或 embedding 语义检索。
- 自动压缩保留“用户目标、关键操作、结论、未解决问题”；提示词没有单独的结构化 Todo 字段。
- PAI.md 每轮全量加载；只有长期记忆按当前 query/task 检索，因此“按任务检索三层记忆”并不准确。
- PAI.md 和长期记忆也会注入基于 `ReactAgent.run_json()` 的 triage/plan/modifier/verifier/test-generator；纯静态 scanner、Maven、patcher 和 rollback 不使用 LLM 记忆。
- 只有 PAI.md 和长期 JSON 能跨会话；Chat/Agent history 与 `ConversationMemory` 都不落盘。
- 长期记忆不会从普通对话自动抽取，必须通过 `/save`、CLI `save` 或显式 `save_memory` 写入。

### 面试指文件顺序（2 分钟）

1. `chat.py` → `_run_assistant`：讲每轮记忆注入时机  
2. `react.py` → `run_json`：讲 ReAct Agent 也注入记忆  
3. `manager.py` → `prompt_context`：讲 PAI.md 全量 + 长期记忆 query 检索  
4. `project.py` → `_sources/_read_with_imports`：讲项目记忆与安全 import  
5. `storage.py` → `LongTermMemory.is_visible`：讲项目/全局 scope  
6. `compression.py` → `ConversationHistoryCompactor._compact`：讲保留最近轮次的摘要替换  

---

## 亮点 6 代码映射

> 对应表述：同一轮多个只读工具通过 AsyncIO 并行执行；只要含写操作就整批串行。

### 端到端调用链

```
LLM 一轮返回多个 tool_calls
  └─ ReactAgent async 执行循环
       └─ _execute_tool_calls(tools, calls, iteration)
            ├─ calls > 1 且全部 tools.is_read_only(name)
            │    └─ asyncio.gather(
            │         _execute_one(call A),
            │         _execute_one(call B),
            │         ...
            │       )
            │         └─ asyncio.to_thread(tools.execute, ...)
            └─ 否则
                 └─ for call in calls:
                      await _execute_one(...)          # 严格串行
```

### 文件 ↔ 职责

| 环节 | 路径 | 关键符号 | 面试一句话 |
|---|---|---|---|
| 并行调度 | `suncli_py/refactor_agent/assistant/react.py` | `_execute_tool_calls()` | 只有“多个且全部只读”才 `asyncio.gather`，否则按返回顺序逐个 await |
| 阻塞转异步 | `suncli_py/refactor_agent/assistant/react.py` | `_execute_one()` | 同步文件/工具调用通过 `asyncio.to_thread` 放入线程执行 |
| 工具协议 | `suncli_py/refactor_agent/assistant/react.py` | `ReactToolRuntime.is_read_only()` | runtime 必须显式声明工具是否只读 |
| 通用工具分类 | `suncli_py/refactor_agent/assistant/toolbox.py` | `RefactorAgentToolRuntime.is_read_only()` | `read_file/search_code/get_*` 等仓库查询工具为只读 |
| Modifier 分类 | `suncli_py/refactor_agent/assistant/agents.py` | `ModifierToolRuntime.is_read_only()` | `apply_edits` 明确为写操作 |
| Test Agent 分类 | `suncli_py/refactor_agent/assistant/test_agent.py` | `TestGeneratorToolRuntime.is_read_only()` | `apply_test_edits` 和 `run_generated_test_precheck` 串行 |
| Verifier 分类 | `suncli_py/refactor_agent/assistant/agents.py` | `VerifierToolRuntime.is_read_only()` | Maven 验证命令视为非只读，避免并发执行构建 |
| Prompt 配合 | `suncli_py/refactor_agent/assistant/prompts.py` | `triage_system_prompt()` | 提醒模型把相互独立的证据工具放在同一轮返回 |
| 计时审计 | `suncli_py/refactor_agent/assistant/react.py` | `ToolTrace.duration_ms` + logger | 记录单工具耗时和整批工具耗时，便于 benchmark |

### 并行判定表

| 同一轮调用 | 执行方式 | 原因 |
|---|---|---|
| `read_file` + `search_code` | 并行 | 全部只读且互不要求状态顺序 |
| 两个 `read_file` | 并行 | 全部只读 |
| `read_file` + `apply_edits` | 串行 | 批次含写操作 |
| `apply_test_edits` + precheck | 串行 | 后者依赖前者写入结果 |
| 两个 Maven 验证命令 | 串行 | 避免并发写 `target/` 和报告 |

### 建议对照测试

| 测试文件 | 证明点 |
|---|---|
| `tests/test_refactor_agent_llm_agent.py` | `test_llm_executes_multiple_tool_calls_in_parallel` 验证两个只读调用进入不同线程并并发完成 |
| `tests/test_refactor_agent_multi_agent.py` | 各 Agent runtime 的工具权限和写操作闭环 |

### 表述边界

- 并行发生在 **同一轮 LLM 返回的多个 tool call** 之间，不是让 scan/plan/apply 这些阶段并行。
- 当前策略是保守的“全有或全无”：一批调用只要包含一个写/命令工具，整批都串行；不会把其中只读子集单独并行。
- `asyncio.gather` 调度协程，实际同步工具由 `asyncio.to_thread` 在线程中运行。
- “平均耗时降低 57.6%”是项目内对照 benchmark；仓库代码有逐工具 `duration_ms` 和批次计时，但没有独立原始 benchmark 数据文件可直接复算该百分比。

### 面试指文件顺序（1 分钟）

1. `react.py` → `_execute_tool_calls`：展示 `all(is_read_only)` 分流  
2. `react.py` → `_execute_one`：展示 `asyncio.to_thread` 与耗时记录  
3. `agents.py` / `test_agent.py` → `is_read_only`：说明写操作为何保持串行  
4. `test_refactor_agent_llm_agent.py` → 并行测试：证明不是只写在设计文档里  
