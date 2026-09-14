# Lenovo Qira — 跟进实验：API 路径时延、阶梯并发、评分规则校准

三组付费跟进实验回答前两份研究留下的问题：Chat Completions 直连基线上看到的“整段一次到达”是否是 API 路径造成的；三个候选模型与 balanced Router 在 4/8/16 并发下的表现；以及评分规则明确写出“助手没有工具”之后，盲评分数会变动多少。同一台 Sweden Central VM、同一资源、不带搜索或工具，使用加固后的 harness（关闭 SDK 静默重试、必须收到 usage）。

> 作者: **Xinyu Wei (魏新宇)** · 2026-09-10 · 运行 `direct_20260910_074346`、`loadtest_20260910_085128`、`quality_v2_*`

[English](README.md) | [中文](README-CN.md)

相关：[场景基准](../qira-scenario-model-benchmark/README-CN.md) · [Router 验证](../qira-model-router-validation/README-CN.md)

## 1. 同一批部署、两条 API 路径：Chat 上首个 token 与最后一个同时到达（497/564），Responses 上没有（32/564）

`gpt-5.6-sol-dz` 与 `gpt-5.6-luna-dz`（DataZoneStandard）用完全相同的 47 题、effort 和 1+3 次迭代，在 **Responses API** 上重跑。Chat 路径有 497/564 次测量回答整段一次到达（首末文本块间隔 <50 ms）；Responses 路径为 **32/564**。TTFT/E2E 均为客户端观测值；两条路径在不同日期测量，绝对时延还叠加了时段效应。

| 实验组 | API 路径 | TTFT P50 / P90 s | E2E P50 / P90 s | TPOT P50 ms | tok/s P50 | 首末<50 ms | 输出 token | 美元 / 1,000 次 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| gpt-5.6-sol-dz | Chat | 2.08 / 7.68 | 2.09 / 11.58 | 0.09 | 10669 | 127/141 | 360 | 11.24 |
| gpt-5.6-sol-dz | Responses | 0.86 / 4.67 | 2.67 / 13.32 | 14.44 | 69 | 10/141 | 341 | 10.65 |
| gpt-5.6-sol-dz@low | Chat | 1.78 / 6.82 | 1.80 / 9.57 | 0.09 | 10759 | 125/141 | 312 | 9.78 |
| gpt-5.6-sol-dz@low | Responses | 0.99 / 3.66 | 2.82 / 13.18 | 15.12 | 66 | 7/141 | 302 | 9.48 |
| gpt-5.6-luna-dz | Chat | 1.57 / 5.29 | 1.57 / 8.67 | 0.09 | 10927 | 126/141 | 372 | 0.46 |
| gpt-5.6-luna-dz | Responses | 1.06 / 3.76 | 2.47 / 12.32 | 9.82 | 102 | 9/141 | 375 | 0.47 |
| gpt-5.6-luna-dz@low | Chat | 1.27 / 4.32 | 1.28 / 6.54 | 0.09 | 10770 | 119/141 | 343 | 0.43 |
| gpt-5.6-luna-dz@low | Responses | 0.67 / 3.03 | 2.08 / 8.62 | 10.51 | 95 | 6/141 | 321 | 0.40 |

- 直连 Sol、不发 effort：TTFT P50 **Chat 2.08 s vs Responses 0.86 s**；Chat 路径的 E2E P50（2.09 s）与其 TTFT 只差 15 ms——首个 token 和最后一个 token 一起到达。直连 Luna：1.57 s vs 1.06 s。若把 Responses 的 TTFT 当作模型侧部分，Chat TTFT 中的交付占比在四个组里为 33% 到 58%——占比很大，但并非在每个组都“占大多数”，且受不同测量日期的混杂。
- Responses 路径下逐 token 节奏可以测：Sol 14.4 ms/token（69 tok/s），Luna 9.8 ms/token（102 tok/s）。Router 研究里 Chat 路径约 10,000 的 `decode_tps` 是整段到达造成的，该报告当时已经说明。
- **对 Router 研究的影响：**Router 组（Chat 路径、GlobalStandard）和直连基线（Chat 路径、DataZoneStandard）都不同程度带有这种交付行为（Router 组约 20–25% 整段到达，直连约 90%）。两者之间的配对 TTFT 差值仍不能解读为 Router 开销；该报告已拒绝这种解读。整段到达究竟来自 DataZone 服务路径还是 Chat Completions 流式层，**仍未隔离**——本轮只换了 API。

## 2. 阶梯并发：每批 48 次请求，并发上限 1 / 4 / 8 / 16

每级 48 次请求，循环使用 17 道 Qira 题，提交到 <level> 个工作线程的线程池；每组 1 次预热不计；级别升序执行，级间停 10 s；各组依次运行。测量代码仍是未修改的 `harness.run_one`。**这是封闭批次，不是稳态负载：**并发只在队列排空前等于目标级别，之后随最长回答收尾逐渐降到零。墙钟时间（因此下表全部聚合值）都包含这段收尾；“达到目标并发的时间占比”一列由逐请求的起止偏移量算出，说明每个窗口真正处于所标并发的时间比例。`gpt-4o-mini-bench`、`gpt-5-mini`、`gpt-5.6-luna` 为 GlobalStandard、capacity 1000、Responses API；`router-sol-luna-balanced` 为 GlobalStandard、capacity 300、Chat Completions。

| 实验组 | 并发上限 | 完成 | 429 | 墙钟 s | 达到目标并发的时间占比 | req/s | 聚合输出 tok/s（含推理） | 聚合可见 tok/s | TTFT P50 / P90 s | E2E P50 / P90 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| gpt-4o-mini-bench | ≤1 | 48/48 | 0 | 191.9 | 100% | 0.25 | 36 | 36 | 0.44 / 1.25 | 2.77 / 5.85 |
| gpt-4o-mini-bench | ≤4 | 48/48 | 0 | 49.4 | 81% | 0.97 | 130 | 130 | 0.48 / 1.25 | 2.72 / 5.11 |
| gpt-4o-mini-bench | ≤8 | 48/48 | 0 | 30.0 | 65% | 1.60 | 223 | 223 | 0.44 / 1.88 | 2.74 / 7.92 |
| gpt-4o-mini-bench | ≤16 | 48/48 | 0 | 18.1 | 45% | 2.65 | 355 | 355 | 0.53 / 2.21 | 2.65 / 5.51 |
| gpt-5-mini@minimal | ≤1 | 48/48 | 0 | 167.8 | 100% | 0.29 | 80 | 80 | 0.63 / 1.34 | 2.09 / 5.10 |
| gpt-5-mini@minimal | ≤4 | 48/48 | 0 | 51.0 | 82% | 0.94 | 277 | 277 | 0.69 / 1.50 | 2.65 / 7.42 |
| gpt-5-mini@minimal | ≤8 | 48/48 | 0 | 33.2 | 73% | 1.45 | 420 | 420 | 0.90 / 1.83 | 2.89 / 10.90 |
| gpt-5-mini@minimal | ≤16 | 48/48 | 0 | 21.4 | 34% | 2.24 | 620 | 620 | 0.77 / 1.58 | 2.38 / 5.16 |
| gpt-5.6-luna@none | ≤1 | 48/48 | 0 | 171.2 | 100% | 0.28 | 70 | 70 | 1.07 / 1.52 | 2.48 / 4.39 |
| gpt-5.6-luna@none | ≤4 | 48/48 | 0 | 60.2 | 75% | 0.80 | 214 | 214 | 0.89 / 1.74 | 2.16 / 6.91 |
| gpt-5.6-luna@none | ≤8 | 48/48 | 0 | 39.1 | 50% | 1.23 | 332 | 332 | 0.98 / 1.85 | 2.54 / 5.60 |
| gpt-5.6-luna@none | ≤16 | 48/48 | 0 | 33.1 | 22% | 1.45 | 386 | 386 | 0.91 / 1.43 | 2.55 / 5.72 |
| router-sol-luna-balanced | ≤1 | 48/48 | 0 | 196.6 | 100% | 0.24 | 75 | 65 | 1.74 / 3.51 | 2.92 / 5.83 |
| router-sol-luna-balanced | ≤4 | 48/48 | 0 | 49.5 | 85% | 0.97 | 291 | 253 | 1.81 / 3.48 | 2.72 / 5.15 |
| router-sol-luna-balanced | ≤8 | 48/48 | 0 | 31.7 | 56% | 1.51 | 465 | 404 | 1.71 / 3.11 | 2.47 / 4.99 |
| router-sol-luna-balanced | ≤16 | 48/48 | 0 | 23.6 | 31% | 2.04 | 604 | 522 | 1.97 / 3.52 | 2.63 / 5.06 |

- **768 次测量请求中，0 次返回 429，0 次其他错误。**`max_retries=0` 下每一次都会被记录。在 capacity 1000（约 1M TPM）、16 并发短提示词下，本测试没有触及 PAYGO 限流；429 出现点取决于部署的 capacity 设置而不是模型，本轮未观测到。
- **不要把 ≤16 这几行读成“16 并发下的吞吐”。**≤16 时目标并发只维持了墙钟的 22%–45%（≤4 时为 75%–85%）；窗口其余时间是少数几条很长的 WFM01 回答在单独收尾，因此 ≤16 的聚合 tok/s 主要由各模型最长回答的长度决定。要得到稳态数字，需要固定时长、持续补充请求的窗口，或每级 ≥10 倍于并发数的请求量——两者本轮都没有做。TTFT/E2E 分位数受影响小得多：每个请求都是在并发刚补满到目标级别时开始的。
- `router-sol-luna-balanced` 在全部 192 次完成的压测请求（17 道 Qira 题）中均服务 **gpt-5.6-luna**，与 Router 研究中 balanced 从未为 Qira 题选择 Sol 一致。由于未发送 effort，Router 服务的 Luna 以默认推理运行（输出 token 中 13%–14% 是推理 token），而 `@none`/`@minimal`/非推理的三个组为 0%：比较 Router 时请看“聚合可见 tok/s”列，不要用含推理的那一列。其单请求 tok/s（见 CSV）还带有 Chat 路径的交付效应（29/192 次整段到达）；按墙钟计算的聚合值不受整段到达影响。

## 3. 评分规则 v2 消除了 S03 的不一致；分数仍有零点几分的波动

规则 v1 对部分“我不能设置计时器，请用时钟应用”这类无工具回答按“没有执行动作”扣分，对另一部分又因“如实说明限制”给高分：S03 上同一行为在十个组的 instruction_following 从 1 到 5。规则 v2（`v2-toolless-2026-09-10`）明确写出助手没有工具，如实说明限制并给出最短正确路径即完全满足请求。全部 Task B（470）和 Task A（187）回答由同一 `judge-terra` 部署重评，每个单元格评的迭代与 v1 相同。

- **S03（设置计时器）：**v2 下十个组的 instruction_following 全部为 5/5——已确认的不一致消失。
- **S04（打开蓝牙设置）：**8 条回答得 5 分；得 2 和 3 分的 2 条，其 v2 评语记录的是“只给路径、没说明无法打开”。这是规则按字面执行的结果，两条几乎相同的回答之间仍有 1 分差距，属于残余的评分噪声。

| 任务 | 实验组 | v1 均分 | v2 均分 | Δ | 满分占比 (v1 → v2) |
|---|---:|---:|---:|---:|---:|
| B | gpt-5.6-luna-dz | 4.855 | 4.838 | -0.017 | 81% → 81% |
| B | gpt-5.6-luna-dz@low | 4.932 | 4.864 | -0.068 | 83% → 79% |
| B | gpt-5.6-sol-dz | 4.877 | 4.872 | -0.004 | 83% → 89% |
| B | gpt-5.6-sol-dz@low | 4.902 | 4.877 | -0.025 | 87% → 87% |
| B | router-sol-luna-balanced | 4.915 | 4.919 | +0.004 | 85% → 87% |
| B | router-sol-luna-balanced@low | 4.906 | 4.847 | -0.060 | 89% → 81% |
| B | router-sol-luna-cost | 4.911 | 4.877 | -0.034 | 83% → 81% |
| B | router-sol-luna-cost@low | 4.915 | 4.911 | -0.004 | 83% → 77% |
| B | router-sol-luna-quality | 4.906 | 4.872 | -0.034 | 87% → 83% |
| B | router-sol-luna-quality@low | 4.962 | 4.936 | -0.025 | 89% → 94% |
| A | gpt-4o-mini-bench | 4.529 | 4.459 | -0.071 | 47% → 53% |
| A | gpt-5-mini@high | 4.788 | 4.729 | -0.059 | 53% → 59% |
| A | gpt-5-mini@low | 4.635 | 4.459 | -0.176 | 53% → 41% |
| A | gpt-5-mini@medium | 4.765 | 4.718 | -0.047 | 71% → 53% |
| A | gpt-5-mini@minimal | 4.553 | 4.377 | -0.176 | 41% → 35% |
| A | gpt-5.6-luna@high | 4.953 | 4.706 | -0.247 | 82% → 47% |
| A | gpt-5.6-luna@low | 4.824 | 4.765 | -0.059 | 65% → 59% |
| A | gpt-5.6-luna@max | 4.882 | 4.906 | +0.024 | 82% → 71% |
| A | gpt-5.6-luna@medium | 4.918 | 4.788 | -0.129 | 76% → 65% |
| A | gpt-5.6-luna@none | 4.918 | 4.847 | -0.071 | 71% → 82% |
| A | gpt-5.6-luna@xhigh | 4.941 | 4.812 | -0.129 | 76% → 65% |

- Task B：102/470 个单元格分数变化，平均 |Δ| 0.135；Task A：64/187 个变化，平均 |Δ| 0.204。各组均分最多变动 0.25，多处排序互换（v1 里还存在完全同分，例如 Task B 两组同为 4.9149）：组间零点零几分的差距落在裁判自身波动之内，不能作为选型依据。完整均分与两版排序见 [judge_rubric_comparison_arms.csv](outputs/judge_rubric_comparison_arms.csv)。
- 两个版本都是没有标准答案的 LLM 评分；v2 消除了一处已确认的不一致，并不使分数成为用户感知质量的度量。逐题差值见 [judge_rubric_comparison_questions.csv](outputs/judge_rubric_comparison_questions.csv)。

## 4. 可运行代码的变更（已同步进 Router 目录）

- `harness.py`：SDK `max_retries=0`；没有 usage 的流记为 `missing_usage`，不算零成本成功；保留 Chat `finish_reason`，非 `stop`/`length` 一律记错。
- `analyze.py`：Router 报告与直连报告使用同一套“已完成 / 未截断 / 有 usage”过滤，混合环境直接退出。
- `judge.py`：规则 v2 加入无工具规则，每行带 `rubric_version`，拒绝覆盖已有评分文件，保留 2 次重试（评分不是时延测量）。
- `loadtest.py`：新增，在 `harness.run_one` 之上做阶梯并发。

## 5. 复现

```bash
# offline: rebuild every CSV and both READMEs from the retained evidence
python scripts/build_followup_report.py
python -m unittest discover -s tests

# live (paid): same-region Linux VM, Entra identity with Cognitive Services OpenAI User
export AZURE_OPENAI_ENDPOINT="https://<RESOURCE_NAME>.openai.azure.com/"
python harness.py --mode direct --api responses --dataset datasets/router_taskb.jsonl \
  --deployments gpt-5.6-sol-dz,gpt-5.6-luna-dz --region swedencentral --client-location swedencentral-linux-vm \
  --efforts=-,low --iterations 3 --warmup 1
python loadtest.py --dataset datasets/qira_scenarios.jsonl \
  --arms "gpt-4o-mini-bench,gpt-5-mini@minimal,gpt-5.6-luna@none,router-sol-luna-balanced#chat" \
  --levels 1,4,8,16 --requests-per-level 48 --region swedencentral --client-location swedencentral-linux-vm
python judge.py outputs/<run>.jsonl --judge-deployment judge-terra --dataset datasets/<dataset>.jsonl --out outputs/quality_v2_<run>.jsonl
```

## 6. 证据

- 全文回答：`outputs/raw_fulltext/direct_20260910_074346.jsonl`（752 条）、`outputs/raw_fulltext/loadtest_20260910_085128.jsonl`；每条记录保留 `response_sha256`。
- `outputs/loadtest_20260910_085128.summary.json` 由 `loadtest.py` 实时写出；生成器从原始记录重算全部聚合值，不一致即拒绝生成。
- `outputs/quality_v2_20260909_223737.jsonl`、`outputs/quality_v2_20260909_120534.jsonl`；v1 输入与历史 Chat 路径直连记录位于 `outputs/inputs/`，见 [manifest.json](outputs/inputs/manifest.json)。
- 成本为按实际服务模型的 Global 牌价估算（不含 DataZone 溢价与路由费），不是账单。运行后的资源状态见 [resource_closeout_followup.json](outputs/resource_closeout_followup.json)。
