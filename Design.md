# Code Review Workflow —— 设计文档

> 本文说明为什么这样拆分、关键决策的取舍，以及可靠性、可扩展性与测试。
> 使用方式见 [README.md](README.md)。

---

## 一、需求理解

### 1.1 目标与实现对照

| 作业要求 | 实现方式 | 位置 |
|----------|----------|------|
| 2–3 个 Agent 协作完成一件事 | 编码 / 审查 / 修复三个 Agent，各管一段 | `agents/` |
| Agent 间的消息传递或状态共享 | **两者都有**：显式 `Message` 总线 + 共享黑板 | `workflow/message.py`、`bus.py`、`state.py` |
| 支持基本工作流控制（顺序执行） | 图驱动顺序推进，条件边决定走向 | `workflow/graph.py`、`engine.py` |
| MVP 可运行，不引入复杂编排 | 零第三方依赖，`--offline` 一条命令跑完 | `main.py` |
| 支持分支 / 循环等高级控制（推荐） | 条件边实现分支，回边实现"修复→复检"循环，带双重循环保护 | `pipelines.py`、`engine.py` |
| 可视化工作流编排界面（可选） | 未做 Web 界面；改为 CLI 打印字符画 + 报告内嵌 Mermaid | `graph.to_ascii()/to_mermaid()` |
| 执行日志与状态追踪（推荐） | 每步 `StepRecord` + 状态审计事件 + `trace.json` | `engine.py`、`state.py` |
| 错误恢复与重试（推荐） | 节点重试 + 状态回滚 + checkpoint 续跑 | `engine.py`、`checkpoint.py` |
| 复用项目 1 的 Agent / 经验 | 保留 LLM 抽象、离线兜底、AST 工具、工程约定；新增适配器说明 | 见第七节 |

### 1.2 选题：为什么做"编码 → 审查 → 修复"

作业给了 5 个方向。选这个方向有三个理由：

1. **有客观判定**：代码问题可以被 AST 规则确定地检出，因此"工作流是否真的起作用"不靠嘴说——
   报告里是"9 → 5"这种可数的数字；
2. **闭环自然**：审查发现问题、修复解决问题、复检确认问题消失，这才是"多 Agent 协作"而不是"三次模型调用"；
3. **能复用项目 1**：项目 1 就是代码理解 Agent，它的 LLM 抽象、AST 分析、离线兜底都能直接迁移。

### 1.3 技术选型

| 选项 | 决定 | 理由 |
|------|------|------|
| 编排框架 | **不用**（LangGraph / AutoGen / CrewAI） | 作业要求"理解为编排、通信、状态管理"，框架会把这些都藏起来；自己写 300 行引擎，反而能讲清楚每个决策 |
| 模型接入 | OpenAI 兼容协议 + 标准库 `urllib` | 换厂商只改一行预设；不装 SDK 就没有依赖冲突，评测方 clone 下来就能跑 |
| 审查手段 | AST 规则（确定性） + 模型语义审查（可选） | 规则负责"能判定"的部分，模型负责"要理解"的部分，两者不互相冒充 |
| 状态存储 | 内存字典 + JSON checkpoint | MVP 规模下不需要数据库；JSON 可读、可 diff、可人工检查 |
| 语言 | Python 3.10+ | `ast` 标准库、`match`、类型标注都够用 |

---

## 二、总体架构

```
┌────────────────────────────────────────────────────────────────────┐
│ 入口层   cli.py（run / graph / rules / resume）· report.py（报告）  │
└───────────────────────────────┬────────────────────────────────────┘
                                │
┌───────────────────────────────▼────────────────────────────────────┐
│ 编排层   pipelines.py（装配图）                                      │
│          graph.py  节点 + 条件边 + 静态校验                          │
│          engine.py 顺序推进 / 分支 / 循环 / 重试 / 快照              │
└───────┬───────────────────────────────────────┬────────────────────┘
        │ 共享黑板 state.py                      │ 消息总线 bus.py
┌───────▼───────────────────┐         ┌─────────▼───────────────────┐
│ Agent 层                  │         │ 基础设施                     │
│ coder / reviewer / fixer  │         │ llm/    provider 抽象+实现   │
│ base.py  Agent→Node 适配  │         │ review/ 规则 + 修复器        │
└───────────┬───────────────┘         │ workspace.py 文件安全        │
            │                         └─────────────────────────────┘
┌───────────▼────────────────────────────────────────────────────────┐
│ 配置层   config.py（.env / 环境变量 / CLI 参数，优先级从低到高）      │
└────────────────────────────────────────────────────────────────────┘
```

**分层原则**：Agent 不知道自己在图里的位置，引擎不知道 Agent 在干什么，报告层不参与执行。
每层只依赖下一层的抽象，所以替换任意一层都不牵连其它层。

---

## 三、状态设计：黑板而不是参数传递

### 3.1 为什么不是"节点函数互相传参"

最直觉的写法是 `fix(code)` 再 `review(result)`。但一旦要加分支、循环、断点续跑，参数传递就会退化成：

- 谁需要什么就得一路往下传，签名越改越乱；
- 循环时"上一轮的状态"和"这一轮的状态"混在一个变量里；
- 失败后无法回到"上一步之前"。

黑板的做法是把所有中间产物放进一个可序列化的字典，节点**按 key 取、按 key 写**：

```python
state.get("code")            # 上游写进来的代码
state.get("findings")        # 审查写进来的问题清单
state.set("iterations", 2)   # 修复写入的轮次
```

### 3.2 三个关键能力

| 能力 | 实现 | 为什么需要 |
|------|------|-----------|
| **可回滚** | 每步前 `snapshot()`，失败 `restore()` | 节点执行到一半抛异常，半截状态会污染后续判断 |
| **可审计** | 每次写入生成 `StateEvent(node, key, summary)` | 报告要说清楚"这个值是哪一步、谁改的" |
| **可序列化** | `to_dict()/from_dict()` 只允许 JSON 类型 | checkpoint 要能把整个黑板冻结到磁盘 |

`diff(before)` 返回 `{key: "old → new"}`，报告的时间线里"状态变更"一列就是它。

---

## 四、消息协议：显式的交接

### 4.1 信封字段

```python
Message(
    sender="reviewer", recipient="fixer",
    kind="result",                  # task / result / feedback / verdict / sys
    content="第 1 轮审查完成：高 3 / 中 0 / 低 6，请修复…",
    refs=["...上游消息 id..."],      # 引用了哪些消息
    artifacts={"findings": [...]},   # 结构化附件
    causation_id="...",              # 因果链：这条消息是谁引起的
    iteration=1, run_id="...",
)
```

### 4.2 为什么消息和状态都要有

听起来重复，其实分工不同：

- **状态**回答"现在是什么"（最新代码、最新问题清单）——下游只需要最新值；
- **消息**回答"发生过什么"（谁在第几轮因为什么交给了谁）——报告、排障、回放需要过程。

如果只有状态，协作过程就丢了；如果只有消息，每个节点都要自己从历史里翻最新值，容易翻错。
所以本项目**状态承载数据、消息承载意图**。

### 4.3 因果链

`causation_id` 让一条"修复完成"的 feedback 能追到"第 1 轮审查结论"，再追到"初稿交付"。
`message.thread_of()` 就是干这个的（有单测）。这在多轮循环里尤其有用：
第 2 轮的修复建议到底是不是针对第 2 轮的问题，看因果链比看时间戳可靠。

---

## 五、编排：条件边 + 回边 + 双重保护

### 5.1 图定义

```python
graph.add_edge("coder", "reviewer", label="交付初稿")
graph.add_edge("reviewer", "fixer", when=needs_fix, label="有高/中风险且未超轮次")
graph.add_edge("fixer", "reviewer", when=made_progress, label="本轮有改动 → 复检")
```

- `when` 是一个 `state -> bool` 的谓词，**分支与循环都是数据**；
- 谓词抛异常时按 `False` 处理——条件写错不应该炸掉整个流程；
- 静态校验会拦住三类常见错误：悬空边、不可达节点、一个节点多条无条件出边（顺序不确定）。

### 5.2 两条终止条件

循环不是靠"改到好为止"，而是靠明确的判定退出：

| 条件 | 代码 | 语义 |
|------|------|------|
| 没有高/中风险 | `needs_fix` 返回 False | 正常收敛，报告"通过" |
| 本轮没有改动 | `made_progress` 返回 False | 剩下的问题机器改不动，转人工 |
| 达到轮次上限 | `iterations > max_rounds` | 防止"改了又改"烧 token |

即使三条都没拦住，引擎还有 `max_visits`（单节点访问次数）和 `max_steps`（总步数）两道硬保护，
命中后状态是 `aborted` 而不是死循环——这一点有专门的单测（`test_loop_guard`）。

### 5.3 执行伪代码

```python
current = graph.start
while current:
    if len(steps) >= max_steps:            break   # 总步数保护
    if visits[current] >= max_visits:      break   # 单点循环保护

    before = state.snapshot()
    result = node.run(ctx) with retries            # 失败 restore(before) 再重试
    state.update(result.patch)
    bus.publish(result.messages)
    steps.append(StepRecord(..., changed=state.diff(before)))
    save_checkpoint(next_node=...)

    targets = graph.resolve(state, current)        # 条件边求值
    current = targets[0] if targets else None
```

---

## 六、三个 Agent

### 6.1 分工与输入输出

| Agent | 输入（读黑板） | 输出（写黑板） | 消息 |
|-------|----------------|----------------|------|
| **coder** | `task` | `code`、`code_path`、`code_history` | result → reviewer |
| **reviewer** | `code`、`code_path` | `findings`、`review_summary`、`verdict`、`findings_history` | result → fixer（有问题）/ verdict → *（通过） |
| **fixer** | `code`、`findings`、`iterations` | `code`、`fix_log`、`patches`、`last_progress`、`manual_todo` | feedback → reviewer / verdict → *（改不动） |

### 6.2 幂等：已有初稿就不重复生成

`--code existing.py` 时上游已经塞了 `state.code`，coder 检测到就跳过并回一条 result。
这样"从需求开始"和"从已有代码开始"是**同一张图**，不需要两套流程，也不需要 CLI 改起点。

### 6.3 审查 Agent：规则优先，模型补充

```python
static  = analyze(code, filename)          # 12 条 AST 规则，确定性
semantic = llm_review(code, static)        # 仅在线；要求返回 JSON 数组
findings = sort_findings(static + semantic)  # 去重：同 rule+line 不重复计
```

模型这一路失败**不拖垮审查**：捕获 `LLMError` 后只打警告，规则结论照样有效。
这是"可选增强"和"关键路径"该有的边界。

### 6.4 修复 Agent：两种手段，同一套闸

```
模型重写整份文件 ─┐
                  ├─→ 三道校验闸 ─→ 采纳 / 回滚
机械修复单点    ─┘
```

- **模型重写**：把问题清单和代码一起给模型，要求输出完整文件。采纳条件是
  `ast.parse` 通过、高危数不增加、问题总数不增加——任何一条不过，整份丢弃；
- **机械修复**：只处理改法唯一的四类问题（裸 except、`== None`、可变默认参数、无用 import），
  从**行号大的往小的改**（避免改动导致行号错位），每改一处都重新扫描校验；
- **改不动的**：写进 `manual_todo`，报告里单列"需人工处理"，不硬改。

这个设计的关键判断是：**自动修复的价值不在于"修得多"，而在于"修错的概率足够低"**。
一个把 `except Exception: pass` 自动改成 `raise` 的工具，会让所有调用方行为变化——
这种修法必须由人决定。

---

## 七、与项目 1 的关系

### 7.1 复用了什么

| 项目 1（`code-explain-agent`） | 本项目 | 迁移方式 |
|-------------------------------|--------|----------|
| `LLMProvider` 抽象、`LLMError.retryable` 分类 | `llm/base.py`、`openai_compat.py` | 重写为 `urllib`，保留"4xx 不重试、429/5xx 重试"的判断 |
| 离线模式（规则规划器兜底） | `llm/offline.py` + 确定性 Agent | 从"没有 Key 时的兜底"升级为"完整可交付路径" |
| AST 分析（`code_outline` / `code_metrics`） | `review/rules.py` | 从"给模型看的指标"变成"可直接判定的规则" |
| 路径安全、越权防护 | `workspace.py` | 增加备份与原子写 |
| `main.py` + `src/` 布局、`package.py`、`unittest` | 同左 | 保持一致，降低维护成本 |

### 7.2 改变了什么

项目 1 是**一个 Agent 的循环**：思考 → 调工具 → 观察 → 再思考。
本项目是**多个 Agent 的流水线**：每个 Agent 仍然可以调工具、问模型，但它的边界由工作流定义。

这个变化带来的最大收益是**可测试性**：项目 1 的 Agent 循环很难断言"它一定读了文件"，
而本项目的每个节点输入输出都是确定的数据结构，79 项测试里有一大半是纯函数级的。

### 7.3 怎么把项目 1 的 Agent 接成节点

```python
class CodeExplainAdapter(BaseAgent):
    role = "explain"

    def act(self, ctx):
        answer = hw1_agent.run(ctx.state.get("code"))
        return AgentResult(patch={"explanation": answer},
                           messages=[Message.result(self.name, "*", "解释完成")])
```

要点只有两个：**从 ctx.state 取输入**、**把结果写回 patch**。
Agent 内部是 ReAct 还是单次调用，工作流不关心——这正是"节点只依赖数据契约"的价值。

---

## 八、可靠性设计

| 层级 | 机制 | 代码 |
|------|------|------|
| 模型调用 | 指数退避 + 抖动，4xx（除 429）立即失败 | `openai_compat.chat` |
| 节点失败 | 重试前先回滚状态，避免半截数据 | `engine._run_node` |
| 修复安全 | 语法 / 问题数 / 新增高危 三道闸 | `review/fixes._validate` |
| 循环 | 轮次上限 + 单点访问上限 + 总步数上限 | `engine.run` |
| 断点 | 每步原子写 `checkpoint.json` | `checkpoint.save` |
| 文件 | 写前 `.bak` 备份、临时文件 + `replace` 原子替换 | `workspace.write_text` |
| 展示层 | `emit` 回调异常被吞掉 | `engine.emit` |

### 8.1 为什么 checkpoint 值得单独做

三种情况在真实使用里一定会遇到：

1. 模型限流，跑到第 3 轮失败；
2. 用户关掉窗口；
3. 修复把文件改坏了，想回到上一步。

没有 checkpoint 就只能从头再来一遍（重新烧 token、重新改文件）。
有了它，`resume --checkpoint` 能从"失败的节点"继续，且之前的状态、消息全在。

**一个容易忽略的细节**：失败的那一步不能算"已完成"，否则续跑会把同一个节点跑两遍。
`from_checkpoint` 会把末尾非 `ok` 的 step 弹掉（有单测覆盖）。

---

## 九、可观测性

### 9.1 三层观测

| 层 | 内容 | 面向谁 |
|----|------|--------|
| 控制台 | 每步一行：节点、耗时、说明；状态变化单独列 | 跑的时候看 |
| `report.md` | 结论 / 时间线 / 协作记录 / 修复明细 / 差异 / 遗留 | 交给老师、同事 |
| `trace.json` | 全量结构化记录 | 以后做统计、可视化 |

### 9.2 报告里的"状态变更"列

```
| 3 | fixer | ok | 1 | 16ms | 第 1 轮修复完成（有改动） | `code`、`fix_log`、`iterations`… |
```

这一列来自 `state.diff(before)`。它的价值是：看报告的人不用读代码，就知道每一步
"动了哪些数据"——这比"执行成功"这种废话有用得多。

---

## 十、扩展性

| 想做什么 | 改哪里 | 是否会碰到引擎 |
|----------|--------|----------------|
| 加一个 Agent | `register_agent("kind", Factory)` + 在图里加节点 | 不用 |
| 加一条审查规则 | `review/rules.py` 里加 `Rule` + 一个 `visit_*` | 不用 |
| 加一种自动修复 | `review/fixes.py` 的 `FIXERS` 表加一项 | 不用 |
| 换模型 / 厂商 | `llm/factory.py` 的 `PRESETS` 加一行 | 不用 |
| 改流程顺序 / 加分支 | `pipelines.py` 的边 | 不用 |
| 换成并行执行 | 引擎里 `resolve` 返回多目标时分派 | 只改引擎 |

`examples/custom_node.py` 是一个可运行的扩展示例：注册一个 `TesterAgent`，
在图尾加一个"编译 + 运行验证"节点，其余代码一行不动。

---

## 十一、测试

79 项单元测试（标准库 `unittest`，零额外依赖）：

| 文件 | 覆盖 |
|------|------|
| `test_state.py` | 点号路径、快照隔离、回滚、diff、审计事件、序列化 |
| `test_bus.py` | 消息校验、投递、latest、统计、因果链、序列化 |
| `test_graph.py` | 悬空边 / 不可达 / 歧义分支 / 条件异常降级 / 环检测 / 渲染 |
| `test_engine.py` | 线性、分支、失败回滚、重试、循环保护、步数保护、续跑 |
| `test_rules.py` | 12 条规则正例 + 干净代码零误报 + 部分使用 import |
| `test_fixes.py` | 4 类修复、幂等、非法修改回滚、模型重写三道闸 |
| `test_workflow_e2e.py` | 离线全流程、跳过编码、写回与备份、报告产物、CLI |

刻意测了三个"容易假装修好了"的点：

1. **修复幂等**：同一份代码修两遍，第二遍必须没有任何改动；
2. **回滚有效**：节点抛异常后，它写了一半的状态必须消失；
3. **零误报**：干净代码跑规则必须一条都不出——否则报告里的数字没有意义。

---

## 十二、已知局限与后续改进

1. **只支持 Python**：规则基于 AST；多语言需要各自的解析器（可先做 tree-sitter 的文本级规则）。
2. **顺序执行**：`Graph.resolve` 已支持多目标，但引擎取第一个；并行分支、join 节点是下一步。
3. **无 Web 界面**：项目 1 的 Streamlit 界面没搬过来。工作流的实时进度用"轮询 `trace.json`"最容易补。
4. **机械修复覆盖面窄**：只做改法唯一的四类。可以按"改完跑测试通过才采纳"扩展更多类型。
5. **语义问题依赖模型**：离线模式下规则只能看到结构和写法，看不到业务意图。
6. **checkpoint 不含文件快照**：只记状态，不记磁盘上的文件内容；回滚代码需要靠 `.bak`。
7. **报告里的 mermaid 未做渲染校验**：语法是标准的，但不同 Markdown 渲染器支持度不同。
