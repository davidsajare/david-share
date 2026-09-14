# 联想 Qira 模型与 Model Router 基准测试：芝加哥研讨会

[![CI](https://github.com/david-xinyuwei/david-share/actions/workflows/model-and-router-benchmark-ci.yml/badge.svg)](https://github.com/david-xinyuwei/david-share/actions/workflows/model-and-router-benchmark-ci.yml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab)](requirements.txt)
[![Matrix](https://img.shields.io/badge/direct_matrix-11_arms-b11f4b)](qira-scenario-model-benchmark/README-CN.md)
[![Region](https://img.shields.io/badge/client%2Fresource-Sweden_Central-16a34a)](#protocol-and-fairness-boundary)
[![License](https://img.shields.io/badge/license-MIT-5c5c5c)](../../LICENSE)

Azure 提供直连模型部署与 Model Router；本仓库提供 17 条合成 Qira 提示词、
Harness（测量程序）和留存证据。4 项研究覆盖 11 个直连模型/effort 实验组、3 种 Router 模式、
多轮会话与持续负载。主直连矩阵中，GPT-4o mini 的 TTFT P50 为 0.367 s；
GPT-5.6 Luna `none` 在低 effort 组中取得最高盲评分 4.92/5。

> Author: **Xinyu Wei (魏新宇)**

[English](README.md) | [中文](README-CN.md)

[立即使用](#use-it-now) · [实测结果](#measured-results) ·
[协议深挖](#protocol-and-fairness-boundary) ·
[证据](#evidence-and-executable-assets) ·
[Model Router 官方指南](https://learn.microsoft.com/azure/foundry/openai/how-to/model-router)

---

<a id="use-it-now"></a>
## 立即使用

| 目标 | 去哪里 | 订阅 / 副作用 |
|---|---|---|
| 阅读选型结论 | [实测结果](#measured-results) | 无 |
| 重建全部历史结论 | 执行下方离线验收路径 | 仅访问本地文件；不调用模型 |
| 复跑或演示基准测试 | [在线门户](http://linuxworkvm1-work.eastasia.cloudapp.azure.com/qira-benchmark/)或[自建控制台](qira-live-benchmark-console/README-CN.md#2-快速开始) | 查看已保存的运行不会调用模型；新建实时运行使用 Azure PAYGO |

### 在线门户

| 项目 | 值 |
|---|---|
| URL | http://linuxworkvm1-work.eastasia.cloudapp.azure.com/qira-benchmark/ |
| 用户名 | `lenovo-qira` |
| 密码 | `qira2026` |
| 测量 Runner | Sweden Central 的 Linux VM |

这里的账号是共享 Demo 登录，不是 Azure 凭据。UI 与持久化历史记录运行在 East Asia
Work VM；请求计时发生在 Sweden Central VM，位置靠近 Azure AI 资源。若 Runner
已解除分配，已保存的运行结果仍可读取，但无法启动新的实时运行。

### 离线验收——Linux Bash

```bash
git lfs version
git clone --filter=blob:none --sparse https://github.com/david-xinyuwei/david-share.git
git -C david-share sparse-checkout set Agents/Model-And-Router-Benchmark .github/workflows
git -C david-share lfs pull --include="Agents/Model-And-Router-Benchmark/**"
cd david-share/Agents/Model-And-Router-Benchmark
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --no-input -r requirements.txt
python scripts/validate_repo.py
```

### 离线验收——Windows PowerShell

```powershell
git lfs version
git clone --filter=blob:none --sparse https://github.com/david-xinyuwei/david-share.git
git -C david-share sparse-checkout set Agents/Model-And-Router-Benchmark .github/workflows
git -C david-share lfs pull --include="Agents/Model-And-Router-Benchmark/**"
Set-Location david-share\Agents\Model-And-Router-Benchmark
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-input -r requirements.txt
.\.venv\Scripts\python.exe scripts\validate_repo.py
```

**Done-When：**最后一行输出
`PASS: 15/15 repository rules and all executable gates`。这条路径不会调用
Azure 或模型。离线验收完成后可退出环境并删除 `.venv`；这条路径不会创建云资源。

### 状态与配置契约

| 状态 | 归属与持久化位置 | 操作方式 |
|---|---|---|
| 已记录研究证据 | 各研究已提交的 `outputs/`；受拆分清单约束，不可变 | 使用离线门禁重建 |
| 正在运行的实时任务 | 同区域 Runner 负责执行；浏览器保存 Run ID 并自动重连；Portal 镜像已完成运行的记录 | 需要取消时显式点击 **Stop**；标签页断开不会取消 |
| 历史运行 | Portal 历史存储中每个 Run 一个 JSON | 在 UI 中打开、导出或删除；被删除的运行会留下 Tombstone（删除标记），且不会再次出现 |

新部署先复制
[`qira-live-benchmark-console/.env.example`](qira-live-benchmark-console/.env.example)。
以下配置二选一：同区域本地 Runner 设置 `AZURE_OPENAI_ENDPOINT`；远端同区域
Runner 设置 `QIRA_RUNNER_URL`。控制台不得直接暴露公网；应绑定 Loopback（回环地址），
并在前方配置认证与 Reverse Proxy（反向代理）。

## Azure 提供什么，本仓库负责什么

| Azure / Model Router 提供 | 本仓库及其操作方负责 |
|---|---|
| 直连模型与 Router 推理 Endpoint | 相同提示词、模型/effort 矩阵与请求采集器 |
| 流式响应事件与 Usage（用量） | TTFT/E2E/TPOT 定义、失败记账及 Token/成本计算 |
| Router 实际服务模型元数据 | 请求级 Trace（轨迹）、直连基线与离线聚合 |
| Azure 身份、Quota（配额）与部署 Capacity（容量） | 同区域 Runner、显式配置与 PAYGO 授权 |
| 服务生命周期与模型版本 | 留存证据、公开版脱敏、测试、报告生成器与 CI |

收益是结果可复算：书面报告与控制台回放使用同一批留存记录。代价是操作方仍需负责
Capacity、身份、价格更新，以及合成提示词能否代表生产流量。

## 实际验证了什么，哪些属于 Demo

| 能力 | 实际验证内容 | 证据 | 不能证明什么 |
|---|---|---|---|
| 直连模型矩阵 | 相同 17 条提示词上的 11 个模型/effort 实验组 | [场景证据](qira-scenario-model-benchmark/outputs/) | 普适的模型赢家 |
| Model Router 行为 | 3 种模式对比 Sol/Luna 直连基线，并保留服务模型 Trace | [Router 报告](qira-model-router-validation/README-CN.md#结果) | 稳定的内部路由规则 |
| 生产路径 | 多轮会话、持续 4/8/16 并发、限流回退与生命周期 | [生产就绪报告](qira-production-readiness/README-CN.md) | Azure SLA 或所有 Capacity 配置 |
| 控制台历史回放 | 使用实时聚合器从留存记录重建图表 | [回放数据包](qira-live-benchmark-console/replay/replay_pack.json) | 查看页面时重新执行了请求 |
| 在线 Portal | 真实页面内登录、已部署 UI、Runner 标签、历史记录，以及 Runner 在线时的实时执行 | [UI 证据](evidence/ui-evidence.json) | 仅靠截图证明数值正确 |

留存的运行记录是实测证据，并非伪装成实时结果的预置测试数据。提示词是合成数据；
Portal 是展示与控制平面。绿色 `LIVE` 只证明 Portal 已连接 Runner；报告正确性由原始记录、哈希值、
Builder（生成器）和测试分别证明。

## 在线控制台走查

![处于 LIVE 模式的 Qira 实时基准测试控制台，显示 Sweden Central Runner 和模型矩阵](images/qira-live-console-desktop.png)

顶栏在运行前先回答研讨会的两个关键问题：测量路径是
**Sweden Central Runner**，请求配置为 **no web search · no tools**。左侧选择直连
模型、Router 模式、提示词和运行参数；右侧显示进度、TTFT、TPOT、Tokens/sec 与成本。
截图证明已部署的产品界面，不证明基准测试数值。桌面与手机截图、哈希值和
不证明项均记录在 [UI 证据](evidence/ui-evidence.json)。

<a id="evidence-and-executable-assets"></a>
## 证据与可执行资产

| 路径 | Contract |
|---|---|
| [`qira-scenario-model-benchmark/`](qira-scenario-model-benchmark/) | 直连矩阵、原始/全文证据、盲评与确定性结果生成器 |
| [`qira-model-router-validation/`](qira-model-router-validation/) | Router 数据集、服务模型 Trace、直连基线与双语文档门禁 |
| [`qira-followup-throughput-recalibration/`](qira-followup-throughput-recalibration/) | API 路径对比、闭批并发与 Judge-v2 证据 |
| [`qira-production-readiness/`](qira-production-readiness/) | 会话、持续负载、回退与生命周期证据 |
| [`qira-live-benchmark-console/`](qira-live-benchmark-console/) | 回放/实时 UI、持久化历史、登录门禁、远端 Runner 与 8513/8514/8515 Loopback 服务 |
| [`scripts/build_split_manifest.py`](scripts/build_split_manifest.py) | 对比不可变源 Tree 与新 Index；任何未声明的 Byte Drift（字节漂移）都会失败 |
| [`scripts/validate_repo.py`](scripts/validate_repo.py) | 统一离线检查布局、链接、双语事实、公开边界、报告与测试 |
| [`evidence/`](evidence/) | 拆分清单、可执行 Rule 结果、UI 证据与 SOP-68 结构映射 |

<a id="measured-results"></a>
## 实测结果

### Run 清单

| 研究 | 实际 Run ID | 测量范围 / 终态证据 |
|---|---|---|
| 直连场景矩阵 | `20260909_120534` | 561 个测量请求；含 Warm-up 共 748/748 完成；未留存整次运行 Wall Time |
| Model Router | `20260909_223737` | 1,410 个测量请求和 470 个盲评分；请求级证据完整 |
| 跟进实验 | `direct_20260910_074346`、`loadtest_20260910_085128` | 564 个直连测量请求和 768 个阶梯负载测量请求；已报告负载矩阵中 0 个错误 |
| 生产就绪 | `sessions_20260911_015218`、`sustained_20260911_020626`、`resilience_20260911_024156` | 360 个会话轮次和 3,770 条持续负载数值记录；每次 Run 均留存终态摘要 |

### 一条完整请求链

| 字段 | 留存值 |
|---|---|
| 工作标识 | Run `20260909_120534`、提示词 `NM01`、实验组 `gpt-4o-mini-bench`、测量迭代 2 |
| 请求路径 | Sweden Central Linux VM → 直连 Responses API Stream；Tools 为 `false`；未发送 effort |
| 终态 | `completed`；未截断；完整业务输出已留存 |
| 指标 | TTFT 358.8 ms；E2E 2811.0 ms；TPOT 22.81 ms；43.84 Visible Tokens/s |
| Usage / 成本 | 121 个 Prompt + 108 个 Completion + 0 个 Reasoning Token；按记录标价为 USD 0.00008295 |
| 来源 | [数据集行](qira-scenario-model-benchmark/datasets/qira_scenarios.jsonl) · [全文 Run](qira-scenario-model-benchmark/outputs/raw_fulltext/direct_20260909_120534.jsonl) |

实际输入：

```text
Current activity context: the user has a spreadsheet named Q3-Forecast.xlsx open with unsaved edits to the revenue tab, an unread email from the finance lead titled 'Forecast sign-off needed by Thursday', and a calendar block tomorrow at 10am called 'Q3 Planning'. Propose the three most useful next actions, ordered by urgency. For each, give a one-line rationale. Do not ask questions.
```

完整留存输出：

```text
1. **Save edits to the Q3-Forecast.xlsx spreadsheet.**
   This ensures that all your current work is preserved before making further changes or addressing other tasks.

2. **Respond to the unread email from the finance lead.**
   Since it requests a sign-off by Thursday, addressing it promptly is crucial to meet the deadline.

3. **Prepare for the Q3 Planning meeting tomorrow at 10am.**
   Reviewing relevant materials and planning discussion points will enhance your contribution during the meeting.
```

这条链把仓库自有输入、当前 Harness 路径、请求标识、终态业务输出和客户端观测指标串在
一起。它是一个实测单元，不替代 561 个请求的聚合结果。

### Qira 提示词上的直连候选模型

| 候选模型 | TTFT P50 | E2E P50 | 每轮 Token | 每 1,000 个请求 USD | 盲评 / 5 |
|---|---:|---:|---:|---:|---:|
| GPT-4o mini | **0.367 s** | 2.26 s | 243 | **0.094** | 4.53 |
| GPT-5 mini，`minimal` | 0.567 s | 2.20 s | 402 | 0.603 | 4.55 |
| GPT-5.6 Luna，`none` | 1.074 s | **1.95 s** | 366 | 0.324 | **4.92** |

来源：[场景基准测试执行摘要](qira-scenario-model-benchmark/README-CN.md#执行摘要)。
完整矩阵覆盖 Luna 的 `none` 至 `max`、GPT-5 mini 的 `minimal` 至 `high`，
以及不发送 Reasoning 参数的 GPT-4o mini。

### Model Router 观测结果

| Router 模式 | 不发送 effort 时的 Sol 占比 | `low` 时的 Sol 占比 | 本样本的含义 |
|---|---:|---:|---|
| `balanced` | 4.3% | 4.3% | 绝大多数请求由 Luna 服务；2 个受控复杂题出现 Sol |
| `cost` | 0.0% | 0.0% | 所有已测请求均由 Luna 服务 |
| `quality` | 48.9% | 48.9% | Sol/Luna 混合；复杂度标签并未界定出明确的 Router 阈值 |

来源：[Model Router 决策简报](qira-model-router-validation/README-CN.md#findings)。
这些数字是合成提示词重复运行的观测计数，不是生产路由概率，也不是对内部路由规则的
逆向推断。

### 生产侧结论

- 在持续补充请求的负载中，3 个直连候选模型在 4/8/16 个并发请求下均完成全部请求；
  Capacity 为 300 的 balanced Router 在 16 并发时出现 237 次流内限流失败。
- 回退实验中，所有限流拒绝都发生在 HTTP 200 流内部，而不是 HTTP 429。
  只看状态码的 Retry（重试）没有触发；识别流内错误的 Reactive（被动）与
  Proactive（主动）回退在已测窗口中均达到 100% 成功。
- Router `quality` 在 18/18 个已测 4 轮会话中切换过服务模型；`balanced` 为 0/18。

来源：[生产就绪决策摘要](qira-production-readiness/README-CN.md#决策摘要)。

<a id="protocol-and-fairness-boundary"></a>
## 协议与公平性边界

| 控制项 | 实际做法 | **不能**证明什么 |
|---|---|---|
| 模型原生能力 | 所有对比组均不带 Web Search、不挂工具 | 未测试 Search/Tool 质量 |
| 客户端位置 | Linux 基准测试 VM 与 Azure AI 资源均在 Sweden Central | Global/DataZone 服务不会公开物理 GPU 区域 |
| 提示词一致 | 每个矩阵单元使用相同的英文合成提示词 | 客户生产流量或多语言质量 |
| 流式计时 | 首个非空文本增量记 TTFT，Stream 完成记 E2E | 纯服务端计算、Prefill 或 GPU Decode 时间 |
| 失败记账 | `max_retries=0`；Usage 缺失或出现流错误时均按失败处理 | 已测窗口之外的可用性 |
| 成本 | Usage 乘以文档记录的模型标价 | Azure 账单、Router 费、VM、Judge 或区域溢价 |

6 个 Qira 产品面为 Next Move、Write For Me、Catch Me Up、Pay Attention、
Live Interaction 与 Creator Zone；留存数据集包含跨这些产品面的 17 个提示词案例。
Live Interaction 与 Creator Zone 仅以文本任务进行替代性评测；语音、视频和图像生成
质量不在评测范围内。

## 测试与拒绝路径

[立即使用](#use-it-now)中的单一验收命令会执行以下门禁：

| 门禁 | 正常路径 | 拒绝的 Mutation（变异）/ 故障 |
|---|---|---|
| 拆分来源 | 198 个源文件映射至 198 个目标文件 | 文件缺失、出现多余文件或存在未声明的字节变更 |
| 科学数据 | 93 个 `config/datasets/outputs/replay` 文件保持字节一致 | 证据 Blob 被修改或拆分清单过期 |
| 报告 | 原始记录重建 Router/跟进/生产报告与控制台回放 | 哈希值不符、缺行、重复单元或 README 过期 |
| 公开边界 | 保留 `PA01`/`PA03` 脱敏 Contract 与安全 Placeholder | 缺脱敏记录或存在明显真实凭据 |
| 控制台 | 实时/回放、认证、历史、重连与响应式布局 | 跨站修改、路径逃逸、历史重复、流断开或桌面 Chart 最小宽度 |
| 双语/文档 | 标题/表格/代码/数字一致，全部本地链接存在 | 数字漂移、链接缺失、旧路径残留或证据目标损坏 |

项目门禁还会解析所有 Python 源码，并为 `RUN-001` 至 `RUN-015` 各输出一条结果。
负向测试会修改 Contract 并确认门禁拒绝，而不是只重复正常路径。

## 兼容性、公开边界与证据

| 范围 | 已验证 | 边界 |
|---|---|---|
| 离线门禁 | Python 3.10+；固定 `openai==3.10.0`、`azure-identity==1.25.3` | 已做干净 Linux CI 与 Windows 验证；不声称 Windows 实时推理 |
| Browser UI（浏览器界面） | 当前 Chromium Desktop 与 390×844 Mobile Viewport | 截图不证明请求正确性 |
| Azure 认证 | 测量 VM 使用 Entra ID；API Key 字段保持可选 | 离线验收不需要 Azure 身份 |
| 公开数据 | 合成提示词；2 个内部会议记录单元撤下文本并保留哈希值 | 不是客户生产流量 |
| License | 仓库级 [MIT License](../../LICENSE) | Azure 服务、模型条款与客户数据仍分别受其规则约束 |

拆分源为不可变提交 `af65768bf2ddc88f7c45432598848cd233fc4aa3`。
[拆分清单](evidence/split-manifest.json)证明搬迁完整与 Byte Identity；
[Rule 结果](evidence/rule-results.json)把每项门禁映射至证据。2 个单元
（`PA01`、`PA03`）已撤下文本；所有数值字段、评分与 `response_sha256` 仍参与聚合。

## 仓库结构

```text
Model-And-Router-Benchmark/
├── qira-scenario-model-benchmark/
├── qira-model-router-validation/
├── qira-followup-throughput-recalibration/
├── qira-production-readiness/
├── qira-live-benchmark-console/
├── scripts/
├── tests/
├── evidence/
└── images/
```
