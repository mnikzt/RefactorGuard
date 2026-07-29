# Refactor Agent 主线逐字稿：scan → plan → apply

怎么用：先背「总开场 + 三层数据流」，再按层练。每层都按同一骨架讲：问题 → 为什么 → 怎么落代码 → 数据长什么样 → 指标 → 边界 → 失败怎么处理 → 后续怎么改。

配套资料：
- 代码跳转：`codemapping.md`
- 追问弹药：`qa.md`（不必全背）

---

## 0. 总开场（40 秒）

这个项目的核心不是让 LLM 直接改代码，而是把 LLM 放进一条受控流水线。

整条链路只有三步：

1. **scan**：静态分析高召回找候选，LLM 做语义 triage，产出结构化 issue  
2. **plan**：围绕单个 issue 生成可审查的重构计划，明确白名单、风险和验证方式  
3. **apply**：先保快照、再补覆盖、再受控修改、再独立验证；失败就反馈修复，耗尽就回滚  

一句话说：静态分析缩小搜索空间，LLM 做语义判断，多 Agent 和本地校验把不可控输出变成可审计、可恢复的修改。

```text
Java Maven Git 项目
  → scan:  Profile + Candidates + Decisions + Issues
  → plan:  Issue → RefactorPlan + task 目录
  → apply: Snapshot → Preflight → [TestAgent] → Modifier ⇄ Verifier → Report / Rollback
```

---

## 1. 层一：scan —— 先发现问题，再决定值不值得改

### 1.1 这一层解决什么问题

纯靠 LLM 全仓搜索有三个痛点：

1. 工具轮次多，耗时长  
2. 大段源码反复进上下文，Token 贵  
3. 每次覆盖集合不稳定，召回不可解释  

所以 scan 的目标不是“立刻改对”，而是：用确定性工具先捞出可疑点，再用 LLM 结合局部源码判断哪些值得进入后续重构。

### 1.2 为什么这么做

坏味道很多是结构信号：方法多长、嵌套多深、调用归属谁、有没有复制粘贴。这类信号用 AST、规则、CPD 更稳，也好解释。

我的分工是：

- 静态扫描偏**召回**：尽量别漏  
- LLM triage 偏**精确**：没源码证据不 accept  
- uncertain 不进后续 plan/apply，避免“逃避决策还继续改”

漏报后面很难补，因为我们不会再让 LLM 全仓盲搜；误报可以在 triage 砍掉。

### 1.3 关键机制怎么落代码

入口：`commands.run_scan()`

```text
run_scan()
  ├─ ProjectDetector.detect()
  │    └─ 必须是 Git + Maven；可选安装 maven-pmd-plugin
  ├─ JavaSmellScanner.scan()
  │    ├─ 收集 *.java（跳过 target/.git 等）
  │    ├─ JavaParserAnalyzer → JavaAstDump（AST + Symbol Solver）
  │    ├─ 七类规则 _scan_*
  │    └─ mvn -q pmd:cpd-check（重复代码）
  ├─ assistant.bind_scan_analyses()   # AST 快照复用，避免 triage 再解析一遍
  ├─ assistant.triage_issues()        # 默认深挖前 20 个候选
  │    └─ accept / reject / uncertain
  └─ storage.save_scan_result / save_scan_audit
```

七类坏味道与信号：

| 坏味道 | 主要信号 | 代码 |
|---|---|---|
| Long Method | 行数 / 分支 / 嵌套 | `_scan_long_methods` |
| Large Class | 类 LOC / 方法数 / 字段数 | `_scan_large_classes` |
| Complex Condition | 嵌套深度 + `&&/||` 数量 | `_scan_complex_conditions` |
| Unclear Naming | Manager/tmp/handle 等启发式 | `_scan_unclear_naming` |
| Dead Code | Symbol Solver 调用图；失败则名字计数 fallback | `_scan_dead_code` |
| Feature Envy | 外部成员访问明显多于本类 | `_scan_feature_envy` |
| Duplicate Code | PMD CPD 输出解析 | `_scan_duplicate_code_with_cpd` |

只读工具并行也在这一层吃到：triage 同一轮多个 `read_file` / `search_code`，会走 `react.py` 的 AsyncIO 并行。

### 1.4 数据流转形态（必须说清楚）

**输入**

- 当前目录：Java Maven Git 项目
- LLM 配置：`.env` / `~/.paicli/config.json`

**中间数据**

1. `ProjectProfile`：是不是 Git/Maven、有没有主源码、工作区是否干净、有没有 PMD 插件  
2. 静态候选：临时 `RefactorIssue`，此时 `id=""`，后面统一编号  
3. AST 快照：`scanner.ast_analyses`，给 triage 工具箱复用  

**候选 Issue 长什么样**

```json
{
  "id": "RA-0001",
  "type": "long_method",
  "severity": "medium",
  "file_path": "src/main/java/.../OrderService.java",
  "symbol": "checkout",
  "start_line": 40,
  "end_line": 180,
  "evidence": [{"message": "...", "metrics": {"lines": 141, "branches": 15}}],
  "suggested_refactoring": "Extract Method",
  "risk_level": "medium"
}
```

**LLM 决策长什么样**

```json
{
  "candidate_id": "RA-0001",
  "status": "accept",
  "confidence": 0.86,
  "reason": "...",
  "source_evidence": [{"file_path": "...", "start_line": 40, "end_line": 80}]
}
```

**落盘产物（目标项目内）**

| 文件 | 内容 |
|---|---|
| `.paicli/refactor-agent/candidates.json` | 静态阶段全部候选 |
| `.paicli/refactor-agent/decisions.json` | 每个候选的 accept/reject/uncertain |
| `.paicli/refactor-agent/issues.json` | 仅 accept 后的最终 issue，供 plan/apply |

排序后统一编号：`RA-0001`、`RA-0002`……

### 1.5 指标怎么测

简历数字：耗时降低约 **56.5%**，Token 降低约 **67.5%**。

口径要讲清：

- 样本：4 个 Java Maven 项目，约 20 个坏味道识别任务  
- Baseline：LLM 只读工具直接全仓搜索  
- 方案：静态候选 + LLM triage  
- 指标：wall-clock time；API input+output Token  
- 结果示例：平均耗时约 312s → 136s；Token 约 5.82 万 → 1.89 万  

边界声明：这是项目内评测集，不是线上大盘；样例有真实代码也有人工复核过的生成变体。

### 1.6 边界是什么

- 只支持 Java Maven Git 项目  
- 每次 scan 基本是全量，不是增量  
- triage 默认只深挖前 20 个候选  
- Feature Envy / Dead Code 强依赖 Symbol Solver；解析失败会降级或跳过  
- Duplicate Code 依赖 PMD plugin；没装就不能静默跳过  
- uncertain 不进入后续自动重构  

### 1.7 失败 / 误报怎么处理

| 情况 | 处理 |
|---|---|
| 不是 Git / 不是 Maven | 直接报错退出 |
| 工作区不干净 | 交互确认后再继续 |
| 没装 PMD plugin | 交互确认安装；非交互直接失败 |
| AST 解析失败 | `JavaAstError`，scan 停止 |
| CPD 跑不起来或结果不可解析 | `CpdError`，scan 停止 |
| 静态误报 | triage reject / uncertain |
| Symbol Solver 挂了 | Feature Envy 难做；Dead Code 退到 identifier-count fallback，风险标低 |

### 1.8 后续可以怎么改进

1. 增量扫描：按 git diff 只扫变更文件和依赖闭包  
2. 候选排序：不只截断前 20，按 severity / 风险 / 可自动化程度排序再 triage  
3. 模块感知：多模块项目按 GAV / reactor 做定向扫描和验证  
4. 评测补齐：公开 Precision / Recall，不只报耗时和 Token  

### 1.9 这一层 60 秒口述

scan 先做项目门禁，确认 Git 和 Maven。然后 JavaParser、Symbol Solver、PMD CPD 生成高召回候选，再让 LLM 结合局部源码做 accept/reject/uncertain。只有 accept 会进 `issues.json`。这样 LLM 处理的是结构化候选，不是整仓盲搜，所以耗时和 Token 都能降下来。

指文件：`commands.py` → `scanner.py` → `llm_assistant.py` → `storage.py`。

---

## 2. 层二：plan —— 把“有问题”变成“准备怎么改”

### 2.1 这一层解决什么问题

scan 只告诉你“哪里有坏味道”，还不够安全改代码。直接 apply 会有三个问题：

1. 不知道允许改哪些文件  
2. 不知道风险、验证命令、回滚策略  
3. 用户没有可审查的中间产物  

所以 plan 的目标是：围绕单个 issue，产出一份人能看、机器能执行的重构计划。

### 2.2 为什么这么做

我不让 LLM 在 plan 阶段直接改代码。原因是：计划是人和系统的契约。

- 对用户：展示目标、方式、风险、修改文件、验证命令  
- 对 apply：白名单、覆盖评估、验证策略都从计划来  
- 对审计：每个 task 都有独立目录，后续 patch / verification / rollback 都能挂上去  

简单说：scan 找问题，plan 定边界，apply 才动刀。

### 2.3 关键机制怎么落代码

入口：`commands.run_plan(issue_id=...)`

```text
run_plan()
  ├─ storage.find_issue(issue_id)          # 从 issues.json 取 issue
  ├─ RefactorPlanner.create_plan(issue)    # 规则骨架
  │    ├─ JavaContextCollector.collect()   # 局部源码、相关测试、调用方
  │    ├─ files_to_modify / expected_changes / risk
  │    └─ coverage_assessment（相关测试是否存在等）
  ├─ assistant.generate_plan(...)          # LLM 充实计划 JSON
  └─ storage.save_plan(plan, issue)
       └─ tasks/{task_id}/plan.json + plan.md + issue.json
```

`RefactorPlanner` 先给一个可执行骨架（`planning_source=rule-fallback`），再让 LLM 在只读工具帮助下补全目标描述、风险理由、期望修改等。真正写文件仍然不在这一步。

### 2.4 数据流转形态

**输入**

- `.paicli/refactor-agent/issues.json` 里的某个 `RA-xxxx`

**输出：RefactorPlan**

```json
{
  "task_id": "ra-0001-1710000000",
  "issue_id": "RA-0001",
  "goal": "拆分长方法 checkout，降低方法规模和理解成本。",
  "refactoring_type": "Extract Method",
  "files_to_modify": ["src/main/java/.../OrderService.java"],
  "expected_changes": ["只在目标文件内提取一到两个小方法", "保持对外签名不变"],
  "out_of_scope": ["不改无关模块", "不做大规模格式化"],
  "risk_level": "medium",
  "risk_reasons": ["目标区域测试覆盖不足"],
  "verification_commands": ["mvn -q -DskipTests compile", "mvn test"],
  "rollback_strategy": "任务级快照回滚，不用 git reset --hard",
  "coverage_assessment": {
    "has_related_test_class": false,
    "needs_characterization_test": true,
    "confidence": "low"
  },
  "context": {
    "source_excerpt": "...",
    "related_tests": [],
    "direct_callers": ["..."]
  }
}
```

**落盘**

```text
.paicli/refactor-agent/tasks/{task_id}/
  ├─ issue.json
  ├─ plan.json
  └─ plan.md
```

关键约束：`files_to_modify` 默认先收敛到 issue 所在文件。后面 apply 的白名单就从这里来。

### 2.5 指标怎么测

plan 层本身不直接贡献简历上的成功率数字，但它决定后续 apply 能不能控住边界。评测时我会看：

1. 计划是否包含白名单、验证命令、回滚策略  
2. LLM 是否试图把无关文件塞进 `files_to_modify`  
3. 覆盖不足时，是否明确标出 `needs_characterization_test`  

面试里可以说：plan 的价值是把“自由生成”变成“可审查契约”，成功率和越界下降主要在 apply 体现，但没有 plan，白名单和验证策略就没有锚点。

### 2.6 边界是什么

- 一次 plan 只服务一个 issue  
- 默认修改范围偏保守，通常先落在 issue 文件  
- plan 不写生产代码，也不写测试  
- 覆盖评估在 plan 阶段偏静态启发式；真正 JaCoCo 数字在 apply 前再测  
- 用户必须先确认，才会进入 apply  

### 2.7 失败 / 误报怎么处理

| 情况 | 处理 |
|---|---|
| 没跑过 scan / 找不到 issue | 直接报错，提示先 scan |
| LLM 计划生成失败 | 抛 `RefactorLlmError` / 用户可见错误 |
| 计划过于激进 | 用户看 `plan.md` 后可以不 apply |
| 覆盖不足 | 写入 `needs_characterization_test`，apply 时先走测试 Agent |

### 2.8 后续可以怎么改进

1. 多文件计划：对 Feature Envy / Duplicate Code 自动纳入相关调用方或重复位点  
2. 风险分级模式：低风险自动 apply，高风险强制人工确认  
3. 计划 diff 预演：在不写盘的情况下模拟行级编辑影响面  
4. 与 issue 严重度联动：优先给高价值 issue 生成计划  

### 2.9 这一层 45 秒口述

plan 是人和系统之间的契约。它从 `issues.json` 取出一个 issue，先用规则生成骨架，再让 LLM 补全目标、风险、验证和回滚策略，最后落到 `tasks/{task_id}/plan.json`。这一步最重要的字段是 `files_to_modify`，因为后面所有写操作都受这个白名单约束。

指文件：`commands.py:run_plan` → `planner.py` → `llm_assistant.generate_plan` → `storage.save_plan`。

---

## 3. 层三：apply —— 在受控闭环里真正改代码

### 3.1 这一层解决什么问题

到了 apply，风险变成真实写盘风险。单 Agent 自己改自己验，容易自我验证偏差：它会倾向于证明自己刚写的改动是对的。

同时还有三类工程风险：

1. LLM 越界改计划外文件  
2. 目标区域没有测试，改完行为漂了却测不出来  
3. 验证失败后越修越偏，最后工作区不可恢复  

所以 apply 的目标是：把“一次 LLM 改代码”变成“可验证、可反馈、可回滚”的闭环。

### 3.2 为什么这么做

我拆成三个 Agent，不是为了看起来复杂，而是为了职责和证据隔离：

| Agent | 只做什么 | 不能做什么 |
|---|---|---|
| Test Generator | 覆盖不足时新建行为锁定测试 | 不改生产代码 |
| Modifier | 按计划做结构化行级编辑 | 不自己宣布成功；必须过验证 |
| Verifier | 看真实 Diff、Maven、JaCoCo、工作区哈希 | 不改任何文件 |

另外还有本地机器层，不信任自然语言：

- patcher：白名单、路径、行号、事务写回  
- AST validator：语法可解析 + 非 private 签名不意外变化  
- snapshot / rollback：失败可恢复  

### 3.3 关键机制怎么落代码

入口：`commands.run_apply(issue_id=..., max_repair_attempts=2)`

```text
run_apply()
  └─ RefactorAgentOrchestrator.run()
       ├─ ensure_initial_snapshot()              # snapshot.json + before/
       ├─ PreModificationVerifier.verify()
       │    ├─ mvn compile
       │    ├─ JaCoCo prepare-agent test
       │    └─ JaCoCo report + CoverageAnalyzer
       ├─ [coverage_gap] TestGeneratorAgent
       │    ├─ apply_test_edits（仅允许新建测试）
       │    └─ run_generated_test_precheck
       └─ attempt 1..N  (N = max_repair_attempts + 1，默认 3)
            ├─ ModifierAgent → apply_edits
            │    ├─ 校验 files_to_modify / 路径 / 行号
            │    ├─ 写盘 + 回读
            │    └─ AstPatchValidator；失败立即恢复 before_text
            ├─ VerifierAgent
            │    ├─ compile / jacoco-test / jacoco-report
            │    ├─ inspect_diff（基于 snapshot 重建真实 Diff）
            │    ├─ coverage assessment
            │    └─ workspace manifest SHA-256 对比
            ├─ approved → 成功
            └─ rejected
                 ├─ feedback.json → 下一轮 Modifier
                 ├─ 先回滚生产文件，保留已验证测试
                 └─ 轮次耗尽 → 全量回滚
```

结构化编辑契约：

```json
{
  "edits": [
    {
      "file_path": "src/main/java/.../OrderService.java",
      "start_line": 88,
      "end_line": 120,
      "replacement": "..."
    }
  ],
  "explanation": "Extract accumulator block into helper method"
}
```

### 3.4 数据流转形态

**输入**

- 最新 `plan.json` + `issue.json`
- 用户确认（或 `--yes`）

**任务目录关键产物**

```text
.paicli/refactor-agent/tasks/{task_id}/
  ├─ snapshot.json                 # 初始快照 + workspace_manifest
  ├─ before/                       # 计划文件原文
  ├─ pre_modification.json         # 修改前基线
  ├─ generated_test_files.json     # 生成测试路径与哈希 guard
  ├─ test.patch.diff               # 仅测试变更
  ├─ patch.diff                    # 最新合并 Diff
  ├─ agent_messages.jsonl          # 跨 Agent 审计
  ├─ attempts/01/
  │    ├─ modifier.json
  │    ├─ verifier.json
  │    ├─ verification.json
  │    └─ feedback.json            # 若拒绝
  ├─ verification.json             # 最新验证结果
  ├─ rollback.json                 # 若最终失败
  └─ reports/... / latest.md
```

**跨 Agent 传递的不是自由对话，而是结构化结果**

- Test → Orchestrator：生成测试路径、预检命令、覆盖评估  
- Modifier → Orchestrator：`PatchApplicationResult`（changed_files / diff）  
- Verifier → Modifier：`VerificationResult`（approved / issues / suggestions / diff_summary）  
- Orchestrator → Rollback：`snapshot.json` + `generated_test_files.json`

### 3.5 指标怎么测

简历数字：重构成功率 Pass@1 从 **75.0%** 提到 **88.3%**。

口径：

- Pass@1：第一次生产代码修改就通过完整验证闭环，不靠后续 repair 轮次  
- Baseline：单 Agent 自改自验  
- 方案：测试 / 修改 / 验证拆分 + 覆盖门禁 + 反馈重试  
- 声明：项目内 benchmark；样本规模有限，证明设计方向，不夸大成工业大盘  

另外，工具阶段并行有约 **57.6%** 的工具耗时下降，口径是同一组 ReAct 轨迹里“只读工具串行 vs 并行”的工具阶段时间，不含完整端到端 LLM 推理外推。

### 3.6 边界是什么

- 默认最多 1 次初始修改 + 2 次修复  
- 测试 Agent 只在覆盖不足时启动，不是每次都跑  
- Verifier 强制 compile / jacoco-test；report 失败通常降级为 warning/覆盖问题  
- “公开 API”当前实现主要是非 private 方法签名，不是完整二进制兼容分析  
- Modifier 跨 repair 轮会复用自己的 history；Verifier / TestGenerator 每轮新建  
- 基础设施错误（比如 Maven 找不到）直接回滚，不进入聪明重试  

### 3.7 失败 / 误报怎么处理

| 情况 | 处理 |
|---|---|
| 原始代码编译/测试不过 | preflight 失败，禁止修改 |
| 覆盖不足 | 先生成行为锁定测试；预检不过则不进 Modifier |
| LLM 改白名单外文件 / 路径穿越 / 行号非法 | patcher 拒绝，不落盘或立即恢复 |
| AST / 非 private 签名异常变化 | patch 失败，恢复原文 |
| Verifier 拒绝 | 结构化 feedback 给下一轮 Modifier；先恢复生产文件 |
| 生成测试被篡改 | guard 检查失败，任务失败并回滚 |
| 多轮仍失败 | `_final_rollback`：恢复生产文件并删除本任务生成测试 |
| Verifier 错拒 | 可看 `verification.json` / feedback；人工 `rollback` 或调整计划后重来 |
| Verifier 错放 | 有硬门禁兜底：compile/test 失败、计划外工作区变化不能批准 |

### 3.8 后续可以怎么改进

1. Modifier 每轮 `reset_history()`，进一步隔离修复轮对话污染  
2. apply 后增加轻量 scanner 复检，不只靠 Verifier 语义判断“坏味道是否消除”  
3. 真正的原子写盘：临时文件 + rename / write-ahead journal  
4. 模块级 Maven 验证，避免大 reactor 全量过重或漏模块  
5. 把 Pass@1、失败原因分布做成可复现评测脚本，而不只停留在口头数字  

### 3.9 这一层 70 秒口述

apply 先做不可变快照，再在原始代码上跑 compile、test、JaCoCo。覆盖不足就让测试 Agent 先生成行为锁定测试，并且必须在原始代码上预检通过。然后修改 Agent 只能输出结构化行级编辑，patcher 校验白名单、路径和行号，JavaParser 检查结构变化。验证 Agent 不看修改 Agent 的自我汇报，而是重建真实 Diff、跑 Maven/JaCoCo、对比工作区哈希。失败就反馈重试，耗尽就回滚。这样成功率从单 Agent 的 Pass@1 75% 提到了 88.3%。

指文件：`orchestrator.py` → `patcher.py` / `patch_validator.py` → `agents.py` → `verifier.py` / `rollback.py`。

---

## 4. 一条主线串起来（90 秒完整版）

我做这个项目时，主线就是 scan、plan、apply。

scan 解决“LLM 全仓搜索又贵又不稳”。我先用 JavaParser、Symbol Solver、PMD CPD 生成高召回候选，再让 LLM 做 triage，只把 accept 的 issue 留给后面。产物是 `candidates.json`、`decisions.json`、`issues.json`。

plan 解决“有问题还不能直接改”。它把单个 issue 变成可审查计划，核心是白名单、风险、验证命令和回滚策略，落到 `tasks/{task_id}/plan.json`。

apply 解决“真正写盘时如何控风险”。先快照和覆盖门禁，必要时生成行为锁定测试；修改 Agent 只做结构化编辑；验证 Agent 独立取证；失败反馈修复，最终失败回滚。

所以这不是一个聊天改代码的 Demo，而是一条带数据契约和失败恢复的工程流水线：  
**候选 → 计划 → 受控修改 → 证据验证 → 回滚兜底。**

---

## 5. 三层对照速查表

| 维度 | scan | plan | apply |
|---|---|---|---|
| 核心问题 | 全仓搜索贵、召回不稳 | 缺少可审查改动契约 | 越界修改、自我验证、不可回滚 |
| 主输出 | `issues.json` | `plan.json` | `patch.diff` + `verification.json` / `rollback.json` |
| LLM 角色 | triage 精排 | 充实计划 | 测试/修改/验证三角色 |
| 本地硬约束 | Git/Maven/PMD、AST/CPD | issue 必须已存在 | 白名单、路径、行号、AST、Maven、哈希 |
| 成功标准 | 产出可解释候选与决策 | 产出可执行计划 | 验证通过且未越界 |
| 失败兜底 | 中止 scan，不进入改码 | 不写盘，用户可不 apply | 反馈重试或全量回滚 |
| 简历指标 | 耗时 -56.5%，Token -67.5% | 契约质量（定性） | Pass@1 75.0% → 88.3% |

---

## 6. 面试指文件顺序（总 3 分钟）

1. `commands.py`：先画 scan → plan → apply  
2. `scanner.py` + `llm_assistant.py`：讲候选与 triage  
3. `planner.py` + `storage.py`：讲计划契约和 task 目录  
4. `orchestrator.py`：讲 apply 闭环  
5. `patcher.py` + `patch_validator.py`：讲越界防护  
6. `agents.py` + `verifier.py` + `rollback.py`：讲验证反馈和恢复  

被追问记忆系统、工具并行时，再从主线岔出去：

- 记忆：服务 chat / ReAct 上下文，不是改码主链核心  
- 并行：加速只读取证，写操作仍串行  

---

## 7. 练习建议

1. 先把第 4 节 90 秒完整版背顺  
2. 再分别练第 1.9 / 2.9 / 3.9 三个单层口述  
3. 最后只准备每层 2 个追问：  
   - scan：为什么偏召回？指标口径是什么？  
   - plan：白名单从哪来？为什么 plan 不直接改代码？  
   - apply：为什么拆三 Agent？失败如何回滚？  

其余 `qa.md` 问题当词典查，不当主线背。
