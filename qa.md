
●项目描述： 
面向 Java Maven 项目，结合静态分析、LLM 语义判断与多 Agent 修改验证回滚闭环，实现七种代码坏味道检测与安全重构。 
●技术栈： Python 3.11+、Java 17、Maven、JavaParser、Symbol Solver、LLM Agent、ReAct、OpenAI-Compatible API、httpx、Pydantic、pytest、Ruff、Mypy、uv、Git、JaCoCo、PMD CPD
●主要工作： 
1. 代码坏味道识别：针对 LLM 全仓搜索成本高的问题，先由 JavaParser AST、Symbol Solver 和 PMD CPD 静态分析仓库代码生成问题候选，再由 LLM 结合源码完成最终筛选，相比 LLM 直接搜索平均耗时降低 56.5%，平均 Token 消耗降低 67.5%。 

2. 针对单 Agent 自我验证偏差：拆分测试、修改和验证 Agent，并隔离提示词与对话记录；修改前,测试 agent 通过 JaCoCo检查目标代码测试覆盖，并生成行为锁定测试；验证失败则反馈修改 Agent 重试，重构成功率由 Pass@1 的 75.0%提升至 88.3% 

3. 针对 LLM 容易越界修改的问题：限制其只输出结构化行级编辑，补丁应用前校验文件白名单、路径边界和行号范围，应用后通过 JavaParser 检查语法结构及公开 API 变化，校验失败则自动恢复原文件。 

4. 验证与回滚：验证 Agent 结合重构计划与真实 Diff，判断目标坏味道是否消除且未引入新问题，并执行 Maven 编译测试和工作区哈希检查；验证失败时将问题反馈给修改 Agent 循环修复，多轮重试仍未通过则自动恢复全部文件。 

5. 针对跨会话知识无法复用的问题：设计 PAI.md 项目记忆、全局长期记忆和会话历史三层记忆，按任务检索相关信息注入上下文，并自动压缩早期对话，保留用户目标、关键结论和待办。 

6. 工具并行调用：多个只读工具通过 AsyncIO 并行执行，写操作保持串行；工具阶段平均耗时降低 57.6%。 


## 1.什么是静态分析？

静态分析是不运行程序，通过源码结构、语法树、符号关系和规则来发现潜在问题。我的项目用它先低成本缩小 LLM 的搜索范围，再让 LLM 做语义判断。
和它相对的是动态分析：需要把程序跑起来，通过测试、日志、运行时数据来判断问题。

## 2.这7种坏味道是怎么选择的，有参考吗，你对坏味道怎么理解？

### 我对坏味道的理解
代码坏味道不是 bug，而是代码中可能降低可读性、可维护性、可扩展性的结构信号。
它通常不一定立刻导致程序错误，但会让后续修改更难、更容易出错。例如：

方法太长，读不懂、测不动
类太大，职责混乱
重复代码，一处改了另一处忘改
条件逻辑复杂，分支组合难覆盖
命名不清，理解成本高
死代码干扰阅读
一个方法总是操作别的类的数据，说明职责可能放错了
所以坏味道的核心是：提示“这里可能值得重构”，但不等于一定要改。

我选这 7 类，是因为它们一方面是重构领域里比较经典、面试官也熟悉的坏味道；另一方面，它们能通过静态分析生成相对可靠的候选，再交给 LLM 做语义筛选和安全重构，比较适合自动化闭环。

这 7 种分别是：

长方法 Long Method
经典坏味道。方法过长通常说明做了太多事，适合提取方法、拆分步骤。

大类 Large Class / God Class
类承担太多职责，字段和方法过多，常见重构方向是拆类、提取职责。

重复代码 Duplicate Code
最经典的坏味道之一。重复逻辑会导致修改不一致，适合提取公共方法或模板。

特性依恋 Feature Envy
一个方法频繁访问其他类的数据，说明行为可能放错了位置，适合移动方法或调整职责。

命名不清 Unclear Naming
需要 LLM 做语义确认。

复杂条件 Complex Condition
分支多、条件表达式复杂，维护和测试成本高，适合提取解释性方法或策略模式。

死代码 Dead Code
未使用的 private 方法、字段、分支等会干扰理解，适合删除。

7 类的选择参考了 Fowler 的 Code Smells、Sonar/PMD 等静态分析工具思想，同时也考虑了自动化可检测性和重构安全性。

## 3.JavaParser AST、Symbol Solver 和 PMD CPD
### JavaParser AST
是一个 Java 源码解析库。
它可以把 .java 源码解析成 AST（Abstract Syntax Tree，抽象语法树）。
负责看清“代码长什么结构”。
### Symbol Solver
是 JavaParser 生态里的符号解析器。

AST 只能告诉你“这里调用了一个方法”或“这里访问了一个字段”，但不一定知道它属于哪个类。
判断方法调用/字段访问属于哪个类，负责看清“这个名字到底指向谁”。

### PMD CPD
CPD可以检测出两个方法里有大段相同逻辑/重复片段
并给出：
重复出现在哪些文件
起始行号
重复 token 数或行数

JavaParser AST 负责把源码结构化，Symbol Solver 在 AST 基础上解析方法和字段到底属于哪个类型，PMD CPD 负责检测复制粘贴式重复代码。它们共同负责低成本生成坏味道候选，后面再交给 LLM 做语义筛选。

## 4.相比 LLM 直接搜索平均耗时降低 56.5%，平均 Token 消耗降低 67.5%是怎么测出来的？
我做的是项目内 benchmark。选了 4 个 Java Maven 项目，共 20 个坏味道识别任务。Baseline 是让 LLM 通过只读工具直接搜索全仓；我的方案是先用 AST、Symbol Solver 和 CPD 生成候选，再让 LLM 对候选做语义筛选。统计从任务开始到输出结构化结果的 wall-clock time，以及 LLM API 返回的 input/output token 总和。最终平均耗时从 312 秒降到 136 秒，Token 从 5.82 万降到 1.89 万，对应耗时降低约 56.5%，Token 降低约 67.5%。

测试样例一部分来自开源 Java Maven 项目的真实代码，一部分由 AI 辅助生成坏味道变体，例如长方法、重复代码、复杂条件和特性依恋。生成后人工复核标签，并用静态规则和单元测试确保样例可编译、可运行，避免直接把 AI 生成结果当作真实标注。

## 5. 亮点 1 口语逐字稿（Q1–Q22）

怎么用：每题就是你开口要说的话，按 40～70 秒练。括号里是可选补充，被追问再加。

---

### Q1：LLM 全仓搜索贵在哪？为啥不用 RAG？

其实贵主要贵在三块。  
第一，Agent 会不停 list、search、read，工具轮次一多，时间就被拖长了。  
第二，每次把大段源码塞进上下文，Token 涨得很快。  
第三，同一仓库扫几遍，覆盖集合还不一定一样，召回不稳定。

为啥不用 embedding RAG 呢？因为坏味道很多是结构信号，比如方法多长、嵌套多深、调用到底属于谁、有没有复制粘贴。这类东西用 AST、规则、CPD 更稳，也好解释。  
RAG 更适合“按一句话找相关代码”，不太适合保证“长方法、重复块一定被系统性捞出来”。  
所以我的分工是：确定性搜索交给静态扫描，值不值得改交给 LLM。triage 的时候它还可以局部 search，但不再负责全仓发现。

---

### Q2：三件套怎么分工？只能留一个留谁？

简单说就是各管一块。  
JavaParser 看结构：方法从哪行到哪行、分支多少、嵌套多深、类里有多少成员。长方法、大类、复杂条件主要靠它。  
Symbol Solver 看归属：这个调用、这个字段到底是哪个类的。特性依恋强依赖它，死代码也优先用它。  
PMD CPD 专门找复制粘贴重复，重复代码这条链路基本就交给 `mvn pmd:cpd-check`。

如果只能留一个，我会留 JavaParser。因为没有靠谱 AST，行号和结构指标都不准，后面 triage 和补丁校验都会垮。没有 Symbol，特性依恋会变弱；没有 CPD，重复代码会缺一块；但主流程至少还能跑。

---

### Q3：你们偏召回还是偏精确？阈值松紧怎么影响？

两阶段目标不一样。静态分析我偏召回，先尽量把可疑点捞出来；LLM triage 偏精确，必须看到源码证据才 accept。  
原因很简单：漏报后面很难补，我们不会再让 LLM 全仓盲搜；误报可以在第二阶段砍掉。

阈值调松，候选就多，Token 和耗时会上去，但更不容易漏；调紧成本低，但命名、特性依恋这类更容易漏。我倾向规则稍微松一点，LLM 收得严一点，因为后面还有计划、覆盖率、编译测试和回滚兜底。  
结构类阈值比较固定，比如长方法大概八十行以上，或者分支、嵌套超标；特性依恋本身就偏紧；命名和死代码更靠 LLM 把关。

（别说“我们又要高召回又要高精确”，直接说两阶段目标不同。）

---

### Q4：哪些靠 AST？哪些必须 Symbol？举个误判例子

长方法、大类、复杂条件，基本看 AST 结构就行。命名不清用黑名单启发式，不一定要 Symbol。重复代码走 CPD。  
真正必须 Symbol 的是特性依恋：解析不成功我直接不报。死代码优先用 Symbol 的调用关系，解析失败才退回“这个名字全项目出现几次”。

误判很好举。比如方法里一堆 `customer.getX()`，AST 只看见调用名是 getX，不知道它是不是 Customer 上的。要是只按名字统计，本类同名方法、别的无关 getX 都可能被算成外部依赖，特性依恋假阳性会很高。所以代码里必须拿到归属类型才继续判。

---

### Q5：Symbol Solver 挂了怎么办？

不是一刀切。  
特性依恋：解析失败就跳过，宁可不报，也不要乱报。  
死代码：有解析签名就按调用图看；没有就退化成标识符出现次数，并且在 evidence 里标清楚用的是 fallback。  
另外说实话，我们 Type Solver 主要配了反射和项目源码根，没有把所有 Maven 依赖 jar 都喂进去，所以第三方类型有时解析不好，这是已知边界。  
如果连 AST 整体都挂了，scan 直接失败，我不会偷偷改成正则扫描糊弄过去。

---

### Q6：为啥用 CPD，不自己做重复检测？阈值怎么定？

自己做 AST 同构或者 token hash，要处理格式化差异、小改动变体，MVP 阶段不划算。CPD 是现成的 token 级重复检测，又和 Maven 集成方便：装好 pmd 插件，跑 cpd-check，把重复行数和出现位置解析出来就行。

minimum tokens 这块，我安装插件时没有在代码里强行改 CPD 默认阈值，基本走插件默认。我们自己用重复行数定严重级别，大概四十行以下中等，再高就偏高，并且把多个重复位置写进 evidence。  
阈值太低会刷屏，太高会漏短重复；要调的话改 pom 里的 CPD 配置，而不是改 Python 解析逻辑。当前优先保证“能稳定打出可定位的候选”。

---

### Q7：哪些规则就够用？去掉 LLM 会怎样？

结构比较客观的，比如长方法、大类、复杂条件、重复代码，规则本身可信度更高。  
强依赖 LLM 的主要是命名不清——黑名单很粗；还有死代码——反射、框架回调容易误报；特性依恋有时也是设计判断，不单是计数。

如果去掉 triage，命名和死代码噪音会明显起来，结构类也会把一些“确实很长但暂时不该动”的代码推给用户。内测体感上，没 triage 时人工要看的噪音大概能到两三倍这个量级，尤其是命名，所以第二阶段是必要的。这个数是量级观察，我不会说成精确 offline 指标。

---

### Q8：候选长什么样？为啥不只丢个路径？

候选是结构化对象，不只是路径。里面有编号、坏味道类型、严重度、文件、符号名、起止行、证据和指标、影响说明、建议怎么重构、风险等级。  
给 LLM 的时候，还会带上局部源码摘要、相关测试、直接调用方，它不够还可以再 read、再 search。

如果只丢路径，模型还得自己重新定位，Token 和漏判都会回来。我们省成本的关键，就是把问题收成“看这几十个带证据的候选”。  
另外只有 accept 会进最终 issues；reject 和 uncertain 留在审计里。默认最多仔细看 20 个，超出的直接标 uncertain，并写明没调查。

---

### Q9：三种决策怎么用？uncertain 会不会偷懒？

accept 才能进后续计划和修改。  
reject 就是明确误报，不自动改。  
uncertain 是证据不够，或者超出 limit 还没来得及看——同样不进自动重构。

防偷懒这块，prompt 里要求它先看目标源码，必要时看调用方和测试；accept 必须带具体源码证据，测试里也卡了“没证据不能 accept”。所以 uncertain 是显式暂缓，审计里看得见，不是悄悄当成通过。

---

### Q10：候选很多怎么控成本？静态会不会更慢？

硬门槛是 triage 默认只深挖前 20 个，后面标 uncertain。  
排序上说实话，现在是按文件路径和行号稳定排完再编号，还没有先按严重度送进 LLM；accept 之后再用模型给的 priority 排一下展示顺序。这个点被问到我直接承认，后续可以改成严重度优先。

大仓库上，全量 JavaParser 加一次 Maven CPD，静态阶段确实可能占不少墙钟时间。但跟 LLM 全仓多轮搜索比，通常还是更便宜、也更可预期。现在用 limit 先把 LLM 成本箍住；下一步可以做严重度预排序、按模块扫、或者缓存 CPD 结果。

---

### Q11：你们是增量扫还是每次全量？瓶颈在哪？

当前是全量。每次 scan 会把项目里的 java 文件收集一遍，跳过 target、.git 这些目录，然后做 AST，再跑一遍 CPD。没有文件级增量缓存。  
不过 triage 会复用这次扫出来的 AST 快照，避免精排阶段再解析一遍。

大仓瓶颈我优先看两块：JavaParser 全量解析，和 Maven CPD。LLM 因为有 20 个上限，反而不是最容易炸的那个。以后可以按 git diff 增量，或者按模块过滤。

---

### Q12：56.5% 和 67.5% 怎么测的？公平吗？

这是项目内 benchmark。我选了 4 个 Java Maven 项目，一共 20 个坏味道识别任务，模型和工具集保持一致。  
Baseline 是让 LLM 用只读工具自己全仓搜；我们是先静态出候选，再让 LLM 做语义筛选。  
比的是从任务开始到输出结构化结果的耗时，还有 API 的 input 加 output Token。大概耗时从 312 秒降到 136 秒，Token 从 5.82 万降到 1.89 万，对应那两个百分比。  
两边都没有故意卡死轮次去刷分。样本不算大，我汇报时会说清楚是评测集上的结果，不是线上大盘。

---

### Q13：会不会挑好做的样本？有没有分层看？

任务覆盖了七类坏味道，仓库规模也不完全一样；有真实开源代码，也有人工复核过的合成变体。  
20 个样本谈不上很严格的分层统计，所以我对外主要报总体均值。内部抽查过结构类和语义类：结构类两阶段优势更稳，命名类更吃 LLM。我不会把这组小数讲成大规模 A/B。

---

### Q14：成本降了，质量跟得上吗？

简历上这两个数字主打效率。质量我看三层：accept 抽检是不是真问题；reject 有没有把误报砍掉；后面重构能不能过编译、测试、覆盖和验证 Agent。  
坏味道本身有主观性，尤其命名，所以我更强调“值不值得自动改”，而不是死磕一个大盘 F1。效率有完整对比；质量目前以抽检加下游成功率为主。

---

### Q15：样例有 AI 生成的，指标会不会虚高？

会有这个风险，所以我们生成之后要人工复核标签，并且用静态规则和单元测试保证样例能编译、能跑，不会把模型生成结果直接当金标准。  
评测比的是：同一批标签上，两种识别路径谁更省时间和 Token；不是看规则有没有刚好拟合生成器。合成样例主要用来补真实仓库里比较少的类型，比如特性依恋、复杂条件。

---

### Q16：Token 包不包含本地 AST、CPD？

简历上的 Token，指的是 LLM API 的 input 和 output。  
静态分析、CPD、Java helper 是本地跑的，算进总耗时，但不计入 API Token。  
完整说法就是：端到端更省时间，计费 Token 降得更明显；我们是用本地 CPU 换更稳的召回。如果面试官问总拥有成本，我可以补本地耗时占比，但不会把本地成本和 API Token 混成一个数。

---

### Q17：为啥不上 Sonar 一整套？

因为我的目标不是再做一个质量平台，而是给自动重构提供可控的候选入口。  
自建这几条规则，产出直接对上我们的 issue 模型、计划和补丁白名单；重复代码这块就复用 CPD，它最擅长。  
Sonar 做门禁很强，但规则集大、接入重，还要跟后面的 LLM triage、多 Agent 修改对齐，成本更高。我的边界是：质量门禁可以继续用 Sonar；自动重构流水线用小而可控的规则集加 LLM。

---

### Q18：漏报怎么办？LLM 还会再全仓搜一遍吗？

不会再开一轮全仓坏味道发现，不然前面省下来的成本和稳定性就没了。  
triage 时 LLM 可以对当前这个候选去读文件、搜一下引用，但那是补充证据，不是重新当扫描器。  
漏了就调阈值、补符号信息、补 CPD，或者让用户指定文件和 issue。靠 LLM 盲搜补召回，我觉得方向不对。

---

### Q19：漏报你能接受吗？

有限漏报可以接受，高置信误报带着自动改，我不能接受。  
因为我们后面是真改代码的，错 accept 一次，成本是改文件、跑测试，还可能回滚；漏一个不太清晰的命名，代价小得多。  
所以在这个场景下，我优先要精确率和可验证性，而不是追求完美召回。

---

### Q20：如果支持 Python 或 Go，什么能复用？

流水线思路可以复用：先出候选，再 triage，再计划、打补丁、验证。决策协议、只读工具箱形态、审计落盘、多 Agent 隔离，这些也能留。  
必须重做的是语言前端：AST 和符号解析、重复检测工具、还有构建测试覆盖这一套。  
我会把边界画在“语言前端产出统一候选”，和“后面跟语言无关的编排层”之间，这样扩语言时不会把整条链路推翻。

---

### Q21：CPD 报了，LLM 也 accept 了，改完行为却变了，锅在谁？

检测和 triage 只说明“这里像重复，值得抽”；行为被改坏，更常见是修改阶段抽公共逻辑抽错了，或者测试没锁住，或者验证没拦住。  
检测侧能加强的是：证据里带齐多个重复位置；triage 必须对比几处副本有没有微妙差异，不能只看见 CPD 报了就 accept。  
下游一定还要有行为锁定测试、Maven 测试，以及验证 Agent 看真实 diff。如果两处代码本来就不完全一样，却被当成可合并，那 triage 过度信任 CPD 也有责任。

---

### Q22：这不就是规则加 LLM 吗？难点在哪？

表面看确实是两阶段，但难点不在“会不会调模型”，而在怎么把不确定的 LLM 关进可重复的工程闭环里。  
第一，证据链要完整：静态指标进 evidence，accept 必须引用具体源码行。  
第二，工具失败要可见，该降级降级，不装成功。  
第三，accept、reject、uncertain 有明确协议，还落审计，跟后面的计划、补丁、验证对齐。  
第四，让 LLM 只做局部语义判断，不做全仓搜索器。

跟 Sonar 的差别也在这：我们不是堆更多 lint 规则，而是为“能安全自动重构”设计入口契约。

---

### 开场 30 秒（先背这个）

我没让 LLM 直接全仓搜坏味道，而是先用 JavaParser、Symbol Solver 和 PMD CPD 做静态扫描，生成结构化候选，再让 LLM 结合局部源码做最终筛选。  
静态偏召回，LLM 偏精确；解析失败按规则降级或跳过；只有 accept 才会进入后面的重构。  
在我那个评测集上，平均耗时大概降了 56.5%，Token 降了 67.5%。核心不是又接了个模型，而是把搜索交给确定性工具，把判断关进带证据的流水线里。

## 6. 亮点 1 代码映射

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

