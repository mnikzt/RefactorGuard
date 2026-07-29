# Refactor Agent：从 0 到 plan 再到 apply

这份文档串起整条主流程，方便面试口述：`scan → plan → apply`。

```text
进入 Java Maven Git 项目
  -> refactor-agent scan
  -> 静态扫描出坏味道候选
  -> LLM triage 筛选
  -> 得到 issue 列表（如 RA-0001）
  -> refactor-agent plan --issue RA-0001
  -> 规则先搭计划骨架
  -> LLM 生成完整重构计划
  -> 落盘 plan.json / plan.md
  -> refactor-agent apply --issue RA-0001
  -> 快照 + 修改前验证
  -> [覆盖不足则测试生成 Agent]
  -> 修改 Agent
  -> 验证 Agent
  -> 成功接受 / 失败重试 / 最终回滚
```

一句话：

> `scan` 发现问题，`plan` 决定怎么改，`apply` 才真正改代码并验证回滚。

---

## 一、从 0 到重构计划：`scan → plan`

### 1. `scan`：发现问题

在目标项目根目录执行：

```bash
refactor-agent scan
```

#### 1.1 项目检测

检查当前目录是不是：

- Git 仓库
- Maven 项目（有 `pom.xml`）
- 有没有 PMD CPD 插件（重复代码检测需要）

不满足就直接停，或提示确认安装插件。

#### 1.2 静态扫描生成候选

用三件套扫源码：

- **JavaParser AST**：方法/类结构、行号、分支、嵌套
- **Symbol Solver**：调用和字段归属哪个类
- **PMD CPD**：重复代码片段

产出 7 类坏味道候选：

- Long Method
- Large Class
- Complex Condition
- Unclear Naming
- Dead Code
- Feature Envy
- Duplicate Code

每个候选带上：文件路径、符号、起止行、证据、建议重构方式。

候选排序是稳定排序：

```text
file_path → start_line → smell_type
```

然后编号：`RA-0001`, `RA-0002`, ...

#### 1.3 LLM triage（语义筛选）

静态候选不等于最终问题。LLM 会按上述顺序逐个深挖前 20 个候选，结合局部源码判断：

- `accept`：值得改
- `reject`：不是真问题 / 不该现在改
- `uncertain`：证据不足，或没轮到调查（第 21 个及以后）

只有 `accept` 的进入最终 issue 列表。  
accept 后再按 LLM 返回的 `priority` 升序重排。

#### 1.4 scan 结果落盘

写到目标项目的 `.paicli/refactor-agent/`，大致包括：

- 扫描结果
- 原始候选
- LLM 决策审计

到这里手里有的是：**哪些地方有问题**，还没有详细怎么改。

---

### 2. `plan`：生成重构计划

选定一个 issue：

```bash
refactor-agent plan --issue RA-0001
```

注意：

- `plan` **不会自动按列表逐个规划**
- 由用户按 issue id 点选
- chat 里不指定时，默认往往会选列表第一个（triage 后 priority 最高/最小的那个）

#### 2.1 加载 issue

从 scan 结果里找到指定 issue。

#### 2.2 规则先搭骨架计划

`RefactorPlanner` 先做一个安全底座，例如：

- 目标是什么
- 建议重构类型（如 Extract Method）
- 允许改哪些文件
- 风险等级
- 默认验证命令
- 回滚策略
- 相关上下文和覆盖评估

这一步偏保守，保证即使 LLM 写得不好，也有底线。

#### 2.3 LLM 生成完整计划

规划阶段的 LLM 在骨架上补全：

- `goal`
- `refactoring_type`
- `files_to_modify`
- `expected_changes`
- `out_of_scope`
- `risk_level` / `risk_reasons`
- `verification_commands`
- `rollback_strategy`

**重构计划不是修改 Agent 生成的。**  
修改 Agent 只在 `apply` 阶段按已有计划改代码。

#### 2.4 计划落盘

保存成：

- `plan.json`：给程序用
- `plan.md`：给人看

到这里，才算真正有了重构计划。系统还没有改业务代码。

---

## 二、`apply`：真正改代码并闭环验证

```bash
refactor-agent apply --issue RA-0001
# 或
refactor-agent apply --issue RA-0001 --yes --max-repair-attempts 2
```

前提：这个 issue 已经有 plan。

### 整体流水线

```text
确认计划
  -> 创建不可变快照
  -> 修改前基线验证（compile / test / JaCoCo）
  -> 覆盖不足则：测试生成 Agent 补行为锁定测试并预检
  -> 修改 Agent 做结构化行级编辑
  -> 验证 Agent 取证并审查（命令 / diff / 覆盖）
  -> 通过则接受；失败则反馈重试；最终失败则回滚
```

### 1. 加载计划并确认

- 读该 issue 的最新重构计划
- 展示计划内容
- 默认要确认（`--yes` 可跳过）

### 2. 创建不可变快照

在任何业务代码修改之前创建。  
主要是任务目录里的 `snapshot.json` + `before/`。

`snapshot.json` 大致包括：


| 字段                         | 含义                                          |
| -------------------------- | ------------------------------------------- |
| `task_id` / `issue_id`     | 对应哪个任务、哪个问题                                 |
| `head`                     | 当时的 `git rev-parse HEAD`                    |
| `git_status`               | 当时的 `git status --porcelain`                |
| `planned_files`            | 计划允许修改的文件列表                                 |
| `user_changes_before_task` | 任务开始前工作区是否已有未提交改动                           |
| `workspace_manifest`       | 工作区文件到哈希的映射                                 |
| `files[]`                  | 计划内每个文件的路径、大小、`before_sha256`、`before_copy` |


`before/` 目录保存计划内业务文件的修改前原文副本。  
后面真实 Diff、工作区越界检查和失败回滚都靠它。

### 3. 修改前预检（Preflight）

跑原始项目基线：

- `mvn compile`
- JaCoCo 测试
- JaCoCo 报告


| 结果                 | 行为           |
| ------------------ | ------------ |
| 原始 compile/test 失败 | 直接禁止修改       |
| JaCoCo 不可用         | 禁止修改         |
| 覆盖不足               | 进入测试生成       |
| 覆盖足够               | 直接进入修改 Agent |


覆盖是否足够，主要看目标坏味道行区间在 JaCoCo 里是否达到约 **80%**；  
找不到报告、找不到目标文件、或目标文件完全没被覆盖，也算不足。

### 4. 覆盖不足时：测试生成 Agent

- 只能在允许的 `src/test/java` 路径新建测试
- 生成行为锁定测试（锁住当前实际行为）
- 必须通过：
  - test-compile
  - 连续两次完整 test
  - JaCoCo 目标文件覆盖
- 预检通过后，才允许修改业务代码
- 修改 Agent **不能改**这些自动生成测试

行为锁定的意思：

> 先用测试把当前代码实际行为钉死；重构后这些测试还得过，说明行为没被偷偷改坏。

### 5. 修改 Agent

- 按计划做最小编辑
- 只能输出结构化行级编辑：`file_path / start_line / end_line / replacement`
- `apply_edits` 会校验：
  - 文件白名单
  - 路径边界
  - 行号范围
  - JavaParser AST / 公开 API 变化
- 校验失败自动恢复原文件

### 6. 验证 Agent

验证 Agent 不能改代码，也不能只靠“嘴上说通过”。  
最终决策前必须取证：

1. 跑必需 Maven 命令（compile / JaCoCo test / report）
2. `inspect_diff`：从快照重建真实 Diff
3. `get_coverage_assessment`：看覆盖情况
4. 再判断目标坏味道是否消除、有没有引入新问题

机器检查回答硬条件；验证 Agent 做语义审查。两者都过才接受。

### 7. 失败反馈与重试

- 默认最多额外修复 2 次（`--max-repair-attempts 2`）
- 每次重试前：
  - 恢复业务代码到快照
  - **保留**已通过预检的生成测试
- 把验证失败证据反馈给修改 Agent，重新生成完整修改

### 8. 最终成功 / 失败

- **成功**：保留改动，写出报告、patch、verification
- **最终失败或基础设施错误**：
  - 恢复业务代码
  - 删除自动生成测试
  - 任务失败退出

---

## 三、角色分工

```text
用户确认 plan
    |
快照 + 修改前验证
    |
覆盖不足? --是--> 测试生成 Agent --预检通过--> 修改 Agent
    |否                                    |
    +--------------------------------------+
                      |
                 验证 Agent
                 /        \
              通过        拒绝
               |           |
             成功     还有重试次数?
                       /        \
                     是          否
                      |           |
                 回滚业务代码     最终回滚
                 +反馈重改       （含删生成测试）
```


| 阶段 / 角色       | 负责什么                        | 不负责什么      |
| ------------- | --------------------------- | ---------- |
| scan 静态分析     | 生成候选                        | 最终是否值得改    |
| LLM triage    | accept / reject / uncertain | 全仓盲搜、改代码   |
| plan / 规划 LLM | 生成可审查重构计划                   | 真正改代码      |
| 测试生成 Agent    | 补行为锁定测试                     | 改生产代码      |
| 修改 Agent      | 按计划做受控行级编辑                  | 验证自己、改生成测试 |
| 验证 Agent      | 取证并审查                       | 直接改代码      |


---

## 四、面试口述模板

### 40 秒版

> 整条链路只有三步。先 `scan`：用 JavaParser、Symbol Solver、PMD CPD 做静态召回，再让 LLM triage 筛出真正值得改的 issue。再 `plan`：规则搭骨架，LLM 写出可审查的重构计划，明确白名单、风险和验证方式。最后 `apply`：先建快照和修改前基线，覆盖不足就先补行为锁定测试，再由修改 Agent 做结构化行级编辑，验证 Agent 基于真实 Diff、Maven 和 JaCoCo 独立审查；失败可反馈重试，最终失败自动回滚。

### 一句话版

> 静态分析缩小搜索空间，LLM 做语义判断和计划，多 Agent 加上快照、覆盖、Diff 和回滚，把不可控输出关进可审计、可恢复的工程闭环。

