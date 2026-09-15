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
| Router 模式 | 用 `cost` 或 `balanced`，不用 `quality` | `quality` 把 48.9% 的请求转给了 GPT-5.6 Sol，并在 18 个会话中有 18 个中途换了模型 |
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

### 2.1 测试集

两套提示词，都已提交在本仓库中，也都是合成的：由作者按六类任务编写，不是从客户流量
中抽样。

| 提示词集 | 文件 | 条数 | 构成 | 使用它的研究 |
|---|---|---:|---|---|
| 助手场景 | [`assistant_scenarios.jsonl`](scenario-model-benchmark/datasets/assistant_scenarios.jsonl) | 17 | Next Move 3、Write For Me 4、Catch Me Up 3、Pay Attention 3、Live Interaction 2、Creator Zone 2；每条自带 80 到 900 Token 的回答预算 | 直连矩阵：11 个实验组 × 17 条 × 3 次迭代 = 561 个测量请求 |
| Router Task B | [`router_taskb.jsonl`](model-router-validation/datasets/router_taskb.jsonl) | 47 | 上述 17 条助手提示词，加 30 条受控提示词（每个难度档 10 条），合计 40 个任务类目 | Router 研究：10 个实验组 × 47 条 × 3 次迭代 = 1,410 个测量请求 |

每条提示词在每个实验组上由盲评模型评一次：直连矩阵 11 × 17 = 187 次，Router 研究
10 × 47 = 470 次。难度档由作者在运行前标注，只是标签，不是路由阈值。公开副本撤回了
2 条助手提示词（PA01、PA03）的原文，因为其中出现了客户团队名称；它们的测量数据原样
保留，撤回集合由仓库门禁强制校验。

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

**建了什么。** 在同一资源上建了 3 个 Model Router 部署，每种路由模式一个——`cost`、
`balanced`、`quality`——均为 `model-router 2025-11-18`、GlobalStandard、容量 300，后面
都只挂 2 个模型：`gpt-5.6-sol 2026-07-09` 和 `gpt-5.6-luna 2026-07-09`
（[部署记录](model-router-validation/outputs/deployment_verification_router.json)）。
每个请求都发给 Router，从不直接发给模型；实际作答的模型从响应里的
`model_selection_details.model_router_details` 读出，逐请求记录。

**发了什么。** [2.1 测试集](#methodology)中的 47 条提示词，每条对每个 Router 测 3 次、
另加 1 次预热，不发送 `reasoning_effort` 与发送 `low` 各跑一遍。加上 4 个 Sol、Luna
直连基线，共 10 个实验组、1,410 个测量请求、470 条盲评回答。所有实验组都走
Chat Completions——Router 只开放这一个 API 面。

**在本样本上，选择是可重复的。** 282 个 Router 单元格（6 个 Router 实验组 × 47 条
提示词）在 3 次重复中全部返回同一个模型，0 个切换。`low` 实验组对每条提示词的路由
与不发送 effort 时完全一致。

**每个实验组的延迟、成本与质量。** 每组 141 个请求的客户端观测中位数；成本按实际作答
模型的标价计算；质量为每组 47 条回答的盲评
（[实验组汇总 CSV](model-router-validation/outputs/router_arm_summary.csv)）。请先看
*整段到达* 一列：直连 DataZone 基线的大多数回答是一次性整段到达的，它们的 TTFT 是交付
时间而不是首 Token 时间，所以这张表里 Router 减直连的差值**不是** Router 开销——那个
问题由下面的配对测试回答。

| 实验组 | 作答 Sol / Luna | TTFT P50 / P90 | E2E P50 | 整段到达 <50 ms | Router 决策 P50 ms | 输出 Token | 每 1k 请求 USD | 盲评 / 5 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `router-sol-luna-cost` | 0 / 141 | 1.50 / 3.88 s | 2.19 s | 29 / 141 | 20 | 399 | 0.50 | 4.91 |
| `router-sol-luna-cost@low` | 0 / 141 | 1.21 / 2.41 s | 1.86 s | 33 / 141 | 20 | 327 | 0.41 | 4.91 |
| `router-sol-luna-balanced` | 6 / 135 | 1.43 / 4.05 s | 2.02 s | 29 / 141 | 20 | 397 | 1.95 | 4.91 |
| `router-sol-luna-balanced@low` | 6 / 135 | 1.23 / 2.98 s | 1.78 s | 34 / 141 | 20 | 325 | 1.28 | 4.91 |
| `router-sol-luna-quality` | 69 / 72 | 1.92 / 4.30 s | 2.59 s | 36 / 141 | 20 | 375 | 7.53 | 4.91 |
| `router-sol-luna-quality@low` | 69 / 72 | 1.79 / 2.99 s | 2.44 s | 34 / 141 | 20 | 309 | 5.93 | 4.96 |
| `gpt-5.6-luna-dz` | 0 / 141 | 1.57 / 5.29 s | 1.57 s | 126 / 141 | — | 372 | 0.46 | 4.86 |
| `gpt-5.6-luna-dz@low` | 0 / 141 | 1.27 / 4.32 s | 1.28 s | 119 / 141 | — | 343 | 0.43 | 4.93 |
| `gpt-5.6-sol-dz` | 141 / 0 | 2.08 / 7.68 s | 2.09 s | 127 / 141 | — | 360 | 11.24 | 4.88 |
| `gpt-5.6-sol-dz@low` | 141 / 0 | 1.78 / 6.82 s | 1.80 s | 125 / 141 | — | 312 | 9.78 | 4.90 |

**每条提示词由哪个模型作答、花了多少。** 每个模式单元格依次是作答模型、3 次测量请求
的 TTFT 中位数、该题每 1,000 个请求的 USD
（[逐问题 CSV](model-router-validation/outputs/router_question_hits.csv)、
[逐请求 CSV](model-router-validation/outputs/router_single_turn_sessions.csv)；其中 2 条
提示词原文在公开副本中已撤回）：

| 难度档 | 提示词 | 类目 | `cost` | `balanced` | `quality` |
|---|---|---|---|---|---|
| simple | CZ02 | `edit_intent_parsing` | Luna · 0.81 s · 0.12 | Luna · 1.01 s · 0.13 | Luna · 1.27 s · 0.12 |
| simple | CMU03 | `executive_condense` | Luna · 1.00 s · 0.10 | Luna · 1.14 s · 0.09 | Luna · 1.20 s · 0.09 |
| simple | S01 | `factual_lookup` | Luna · 0.69 s · 0.03 | Luna · 1.23 s · 0.03 | Sol · 1.63 s · 0.75 |
| simple | S02 | `factual_lookup` | Luna · 1.50 s · 0.09 | Luna · 1.02 s · 0.09 | Sol · 2.25 s · 2.27 |
| simple | S09 | `faq` | Luna · 1.79 s · 0.30 | Luna · 1.64 s · 0.31 | Sol · 3.29 s · 7.60 |
| simple | S10 | `faq` | Luna · 1.21 s · 0.06 | Luna · 0.79 s · 0.06 | Sol · 1.71 s · 1.52 |
| simple | S07 | `formatting` | Luna · 1.05 s · 0.03 | Luna · 0.83 s · 0.03 | Luna · 0.97 s · 0.03 |
| simple | S08 | `formatting` | Luna · 1.00 s · 0.05 | Luna · 0.76 s · 0.04 | Sol · 1.79 s · 1.21 |
| simple | LI02 | `grounded_followup` | Luna · 1.04 s · 0.07 | Luna · 1.22 s · 0.06 | Sol · 1.68 s · 1.39 |
| simple | PA03 | `instant_recall` | Luna · 1.21 s · 0.05 | Luna · 1.66 s · 0.05 | Sol · 0.84 s · 1.28 |
| simple | S03 | `intent_classification` | Luna · 1.36 s · 0.10 | Luna · 1.36 s · 0.10 | Luna · 1.74 s · 0.09 |
| simple | S04 | `intent_classification` | Luna · 1.79 s · 0.16 | Luna · 1.70 s · 0.15 | Luna · 1.58 s · 0.16 |
| simple | S05 | `short_form` | Luna · 0.77 s · 0.05 | Luna · 1.01 s · 0.04 | Sol · 1.90 s · 1.13 |
| simple | S06 | `short_form` | Luna · 0.69 s · 0.02 | Luna · 0.70 s · 0.02 | Luna · 0.91 s · 0.02 |
| simple | NM03 | `short_suggestion` | Luna · 1.18 s · 0.05 | Luna · 1.26 s · 0.05 | Sol · 2.46 s · 2.09 |
| moderate | M07 | `classification_reasoning` | Luna · 0.93 s · 0.07 | Luna · 0.98 s · 0.08 | Luna · 1.00 s · 0.07 |
| moderate | M08 | `code_snippet` | Luna · 2.07 s · 0.35 | Luna · 1.92 s · 0.31 | Sol · 3.45 s · 10.34 |
| moderate | M04 | `comparison` | Luna · 1.65 s · 0.20 | Luna · 1.20 s · 0.20 | Sol · 1.99 s · 4.66 |
| moderate | LI01 | `conversational_turn` | Luna · 1.04 s · 0.07 | Luna · 0.91 s · 0.07 | Luna · 1.22 s · 0.07 |
| moderate | NM02 | `cross_device_continuity` | Luna · 1.53 s · 0.19 | Luna · 1.52 s · 0.18 | Sol · 2.20 s · 4.32 |
| moderate | CMU02 | `decision_extraction` | Luna · 1.95 s · 0.28 | Luna · 2.03 s · 0.24 | Luna · 2.20 s · 0.29 |
| moderate | M02 | `drafting` | Luna · 0.80 s · 0.11 | Luna · 0.97 s · 0.11 | Sol · 1.94 s · 2.51 |
| moderate | M09 | `planning` | Luna · 1.32 s · 0.26 | Luna · 1.03 s · 0.23 | Luna · 1.42 s · 0.25 |
| moderate | CZ01 | `prompt_expansion` | Luna · 1.08 s · 0.26 | Luna · 1.33 s · 0.30 | Sol · 1.98 s · 6.31 |
| moderate | M06 | `rewriting` | Luna · 0.86 s · 0.03 | Luna · 0.76 s · 0.03 | Luna · 0.90 s · 0.03 |
| moderate | WFM04 | `short_draft` | Luna · 1.86 s · 0.37 | Luna · 1.37 s · 0.34 | Sol · 1.78 s · 6.90 |
| moderate | M03 | `structured_extraction` | Luna · 0.76 s · 0.06 | Luna · 0.88 s · 0.06 | Luna · 1.19 s · 0.06 |
| moderate | M01 | `summarization` | Luna · 1.00 s · 0.07 | Luna · 0.98 s · 0.07 | Luna · 1.28 s · 0.07 |
| moderate | M10 | `summarization` | Luna · 0.82 s · 0.06 | Luna · 0.86 s · 0.06 | Luna · 0.86 s · 0.06 |
| moderate | WFM02 | `tone_continuation` | Luna · 1.54 s · 0.28 | Luna · 1.66 s · 0.24 | Luna · 1.61 s · 0.27 |
| moderate | WFM03 | `tone_shift_rewrite` | Luna · 2.00 s · 0.20 | Luna · 2.53 s · 0.21 | Luna · 2.07 s · 0.16 |
| moderate | PA02 | `translate_and_summarize` | Luna · 1.79 s · 0.29 | Luna · 1.91 s · 0.30 | Luna · 1.94 s · 0.29 |
| moderate | M05 | `troubleshooting` | Luna · 2.38 s · 0.36 | Luna · 2.12 s · 0.31 | Sol · 4.15 s · 7.63 |
| complex | C09 | `ambiguity_resolution` | Luna · 1.45 s · 0.53 | Luna · 1.96 s · 0.48 | Sol · 3.05 s · 11.57 |
| complex | C03 | `architecture_reasoning` | Luna · 2.44 s · 0.89 | Luna · 2.87 s · 0.94 | Luna · 3.02 s · 0.86 |
| complex | CMU01 | `backlog_digest` | Luna · 2.49 s · 0.45 | Luna · 2.41 s · 0.44 | Luna · 2.32 s · 0.43 |
| complex | C06 | `code_reasoning` | Luna · 5.80 s · 1.05 | Sol · 9.45 s · 26.65 | Sol · 8.86 s · 25.32 |
| complex | C04 | `constrained_reasoning` | Luna · 9.50 s · 1.77 | Sol · 14.03 s · 44.86 | Sol · 15.49 s · 48.18 |
| complex | PA01 | `keypoint_capture` | Luna · 1.60 s · 0.56 | Luna · 1.11 s · 0.59 | Luna · 1.13 s · 0.51 |
| complex | WFM01 | `long_form_draft` | Luna · 2.16 s · 3.29 | Luna · 1.72 s · 3.08 | Sol · 3.04 s · 64.92 |
| complex | C08 | `long_form_synthesis` | Luna · 2.00 s · 2.39 | Luna · 1.71 s · 2.37 | Luna · 2.04 s · 2.47 |
| complex | C10 | `multi_constraint_planning` | Luna · 8.29 s · 3.89 | Luna · 7.75 s · 4.31 | Sol · 11.27 s · 92.73 |
| complex | C01 | `multi_step_math` | Luna · 7.92 s · 1.48 | Luna · 8.20 s · 1.28 | Sol · 12.36 s · 30.20 |
| complex | C02 | `multi_step_math` | Luna · 2.48 s · 0.43 | Luna · 2.63 s · 0.52 | Sol · 3.50 s · 11.05 |
| complex | NM01 | `proactive_suggestion` | Luna · 1.69 s · 0.25 | Luna · 1.55 s · 0.23 | Luna · 2.04 s · 0.23 |
| complex | C05 | `root_cause_analysis` | Luna · 3.00 s · 0.59 | Luna · 2.63 s · 0.48 | Luna · 3.55 s · 0.51 |
| complex | C07 | `tradeoff_analysis` | Luna · 2.60 s · 0.95 | Luna · 4.05 s · 1.04 | Luna · 2.96 s · 0.99 |

**按难度档计数。** 按行横着读：Sol + Luna 等于该行的请求数。百分比是 Sol 在该档内的
占比，所以这一列本来就不会加到 100%。

| 作者标注的难度档 | 提示词 | 请求数 | `cost`：Sol / Luna | `balanced`：Sol / Luna | `quality`：Sol / Luna |
|---|---:|---:|---:|---:|---:|
| simple | 15 | 45 | 0 / 45 (0.0%) | 0 / 45 (0.0%) | 27 / 18 (60.0%) |
| moderate | 18 | 54 | 0 / 54 (0.0%) | 0 / 54 (0.0%) | 21 / 33 (38.9%) |
| complex | 14 | 42 | 0 / 42 (0.0%) | 6 / 36 (14.3%) | 21 / 21 (50.0%) |
| 全部 | 47 | 141 | 0 / 141 (0.0%) | 6 / 135 (4.3%) | 69 / 72 (48.9%) |

`cost` 从未选过 Sol。`balanced` 只在 2 条提示词上选了 Sol，都是复杂档：`code_reasoning`
与 `constrained_reasoning`。`quality` 在 47 条中有 23 条选了 Sol，且并不跟随作者标注的
难度档——送往 Sol 的简单题占比高于复杂题。真正与选择相关的是所要求的工作类型：需要
生成内容、或依赖模型自身知识作答的提示词由 Sol 作答（`factual_lookup`、`faq`、
`code_snippet`、`drafting`、`multi_step_math`、`code_reasoning`），加工既有文本的提示词由
Luna 作答（`summarization`、`structured_extraction`、`rewriting`、`tone_shift_rewrite`、
`keypoint_capture`）。

**Router 有没有增加延迟？相同条件下的配对测试。** 上面那张表回答不了这个问题：Router
组跑在 GlobalStandard，直连基线跑在 DataZoneStandard，各组串行运行，直连回答又是整段
到达。一次补充运行把这些全部排除。`router-sol-luna-cost`——它把每条提示词都转给
Luna——与直连 `gpt-5.6-luna` 部署对测：两边都是 GlobalStandard、都走 Chat Completions、
都不发送 `reasoning_effort`、同样的 45 条公开提示词，每条提示词按 Router/直连**背靠背
成对**测量，1 对预热加 3 对测量，轮换先发哪一边以抵消顺序偏差
（[设计与已执行源码](model-router-validation/scripts/paired_overhead.py)、
[部署记录](model-router-validation/outputs/paired_deployment_record_20260915.json)、
[配对 CSV](model-router-validation/outputs/router_overhead_paired_20260915_162607.csv)、
[汇总 CSV](model-router-validation/outputs/router_overhead_paired_summary.csv)）。

| 实验组 | SKU | n | TTFT P50 / P90 | E2E P50 | 整段到达 <50 ms | 输出 Token | 每 1k 请求 USD | Router 自报 ms P50 / P95 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `router-sol-luna-cost` | GlobalStandard | 135 | 2.12 / 4.80 s | 2.65 s | 17.8% | 392 | 0.49 | 20 / 26 |
| `gpt-5.6-luna` | GlobalStandard | 135 | 2.28 / 6.47 s | 2.29 s | 88.9% | 379 | 0.47 | — |

逐对差值（Router 减直连），取两边都由 Luna 作答的 135 对（0 对因作答模型不同被排除）。
端到端时间是可比的那一列；最后一列保留下来是为了说明 TTFT 为什么不可比：

| 配对集合 | n | ΔE2E P25 ms | P50 | P75 | P90 | 均值 | Router 更慢（E2E） | ΔTTFT P50 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 全部 | 135 | -97 | 324 | 992 | 1569 | 501 | 71.9% | -147 |
| 先发 Router | 45 | -97 | 261 | 956 | 2044 | 654 | 71.1% | -158 |
| 先发直连 | 90 | -32 | 324 | 992 | 1533 | 424 | 72.2% | -147 |
| simple | 42 | 92 | 488 | 946 | 1350 | 556 | 81.0% | 357 |
| moderate | 54 | -32 | 187 | 775 | 1533 | 390 | 70.4% | -152 |
| complex | 39 | -652 | 264 | 1490 | 5086 | 594 | 64.1% | -1966 |

读法：两边的流式行为并不一样。直连回答有 88.9% 是整段一次到达的，经 Router 只有
17.8%，所以直连的 TTFT 大多是整段回答的交付时间，TTFT 那一列比的是流式交付层而不是
Router——在长回答上它甚至让 Router 看起来首 Token 更快。看**端到端时间**：其他条件全部
相同时，在 Luna 前面加上 Router 使**中位数增加 324 ms**（四分位区间 -97 到
992 ms，P90 为 1569 ms），71.9% 的配对中 Router 一侧更慢，且先发哪一边结果一致
（中位数 261 ms 与 324 ms），这正是排除顺序偏差的依据。Router 自报的决策时间
中位数是 20 ms，所以增加的时间大部分是多出来的那一跳及其交付路径，而不是决策本身。
经 Router 的回答平均 392 个输出 Token，直连为 379 个，同一个模型、同一个标价；
Router 本身如有费用，不在这些数字里。

这些是合成提示词重复运行得到的观测计数；按任务类型的读法是本文作者对这 40 个类目的
归类，而非已公开的规则。它们不是生产环境的路由概率，也不是对内部路由规则的还原。
配对开销来自一对部署、一个区域、一个晚上、并发 1。

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
