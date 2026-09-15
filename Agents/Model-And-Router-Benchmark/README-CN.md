# Azure OpenAI 模型与 Model Router 基准测试

[![CI](https://github.com/david-xinyuwei/david-share/actions/workflows/model-and-router-benchmark-ci.yml/badge.svg)](https://github.com/david-xinyuwei/david-share/actions/workflows/model-and-router-benchmark-ci.yml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab)](requirements.txt)
[![Direct matrix](https://img.shields.io/badge/direct_matrix-11_arms-b11f4b)](scenario-model-benchmark/README-CN.md)
[![Region](https://img.shields.io/badge/client%2Fresource-same_region-16a34a)](#methodology)
[![License](https://img.shields.io/badge/license-MIT-5c5c5c)](../../LICENSE)

为延迟敏感的助手选模型，不是查排行榜：结论取决于提示词、reasoning effort、API 路径
和部署容量。本项目在全部受支持的 `reasoning_effort` 上实测三个候选模型，用三种
Model Router 模式对比各自的直连基线，再补上试点通常要到生产才发现的两件事——多轮
会话成本和触及限流时的表现。4 项研究都从与部署同区域的 Linux VM 发起，不带联网搜索、
不挂任何工具，因此比较的是模型原生能力。在 561 个测量请求中，首 Token 中位耗时落在
**0.367 s 到 15.47 s** 之间，每 1,000 个请求的成本从 0.094 到 4.564 USD——时延相差
42×、成本相差 49×，差距来自配置而不是模型名字。

> Author: **Xinyu Wei (魏新宇)**

[English](README.md) | [中文](README-CN.md)

[执行摘要](#executive-summary) · [方法](#methodology) · [结果](#results) · [复现](#reproducing) · [Model Router 官方指南](https://learn.microsoft.com/azure/ai-foundry/openai/how-to/model-router)

---

<a id="executive-summary"></a>
## 执行摘要

**没有单一赢家。请按产品面，用绝对质量底线、TTFT 预算和真实请求量来选——在这批任务
上，"提高 reasoning effort"并没有换来更好的回答。**

| 候选模型 | TTFT P50 | E2E P50 | 每轮 Token | 每 1k 请求 USD | 盲评 / 5 | 测试条件 |
|---|---:|---:|---:|---:|---:|---|
| GPT-4o mini | **0.367 s** | 2.26 s | 243 | **0.094** | 4.53 | Responses API、`stream=True`、不发送 reasoning 参数、不挂工具 |
| GPT-5 mini `minimal` | 0.567 s | 2.20 s | 402 | 0.603 | 4.55 | 同上，`reasoning_effort=minimal` |
| GPT-5 mini `low` | 1.969 s | 3.97 s | 564 | 0.928 | 4.64 | 同上，`reasoning_effort=low` |
| GPT-5 mini `medium` | 5.302 s | 7.38 s | 967 | 1.733 | 4.76 | 同上，`reasoning_effort=medium` |
| GPT-5 mini `high` | 15.472 s | 17.55 s | 2 383 | 4.564 | 4.79 | 同上，`reasoning_effort=high` |
| GPT-5.6 Luna `none` | 1.074 s | **1.95 s** | 366 | 0.324 | 4.92 | 同上，`reasoning_effort=none` |
| GPT-5.6 Luna `low` | 1.171 s | 2.42 s | 387 | 0.350 | 4.82 | 同上，`reasoning_effort=low` |
| GPT-5.6 Luna `medium` | 1.408 s | 3.14 s | 413 | 0.380 | 4.92 | 同上，`reasoning_effort=medium` |
| GPT-5.6 Luna `high` | 1.918 s | 3.08 s | 468 | 0.447 | **4.95** | 同上，`reasoning_effort=high` |
| GPT-5.6 Luna `xhigh` | 2.590 s | 3.67 s | 571 | 0.570 | 4.94 | 同上，`reasoning_effort=xhigh` |
| GPT-5.6 Luna `max` | 3.414 s | 4.37 s | 697 | 0.722 | 4.88 | 同上，`reasoning_effort=max` |

> 每个实验组 51 个测量请求（17 条提示词 × 3 次迭代，另丢弃 1 次预热），11 个实验组
> 共 561 个测量请求。TTFT 与 E2E 为客户端观测中位数；Token 与成本为每请求均值，按
> 公开标价计算。质量为盲评模型在 5 个维度上的评分（187 次评估）。上表已列出全部已测
> 实验组；推理 Token 与逐场景明细见 [3.1 直连模型矩阵](#results)。

**本证据支持的配置**

| 配置项 | 取值 | 理由 |
|---|---|---|
| API 路径 | Responses API + 流式 | Chat Completions 有 497/564 条回答整段一次到达，导致 TTFT 与逐 Token 速率无法测量 |
| Reasoning effort | 各模型支持的最低值 | 在 GPT-5 mini 上，`high` 的成本高 7.6×、TTFT 高 27×，本样本中质量并未提升 |
| Router 模式 | 用 `cost` 或 `balanced`，不用 `quality` | `quality` 在 48.9% 的请求上选了更贵的模型，并在 18 个会话中有 18 个中途换了模型 |
| 限流处理 | 识别流内错误的回退，而不是看状态码重试 | 所有拒绝都发生在 HTTP 200 的流内部；只看状态码的重试从未触发 |
| 客户端位置 | 与部署同区域 | 跨区域调用会把网络耗时混进来，被误读成模型时延 |

<a id="background"></a>
## 1. 背景

被测负载是一款跨设备消费级助手，有六类面向用户的任务：Next Move、Write For Me、
Catch Me Up、Pay Attention、Live Interaction 和 Creator Zone。它们对延迟敏感、以
非推理文本任务为主，所以本研究优化的是首 Token 时延和每轮成本，而不是跑分。

| 输入 | 取值 | 来源 |
|---|---|---|
| 候选模型 | GPT-4o mini、GPT-5 mini、GPT-5.6 Luna | [`config/models.json`](scenario-model-benchmark/config/models.json) |
| Router 部署 | `model-router`，模式 `cost` / `balanced` / `quality` | [`model-router-validation`](model-router-validation/README-CN.md) |
| 标价 | 每 1M 输入 / 缓存 / 输出 Token | [`config/pricing.json`](scenario-model-benchmark/config/pricing.json) |
| 模型生命周期 | 通过 Models API 查询的退役日期 | [`model_lifecycle_swedencentral.json`](production-readiness/outputs/model_lifecycle_swedencentral.json) |

生命周期和时延同样重要：查询当天，GPT-5 mini `2025-08-07` 计划于 2027-02-09 退役，
GPT-4o mini `2024-07-18` 已对新客户弃用，而 GPT-5.6 系列可用至 2028-01-11。

<a id="methodology"></a>
## 2. 方法

| 控制项 | 实现方式 | **不能**证明什么 |
|---|---|---|
| 仅测原生能力 | 所有对比组均不带联网搜索、不挂工具 | 不涉及搜索或工具质量 |
| 同区域 | 基准 VM 与 Azure AI 资源在同一区域；导出时从 IMDS 重新读取 VM 位置 | Global 与 DataZone 服务不会公开物理 GPU 区域 |
| 提示词一致 | 每个矩阵单元使用相同的 17 条合成提示词 | 不代表客户生产流量或多语言质量 |
| 流式计时 | 首个非空文本增量记 TTFT，流结束记 E2E | 不是服务端计算、Prefill 或 GPU Decode 时间 |
| 失败记账 | `max_retries=0`；没有带回 usage 的流按失败处理 | 不代表已测窗口之外的可用性 |
| 成本 | 实测 usage × 公开标价，按实际服务模型计算 | 不是 Azure 账单；不含 Router 费、VM 和评审模型 |

各研究的样本量、SDK 版本与矩阵见各自的 README。Harness 每个研究一份，与实时控制台
调用的是同一份代码，因此回放出的图和现场测出的图含义相同。

**为什么 API 路径属于方法的一部分。** 在 Chat Completions 上，564 条测量回答中有 497
条的首尾文本块间隔不足 50 ms。这样测出的 TTFT 反映的是交付，不是生成。用同样的提示词
改走 Responses API 后，该比例降到 564 条中的 32 条，逐 Token 速率才变得可测。因此本
仓库的每条时延结论都标明了 API 路径。

<a id="results"></a>
## 3. 结果

### 3.1 直连模型矩阵

覆盖两个推理候选支持的每一档 reasoning effort，外加非推理基线：11 个实验组 × 6 个
场景、66 个单元格全部填满，每组 51 个测量请求、合计 561 个，0 个 API 错误、0 条被
截断的回答。

| 实验组 | TTFT P50 | E2E P50 | 每轮 Token | 推理 Token | 每 1k 请求 USD | 盲评 / 5 |
|---|---:|---:|---:|---:|---:|---:|
| GPT-4o mini | 0.367 s | 2.26 s | 243 | 0 | 0.094 | 4.53 |
| GPT-5 mini `minimal` | 0.567 s | 2.20 s | 402 | 0 | 0.603 | 4.55 |
| GPT-5 mini `low` | 1.969 s | 3.97 s | 564 | 133 | 0.928 | 4.64 |
| GPT-5 mini `medium` | 5.302 s | 7.38 s | 967 | 550 | 1.733 | 4.76 |
| GPT-5 mini `high` | 15.472 s | 17.55 s | 2 383 | 1 950 | 4.564 | 4.79 |
| GPT-5.6 Luna `none` | 1.074 s | 1.95 s | 366 | 0 | 0.324 | 4.92 |
| GPT-5.6 Luna `low` | 1.171 s | 2.42 s | 387 | 15 | 0.350 | 4.82 |
| GPT-5.6 Luna `medium` | 1.408 s | 3.14 s | 413 | 36 | 0.380 | 4.92 |
| GPT-5.6 Luna `high` | 1.918 s | 3.08 s | 468 | 92 | 0.447 | 4.95 |
| GPT-5.6 Luna `xhigh` | 2.590 s | 3.67 s | 571 | 187 | 0.570 | 4.94 |
| GPT-5.6 Luna `max` | 3.414 s | 4.37 s | 697 | 329 | 0.722 | 4.88 |

先按要求的模型分组，再按 effort 从最低到最高排列。在 Luna 上，6 档 effort 的盲评分布
在 4.82 到 4.95 之间，且不随 effort 高低单调变化；在 GPT-5 mini 上，盲评确实随
effort 上升，4 档从 4.55 升到 4.79——这 0.24 分的提升，代价是 7.6× 的成本和
27× 的 TTFT。逐场景拆解与盲评细节见
[场景研究](scenario-model-benchmark/README-CN.md)。

### 3.2 Model Router 的选择行为

10 个实验组、1,410 个测量请求，470 条盲评回答，282 个实验组与问题的组合。

**在本样本上，选择是可重复的。** 282 个组合中的每一个，在三次测量重复中都返回了同一个
模型，0 个组合发生切换。[逐问题 CSV](model-router-validation/outputs/router_question_hits.csv)
保留了每一次的服务序列。

| 模式 | 不发送 effort 时选用贵模型的占比 | effort 为 `low` 时 | 本样本的读法 |
|---|---:|---:|---|
| `balanced` | 4.3% | 4.3% | 绝大多数走便宜模型；贵模型只出现在 2 道受控复杂题上 |
| `cost` | 0.0% | 0.0% | 所有已测请求都走便宜模型 |
| `quality` | 48.9% | 48.9% | 两者混合；作者标注的复杂度并未构成路由阈值 |

决定选择的不是难度。`quality` 模式送往贵模型的简单题**比**复杂题还多
（[按难度档 CSV](model-router-validation/outputs/router_routing_by_tier.csv)）：

| 作者标注的难度档 | 请求数 | `balanced` 选用贵模型占比 | `quality` 选用贵模型占比 |
|---|---:|---:|---:|
| simple | 45 | 0.0% | 60.0% |
| moderate | 54 | 0.0% | 38.9% |
| complex | 42 | 14.3% | 50.0% |

真正与选择相关的是所要求的工作类型。在 40 个已测类目中
（[按类目 CSV](model-router-validation/outputs/router_routing_by_category.csv)），`quality`
把需要生成内容、或依赖模型自身知识作答的提示词交给贵模型 —— `factual_lookup`、`faq`、
`code_snippet`、`drafting`、`multi_step_math`、`code_reasoning` —— 而把加工既有文本的
提示词留给便宜模型：`summarization`、`structured_extraction`、`rewriting`、
`tone_shift_rewrite`、`keypoint_capture`。`balanced` 仅在 2 个类目上选用贵模型，且都属于
复杂档：`code_reasoning` 与 `constrained_reasoning`。

这些是合成提示词重复运行得到的观测计数；按任务类型的划分是本文作者对这 40 个类目的解读，
而非已公开的规则。它们不是生产环境的路由概率，也不是对内部路由规则的还原。

### 3.3 会话、持续负载与限流

- 一个 4 轮会话的成本是 4 个首轮的 0.90–1.14×。提示缓存没有起作用：没有任何请求
  重发过足够长的前缀。
- `quality` 模式在 18 个会话中有 18 个中途换了服务模型，`balanced` 为 18 个中 0 个。
  因此同一段对话内的语气和每轮成本都会变化。
- 在持续补充请求的负载下，三个直连候选在 4、8、16 并发时都完成了全部请求；容量为
  300 的 Router 部署在 16 并发时出现 237 次流内限流失败。**决定上限的是容量，不是
  模型。**
- 回退实验中观测到的每一次限流拒绝，都发生在 **HTTP 200 之后的流内部**。只按请求时
  状态码重试的客户端从未触发，结果与完全不回退一致（成功率 2.7%）。能识别流内错误的
  reactive 客户端达到 100%，代价是被挽救的请求 TTFT P50 增加 411 ms。

逐级数据、明细表和回退客户端见[生产就绪研究](production-readiness/README-CN.md)。

### 3.4 要求范围已完整覆盖

| 要求的直连模型 | 已测 effort | 覆盖位置 |
|---|---|---|
| GPT-4o mini | 不发送 reasoning 参数 | 直连矩阵 |
| GPT-5 mini | `minimal`、`low`、`medium`、`high` | 直连矩阵 |
| GPT-5.6 Luna | `none`、`low`、`medium`、`high`、`xhigh`、`max` | 直连矩阵 |

这正是要求的 3 个直连候选模型。它们的 11 个实验组穷尽了各自支持的 effort 档，并覆盖
6 个场景：66/66 个单元格，每组 51 个测量请求，共 561 个测量请求。直连模型测试要求
已经完整覆盖。

GPT-5.6 Sol **不是**第 4 个要求的直连候选模型。它只在 Router 研究中作为 Router 的
高能力选项以及匹配的直连基线出现；不发送 effort 和 `low` 的观测回答的是 Router
选择问题，而不是 Sol 的 effort 扫描问题。

模型注册表从较早的迁移基准继承了未使用的候选条目。出现在注册表里不会扩大本研究范围，
也不代表该模型已经测量。
[部署核验记录](scenario-model-benchmark/outputs/deployment_verification.json)
才是 3 个要求的直连部署的权威记录。

<a id="cost-analysis"></a>
## 4. 成本分析

成本按每个请求的实测 usage，乘以**实际服务该请求的模型**的公开标价计算，再折算到每
1,000 个请求。它不含 DataZone 溢价、Router 费用、评审模型、基准 VM 和探测流量，因此
是对比口径，不是账单。

| 问题 | 本证据给出的答案 |
|---|---|
| 单请求最便宜 | GPT-4o mini，0.094 USD / 1k 请求 |
| 低 effort 下最佳质量的代价 | GPT-5.6 Luna `none`，0.324 USD / 1k，是 GPT-4o mini 的 3.5× |
| 提高 effort 的代价 | GPT-5 mini `high`，4.564 USD / 1k，是自身 `minimal` 的 7.6× |
| 为质量而路由的代价 | `quality` 模式 7.533 USD / 1k，`cost` 模式为 0.496 |
| 每段会话的成本 | 每 1,000 个四轮会话 0.44 至 11.43 USD，取决于实验组 |

<a id="configuration"></a>
## 5. 配置

本证据支持的配置已列在[执行摘要](#executive-summary)。其中两条最容易做错：

1. **在这批任务上，reasoning effort 不是质量旋钮。** 每个模型接受的全部 effort 取值
   都实测过。调高它会增加 Token、时延和成本；盲评并没有把结果区分开。
2. **限流回退必须看流内部。** 按 HTTP 429 重试的参考策略在这里从未触发，因为服务先
   返回 200，随后在流内部失败。可用的客户端会在尚未输出任何内容时就切换后端。

<a id="reproducing"></a>
## 6. 复现

离线路径会从留存证据重建每一个已发布数字，且不调用任何模型。

### Linux

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

### Windows PowerShell

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

**验收条件**：最后一行输出
`PASS: 15/15 repository rules and all executable gates`。该命令不会创建任何云资源；
删除 `.venv` 即可清理。

**发起新的实测**会产生费用，并且需要你自己的 Azure 部署。各研究的 README 里都带了各自
的在线运行命令。请把基准 VM 放在与部署相同的区域，否则测到的是你的网络。实时控制台的
说明见 [`live-benchmark-console`](live-benchmark-console/README-CN.md)；它只绑定回环
地址，并要求在其前方配置认证与反向代理。本仓库不发布任何托管实例、Endpoint 或凭据。

<a id="evidence"></a>
## 7. 证据与边界

| 路径 | 内容 |
|---|---|
| [`scenario-model-benchmark/`](scenario-model-benchmark/) | 直连模型 × effort 矩阵、原始与全文记录、盲评、报告生成器 |
| [`model-router-validation/`](model-router-validation/) | Router 模式、直连基线、实际服务模型轨迹、文档门禁 |
| [`throughput-recalibration/`](throughput-recalibration/) | API 路径对比、阶梯并发、评分规则 v2 |
| [`production-readiness/`](production-readiness/) | 多轮会话、持续负载、限流回退、模型生命周期 |
| [`live-benchmark-console/`](live-benchmark-console/) | 回放与实时控制台、持久化运行历史、同区域 Runner |
| [`scripts/`](scripts/) | 公开边界脱敏与仓库门禁 |
| [`evidence/`](evidence/) | 去标识清单与可执行 Rule 结果 |

**本证据未覆盖的范围。** 提示词是合成数据，不是客户生产流量。质量由盲评模型给出且
接近量表上限，不等同于人工判断。未测量的还包括：超过 16 的并发、超过 90 s 的窗口，
以及大容量部署的限流出现点。有三个注册表模型从未部署，GPT-5.6 Sol 也没有做 effort
扫描；该边界见 3.4 节。两个提示词单元和一段依赖式会话含有标识信息，已从公开版
撤下正文；它们的数值字段、评分与回答哈希均未改动，因此各项聚合仍然覆盖它们。每项研究
都在自己的 `outputs/public_redaction.json` 中记录了该变换。

## 仓库结构

```text
Model-And-Router-Benchmark/
├── scenario-model-benchmark/
├── model-router-validation/
├── throughput-recalibration/
├── production-readiness/
├── live-benchmark-console/
├── scripts/
├── tests/
└── evidence/
```
