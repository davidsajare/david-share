# Lenovo Qira — 生产就绪测试：多轮会话、持续负载、故障回退、生命周期

前三份研究（[场景基准](../qira-scenario-model-benchmark/README-CN.md)、[Router 验证](../qira-model-router-validation/README-CN.md)、[跟进实验](../qira-followup-throughput-recalibration/README-CN.md)）测的都是并发 1 的单轮请求，并已如实说明。本轮补上做生产决策还缺的四项：**多轮会话的成本与时延**、**持续并发下的稳态吞吐**、**触及 PAYGO 限流时有无回退的表现**、以及**模型生命周期与 API 兼容性**。同一台 Sweden Central VM、同一资源、不带搜索或工具，使用加固后的 harness（`max_retries=0`、必须收到 usage）。

> 作者: **Xinyu Wei (魏新宇)** · 2026-09-11 · 运行 `sessions_20260911_015218`、`sustained_20260911_020626`、`resilience_20260911_024156`；生命周期查询于 2026-09-11

[English](README.md) | [中文](README-CN.md)

## 决策摘要

- **Qira 的预算要按会话算，不按单轮算——在这些脚本上两者相差不大。**一个 4 轮会话的成本是 4 个首轮的 0.90–1.14 倍：重发历史让所有组的 prompt token 从第 1 轮到第 4 轮持续增长（见下表），但脚本把后续轮次的输出上限设得比开场轮短（`datasets/qira_sessions.jsonl` 中为 400 → 200 → 200 → 80 输出 token，属设计选择），两个效应在这里大体抵消。每 1,000 个会话：gpt-4o-mini-bench $0.44 … router-sol-luna-quality $11.43。**提示缓存没有贡献：360 轮里 0 轮报告了缓存 token。**缓存要求存在一个早先请求已经发送过的 ≥ 1,024 token 前缀；本轮会话中之后被作为前缀重发的最长 prompt 为 924 token。更长的对话或长系统提示词会越过这条线——本轮未测。
- **Model Router 的 `quality` 模式在 18 个会话中有 18 个中途换了模型；`balanced` 模式 18 个中 0 个。**quality 模式的 Sol 占比随对话推进上升：第 1 轮到第 4 轮为 33% → 33% → 67% → 83%，观察到的会话序列有 `luna>luna>sol>sol`、`luna>sol>luna>sol`、`luna>sol>sol>sol`、`sol>luna>luna>luna`、`sol>luna>sol>sol`。使用 quality 模式的 Qira 用户在同一段对话里会被不同模型回答——语气与格式可能逐轮变化，每轮成本也不可预测。balanced 模式每一轮都由 Luna 服务。每种模式 18 个会话；这是观测到的规律，不是规则。
- **持续负载：**持续补充请求（目标并发在每个计数窗口内维持 100%），capacity 1000 的三个候选模型在 4/8/16 并发下全部完成；capacity 300 的 balanced Router 在 16 并发下返回 **237 次 429**（8 并发 0 次，4 并发 0 次）。上限由 capacity 决定，不由模型决定。
- **PAYGO 上回退是必需的，而且必须盯着流本身，不能只看状态码。**用 8 并发压 capacity 10 的 Luna 部署，没有回退的客户端成功率 **2.7%**。三种策略下观察到的每一次限流拒绝，**都是在 HTTP 200 之后以流内错误事件的形式到达**：计数窗口内 473 次（含预热与截止时被切断的记录共 585 次），请求时 0 次，HTTP 429 共 0 次。第一版只在请求时按状态码重试的回退客户端——即 APIM `<retry condition="429">` 的语义——因此从未触发，结果与无回退相同（证据保留在 `outputs/resilience_v1_request_time_only/`）。修正后的客户端在任何内容送达之前遇到错误即切换后端：被动回退成功率 **100.0%**（108 次流内切换），被救回的请求 TTFT 增加 P50 411 ms / P90 1035 ms；主动回退（主部署利用率 ≥ 80% 时直接走备用，缓存 60 s）成功率 **100.0%**，其中 122 次请求完全没碰主部署。
- **生命周期是选型条件，不是脚注：**按 2026-09-11 的 Models API，**gpt-5-mini 2025-08-07 将于 2027-02-09 退役**，**gpt-4o-mini 2024-07-18 已对新客户停用**（2027-04-14 退役）；GPT-5.6 系列到 2028-01-11。`model-router` 只声明 `chatCompletion` 能力——与 #2 中观察到的 Responses 路径被拒一致。

## 1. 多轮会话：一次对话的真实成本

六个脚本化会话，每个 Qira 场景一个，各 4 轮且轮次相互依赖（第 k 轮针对模型自己在第 k−1 轮的回答提问）。无状态回放——系统消息、此前的 user/assistant 对、新的 user 消息——在 Responses（三个候选）与 Chat Completions（两个 Router 模式）上完全一致；每个会话 3 次迭代；共 360 轮，全部完成。成本按实际服务模型的 Global 牌价。

| 实验组 | Prompt token T1 → T4 | 命中缓存的轮次 | TTFT P50 T1 → T4 s | 每会话等待（ΣTTFT）P50 s | 美元 / 1,000 会话 | 会话 ÷ 4×T1 | Router：Sol 轮占比 / 中途换模会话数 |
|---|---:|---:|---:|---:|---:|---:|---:|
| gpt-4o-mini-bench | 120 → 553 | 0/72 | 0.35 → 0.43 | 1.69 | 0.441 | 1.14× | — |
| gpt-5-mini@minimal | 119 → 890 | 0/72 | 0.78 → 0.63 | 3.06 | 2.212 | 0.90× | — |
| gpt-5.6-luna@none | 119 → 640 | 0/72 | 0.94 → 0.73 | 3.10 | 0.910 | 1.03× | — |
| router-sol-luna-balanced | 119 → 686 | 0/72 | 1.91 → 1.31 | 5.89 | 1.128 | 0.98× | 0% / 0 |
| router-sol-luna-quality | 119 → 680 | 0/72 | 1.93 → 1.60 | 6.69 | 11.433 | 1.11× | 54% / 18 |

- 逐轮明细（prompt/缓存/输出 token、TTFT/E2E、成本、各轮 Sol 占比）见 [sessions_by_turn.csv](outputs/sessions_by_turn.csv)；各会话 Router 序列见 [sessions_router_sequences.csv](outputs/sessions_router_sequences.csv)。
- “会话 ÷ 4×T1”把实测的 4 轮成本与 4 个首轮相比。小于 1 表示后续轮次虽然带着历史，仍比开场轮便宜；这与这些脚本相关，开场很长且被多次重发的对话会增长快于线性。
- 零缓存命中是预期行为，不是缺陷：这些会话中没有任何请求带有早先请求已发送过的 ≥ 1,024 token 前缀（见 [sessions_by_arm.csv](outputs/sessions_by_arm.csv) 的 `max_reusable_prefix_tokens`）。

## 2. 持续负载：4 / 8 / 16 并发的稳态

每个级别运行 90 s，正好 N 个工作线程，一个请求结束立刻发起下一个；前 15 s 为预热，只统计在预热后开始、在截止前结束的请求（“达到目标并发的窗口占比”一列由逐请求起止偏移量重算，表示 N 个请求真正同时在飞的时间比例）。提示词循环使用 17 道 Qira 场景题。本表替代 #3 中的封闭批次表——那里 ≤16 的行只有 22–45% 的时间处于目标并发。

| 实验组 | N | 完成 | 429 | 处于 N 并发的窗口占比 | req/s | 输出 tok/s（含推理） | 可见 tok/s | TTFT P50 / P90 / P95 s | E2E P50 / P90 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| gpt-4o-mini-bench | 4 | 114/114 | 0 | 100% | 1.52 | 196 | 196 | 0.34 / 0.81 / 1.47 | 1.75 / 4.79 |
| gpt-4o-mini-bench | 8 | 251/251 | 0 | 100% | 3.35 | 435 | 435 | 0.34 / 0.63 / 0.92 | 1.74 / 4.31 |
| gpt-4o-mini-bench | 16 | 506/506 | 0 | 100% | 6.75 | 876 | 876 | 0.34 / 0.66 / 1.00 | 1.64 / 4.20 |
| gpt-5-mini@minimal | 4 | 90/90 | 0 | 100% | 1.20 | 315 | 315 | 0.53 / 1.30 / 2.13 | 2.04 / 5.61 |
| gpt-5-mini@minimal | 8 | 191/191 | 0 | 100% | 2.55 | 700 | 700 | 0.53 / 1.17 / 1.42 | 1.94 / 4.82 |
| gpt-5-mini@minimal | 16 | 391/391 | 0 | 100% | 5.21 | 1416 | 1416 | 0.50 / 1.27 / 2.09 | 2.06 / 4.57 |
| gpt-5.6-luna@none | 4 | 86/86 | 0 | 100% | 1.15 | 228 | 228 | 0.79 / 1.12 / 1.31 | 1.85 / 3.62 |
| gpt-5.6-luna@none | 8 | 161/161 | 0 | 100% | 2.15 | 474 | 474 | 0.80 / 1.09 / 1.27 | 1.86 / 3.67 |
| gpt-5.6-luna@none | 16 | 322/322 | 0 | 100% | 4.29 | 939 | 939 | 0.77 / 1.20 / 1.76 | 1.87 / 3.88 |
| router-sol-luna-balanced | 4 | 83/83 | 0 | 100% | 1.11 | 293 | 256 | 1.65 / 2.81 / 3.10 | 2.44 / 3.79 |
| router-sol-luna-balanced | 8 | 177/177 | 0 | 100% | 2.36 | 620 | 530 | 1.62 / 2.82 / 3.41 | 2.29 / 3.94 |
| router-sol-luna-balanced | 16 | 349/586 | 237 | 100% | 4.65 | 1241 | 1072 | 1.56 / 2.53 / 2.84 | 2.26 / 3.76 |

| 实验组 | 可见 tok/s 4 → 16 | req/s 4 → 16 | TTFT P50 4 → 16 s | TTFT P90 4 → 16 s | 429（4 / 8 / 16） |
|---|---:|---:|---:|---:|---:|
| gpt-4o-mini-bench | 196 → 876 | 1.52 → 6.75 | 0.34 → 0.34 | 0.81 → 0.66 | 0 / 0 / 0 |
| gpt-5-mini@minimal | 315 → 1416 | 1.20 → 5.21 | 0.53 → 0.50 | 1.30 → 1.27 | 0 / 0 / 0 |
| gpt-5.6-luna@none | 228 → 939 | 1.15 → 4.29 | 0.79 → 0.77 | 1.12 → 1.20 | 0 / 0 / 0 |
| router-sol-luna-balanced | 256 → 1072 | 1.11 → 4.65 | 1.65 → 1.56 | 2.81 → 2.53 | 0 / 0 / 237 |

- 总计 237 次 429、0 次其他错误，全部发生在 Router 部署上（capacity 300）；这 237 条拒绝响应体里带有 Router 自己的 trace：`x-ratelimit-limit-requests: 300`、`Retry-After` 1–3 s，并且**每条只有一次尝试**（gpt-5.6-luna→429: 237；每请求尝试次数分布 {1: 237}）——Luna 尝试被限流时，Router 没有改试 Sol。capacity 1000 的候选模型在 16 并发、当前提示词长度下没有触及上限。生产容量规划要从部署的 RPM/TPM 设置和每个并发用户的实测 req/s 出发，而不是从模型出发。
- Router 的聚合值包含其 Luna 默认运行时的推理 token（未发送 effort）；与 `gpt-5.6-luna@none` 比较请看“可见 tok/s”列。
- 单部署、单区域、单客户端：这是这些部署当天的稳态，不是 SLA。带起止偏移量的逐请求记录（回答正文以其 SHA256 代替，见第 6 节）：[outputs/raw_fulltext/sustained_20260911_020626.numeric.jsonl](outputs/raw_fulltext/sustained_20260911_020626.numeric.jsonl)。

## 3. 限流与回退：三种客户端策略对被限流主部署的表现

`gpt-5.6-luna-lowcap` 是同一个 gpt-5.6-luna 模型以 DataZoneStandard capacity 10 部署（服务端限流：10 请求/分、10,000 token/分），因此 PAYGO 限流用极低成本就能触及。`gpt-5.6-luna`（GlobalStandard、capacity 1000）为备用。每种策略 8 个工作线程运行 75 s（不计 15 s 预热），请求失败后停 1 s，策略之间停 70 s 让主部署的分钟窗口复位。客户端为 [fallback_client.py](fallback_client.py)。它取了 [config/apim-policy-ptu-routing.reference.xml](config/apim-policy-ptu-routing.reference.xml) 这份参考 APIM 策略的两个思路——第一个后端失败时改走第二个后端、根据 `x-ratelimit-*` 响应头在触限之前提前分流——并**在两点上做了扩展**：在第一个内容增量之前遇到流内错误即切换（XML 的 `<retry condition="429">` 只看 HTTP 状态，本轮状态是 200），以及利用率取*请求*与*token* 两者中较高者（XML 只读 token；在这个受 RPM 约束的部署上 token 利用率始终约 10%，按原文策略不会触发主动分流）。若要把同样逻辑放进 API Management，策略需在这两点上修订；本轮未部署、未测试 APIM。

| 策略 | 完成 | 成功率 | 限流失败 | 主部署服务 | 备用服务 | 流内切换 | 主动跳过 | TTFT P50 主 s | TTFT P50 备 s | 回退开销 P50 / P90 ms | req/s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| none | 10/375 | 2.7% | 365 | 10 | 0 | 0 | 0 | 0.44 | - | - | 0.17 |
| reactive | 118/118 | 100.0% | 0 | 10 | 108 | 108 | 0 | 0.48 | 1.03 | 411 / 1035 | 1.97 |
| proactive | 131/131 | 100.0% | 0 | 9 | 122 | 0 | 122 | 0.45 | 0.76 | - | 2.18 |

- **拒绝的形态比次数更重要。**在流式 Responses 路径上，DataZone 部署接受了每个请求（HTTP 200），然后在流内发出 `Your requests to gpt-5.6-luna for gpt-5.6-luna-lowcap in swedencentral have exceeded rate limit.` 错误事件；HTTP 交换已经成功，客户端读不到 `Retry-After` 响应头。第 2 节中 Chat Completions 的 Router 部署则返回真正的 HTTP 429（`Retry-After: 3`）。只检查响应状态码的网关或客户端会把流内这种情况当作失败回答直接透给用户；重试必须发生在第一个流事件处。对任何要把 Qira 接到 PAYGO 上的人来说，这是本轮最重要的一条发现。
- **客户应当带走的结论：**需要第二个后端，加上一个在第一个内容增量之前遇到*任何*失败就切换的客户端（或网关）。没有它，本轮 97.3% 的请求直接失败。有了它，回退的 92% 请求 TTFT P50 为 1.03 s = 主部署拒绝耗时（P50 411 ms）+ 备用部署自身的首字（扣除该开销后 P50 0.55 s；备用部署不受干扰时，主动路由下测得 0.76 s，第 2 节 8 并发下为 0.80 s）。
- **主动路由省掉拒绝耗时**（直接走备用的请求），代价是每次主部署响应都要读限流响应头，并在 60 s 缓存过期后重新探测主部署（对应参考策略的 `duration="60"`）。是否值 411 ms，取决于产品的 TTFT 预算。
- **Model Router 不会在部署级 429 上回退。**第 2 节的 237 次 Router 拒绝，每条 trace 只有一次尝试（`gpt-5.6-luna → 429`），没有改试 Sol；#2 的 1,128 次路由请求加本轮的 Router 运行中，trace 从未出现第二次尝试。底层模型本身故障时的表现**未被观测**。无论哪种情况，都由上面的客户端回退来处理。
- 链两端是同一个模型，回退不改变质量；跨模型回退（例如到 gpt-4o-mini）机制相同但答案会变，采用前须先评分。

## 4. 生命周期与 API 兼容性（来自 Models API，非文档转述）

于 2026-09-11 用 [scripts/query_model_lifecycle.py](scripts/query_model_lifecycle.py) 查询；原始结果见 [model_lifecycle_swedencentral.json](outputs/model_lifecycle_swedencentral.json)。此处“已停用”对应文档定义的“仅限现有客户”状态（API 值 `Deprecating`）；日期是当天服务端的程序化取值，可能变动。标准生命周期为 GA 后 18 个月，第 12 个月起对新客户停用。

| 模型 | 版本 | 生命周期 | 推理退役日期 | API 能力 | 部署 SKU | 在本系列中的角色 |
|---|---:|---:|---:|---:|---:|---:|
| gpt-5-mini | 2025-08-07 | GA | 2027-02-09 | chatCompletion, responses, assistants | DataZoneProvisionedManaged, DataZoneStandard, GlobalProvisionedManaged, GlobalStandard, ProvisionedManaged | Task A candidate |
| gpt-4o-mini | 2024-07-18 | Deprecated (existing customers only) | 2027-04-14 | chatCompletion, responses, assistants, jsonObjectResponse, fineTune | DataZoneProvisionedManaged, DataZoneStandard, GlobalProvisionedManaged, GlobalStandard, ProvisionedManaged, Standard | Task A candidate (gpt-4o-mini-bench) |
| model-router | 2025-11-18 | GA | 2027-05-20 | chatCompletion | DataZoneStandard, GlobalStandard | Task B router deployments |
| gpt-5.6-luna | 2026-07-09 | GA | 2028-01-11 | chatCompletion, responses, assistants | DataZoneStandard, GlobalProvisionedManaged, GlobalStandard | Task A candidate; Task B fast tier; fallback target |
| gpt-5.6-sol | 2026-07-09 | GA | 2028-01-11 | chatCompletion, responses, assistants | DataZoneProvisionedManaged, DataZoneStandard, GlobalProvisionedManaged, GlobalStandard | Task B strong tier |
| gpt-5.6-terra | 2026-07-09 | GA | 2028-01-11 | chatCompletion, responses, assistants | DataZoneProvisionedManaged, DataZoneStandard, GlobalProvisionedManaged, GlobalStandard | blind judge (judge-terra) |

- 对于 2026 年第四季度上线的负载，gpt-5-mini 2025-08-07 剩余不到六个月，gpt-4o-mini 2024-07-18 对从未部署过它的订阅已不可再部署。二者正是 #1 中便宜/快速的候选；其性价比要和上线首年内被迫迁移放在一起权衡。
- 三个候选模型和 Sol 都提供 `chatCompletion`、`responses`、`assistants`；`model-router` 只提供 `chatCompletion`。在 Sweden Central 的目录中，gpt-5.6-luna 不提供区域级 `ProvisionedManaged`（只有 Global/DataZone Standard 与 Global Provisioned）——若 PTU 讨论重启，这点相关；SKU 可用性按区域列出，目标区域应重新查询。

## 5. 复现

```bash
# offline: rebuild every CSV and both READMEs from the retained evidence; run the tests
python scripts/build_readiness_report.py
python -m unittest discover -s tests

# live (paid): same-region Linux VM, Entra identity with Cognitive Services OpenAI User on the resource
export AZURE_OPENAI_ENDPOINT="https://<RESOURCE_NAME>.openai.azure.com/"
python sessions.py --dataset datasets/qira_sessions.jsonl \
  --arms "gpt-4o-mini-bench,gpt-5-mini@minimal,gpt-5.6-luna@none,router-sol-luna-balanced#chat,router-sol-luna-quality#chat" \
  --iterations 3 --region swedencentral --client-location swedencentral-linux-vm
python sustained_load.py --dataset datasets/qira_scenarios.jsonl \
  --arms "gpt-4o-mini-bench,gpt-5-mini@minimal,gpt-5.6-luna@none,router-sol-luna-balanced#chat" \
  --levels 4,8,16 --duration 90 --ramp 15 --region swedencentral --client-location swedencentral-linux-vm
# create a capacity-10 deployment of the primary model first (az cognitiveservices account deployment create ... --sku-capacity 10)
python resilience_test.py --dataset datasets/qira_scenarios.jsonl --primary gpt-5.6-luna-lowcap --fallback gpt-5.6-luna \
  --effort none --policies none,reactive,proactive --level 8 --duration 75 --ramp 15 --region swedencentral --client-location swedencentral-linux-vm
python scripts/query_model_lifecycle.py --region swedencentral
```

## 6. 证据与边界

- 带 `response_sha256` 的全文回答：`outputs/raw_fulltext/sessions_20260911_015218.jsonl` 与 `resilience_20260911_024156.jsonl`。持续负载运行以**数值形式**保留（`sustained_20260911_020626.numeric.jsonl`：除 `response_text` 外的全部字段，每条记录保留其 SHA256）；对同样 17 道题的 3,770 条回答仍留在已释放的 VM 磁盘上（其哈希见来源文件的 `sustained_fulltext_retained_on_vm_only.sha256`），未经管理平面传输通道搬运。运行器实时写出的汇总由生成器交叉校验并一并保留。第一次回退尝试（只看请求时状态的客户端）保留在 `outputs/resilience_v1_request_time_only/`。哈希见 [provenance_readiness.json](outputs/provenance_readiness.json)；运行后的资源状态见 [resource_closeout_readiness.json](outputs/resource_closeout_readiness.json)。
- 合成英文提示词与脚本化会话，不是客户流量；单台客户端 VM；TTFT/E2E 为客户端观测值，包含网络与交付；成本为按实际服务模型的 Global 牌价估算（不含 DataZone 溢价与路由费），不是账单。
- 未测：长到足以触发提示缓存的会话；超过 16 的并发或超过 90 s 的时长；capacity 1000 部署的 429 出现点；流内限流形态是否同样出现在 GlobalStandard 部署或直连模型的 Chat Completions 上（Chat 上的 Router 部署返回的是 HTTP 429）；底层模型不可用时 Model Router 的表现；跨模型回退的质量；API Management 本身（参考策略既未部署也未执行——进程内客户端按第 3 节所述的两点扩展重现了它的意图）。
