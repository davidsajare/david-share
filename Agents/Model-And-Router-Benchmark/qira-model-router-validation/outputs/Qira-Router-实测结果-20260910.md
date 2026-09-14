# Qira 任务 B：Foundry Model Router（GPT-5.6 Sol / GPT-5.6 Luna 两档）路由实测

测试日期：2026-09-09/10（UTC）；正式 run：`20260909_223737`。目标是回答会上提出的两个问题：**Router 什么时候切到 Sol、什么 case 会切**。
这是受控数据集上的路由行为验证，不是生产并发容量压测，也不是对 Sol/Luna 本身的全面能力评测。

## 结论先行（只对本轮 47 题、两档子集成立）

- **各模式的 Sol 使用比例不同**：`quality` 不发 reasoning_effort 时 48.9% 的请求落到 Sol，`low` 时 48.9%。`balanced` 和 `cost` 不发 effort 时 Sol 占比分别为 4.3% / 0.0%；具体触发题见第 3 节。
- **人工复杂度标签与实际选择不是同一个概念**：`quality` 模式下 simple/moderate/complex 三档的 Sol 占比为 60% / 39% / 50%。这只是本轮观测，不能据此推断内部阈值、关键词规则或模型能力需求。
- **同题稳定性**：全部 282 个 Router arm×题目组合中，0 个在 3 次测量间出现模型变化。`quality` 模式 3 次测量全部一致的题占 100%（`low`：100%）。每题给出样本 **Sol 命中率**；3 次观测不代表长期概率或确定性保证。
- **路由 trace 自报决策耗时**：中位数 20 ms、P95 24 ms；落到 Luna 的路由请求与直连 Luna 在相同题目上配对比较，TTFT 中位差 +45 ms（balanced）/ +71 ms（cost）；SKU 与测试时段不同，不能把差值归因于 Router。
- **成本与质量**：`quality` 模式千次成本 $7.53，为全量直连 Sol 的 67%、全量直连 Luna 的 16.3 倍；盲评均分 4.91，直连 Sol 4.88、直连 Luna 4.86。在本轮样例上 Luna 单独已经拿到很高的自动评分，因此 Router 的价值不能用本轮质量分差证明，需要用更难、更贴近客户真实流量的题集重测。

| 想要的效果 | 本轮数据支持的选择 | 说明 |
|---|---|---|
| 让更多请求有机会使用 Sol | 先验证 `quality` 模式 | 不保证只升级人工标为复杂的题；仍需质量验收 |
| 成本优先，主要使用 Luna | 比较 `cost`、`balanced` 与直连 Luna | balanced 存在少量 Sol 命中；不能与 cost 混称全部 Luna |
| 需要确定性、可审计的切换规则 | APIM 策略路由（对照组，未在本轮实测） | 本轮只观察 Model Router 的选择，不证明其内部规则 |

## 1. 已验证的测试矩阵

- 3 个 Model Router 部署（model-router 2025-11-18，子集固定为 gpt-5.6-sol 2026-07-09 + gpt-5.6-luna 2026-07-09）：routing mode 分别为 `balanced`、`cost`、`quality`。
- 2 个直连基线：`gpt-5.6-sol-dz`、`gpt-5.6-luna-dz`，作为“全量 Sol”“全量 Luna”两种策略的成本/时延/质量对照。
- 每个部署跑 2 种 reasoning_effort：不发送、`low`；此处不发送不等同于显式 none，也未观测服务内部实际采用的 effort。共 10 个 arm。
- 47 题：30 题受控题（每档 10 题）+ 17 题任务 A 的 Qira 六场景样例；合并后的 simple/moderate/complex 题数分别为 15/18/14。
- 10 × 47 ×（1 次预热 + 3 次测量）= 1880 次；1880/1880 completed，0 API 错误、0 截断、0 空答案；1410 次测量。
- 全部请求走 Chat Completions 流式接口并带 `Foundry-Features: ModelRouterControls=V1Preview`，从响应 `model_selection_details.model_router_details` 读取实际命中模型、路由耗时与尝试链；直连 arm 同样走 Chat Completions 以对齐 API 面。
- 探活被拒的是本轮 Azure OpenAI 资源级 Responses 调用路径；官方另有 Foundry 项目客户端 Responses 路径，不能概括成所有 Responses API 均不支持 Model Router。
- 不挂载搜索或任何工具；同一题所有 arm 的输出上限一致。

## 2. 环境与边界

- Sweden Central Linux VM（Standard_D4s_v5，IMDS 核验 location=SwedenCentral）→ 同区 AOAI 资源；与 AOAI 端点的 TCP 连接基线 4.26 ms。
- **SKU 差异**：3 个 Router 部署为 GlobalStandard；两条直连基线为 DataZoneStandard（Sol GlobalStandard 可用配额不足）。GlobalStandard 可能跨区执行推理，DataZone 限定在 EU 数据区；同区资源不保证同区 GPU，第 5 节的时延差值不是 Router 开销估计或上界。
- **价格口径**：所有成本按 Global 公开价、按**实际命中模型**计费单价计算（Sol $5/$0.50/$30，Luna $0.20/$0.02/$1.20，USD/1M）。DataZone 约高 10% 未计入；Model Router 自身按输入提示收取的路由费在渲染定价页上未找到，**未计入**。
- 两档子集是会上拍板的范围；客户生产环境更可能是 3～4 档，见第 8 节。
- 运行顺序按 arm 串行、并发=1；未随机交错时段，P95 为小样本描述。
- TTFT 从发送请求计到第一个非空文本块；TCP connect 只是网络基线，不能从 TTFT 扣掉后称为纯模型时间。流式块可能包含多个 token，decode/TPOT 是客户端估计而非 GPU 指标。
- 每次请求均包含同一个系统提示词（跨设备助手，直接简洁回答，不提澄清问题）与一条用户问题；没有会话历史，不代表长历史多轮会话的路由行为。
- 本轮切换指不同请求由 Sol 或 Luna 处理，不是观测到生成过程中换模型；Task B 只测不发 effort 与 low，未声称穷尽 Sol 的全部 effort。
- 历史收集器没有保留 finish_reason；完成数依据当时记录的 status、error 与 truncated 字段，无法追溯排除未记录的过滤或缺失终止事件。PR 复核后已加固新运行的终止判断与评分选择；原始数据未改写，原运行代码保存在 source_snapshot 供哈希核验。

## 3. 什么 case 会切到 Sol

### 3.1 按复杂度分档（各 Router arm 的 Sol 占比，n = 题数 × 3 次测量）

| Router arm | simple | moderate | complex | 全部 |
|---|---:|---:|---:|---:|
| router-sol-luna-balanced | 0/45（0%） | 0/54（0%） | 6/42（14%） | 6/141（4%） |
| router-sol-luna-balanced@low | 0/45（0%） | 0/54（0%） | 6/42（14%） | 6/141（4%） |
| router-sol-luna-cost | 0/45（0%） | 0/54（0%） | 0/42（0%） | 0/141（0%） |
| router-sol-luna-cost@low | 0/45（0%） | 0/54（0%） | 0/42（0%） | 0/141（0%） |
| router-sol-luna-quality | 27/45（60%） | 21/54（39%） | 21/42（50%） | 69/141（49%） |
| router-sol-luna-quality@low | 27/45（60%） | 21/54（39%） | 21/42（50%） | 69/141（49%） |

### 3.2 `quality` 模式下被送到 Sol 的题目类别（不发 effort / low）

| 分档 | 类别 | 题数 | Sol 命中（不发 effort） | Sol 命中（low） |
|---|---|---:|---:|---:|
| simple | factual_lookup | 2 | 6/6 | 6/6 |
| simple | faq | 2 | 6/6 | 6/6 |
| simple | grounded_followup | 1 | 3/3 | 3/3 |
| simple | instant_recall | 1 | 3/3 | 3/3 |
| simple | short_suggestion | 1 | 3/3 | 3/3 |
| simple | formatting | 2 | 3/6 | 3/6 |
| simple | short_form | 2 | 3/6 | 3/6 |
| simple | edit_intent_parsing | 1 | 0/3 | 0/3 |
| simple | executive_condense | 1 | 0/3 | 0/3 |
| simple | intent_classification | 2 | 0/6 | 0/6 |
| moderate | code_snippet | 1 | 3/3 | 3/3 |
| moderate | comparison | 1 | 3/3 | 3/3 |
| moderate | cross_device_continuity | 1 | 3/3 | 3/3 |
| moderate | drafting | 1 | 3/3 | 3/3 |
| moderate | prompt_expansion | 1 | 3/3 | 3/3 |
| moderate | short_draft | 1 | 3/3 | 3/3 |
| moderate | troubleshooting | 1 | 3/3 | 3/3 |
| moderate | classification_reasoning | 1 | 0/3 | 0/3 |
| moderate | conversational_turn | 1 | 0/3 | 0/3 |
| moderate | decision_extraction | 1 | 0/3 | 0/3 |
| moderate | planning | 1 | 0/3 | 0/3 |
| moderate | rewriting | 1 | 0/3 | 0/3 |
| moderate | structured_extraction | 1 | 0/3 | 0/3 |
| moderate | summarization | 2 | 0/6 | 0/6 |
| moderate | tone_continuation | 1 | 0/3 | 0/3 |
| moderate | tone_shift_rewrite | 1 | 0/3 | 0/3 |
| moderate | translate_and_summarize | 1 | 0/3 | 0/3 |
| complex | ambiguity_resolution | 1 | 3/3 | 3/3 |
| complex | code_reasoning | 1 | 3/3 | 3/3 |
| complex | constrained_reasoning | 1 | 3/3 | 3/3 |
| complex | long_form_draft | 1 | 3/3 | 3/3 |
| complex | multi_constraint_planning | 1 | 3/3 | 3/3 |
| complex | multi_step_math | 2 | 6/6 | 6/6 |
| complex | architecture_reasoning | 1 | 0/3 | 0/3 |
| complex | backlog_digest | 1 | 0/3 | 0/3 |
| complex | keypoint_capture | 1 | 0/3 | 0/3 |
| complex | long_form_synthesis | 1 | 0/3 | 0/3 |
| complex | proactive_suggestion | 1 | 0/3 | 0/3 |
| complex | root_cause_analysis | 1 | 0/3 | 0/3 |
| complex | tradeoff_analysis | 1 | 0/3 | 0/3 |

`quality`（不发 effort）从未送到 Sol 的题：C03, C05, C07, C08, CMU01, CMU02, CMU03, CZ02, LI01, M01, M03, M06, M07, M09, M10, NM01, PA01, PA02, S03, S04, S06, S07, WFM02, WFM03。
`quality`（不发 effort）至少一次送到 Sol 的 simple 题：LI02, NM03, PA03, S01, S02, S05, S08, S09, S10。

### 3.3 `balanced` / `cost` 模式送到 Sol 的题

- `router-sol-luna-balanced`：2 题 — C04（complex/constrained_reasoning，3/3）、C06（complex/code_reasoning，3/3）
- `router-sol-luna-balanced@low`：2 题 — C04（complex/constrained_reasoning，3/3）、C06（complex/code_reasoning，3/3）
- `router-sol-luna-cost`：0 题 — 无
- `router-sol-luna-cost@low`：0 题 — 无

### 3.4 单独看 Qira 六场景

下表仅使用 17 道 Qira 题，不与 30 道通用受控题混算；命中数均不含预热。

| 场景 | 题数 | balanced Sol/请求 | cost Sol/请求 | quality Sol/请求 | quality@low Sol/请求 |
|---|---:|---:|---:|---:|---:|
| CatchMeUp | 3 | 0/9 | 0/9 | 0/9 | 0/9 |
| CreatorZone | 2 | 0/6 | 0/6 | 3/6 | 3/6 |
| LiveInteraction | 2 | 0/6 | 0/6 | 3/6 | 3/6 |
| NextMove | 3 | 0/9 | 0/9 | 6/9 | 6/9 |
| PayAttention | 3 | 0/9 | 0/9 | 3/9 | 3/9 |
| WriteForMe | 4 | 0/12 | 0/12 | 6/12 | 6/12 |

全部 10 arm×6 场景的命中率、TTFT、标准化成本与自动评分见 [六场景明细](router_qira_scenarios.csv)。Pay Attention 只测转录文本，Live Interaction 只测文字代理，Creator Zone 只测提示词与编辑意图，不含语音、图像生成或动作执行。

## 4. 同题稳定性（每题 3 次测量）

| Router arm | 题数 | 3 次测量命中同一模型的题 | 出现切换的题 |
|---|---:|---:|---|
| router-sol-luna-balanced | 47 | 47（100%） | 无 |
| router-sol-luna-balanced@low | 47 | 47（100%） | 无 |
| router-sol-luna-cost | 47 | 47（100%） | 无 |
| router-sol-luna-cost@low | 47 | 47（100%） | 无 |
| router-sol-luna-quality | 47 | 47（100%） | 无 |
| router-sol-luna-quality@low | 47 | 47（100%） | 无 |

本轮有 0 个 arm×题目组合在测量之间改变模型。每题 **Sol 命中率**（`router_question_hits.csv`）的分母只有 3，不应表述为已知的生产流量切换概率。

## 5. 时延与 Router 开销

**Decode 边界：直连基线有 497/564 次测量的首末文本块跨度小于 50 ms。**同一收集器下，直连结果出现集中交付，不能把极高的客户端 decode_tps 当模型生成速度。本轮保留原值供排查，不据此比较 GPU decode 性能，也未定位集中交付发生在哪一层。

TTFT/E2E 为客户端观测值（含网络、排队与交付节奏）。客户端 TPOT = 首个文本块到最后一个文本块的跨度 ÷（可见输出 token − 1），只在流式逐块到达时才近似生成速度；“首末<50 ms”列给出整段近乎一次到达的请求数，该值高的行其 TPOT 不可当作模型解码性能。

| arm | Sol 占比 | 路由决策 P50/P95 ms | TTFT P50/P90/P95 s | E2E P50/P90/P95 s | 客户端 TPOT P50/P90 ms | 首末<50 ms | 输出 Token | 其中推理 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| router-sol-luna-balanced | 4% | 20 / 24 | 1.430 / 4.051 / 9.289 | 2.016 / 13.044 / 18.039 | 3.65 / 7.85 | 29/141 | 397 | 99 |
| router-sol-luna-balanced@low | 4% | 20 / 24 | 1.235 / 2.977 / 4.021 | 1.778 / 8.029 / 15.064 | 3.21 / 8.04 | 34/141 | 325 | 40 |
| router-sol-luna-cost | 0% | 20 / 24 | 1.501 / 3.877 / 6.939 | 2.188 / 8.812 / 15.696 | 3.24 / 7.77 | 29/141 | 399 | 105 |
| router-sol-luna-cost@low | 0% | 20 / 24 | 1.210 / 2.413 / 3.631 | 1.855 / 7.293 / 13.867 | 3.17 / 7.79 | 33/141 | 327 | 46 |
| router-sol-luna-quality | 49% | 20 / 26 | 1.917 / 4.300 / 11.453 | 2.587 / 13.831 / 20.123 | 4.22 / 12.00 | 36/141 | 375 | 100 |
| router-sol-luna-quality@low | 49% | 20 / 23 | 1.794 / 2.992 / 4.864 | 2.442 / 8.880 / 14.556 | 3.66 / 12.07 | 34/141 | 309 | 42 |
| gpt-5.6-luna-dz | 0% | - | 1.572 / 5.292 / 8.314 | 1.575 / 8.670 / 12.627 | 0.09 / 1.82 | 126/141 | 372 | 98 |
| gpt-5.6-luna-dz@low | 0% | - | 1.274 / 4.323 / 5.440 | 1.285 / 6.544 / 12.946 | 0.09 / 1.47 | 119/141 | 343 | 52 |
| gpt-5.6-sol-dz | 100% | - | 2.075 / 7.676 / 12.144 | 2.090 / 11.578 / 21.814 | 0.09 / 0.11 | 127/141 | 360 | 106 |
| gpt-5.6-sol-dz@low | 100% | - | 1.782 / 6.819 / 7.998 | 1.799 / 9.566 / 21.948 | 0.09 / 0.40 | 125/141 | 312 | 48 |

配对开销：把路由到模型 F 的请求与直连 F、相同 effort、相同题目的 TTFT 中位数逐题相减（正值 = 经过 Router 更慢）。

| Router arm | 命中模型 | 配对请求数 | ΔTTFT P25 | ΔTTFT P50 | ΔTTFT P75 |
|---|---|---:|---:|---:|---:|
| router-sol-luna-balanced | gpt-5.6-sol | 6 | -2242 ms | -2078 ms | -1564 ms |
| router-sol-luna-balanced | gpt-5.6-luna | 135 | -665 ms | +45 ms | +295 ms |
| router-sol-luna-balanced@low | gpt-5.6-sol | 6 | -5816 ms | -5509 ms | -3086 ms |
| router-sol-luna-balanced@low | gpt-5.6-luna | 135 | -450 ms | +74 ms | +254 ms |
| router-sol-luna-cost | gpt-5.6-luna | 141 | -732 ms | +71 ms | +319 ms |
| router-sol-luna-cost@low | gpt-5.6-luna | 141 | -1106 ms | +74 ms | +289 ms |
| router-sol-luna-quality | gpt-5.6-sol | 69 | -1009 ms | +179 ms | +997 ms |
| router-sol-luna-quality | gpt-5.6-luna | 72 | -644 ms | +161 ms | +481 ms |
| router-sol-luna-quality@low | gpt-5.6-sol | 69 | -1744 ms | +26 ms | +638 ms |
| router-sol-luna-quality@low | gpt-5.6-luna | 72 | -870 ms | +139 ms | +321 ms |

注意：直连基线是 DataZoneStandard、Router 是 GlobalStandard，且两者在不同时段测量；ΔTTFT 混有调度池和负载差异，不能证明 Router 增加、减少或不影响首字时延。

## 6. 成本与质量 vs 两种直连策略

| arm | $/千次 | vs 全量 Sol（同 effort） | vs 全量 Luna（同 effort） | $/千次 simple / moderate / complex | 质量/5 | 质量 simple / moderate / complex |
|---|---:|---:|---:|---|---:|---|
| router-sol-luna-balanced | 1.955 | -83% | +322% | 0.08 / 0.19 / 6.23 | 4.91 | 4.84 / 4.94 / 4.96 |
| router-sol-luna-balanced@low | 1.285 | -87% | +200% | 0.07 / 0.16 / 4.03 | 4.91 | 4.77 / 4.99 / 4.94 |
| router-sol-luna-cost | 0.496 | -96% | +7% | 0.08 / 0.20 / 1.32 | 4.91 | 4.89 / 4.94 / 4.89 |
| router-sol-luna-cost@low | 0.409 | -96% | -4% | 0.07 / 0.16 / 1.09 | 4.91 | 4.88 / 4.99 / 4.86 |
| router-sol-luna-quality | 7.533 | -33% | +1526% | 1.32 / 2.46 / 20.71 | 4.91 | 4.81 / 4.96 / 4.94 |
| router-sol-luna-quality@low | 5.927 | -39% | +1283% | 1.03 / 2.13 / 16.05 | 4.96 | 4.93 / 4.99 / 4.96 |
| gpt-5.6-luna-dz | 0.463 | -96% | +0% | 0.08 / 0.19 / 1.22 | 4.86 | 4.92 / 4.90 / 4.73 |
| gpt-5.6-luna-dz@low | 0.428 | -96% | +0% | 0.07 / 0.16 / 1.16 | 4.93 | 4.85 / 4.99 / 4.94 |
| gpt-5.6-sol-dz | 11.239 | +0% | +2327% | 2.07 / 4.18 / 30.14 | 4.88 | 4.89 / 4.80 / 4.96 |
| gpt-5.6-sol-dz@low | 9.777 | +0% | +2182% | 2.00 / 3.93 / 25.62 | 4.90 | 4.81 / 4.94 / 4.94 |

质量：同一个 judge-terra（gpt-5.6-terra）盲评，5 维 1–5 分，每个 arm×题只评第 1 个非预热答案，共 470 份；评分时不知道模型名与路由结果。
自动评分仅作初筛：无法消除同家族偏好与裁判随机性；本轮题目对 Luna 而言大多不难，因此 Sol 与 Luna 的分差很小，不能据此断言“Sol 不值钱”，只能说明**本轮题集不足以区分两档**，下一轮需要更难的题（长上下文、多约束规划、代码推理）。

## 7. Token 与总体消耗

- 正式 1880 次（含预热）：输入 162,120；输出 657,472；其中推理 136,296；命中 Sol 576 次。
- 按实际命中模型的 Global 公开价估算模型调用费 **$7.4058**（不含 Router 路由费、judge 调用、探活、VM/磁盘/网络）。
- 每个样例是独立单轮请求；多轮会话需逐轮相加 usage，本轮未测。

## 8. 扩展到 3～4 档的路径（Roadmap，非本轮结论）

1. 从 `router_question_hits.csv` 选取稳定命中 Sol 和 Luna 的题作为路由回归样例；另用参考答案与人工评分判断是否真的需要强模型，不能把路由选择本身当质量标签。
2. 经区域与模型支持核验后，在子集中加入中间档或更便宜档，观察 balanced 的分流比例如何变化；本轮两档 balanced 已存在少量 Sol 命中。
3. 对照组：用 APIM 策略（`apim-policy-ptu-routing.xml` 思路）按意图/长度做确定性路由，与 Model Router 在同一题集上比较命中分布、成本和时延，回答“黑盒自动 vs 可控确定”的取舍。
4. 用真实 Qira 多轮会话（带历史）重测：路由器看到的是整段上下文，单轮结论不能直接外推。

## 9. 可复核文件与保留

- [1410 次单轮明细](router_single_turn_sessions.csv)、[10 arm 汇总](router_arm_summary.csv)、[分档路由占比](router_routing_by_tier.csv)、[类别路由占比](router_routing_by_category.csv)、[每题命中率](router_question_hits.csv)、[配对开销](router_overhead.csv)。
- [完整数值 JSONL](router_20260909_223737.metrics.jsonl)、[470 份评分](quality_router_20260909_223737.jsonl)、[来源与校验](provenance_router_20260909_223737.json)、[部署核验](deployment_verification_router.json)。
- [压缩证据包](evidence_router_20260909_223737.json.xz)：保留全部数值字段、路由 trace 与每份输出的 SHA256；不含生成全文。
- 生成全文 `raw_fulltext/router_20260909_223737.jsonl` 由 VM 回收保留，每条 response_text 与数值记录的 response_sha256 逐条一致（`scripts/verify_router_fulltext.py`）。
- 资源收尾见 [resource_closeout_router.json](resource_closeout_router.json)：VM deallocated（未删除），部署保留供复核。
