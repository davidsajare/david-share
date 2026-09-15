# 部署 OSS 模型时的推测解码选型：MTP、DFlash 2 与 draft model 再适配

[![vLLM](https://img.shields.io/badge/vLLM-0.28.0-0078D4.svg)](https://github.com/vllm-project/vllm/releases/tag/v0.28.0)
[![GPU](https://img.shields.io/badge/GPU-H100%20NVL-76B900.svg?logo=nvidia&logoColor=white)](#测试方法)
[![Precision](https://img.shields.io/badge/Precision-BF16-008080.svg)](#测试方法)
[![Test scope](https://img.shields.io/badge/Scope-64%20tasks%20repeated-D97706.svg)](#测试覆盖与未执行项)
[![Evidence CI](https://github.com/david-xinyuwei/david-share/actions/workflows/speculative-decoding-ci.yml/badge.svg?branch=master)](https://github.com/david-xinyuwei/david-share/actions/workflows/speculative-decoding-ci.yml)

部署开源模型时，要不要开推测解码、开哪一种，最终落在三个问题上：

- **怎么选。** 目标 checkpoint 自带 MTP 权重，还是上游另有配套的 DFlash 2 draft model？两条路线的前置条件不同。
- **差多少。** 同一批题目、同一套请求参数下，吞吐、延迟和答案得分各自变化多少，代价是什么。
- **微调之后怎么办。** 目标模型做过业务微调后，发布版 draft model 还能不能用，要不要继续训练 draft model 和 selector。

本仓库在一张 H100 NVL 上把这三个问题跑成可离线复算的实测：推理对比、目标模型微调、draft model 与 selector 继续训练、checkpoint 重载、留出集评估和 vLLM 服务验证。

> 作者：魏新宇（Xinyu Wei）

[English](README.md) | [中文](README_CN.md)

[如何选择](#如何选择mtp-还是-dflash-2) · [实测对比](#实测对比mtp-与-dflash-2) · [微调之后](#微调之后如何调整-draft-model) · [快速上手](#快速上手) · [测试](#测试与离线复算)

推理对比：2026-09-06，`qwen38-quality-20260906`；draft model 再适配：2026-09-09 至 2026-09-10，设置 B 的服务吞吐于 2026-09-13 复测、2026-09-14 按提示块与并发档扩测。Qwen3.6 历史实验单独保留，不与这两项实验混算。

**结论边界：** 这些是单张 H100 NVL 上的工程实测，不是普遍的质量或生产保证。selector 的训练目标由作者实现，并非官方训练配方；训练从发布版 draft model 权重开始，**不代表已完成从零训练**。推理全量题集阶段未执行，再适配实验未对答案质量评分。设置 B 的服务吞吐在同一 40 条提示上复测了 4 次，极差表示计时抖动，不表示提示集抽样方差；另在 5 个不重叠的 40 条提示块上各测一次，再训相对发布版的增益均值为 +6%～+9%、逐块 +2%～+14%，5 块 × 4 档并发共 20 格全为正；引用时不应单取偏高的 block 0。并发最高测到 16，发布版 draft model 的加速从并发 1 的 1.81× 衰减到并受 16 的 1.36×。thinking 模式下的再适配收益未验证：一次尝试因客户端未取回任何文本而无效。在 vLLM 0.28.0 bf16 上，greedy 推测解码的输出与 greedy 自回归不是逐字相同的：每 40 条中 20～24 条相同（五块），且差异可硬确定性复现。发布方模型卡写的“greedy output matches the target model exactly”在本引擎上未观察到；是算法还是引擎数值路径造成，未隔离。

---

## 从这里开始

| 你想了解什么 | 入口 |
|---|---|
| 在 MTP 和 DFlash 2 之间做选择 | [如何选择：MTP 还是 DFlash 2](#如何选择mtp-还是-dflash-2) |
| 了解交付内容与责任边界 | [这个 Repo 交付什么](#这个-repo-交付什么) |
| 核对吞吐、延迟与答案得分的原始数据 | [实测对比：MTP 与 DFlash 2](#实测对比mtp-与-dflash-2) |
| 理解 draft model 与 selector 怎样训练、loss 怎样计算 | [损失函数与 selector](#损失函数与-selector) |
| 判断再适配有没有收益 | [留出集与服务验证](#留出集与服务验证) |
| 在自己的 GPU 上启动推理或再适配 | [快速上手](#快速上手)、[复现再适配](#复现再适配) |
| 不使用 GPU，核对日志、源码和已保存结果 | [训练日志与代码](#训练日志与代码)、[测试与离线复算](#测试与离线复算) |
| 判断兼容性、从零训练成本和历史故障边界 | [兼容性与边界](#兼容性与边界) |

## 如何选择：MTP 还是 DFlash 2

### 1. 先判断哪条路线可用

| 你的情况 | 可选路线 | 前置条件 |
|---|---|---|
| 目标 checkpoint 自带 MTP 权重 | MTP | 不需要额外权重文件，开启服务参数即可 |
| 上游发布了配套的 DFlash 2 draft model | DFlash 2 | 另外下载约 3.8 GB 的 draft model，架构与 tokenizer 必须与目标匹配 |
| 两者都有 | 两条都测 | 用自己的业务负载各跑一遍，同时看吞吐和答案质量 |
| 两者都没有 | 先不开推测解码 | 训练新 draft model 是独立项目，成本见[从零训练需要什么](#从零训练需要什么) |

打开服务参数不会凭空生成缺失的 MTP 权重。DFlash 2 的 draft model 只能配它对应的目标模型；换目标模型必须重新核对架构、tokenizer、隐藏维度和输出头。三条路线的完整启动命令见[快速上手](#快速上手)。

### 2. 两条路线的实测差异

<!-- BEGIN DECISION_TABLE -->
| 对比项 | MTP7 | DFlash 2-7 |
| --- | --- | --- |
| 输出吞吐 tok/s（并发 1 / 4 / 8） | 113.3 / 382.9 / 565.2 | 150.5 / 451.7 / 741.1 |
| 相对不开推测的倍数（并发 1 / 4 / 8） | 2.12× / 2.01× / 1.96× | 2.82× / 2.38× / 2.58× |
| token 交付间隔 TPOT ms（并发 1 / 4 / 8） | 8.01 / 8.55 / 10.04 | 5.87 / 6.74 / 7.89 |
| 代码答对数：9 组配对比较 | 参照 | 高 2 组 / 低 2 组 / 平 5 组 |
| 数学答对数：9 组配对比较 | 参照 | 高 6 组 / 低 2 组 / 平 1 组 |

吞吐与延迟取三个随机种子的中位数；得分按“同一并发 + 同一种子”逐组配对比较，不把三次重复当成独立题目。
<!-- END DECISION_TABLE -->

**速度可以直接读，准确率不能。** 九组配对里 DFlash 2 的吞吐全部高于 MTP7，这一项方向一致。答案得分两个方向都出现过，没有观察到系统性下降；但每类只有 32 道不同题目、重复三次，样本量不足以证明“准确率不劣于”。把得分差异当噪声，或当成质量退化，目前都缺少证据。

**token 更快不等于正确答案更快交付。** 并发 4 的一次运行里，DFlash 2 输出 token 更快，做完同一组题反而更慢，正常结束且答对的答案交付速率也低于 MTP7。这个例外的完整数据见[实测对比](#实测对比mtp-与-dflash-2)。

实际选型时，把 DFlash 2 当作“吞吐候选”：先在自己的业务题目上复测答案质量，再决定是否上线，不要直接引用本仓库的得分作为验收结论。

### 3. 目标模型微调之后，draft model 怎么调整

draft model 是跟着目标模型的隐藏特征训练出来的。目标一旦微调，它的预测分布就可能偏移。本仓库实测了两种微调设置：

| 你的微调方式 | 建议动作 | 实测依据 |
|---|---|---|
| 任何微调之后 | **先测发布版 draft model，不要默认重训** | 设置 A 的三个种子继续训练后 loss 都下降，成对命中率没有改善 |
| 轻量 LoRA（仅注意力投影、低 rank） | 大概率可直接沿用发布版 | 设置 A：rank 16、仅注意力、1 个 epoch，再训练未测到收益 |
| 重度 LoRA（全部投影模块、高 rank、换语言） | 值得做一次再适配 | 设置 B：rank 128、7 个投影模块、中文语料，再训练后首位命中率与服务吞吐都提高；服务吞吐在 5 个独立提示块 × 并发 1/4/8/16 上均高于发布版，增益均值 +6%～+9%、逐块 +2%～+14% |
| 无论哪种 | 用自己的留出集和答案质量标准验收 | 本实验只测命中率和吞吐，未对答案质量评分 |

**这两组不是干净的强度对照。** 设置 A 是英文注意力 LoRA，设置 B 是中文全模块 LoRA，语言和参数同时不同。上表可作为选型起点，但不能证明“微调越重就越需要再适配”。

**不要用 loss 下降当验收。** 设置 A 的 loss 确实下降了，命中率和服务吞吐都没有改善。完整训练流程、损失函数和逐项数字见[微调之后：如何调整 draft model](#微调之后如何调整-draft-model)。

## 这个 Repo 交付什么

| 目标 | 仓库提供什么 | 对你的帮助 |
|---|---|---|
| 启动三条推理路线 | 固定权重版本、完整启动命令和相同请求样例 | 不必从零拼装 MTP、DFlash 与客户端参数 |
| 判断哪条路线值得验证 | 同一批题目的吞吐、延迟、答对数和截断记录 | 同时比较速度与答案质量，识别“token 更快、正确答案反而交付更慢”的情况 |
| 再适配 draft model | 目标 LoRA、自生成语料、draft model 训练、checkpoint 重载与服务导出 | 对比保留发布版 draft model 与再训练 draft model，不默认必须重训 |
| 训练 selector | `selector_loss()`、`--train-selector`、训练历史和执行代码 | 理解候选重排目标及其梯度边界，不把联合训练效果归给单一组件 |
| 复用训练方法 | 固定切分、冻结目标、同文本成对比较、两类 loss 日志与服务侧复测 | 不用 loss 下降或命中率替代最终答案质量验收 |
| 核对选型依据 | 逐组数据、评分接入源码、分析程序和测试 | 能检查报告数字从何而来，再为自己的业务设计验收 |

这里提供的是部署参考和测试依据，不是经过生产验收的托管服务。完整 27 组实验的准备与调度尚未提供独立运行入口，边界见[复现范围](#复现范围)。

MTP、DFlash 算法及发布版权重属于上游工作；本仓库贡献的是训练与测量实现、受控对照和可追溯证据。客户需提供兼容硬件、自己的业务数据和答案质量标准。训练产出的权重不随仓库分发，代码与 loss history 可直接检查。

## 实测对比：MTP 与 DFlash 2

这项实验比较：固定目标、题目与请求参数后，更换起草方式是否提高服务性能，答案得分是否变化。输入为 32 道 HumanEval+ 和 32 道 MATH-500 题，不是自由聊天压测。

实际请求 `HumanEval/69` 的示例原文摘录：

> `search([4, 1, 2, 2, 3, 1]) == 2`

数学请求 `MATH-500/100` 的开头为 “A hexagon is inscribed in a circle:”。两份[完整请求](experiments/20260906-qwen38/evidence/request-examples.json)保留原题、图形描述、采样参数和哈希。正式比较只切换基线、MTP7、DFlash 2-7；输出预算和评分方法不随路线改变。

### 本次实测说明了什么

固定同一批 32 道代码题和 32 道数学题，三档并发各使用三个随机种子。DFlash 2 对 MTP7 的代码答对数高 2 组、低 2 组、持平 5 组；数学高 6 组、低 2 组、持平 1 组。基线在并发 1 的三次代码测试为 29、31、30；它说明重复运行存在波动，但不能把另一组的差异直接归因为采样噪声。

| 问题 | 实测回答 |
|---|---|
| 输出吞吐更高吗 | 是。三档并发、三个随机种子的九组配对中，DFlash 2 都高于 MTP7 |
| 准确率不下降得到证明了吗 | 没有。每类只有 32 道不同题目，三次重复用于观察波动，不能当成 96 道独立题 |
| 正确答案也一定更快交付吗 | 不一定。并发 4 的第三次运行中，DFlash 2 做完同一组题更慢，正常结束且答对的答案交付速率也低于 MTP7 |
| 原定全量题集都测了吗 | 没有。已完成 1,920 份响应，原计划 5,904 份；其余 3,984 份未执行，不计作答错 |

吞吐优势不能替代客户负载上的准确率、延迟和异常率验收。

### 测试方法

三条路线固定目标模型、数值精度、题目、输出预算和采样设置，只切换推测解码配置。参数来自当时保存的[配置](experiments/20260906-qwen38/evidence/configuration.json)，实际加载检查记录在[运行证据](experiments/20260906-qwen38/evidence/run.json)的 `activation` 中。

客户端、推理服务与评分程序的关系见[架构与测试环境](#架构与测试环境)。本次客户端与服务端同机，计时包含请求派发与流式响应接收，不把模型启动或离线评分时间算作推理性能。

| 项目 | 本次设置 |
|---|---|
| 硬件 | 单张 H100 NVL，张量并行度为 1 |
| 目标模型 | Qwen3.8-27B，BF16 |
| MTP | 目标模型文件自带的多 token 预测权重，未另行训练或转换 |
| DFlash 2 | incoai 发布的 Qwen3.8-27B-DFlash2，BF16 |
| 推理引擎 | vLLM 0.28.0，实际加载 Model Runner V2 |
| 候选数量 | 基线为 0；MTP7 和 DFlash 2-7 均为 7 |
| 输出预算 | 每题最多 16,384 个 token，包含思考过程 |
| 思考设置 | 开启并保留思考过程（thinking），`reasoning_effort="xhigh"` |
| 采样 | temperature 为 1.0，top_p 为 0.95，top_k 为 20 |
| 配对方式 | 并发 1、4、8；随机种子为 20260906、20260907、20260908 |

每类 32 个题目 ID 按预先固定的 SHA-256 排序规则选取，不参考回答和分数。三条路线使用相同的逐题随机种子，但这不代表每一步随机抽样完全对齐。代码题由 EvalPlus 官方工具评分，数学题由 Math-Verify 官方工具评分。

#### 固定版本、完整参数与请求样例

以下链接固定到实际使用的版本，不指向模型仓库当前最新版：

| 对象 | 版本记录 |
|---|---|
| 目标模型 | [Qwen3.8-27B 固定版本权重](https://huggingface.co/Qwen/Qwen3.8-27B/tree/1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0) |
| DFlash 2 | [Qwen3.8-27B-DFlash2 固定版本权重](https://huggingface.co/incoai/Qwen3.8-27B-DFlash2/tree/dedf8df68adfb1afeaf7b7480c0a0243108177b4)，架构为 `DFlash2DraftModel` |
| vLLM | [0.28.0 固定源码](https://github.com/vllm-project/vllm/tree/2cf0a6915ce544dc493a0990f2ea38d81601128a) |
| EvalPlus | [固定源码](https://github.com/evalplus/evalplus/tree/26d6d00bb1fd0fa37f39c99d5290da67891d1c5e)，使用官方 sanitize/evaluate CLI |
| Math-Verify | [固定源码](https://github.com/huggingface/Math-Verify/tree/ba3d3aaff23b3f4cac7a14672b4f6e293d97c98b)，使用官方 `evaluate_model_outputs.py` |

其余参数为 `min_p=0.0`、`presence_penalty=0.0`、`repetition_penalty=1.0`。模板同时设置 `enable_thinking=true` 和 `preserve_thinking=true`；调度上限为 `max_model_len=32768`、`max_num_seqs=16`、`max_num_batched_tokens=16384`。

客户端按固定顺序发送请求：一个请求完成后再补发下一个，保持指定并发数。相同客户端并发不代表 GPU 每批处理的请求数和序列长度相同。记录中的路线标识分别为 `baseline`、`mtp7`、`dflash2_7`；配置中的基础随机种子为 20260906。

[请求样例](experiments/20260906-qwen38/evidence/request-examples.json)保存了第一组基线运行中 `HumanEval/69` 和 `MATH-500/100` 的提示词、完整参数及哈希。`raw_correct` 记录评分器判对数，`normal_correct` 还要求 `finish_reason=stop`；二者在本次 S 阶段恰好一致。离线分析使用已保存的评分，不重新判分。

### 吞吐与答案质量

使用 32 道 HumanEval+ 代码题和 32 道 MATH-500 数学题，三条路线在并发 1、4、8 下各跑三次，共 27 组。每组都是同一批题，**每类 32 题重复三次，不是 96 道独立题**。

基线不开推测解码；MTP7 和 DFlash 2-7 都起草 7 个候选 token。下表的吞吐和整组耗时分别取三次运行的中位数。得分和截断列的三个数，依次对应随机种子 **20260906、20260907、20260908**；每个得分的分母都是 32。

![三种路线在并发 1、4、8 下的输出吞吐](images/throughput-cn.png)

*图 1：作者实测。柱形表示三次运行的中位数，误差线表示最小值和最大值，不是置信区间；同一批 64 题，吞吐包含思考过程中的 token。数据来自[数值汇总](experiments/20260906-qwen38/data/summary.json)，由[绘图脚本](tools/make_readme_figures.py)生成。*

<!-- BEGIN RESULT_TABLE -->
#### 吞吐与整组耗时

| 并发 | 路线 | 吞吐（tok/s） | 整组耗时（秒） |
| --- | --- | --- | --- |
| 1 | 基线 | 53.40 | 3458.61 |
| 1 | MTP7 | 113.34 | 1591.09 |
| 1 | DFlash 2-7 | 150.51 | 1149.41 |
| 4 | 基线 | 190.08 | 1037.22 |
| 4 | MTP7 | 382.85 | 470.35 |
| 4 | DFlash 2-7 | 451.74 | 377.79 |
| 8 | 基线 | 287.72 | 577.44 |
| 8 | MTP7 | 565.24 | 308.08 |
| 8 | DFlash 2-7 | 741.07 | 249.68 |

#### 代码与数学得分

| 并发 | 路线 | 代码答对数 /32 | 数学答对数 /32 |
| --- | --- | --- | --- |
| 1 | 基线 | 29、31、30 | 30、30、30 |
| 1 | MTP7 | 30、31、31 | 30、28、31 |
| 1 | DFlash 2-7 | 30、31、30 | 29、32、30 |
| 4 | 基线 | 31、31、31 | 30、31、30 |
| 4 | MTP7 | 30、30、31 | 29、29、29 |
| 4 | DFlash 2-7 | 31、31、29 | 31、31、30 |
| 8 | 基线 | 31、31、31 | 30、29、30 |
| 8 | MTP7 | 31、31、30 | 29、29、30 |
| 8 | DFlash 2-7 | 31、31、30 | 30、31、30 |

本次所有被评分器判对的回答都正常结束，因此“答对数”和“正常结束且答对数”相同，不重复列两遍。两项原始字段均保留在数据文件中。

#### 三次运行的截断情况

| 并发 | 路线 | 代码截断数 | 数学截断数 |
| --- | --- | --- | --- |
| 1 | 基线 | 1、1、2 | 2、1、2 |
| 1 | MTP7 | 2、1、1 | 1、2、1 |
| 1 | DFlash 2-7 | 2、1、2 | 2、0、1 |
| 4 | 基线 | 1、1、1 | 1、0、2 |
| 4 | MTP7 | 2、2、1 | 2、1、2 |
| 4 | DFlash 2-7 | 1、1、3 | 1、1、1 |
| 8 | 基线 | 1、1、1 | 2、2、2 |
| 8 | MTP7 | 1、1、2 | 2、1、1 |
| 8 | DFlash 2-7 | 1、1、2 | 2、1、2 |

达到输出上限的回答仍保留在每次 32 题的分母中。
<!-- END RESULT_TABLE -->

以上各表对应[数值汇总](experiments/20260906-qwen38/data/summary.json)。吞吐按“服务端确认的输出 token 总数 ÷ 整组耗时”计算，**包含思考过程、错答和截断回答中的 token**。计时从首个测量请求派发，到最后一个请求的终止事件接收完成；不含模型下载、启动、预热和评分。这不是 GPU 纯解码吞吐。

#### 为什么还要看正确答案的交付速度

<!-- BEGIN COUNTEREXAMPLE -->
并发 4 的第三次运行（seed 20260908）出现了一个例外：**DFlash 2 输出 token 更快，但做完同一组题反而更慢。** 下表只统计正常结束且答对的回答，耗时取自这一次运行，不是三次运行的中位数。

| 路线 | 整组耗时（秒） | 代码答对数 /32 | 数学答对数 /32 |
| --- | --- | --- | --- |
| MTP7 | 450.87 | 31 | 29 |
| DFlash 2-7 | 484.07 | 29 | 30 |

DFlash 2 有 3 份代码回答达到输出上限。用“正常结束且答对数 ÷ 整组耗时”衡量正确答案的交付速度，DFlash 2 与 MTP7 的比值为：代码 **0.8713**，数学 **0.9635**。两者都小于 1。这说明 token 吞吐优势不能直接当成正确答案的交付优势；这次差异的原因尚未定位。
<!-- END COUNTEREXAMPLE -->

### 客户端延迟

TTFT 是等待首个输出 token 的时间；TPOT 是首个 token 之后，平均每个输出 token 的交付间隔；回答耗时是从请求派发到接收终止事件的时间。三项都从客户端观察，数值越低越好。

每个配置先分别计算三次运行的 P50，再取三个 P50 的中位数，**不是把所有响应合并后求一次分位数**。缺失或无定义的值不补零。

<!-- BEGIN LATENCY_TABLE -->
#### 首 token 等待（TTFT，ms）

| 并发 | 基线 | MTP7 | DFlash 2-7 |
| --- | --- | --- | --- |
| 1 | 82.727 | 75.127 | 80.286 |
| 4 | 104.008 | 110.259 | 115.994 |
| 8 | 106.598 | 124.699 | 124.781 |

#### token 交付间隔（TPOT，ms/token）

| 并发 | 基线 | MTP7 | DFlash 2-7 |
| --- | --- | --- | --- |
| 1 | 18.567 | 8.011 | 5.875 |
| 4 | 20.104 | 8.549 | 6.739 |
| 8 | 20.845 | 10.041 | 7.892 |

#### 单次回答耗时（秒）

| 并发 | 基线 | MTP7 | DFlash 2-7 |
| --- | --- | --- | --- |
| 1 | 16.359 | 6.236 | 6.185 |
| 4 | 19.035 | 8.661 | 5.237 |
| 8 | 18.457 | 9.915 | 7.742 |

每个配置、每项指标均有 192 份有效响应记录，缺失 0 份。这是重复运行的观测数，不是独立题目数。
<!-- END LATENCY_TABLE -->

#### 延迟的精确定义

TTFT 从派发请求计时，到首个非空生成 `token_ids` 事件为止；空的角色事件或 usage 事件不算首 token。TPOT 按 `(last_token_time - first_token_time) / (completion_tokens - 1)` 计算，仅在输出多于一个 token、且 token-ID 覆盖校验通过时有效。

一个推测解码 SSE 块可以包含多个 token，所以这些是客户端接收侧指标，不是 GPU kernel 的执行时间。定义见[配置](experiments/20260906-qwen38/evidence/configuration.json)，观测值见[逐组记录](experiments/20260906-qwen38/data/groups.json)。

### 测试覆盖与未执行项

原计划包含四个阶段。本文的速度和得分表只使用 S 阶段，兼容性检查和贪心诊断不混入正式子集结果。

| 阶段 | 完成组数 | 实际响应数 | 状态 |
|---|---:|---:|---|
| C：兼容性检查 | 6 | 48 | 已完成 |
| G：贪心诊断 | 36 | 144 | 已完成 |
| S：重复子集 | 27 | 1,728 | 已完成 |
| F：完整题集 | 0 | 0 | 未执行 |

总计完成 **69/81 组、1,920/5,904 份响应**。剩余 12 组、3,984 份响应未执行，既不从计划中删除，也不当作答错。因此，当前结果只覆盖已完成部分，不代表全部计划通过。

F 阶段计划让三条路线在并发 1 和 8 下，分别完成全部 164 道 HumanEval+ 和 500 道 MATH-500，每题一次，随机种子为 20260906。该阶段未执行；已测子集不能写成全量题集成绩。[覆盖记录](experiments/20260906-qwen38/evidence/run.json)保留原计划和未执行项。

#### 各阶段测试耗时

| 阶段 | 测量组耗时合计（秒） |
|---|---:|
| 兼容性检查（C） | 928.51 |
| 贪心诊断（G） | 387.09 |
| 重复子集（S） | 27,800.41 |
| 完整题集（F） | 0，未执行 |

这里只累加各测量组从请求派发到响应结束的耗时，不含模型下载、服务启动、预热和评分，显示到小数点后两位；精确值保留在[运行证据](experiments/20260906-qwen38/evidence/run.json)中。测试已完成不表示每份答案都正确。

## 微调之后：如何调整 draft model

部署团队真正会问的是：**目标模型微调之后，发布版 DFlash 2 draft model 还能用吗？再训练 draft model 和 selector 有没有收益？** 本实验在同一张 H100 NVL 上比较英文注意力 LoRA（设置 A）与中文全模块 LoRA（设置 B）。两种设置的语言和参数都不同，不是只改变微调强度的对照。

### 数据与训练流程

两种设置都从 [medical-o1-reasoning-SFT](https://huggingface.co/datasets/FreedomIntelligence/medical-o1-reasoning-SFT) 固定切分 2,000 条目标微调问题和 200 条留出提示。英文留出集首条问题的原文开头为：

> A 65-year-old woman presents to her family doctor to reestablish care since her retirement from her corporate job and loss of her employer-sponsored health insurance.

完整输入在 [英文提示](experiments/20260909-drafter-adaptation/inputs/round4/eval_prompts_en200.jsonl) 和 [中文提示](experiments/20260909-drafter-adaptation/inputs/round4/eval_prompts_zh200.jsonl)。这些是公开数据集问题，不是客户病历；本实验未对医疗回答质量评分。

目标是 `Qwen/Qwen3.8-27B`（版本 `1d4bf0f2`）加 LoRA Adapter。设置 A 使用英文数据、rank 16、仅注意力投影、1 个 epoch；设置 B 使用中文数据、rank 128、全部 7 个投影模块、2 个 epoch。

draft model 从 `incoai/Qwen3.8-27B-DFlash2`（版本 `dedf8df6`）继续训练，更新五个 draft model 层与 selector，目标模型及其 embedding、输出头冻结。语料由微调后的目标针对 1,200 条训练问题生成；经过脚本的有效长度筛选，英文实际进入训练的序列为 1,160 条，中文为 1,199 条，各训练两个 epoch。这是发布版 draft model 的继续训练，不是从零训练。

### 损失函数与 selector

训练分为两个阶段，不能把目标 LoRA 的 loss 与 draft model loss 混为一条曲线。

- **目标 LoRA：** [finetune_target.py](experiments/20260909-drafter-adaptation/source/round4/finetune_target.py) 的 `DomainDataset` 把问题部分的 label 设为 `-100`；`out.loss` 只监督回答与结束 token，更新 LoRA 参数。
- **draft model backbone：** 冻结微调后的目标模型及其 embedding、输出头。`anchor_loss()` 对每个块的七个待预测位置计算交叉熵，靠前位置权重更高。本次记录为 `block=8`、`gamma=7.0`。
- **selector：** `selector_loss()` 在 backbone 的 top-k 候选上做教师强制训练，用真实前驱 token 与候选后继 token 的配对分数调整排序。真实 token 不在 top-k 时，该位置不参与 selector loss。输入 hidden/logits 已 `detach()`，这项 loss 只更新 selector；backbone 仍由自身 loss 更新。

对块内待预测位置 $k=1,\ldots,7$，本次骨干损失是：

$$
w_k=\exp\left(-\frac{k-1}{7}\right),\qquad
L_{\mathrm{draft}}=\frac{\sum_{k=1}^{7}w_k\,\mathrm{CE}(z_k,y_k)}{\sum_{k=1}^{7}w_k}.
$$

selector 给候选 $c$ 的分数为 $s_{k,c}=z_{k,c}+\langle E_{\mathrm{prev}}(y_{k-1})\odot Ph_k,E_{\mathrm{next}}(c)\rangle$。其交叉熵只在真实 token 位于候选集的有效位置上，按同一组 $w_k$ 归一化求平均。一个样本的训练目标为锚点平均的 $L_{\mathrm{draft}}+1.0\,L_{\mathrm{selector}}$；`selector_weight=1.0` 来自训练实参，不是官方固定常数。

下面是 [train_drafter.py](experiments/20260909-drafter-adaptation/source/round4/train_drafter.py) 的原函数摘录，不是独立运行入口：

```python
    per_token = nn.functional.cross_entropy(logits[0].float(), labels, reduction="none")
    backbone = (per_token * weights).sum() / weights.sum()
    if not train_selector:
        return backbone, None
    selector = selector_loss(drafter, hidden.detach(), logits.detach(), input_ids, anchor, block,
                             weights, autocast)
    return backbone, selector
```

候选重排使用下列实际打分代码：

```python
        pairwise = torch.einsum("pr,pkr->pk", pred_emb * projected[0], succ_emb)
        scores = unary[0].float() + pairwise.float()                              # [B-1, k]
```

此 selector 目标是作者实现，不是 DFlash 2 官方训练配方。执行快照的文件头仍有早期“不训练 selector”的旧说明；后续的 `selector_loss()`、`--train-selector` 和保存的训练实参记录了实际行为，归档源码保持原样。

### 训练日志与代码

四次 draft model 训练均保留了每步 backbone 与 selector loss。下表从完整 history 生成，比较各次运行最前与最后 10% 步数的均值，窗口取 `max(1, steps // 10)`。数值只说明训练目标变化，不代表留出集质量或服务性能。

![四次 draft model 训练的 backbone 与 selector loss](experiments/20260909-drafter-adaptation/images/training-loss-cn.png)

*作者实测，2026-09-09 至 2026-09-10，H100 NVL，两个 epoch。英文三次各 2,320 步，中文一次 2,398 步；图为连续 50 步分组均值，尾组保留实际步数，不是置信区间。来源与图片哈希在 [loss-figures.json](experiments/20260909-drafter-adaptation/images/loss-figures.json)，由 [绘图程序](tools/make_readme_figures.py) 从完整 history 生成。两种语言与目标的绝对 loss 不作排名。*

<!-- BEGIN TRAINING_LOSS -->
| 训练运行 | 步数 / 窗口 | Backbone loss：首 / 尾 | Selector loss：首 / 尾 |
| --- | --- | --- | --- |
| [英文 seed 20260908](experiments/20260909-drafter-adaptation/results/round3/training/drafter_v3_history.json) | 2320 / 232 | 2.0278 / 1.5448 | 0.8209 / 0.6064 |
| [英文 seed 1](experiments/20260909-drafter-adaptation/results/round4/training/drafter_v3_seed1_history.json) | 2320 / 232 | 2.0318 / 1.5081 | 0.8103 / 0.5854 |
| [英文 seed 2](experiments/20260909-drafter-adaptation/results/round4/training/drafter_v3_seed2_history.json) | 2320 / 232 | 2.0126 / 1.5282 | 0.7989 / 0.6043 |
| [中文 seed 20260908](experiments/20260909-drafter-adaptation/results/round4/training/drafter_zh_history.json) | 2398 / 239 | 3.2205 / 2.2704 | 1.4236 / 1.0523 |
<!-- END TRAINING_LOSS -->

目标 LoRA 另有 [英文 loss 记录](experiments/20260909-drafter-adaptation/results/round3/training/adapter_v2_summary.json) 和 [中文 loss 记录](experiments/20260909-drafter-adaptation/results/round4/training/adapter_zh_summary.json)。可阅读的 [Round 3 日志](experiments/20260909-drafter-adaptation/logs/round3/) 与 [Round 4 日志](experiments/20260909-drafter-adaptation/logs/round4/) 保留训练指标、阶段/终止标记和服务端计数；[provenance.json](experiments/20260909-drafter-adaptation/evidence/provenance.json) 记录原文件哈希、保留行号及公开文件哈希。它们是明确裁剪的公开投影，完整原始日志和含私有路径的编排脚本留在作者归档中。

### 留出集与服务验证

**同文本 draft model 比较：** 两份 draft model 读取同一份缓存的目标回答。首位命中率统计块首预测是否正确；联合前缀接受长度是 1 加上连续猜对位置数的均值。固定每八个 token 取锚点，不等于运行时在拒绝位置重新起草，因此属于教师强制指标。只有缓存与文本哈希相同的结果才计算成对 bootstrap。

**运行时验证：** HF 参考解码与 vLLM 服务各自执行真实起草和验证。分别保留端到端接受长度、服务端 accepted/drafted 计数及客户端吞吐，不把这些指标混算，也不用它们代替答案评分。

<!-- BEGIN ADAPTATION_TABLE -->
#### 首位命中率

| 设置 / draft model 路径 | 发布版 / 再训 | 差值的 95% 区间 | 成对提示 |
| --- | --- | --- | --- |
| A / seed 20260908 | 0.857 / 0.848 | -0.019, +0.002 | 193 |
| A / seed 1 | 0.857 / 0.845 | -0.024, +0.000 | 193 |
| A / seed 2 | 0.857 / 0.840 | -0.029, -0.005 | 193 |
| B / selector | 0.677 / 0.707 | +0.015, +0.044 | 200 |
| B / argmax | 0.685 / 0.718 | +0.019, +0.048 | 200 |

#### 联合前缀接受长度

| 设置 / draft model 路径 | 发布版 / 再训 | 差值的 95% 区间 | 成对提示 |
| --- | --- | --- | --- |
| A / seed 20260908 | 4.33 / 4.33 | -0.056, +0.069 | 193 |
| A / seed 1 | 4.33 / 4.35 | -0.044, +0.088 | 193 |
| A / seed 2 | 4.33 / 4.35 | -0.042, +0.095 | 193 |
| B / selector | 2.85 / 3.08 | +0.163, +0.295 | 200 |
| B / argmax | 2.78 / 3.00 | +0.157, +0.274 | 200 |

每种语言请求 200 条提示；表中显示实际可成对评估数，短输出的排除项仍在记录中。A 使用 selector；B 的 argmax 行是在相同训练权重上禁用 selector 的诊断，不是单独的训练消融。五组比较、每组两项指标，共十个区间；按提示 bootstrap 2,000 次，未作多重比较修正。

#### vLLM 服务测量

同一个微调目标分别配不开推测、发布版 draft model、再训 draft model，每条路线只执行一次；每档并发测 40 条提示，`max_tokens=256`。A 的服务测试只覆盖 seed 20260908，另两个种子未做服务测试。

| 指标 | A / en | B / zh |
| --- | --- | --- |
| 吞吐 tok/s，并发 1 | 53.5 / 162.2 / 158.0 | 53.6 / 97.1 / 106.0 |
| 吞吐 tok/s，并发 4 | 184.7 / 490.0 / 485.3 | 194.5 / 323.6 / 349.4 |
| 服务端接受长度 | 4.19 / 4.06 | 2.39 / 2.61 |

吞吐列依次为不开推测 / 发布版 / 再训；接受长度列为发布版 / 再训。接受长度由日志累计 accepted/drafted 推导，含预热与两档并发，不是同一个测量分母。服务性能不作显著性声明；答案质量未评分。

#### 设置 B 服务复测（Round 5）

2026-09-13 在新的 VM 会话与新的 vLLM 安装上重跑设置 B 的三条服务路线：同一份权重、同一 40 条提示、同一 seed 与引擎参数。每条路线启动两次 server（A 轮顺序不开推测→发布版→再训，B 轮反序），每次 server 内跑两遍客户端，每格共 4 个吞吐观测。下表给出均值与原始极差，不假设分布。

| 并发 | 不开推测 | 发布版 | 再训 | 再训 / 发布版 | 极差重叠 |
| --- | --- | --- | --- | --- | --- |
| 1 | 53.2 [53.0–53.6] | 96.6 [96.3–97.0] | 105.4 [105.1–105.8] | +9.1% | 否 |
| 4 | 194.4 [194.2–194.7] | 323.9 [323.5–324.4] | 349.1 [348.7–349.4] | +7.8% | 否 |
| 8 | 336.3 [335.5–337.5] | 531.9 [529.9–533.5] | 577.1 [575.3–578.5] | +8.5% | 否 |

吞吐单位 tok/s，每格格式为均值 [最小–最大]，4 次观测。这些观测是同一确定性 greedy 解码的计时重复，极差表示测量抖动，不表示提示集抽样方差。同一 40 条提示仍是单一样本。并发 1 的逐字一致性（按 `text_sha256`）：发布版与再训 draft model 的输出在 4 次运行中均为 40/40 相同；不开推测与发布版推测解码的输出为 21/40 相同；同一路线在两次 server 启动间为 40/40、40/40、40/40 相同。并发 4 与 8 的对应计数在 [汇总文件](experiments/20260909-drafter-adaptation/data/summary.json) 的 `round5.text_identity` 中。

#### 提示集方差与高并发（Round 6）

Round 5 的极差只覆盖计时抖动。为了看换一批提示后增益是否还在，2026-09-14 把同一 200 条中文留出提示按原顺序切成 5 块 × 40（block 0 即 Round 4/5 那 40 条），三条服务路线各启动一次 server，每块在并发 1/4/8/16 下各测一次。下表每格是该块上再训相对发布版 draft model 的吞吐增益；均值与最小值直接由五个数算出，不做分布假设。

| 并发 | b0 | b1 | b2 | b3 | b4 | 均值 | 最小 | 5 块全正 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | +9.0% | +9.4% | +8.9% | +5.3% | +6.0% | +7.7% | +5.3% | 是 |
| 4 | +7.7% | +10.8% | +10.5% | +6.2% | +5.5% | +8.1% | +5.5% | 是 |
| 8 | +8.7% | +2.1% | +8.6% | +3.9% | +7.0% | +6.1% | +2.1% | 是 |
| 16 | +13.6% | +8.7% | +12.5% | +8.1% | +3.0% | +9.2% | +3.0% | 是 |

| 路线 | c1 | c4 | c8 | c16 |
| --- | --- | --- | --- | --- |
| 发布版 / 不开推测 | 1.81× | 1.68× | 1.53× | 1.36× |
| 再训 / 不开推测 | 1.96× | 1.81× | 1.62× | 1.49× |

增益在 20 个块×并发格中全部为正，但幅度随提示块变化：Round 4/5 所用的 block 0 处于偏高一端，其余块的增益低至 2.1%。引用时应写均值与逐块范围，不应单引 block 0。上表为五块均值的加速倍数：发布版从并发 1 到 16 衰减约 25%，再训的相对增益在并发 16 仍为正。并发 16 等于引擎的 `max-num-seqs`。block 0 与 Round 5 四次运行均值的最大偏差为 0.66%。并发 1 下不开推测与发布版推测解码的逐字一致数逐块为 21/24/20/21/21（每块 40 条），发布版与再训为 40/40/40/40/40。

同日对 block 0 另做了一次 `enable_thinking=true`、`max_tokens=2048` 的尝试（并发 4/8）。服务端生成并计费了每条约 130 个 token，但客户端取回的 `content` 全为空且 `reasoning_content` 为零字符；模型实际运行的模式无法确认，逐字一致数也只是空串对空串。这次尝试的文件仍在 [results/round6/vllm/](experiments/20260909-drafter-adaptation/results/round6/vllm/) 中发布，但**不作为 thinking 模式的任何结论**；thinking 下的再适配收益仍未验证。
<!-- END ADAPTATION_TABLE -->

**设置 A：** 三个训练种子的成对测量均未显示命中率改善；只有 seed 20260908 做过 vLLM 服务对比，该次未观察到吞吐收益。这批英文提示没有基座目标的对应测量，不能断言微调是否损害了发布版 draft model。

**设置 B：** 同文本成对测量显示命中率与联合前缀接受长度提高，vLLM 服务端计数和吞吐也同向提高。这是一次联合训练的结果，没有隔离 selector 训练的独立贡献。Round 5 在新会话上复测三条服务路线各 4 次，再训 draft model 在并发 1/4/8 上分别高出发布版 9.1% / 7.8% / 8.5%，三档极差均不重叠；与 Round 4 单次测量的 +9.2% / +8.0% 相差不超过 0.25 个百分点。两次共用权重、提示、seed 与引擎参数，确定性 greedy 解码让它们必然相近，因此这是跨会话可复现，不是独立复制。两个 draft model 在并发 1 下的输出 40/40 逐字相同：推测解码接受的每个 token 都是目标模型自己的 argmax，draft model 只改变验证步数，不改变输出；不应期待换 draft model 会改变答案。Round 6 再把 200 条提示切成 5 块各测一次：增益在 20 个块×并发格中全为正，但 Round 4/5 用的那 40 条处于偏高一端，五块均值为 +6%～+9%，单块最低 +2.1%。**对外引用应用这个均值与范围，而不是 Round 5 的 +9%。**

基座对照另属跨文本诊断：发布版 draft model 在基座目标上的首位命中为 0.720、联合前缀接受长度为 3.24。该对照的输出与微调目标不同，不能接到成对比较中当作“恢复比例”。40 条输出检查中，基座平均生成 252 个 token、39 条触及 256 上限；微调目标平均 119 个 token。token 级重复 4-gram 比例分别为 0.054 和 0.021。这些差异是混淆因素，不是已经验证的命中变化原因。

**保留反向结果。** 设置 B 的 HF 参考路径 `dflash_generate` 在 40 条提示的一次运行中，发布版 draft model 端到端接受长度为 3.12，再训 draft model 为 2.91，方向与成对测量及 vLLM 服务结果相反。40 条中没有逐字相同的输出；这使该结果不能作为同文本配对，但不构成忽略它的理由。

HF 与 vLLM 的接受长度还存在跨引擎差距。两者未在相同批处理、精度与缓存路径下对比，差距原因仍未定位。

**结果边界：** 一个目标模型系列、一个数据集系列、一张 GPU。两种设置由作者选择，不是标定后的重训阈值。

中文微调目标在 token 级重复筛查中失败：40 条中 11 条存在某个 4-gram 出现至少三次，门限为两条。流水线停下后，作者检查到基座有 38 条重复触发、39 条长度截断，随后带质量警告继续实验。两者都失败并不能证明筛查无效，也不能证明任一目标的答案质量合格。事后复核逐条记录（[gates/target_zh.json](experiments/20260909-drafter-adaptation/results/round4/gates/target_zh.json)）：被标记的 11 条中 `max_4gram_count` 最大仅为 4，40 条的 `repeat_4gram` 中位数 0.009、最大 0.088，而 Round 1–2 确认退化的英文目标最大值为 0.541；重复项是“根据患者的”“最可能的”这类中文医学套语与平行句式。`>= 3` 是在英文实验中引入的裸常数，没有标定记录；中文 tokenizer 下 4 个 token 约对应 2–4 个汉字，该判据不能跨语言直接使用。作者的判断是这次失败属于筛查假阳性；这是对重复指标的判断，仍不是答案评分。

输入和切分清单发布在 [inputs/](experiments/20260909-drafter-adaptation/inputs/) 并与运行哈希核对。数据集 revision 未固定，重新下载须先通过哈希检查。Round 3 的目标文本每次重新生成，只记录边际命中率，因此不报告成对区间。权重未分发，哈希保留在 [provenance.json](experiments/20260909-drafter-adaptation/evidence/provenance.json)。

### 可复用的训练做法

1. **先测发布版 draft model。** 英文三个种子的 loss 都下降，但成对命中未改善；唯一做过服务对比的 seed 20260908 未见吞吐收益。不能因为 loss 变小就替换线上 draft model。
2. **让训练与服务使用同一个目标。** 自生成语料、隐藏特征、embedding 和输出头都来自带同一 Adapter 的目标，不把基座特征与微调目标混用。
3. **分别记录两类 loss。** 保存 `history`、`selector_history`、`gamma` 和 `selector_weight`；骨干 loss 下降不证明 selector 学到了有效排序。
4. **保存、重载后再评估。** 保留 checkpoint 重载检查，再做同文本留出比较和三条 vLLM 路线验证。
5. **失败也进入证据。** 筛查 FAIL、HF 反向结果、短文本排除和未评分项都保留。当前实验联合训练骨干与 selector，不能把全部收益单独归因于 selector。

## 架构与测试环境

客户端与推理服务运行在同一台机器上，通过回环地址通信。服务端每次只启动基线、MTP 或 DFlash 中的一种模式；切换模式不改变客户端 API。客户端记录请求耗时，评分程序检查生成的完整答案。

![测试流程：客户端、推理服务、draft model、记录、评分与汇总](experiments/20260906-qwen38/images/test-flow-cn.png)

*原创测试流程图，依据本次[执行程序](experiments/20260906-qwen38/source/campaign_runner.py)、[流式计时](experiments/20260906-qwen38/source/stream_metrics.py)与[评分接入](experiments/20260906-qwen38/source/scoring.py)绘制；图源为 [test-flow-cn.mmd](experiments/20260906-qwen38/images/test-flow-cn.mmd)。图中区分推理、客户端测量和评分；三种模式并非同时运行。*

draft model 再适配走另一条训练路径，同样在单张 GPU 上按阶段执行，不与推理服务同时驻留：

![draft model 再适配的数据与模型流](experiments/20260909-drafter-adaptation/images/training-flow-cn.png)

*原创训练流程图，依据 [目标训练](experiments/20260909-drafter-adaptation/source/round4/finetune_target.py)、[语料生成](experiments/20260909-drafter-adaptation/source/round4/generate_responses.py)、[draft model 训练](experiments/20260909-drafter-adaptation/source/round4/train_drafter.py)和 [成对测量](experiments/20260909-drafter-adaptation/source/round4/analyze_predictability.py)。[图源](experiments/20260909-drafter-adaptation/images/training-flow.json)由同一绘图程序渲染。训练、保存重载和效果验证是三个独立检查点，流程图本身不是运行证明。*

## 快速上手

### 推理服务与请求

<a id="how-to-run"></a>

#### 1. 先分清目标模型和 draft model 权重

约 3.8 GB 的文件是 **DFlash 2 的 draft model，不是完整的 Qwen3.8-27B，也不是它的 MTP 权重**。不能把该文件作为 `--model` 再指定 `method=mtp`。三条路线都必须加载完整目标模型权重：

| 路线 | 目标模型 | 推测配置 |
|---|---|---|
| 基线 | Qwen3.8-27B | 不传 `--speculative-config` |
| MTP7 | 同一目标模型，使用其自带 MTP 权重 | `method="mtp"`，不另传 draft model |
| DFlash 2-7 | 同一目标模型，另加载 DFlash 2 draft model | `method="dflash"`，`model` 指向 draft model 目录 |

本次记录的目标 `.safetensors` 共 18 个、55,563,006,776 字节（约 55.56 GB）；draft model 权重为 1 个、3,848,817,896 字节（约 3.85 GB / 3.58 GiB）。这是磁盘权重大小，不是推理所需总显存。**DFlash 2 是模型权重的名称，本次 vLLM 的启动方法仍写 `dflash`，不是 `dflash2` 或 `draft_model`。**

以下使用 Linux x86_64、Bash 和 Python 3.12。实测硬件为单张 H100 NVL；需要支持 CUDA 13 的 NVIDIA 驱动，以及目标模型、draft model、KV cache 和工作区所需显存。其他 GPU 容量与数值行为需另行验证。

#### 2. 准备固定版本

以下命令都在本仓库的 `Deep-Learning/Speculative-Decoding` 目录执行。环境创建仅用于首次安装；已有经过验证的相同版本环境时直接激活，不要重建。下载会占用数十 GB 磁盘空间。

```bash
python3 -m venv "$HOME/.venvs/qwen38-specdec"
source "$HOME/.venvs/qwen38-specdec/bin/activate"
python -m pip install 'vllm==0.28.0' 'torch==2.13.0' 'transformers==5.16.1'
python -m pip check

export MODEL_ROOT="$HOME/models/qwen38"
hf download Qwen/Qwen3.8-27B \
	--revision 1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0 \
	--local-dir "$MODEL_ROOT/target"
hf download incoai/Qwen3.8-27B-DFlash2 \
	--revision dedf8df68adfb1afeaf7b7480c0a0243108177b4 \
	--local-dir "$MODEL_ROOT/draft"
```

模型目录中应包含配置、全部权重分片和目标模型的分词器（tokenizer），不能只下载一个权重分片。上述包版本来自实际安装记录；这里只固定关键包，不保证未来安装时取得的所有间接依赖完全相同。

#### 3. 设置三条路线共用的启动参数

在服务端终端执行一次，三种启动方式共用同一个 Bash 数组。目标路径不要包含 `dflash` 字样，避免模型路径识别与实际角色混淆。每次启动的日志保存到 `$HOME/specdec-runs/` 下的独立目录，不覆盖已有结果。

```bash
set -euo pipefail
export VLLM_USE_V2_MODEL_RUNNER=1
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
export RUN_DIR="$HOME/specdec-runs/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$RUN_DIR"

COMMON=(
	--model "$MODEL_ROOT/target"
	--served-model-name Qwen/Qwen3.8-27B
	--dtype bfloat16 --tensor-parallel-size 1
	--max-model-len 32768
	--max-num-seqs 16 --max-num-batched-tokens 16384
	--gpu-memory-utilization 0.9
	--no-enable-prefix-caching --kv-cache-dtype auto
	--attention-backend FLASH_ATTN --mamba-ssm-cache-dtype float32
	--reasoning-parser qwen3 --stream-interval 1
	--generation-config vllm --seed 20260906
	--limit-mm-per-prompt '{"image":0,"video":0,"audio":0}'
	--host 127.0.0.1 --port 18080
)
```

关键区别：模型和 KV cache 使用 BF16，但 Mamba SSM cache 固定为 FP32。`max-num-seqs=16` 是服务端调度上限，不是客户端必须发 16 路并发。`generation-config=vllm` 避免模型目录的生成默认值覆盖显式实验设置。

#### 4. 选择一种方式启动

**同一 GPU、同一端口只运行其中一条。** 一条路线测完后，在服务端终端按 `Ctrl+C` 停止，并用 `nvidia-smi` 确认该服务已退出，再启动下一条。

基线，不启用推测解码：

```bash
python -I -B -m vllm.entrypoints.openai.api_server "${COMMON[@]}" \
	2>&1 | tee "$RUN_DIR/baseline-server.log"
```

MTP7，使用目标模型自带的 MTP 权重：

```bash
python -I -B -m vllm.entrypoints.openai.api_server "${COMMON[@]}" \
	--speculative-config '{"method":"mtp","num_speculative_tokens":7,"rejection_sample_method":"standard"}' \
	2>&1 | tee "$RUN_DIR/mtp7-server.log"
```

DFlash 2-7，额外加载配套 draft model 权重：

```bash
python -I -B -m vllm.entrypoints.openai.api_server "${COMMON[@]}" \
	--speculative-config "{\"method\":\"dflash\",\"model\":\"$MODEL_ROOT/draft\",\"num_speculative_tokens\":7,\"rejection_sample_method\":\"standard\"}" \
	2>&1 | tee "$RUN_DIR/dflash2_7-server.log"
```

另开同机终端，先检查 `curl --fail http://127.0.0.1:18080/v1/models` 能返回 `Qwen/Qwen3.8-27B`。同时核对启动日志中实际生效的模式、Model Runner V2 和精度；DFlash 应加载 `DFlash2DraftModel`。服务就绪只证明加载完成，还需要下一步真实请求。

#### 5. 设置客户端请求与采样

客户端始终调用同一个 `/v1/chat/completions` 和同一个 `model` 名称，**不在客户端切换 MTP/DFlash**。模式由服务端启动参数决定。下面配置的是采样、思考过程、输出上限和请求并发，不包含联网搜索或检索增强生成（RAG）。`top_k=20` 是输出采样范围，不是服务端每轮起草的 7 个 token。

| 客户端设置 | 本次值 |
|---|---|
| 采样 | `temperature=1.0`、`top_p=0.95`、`top_k=20`、`min_p=0.0` |
| 重复惩罚 | `presence_penalty=0.0`、`repetition_penalty=1.0` |
| 思考 | `reasoning_effort="xhigh"`，模板开启并保留思考过程 |
| 输出上限 | `max_completion_tokens=16384`，包含思考过程 |
| 流式统计 | `stream=true`、`include_usage=true`、`return_token_ids=true`、`include_reasoning=true`、`stream_interval=1` |
| 客户端并发 | 正式子集分别为 1、4、8；三次基础随机种子为 20260906、20260907、20260908 |

每题实际随机种子为 `int(SHA256(f"{base_seed}|{task_id}")[:8], 16) % 2147483647`，不是把基础随机种子原样用于每题。[请求样例](experiments/20260906-qwen38/evidence/request-examples.json)保存了当时发送的完整 JSON。下面直接发送其中一份，以保持提示词和参数一致。

在客户端终端进入同一个 `Deep-Learning/Speculative-Decoding` 目录并激活相同 Python 环境，然后执行：

```bash
set -euo pipefail
source "$HOME/.venvs/qwen38-specdec/bin/activate"
CLIENT_RUN="$HOME/specdec-runs/client-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$CLIENT_RUN"
python -c 'import json; from pathlib import Path; samples=json.loads(Path("experiments/20260906-qwen38/evidence/request-examples.json").read_text(encoding="utf-8")); print(json.dumps(samples[0]["request"], ensure_ascii=False))' \
	> "$CLIENT_RUN/request.json"
curl --fail-with-body --no-buffer --connect-timeout 10 --max-time 600 \
	http://127.0.0.1:18080/v1/chat/completions \
	-H 'Content-Type: application/json' \
	--data-binary @"$CLIENT_RUN/request.json" \
	| tee "$CLIENT_RUN/response.sse"
```

`samples[0]` 是代码题，改为 `samples[1]` 可发送数学题。检查完整 SSE 中的生成 `token_ids`、最终 `usage`、`finish_reason` 和 `[DONE]`；`finish_reason=length` 表示触及上限，不能当作正常完成的答案。三条路线应使用相同请求 JSON。此处 curl 的超时与记录方式仅用于单请求复现，不用于计算上文性能表。

#### 复现范围

上述命令依据实际安装记录、启动参数及[测量源码](experiments/20260906-qwen38/source/campaign_runner.py)的 `server_command` 整理，将本机路径改为环境变量。**适用范围是三种服务的启动和单个请求的调用，不是完整的性能与质量评测。** 新安装环境仍需验证模型加载与请求结果，不能将已有测试成绩视为新环境的验收结果。

复跑得分表还必须保持同一批 64 题、27 组、固定顺序和并发策略，执行原测量逻辑及 EvalPlus/Math-Verify 评分。`campaign_runner.py` 所需的完整准备步骤、题目输入和调度配置尚未打包为可独立运行的公开入口，因此不能仅凭本页配置直接运行 `--stage all`。现有文件支持启动与调用参考、已保存结果的离线复算，不是完整 27 组实验的独立安装包。官方方法入口：[MTP](https://github.com/vllm-project/vllm/blob/v0.28.0/docs/features/speculative_decoding/mtp.md)、[固定版本推测配置源码](https://github.com/vllm-project/vllm/blob/2cf0a6915ce544dc493a0990f2ea38d81601128a/vllm/config/speculative.py)。

### 复现再适配

以下步骤依据已执行的脚本和参数整理，工作目录改为用户自己的新目录。原始执行代码位于 [source/round4/](experiments/20260909-drafter-adaptation/source/round4/)。本轮文档检查没有重新执行 GPU 训练；新环境仍需逐步验收。

**环境要求：** Linux x86_64、Bash、Python 3.12、兼容 CUDA 13 的驱动。实测 GPU 是 H100 NVL，报告可用显存 95,830 MiB；draft model float32 主权重训练的峰值分配约 86 GiB，**不能据此宣称一张 80 GB GPU 能运行原配置**。磁盘需同时容纳基座、draft model、训练 checkpoint 和另一份约 56 GB 的合并目标，不能只预留下载大小。

#### 1. 选择设置并准备环境

从本仓库 `Deep-Learning/Speculative-Decoding` 目录开始。`zh` 对应中文全模块 LoRA，`en` 对应英文注意力 LoRA；每次新运行使用独立目录，模型下载目录可复用。

```bash
set -euo pipefail
export ADAPT_LANGUAGE=zh
export ADAPT_EVIDENCE="$PWD/experiments/20260909-drafter-adaptation"
export ADAPT_SOURCE="$ADAPT_EVIDENCE/source/round4"
export ADAPT_RUN="$(mktemp -d "$HOME/drafter-adaptation.XXXXXXXX")"
export MODEL_ROOT="$HOME/models/qwen38"
export TRAIN_PYTHON="$HOME/.venvs/qwen38-drafter/bin/python"
export SERVE_PYTHON="$HOME/.venvs/qwen38-specdec/bin/python"
mkdir -p "$ADAPT_RUN"/{data,cache,out,served,results,logs}
printf 'export ADAPT_RUN=%q\nexport ADAPT_SOURCE=%q\n' "$ADAPT_RUN" "$ADAPT_SOURCE"
printf 'export TRAIN_PYTHON=%q\n' "$TRAIN_PYTHON"
```

仅首次安装执行下面的环境块。已有这些版本的环境时，设置两个 Python 路径后跳过安装，不按每次运行重建环境。

```bash
python3.12 -m venv "$HOME/.venvs/qwen38-drafter"
"$TRAIN_PYTHON" -m pip install \
	torch==2.13.0 transformers==5.16.1 peft==0.20.0 \
	dflash==0.1.0 datasets huggingface_hub
python3.12 -m venv "$HOME/.venvs/qwen38-specdec"
"$SERVE_PYTHON" -m pip install \
	vllm==0.28.0 torch==2.13.0 transformers==5.16.1
"$TRAIN_PYTHON" -m pip check
"$SERVE_PYTHON" -m pip check
```

检查两个 `pip check` 均退出 0。这里只固定关键包，未锁定全部间接依赖。

#### 2. 获取固定版本权重和数据

```bash
"$(dirname "$TRAIN_PYTHON")/hf" download Qwen/Qwen3.8-27B \
	--revision 1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0 \
	--local-dir "$MODEL_ROOT/target"
"$(dirname "$TRAIN_PYTHON")/hf" download incoai/Qwen3.8-27B-DFlash2 \
	--revision dedf8df68adfb1afeaf7b7480c0a0243108177b4 \
	--local-dir "$MODEL_ROOT/draft"
"$TRAIN_PYTHON" "$ADAPT_SOURCE/prepare_domain_data.py" \
	--dataset FreedomIntelligence/medical-o1-reasoning-SFT --config "$ADAPT_LANGUAGE" \
	--question-field Question --response-field Response --instruction-field "" \
	--train-size 2000 --eval-size 200 --seed 20260909 --max-output-chars 1000000 \
	--out-dir "$ADAPT_RUN/data" 2>&1 | tee "$ADAPT_RUN/logs/prepare-data.log"
```

产物是 2,000 条训练问题、200 条留出提示和切分清单。数据集下载时未固定 revision；开始训练前必须核对 train/eval 哈希，不能仅凭行数相同就比较。

```bash
"$TRAIN_PYTHON" - <<'PY'
import json, os
from pathlib import Path
run = Path(os.environ["ADAPT_RUN"])
evidence = Path(os.environ["ADAPT_EVIDENCE"])
language = os.environ["ADAPT_LANGUAGE"]
current = json.loads((run / "data/split_manifest.json").read_text())
recorded = json.loads((evidence / f"inputs/round4/split_manifest_{language}.json").read_text())
for field in ("train_sha256", "eval_sha256", "seed", "train_rows", "eval_rows"):
		assert current[field] == recorded[field], f"SPLIT_MISMATCH:{field}"
print("SPLIT_HASH_MATCH=PASS")
PY
```

#### 3. 微调目标模型

两种设置使用不同的 LoRA 配方；数据种子与训练种子分别为 `20260909` 和 `20260908`。

```bash
case "$ADAPT_LANGUAGE" in
	en) ADAPTER_ARGS=(--epochs 1 --lr 5e-5 --lora-rank 16 --lora-alpha 32
									 --target-modules attention) ;;
	zh) ADAPTER_ARGS=(--epochs 2 --lr 1e-4 --lora-rank 128 --lora-alpha 256
									 --target-modules all) ;;
	*) exit 2 ;;
esac
"$TRAIN_PYTHON" "$ADAPT_SOURCE/finetune_target.py" \
	--target "$MODEL_ROOT/target" --data "$ADAPT_RUN/data/train.jsonl" \
	--output "$ADAPT_RUN/out/adapter" --grad-accum 8 --max-length 1024 \
	--seed 20260908 "${ADAPTER_ARGS[@]}" \
	2>&1 | tee "$ADAPT_RUN/logs/target-training.log"
```

检查 `FINETUNE_TARGET=PASS`、Adapter 权重与 `out/adapter/training_summary.json`。该摘要保留实际使用样本数、丢弃长样本数、loss 记录和显存，不以切分行数代替训练样本数。

#### 4. 筛查目标输出并记录基准 draft model

该筛查是启发式，不是答案评分。已归档中文运行在这里返回 `DEGENERATION_GATE=FAIL`；出现失败应先检查输出，再决定是否只继续研究 draft model 行为，不能自动忽略失败或当作质量通过。

```bash
"$TRAIN_PYTHON" "$ADAPT_SOURCE/check_degeneration.py" \
	--target "$MODEL_ROOT/target" --adapter "$ADAPT_RUN/out/adapter" \
	--prompts "$ADAPT_RUN/data/eval_prompts.jsonl" --limit 40 \
	--max-new-tokens 256 --repetition-unit token --label target \
	--output "$ADAPT_RUN/results/degeneration.json" \
	2>&1 | tee "$ADAPT_RUN/logs/target-screen.log"
```

在筛查通过或已明确接受“答案质量未验证”的研究边界后，记录发布版 draft model，并保存供两份 draft model 共用的目标输出缓存。

```bash
"$TRAIN_PYTHON" "$ADAPT_SOURCE/analyze_predictability.py" \
	--target "$MODEL_ROOT/target" --adapter "$ADAPT_RUN/out/adapter" \
	--prompts "$ADAPT_RUN/data/eval_prompts.jsonl" --cache "$ADAPT_RUN/cache/target.pt" \
	--drafter "$MODEL_ROOT/draft" --draft-path selector --label released \
	--output "$ADAPT_RUN/results/pred-released.json" \
	2>&1 | tee "$ADAPT_RUN/logs/agreement-released.log"
```

#### 5. 生成训练回答，训练 draft model 与 selector

训练问题来自 train 切分，回答由带 Adapter 的目标模型生成。不能把留出问题或人工参考答案混入 draft model 训练语料。

```bash
"$TRAIN_PYTHON" "$ADAPT_SOURCE/generate_responses.py" \
	--target "$MODEL_ROOT/target" --adapter "$ADAPT_RUN/out/adapter" \
	--prompts "$ADAPT_RUN/data/train.jsonl" --output "$ADAPT_RUN/data/corpus.jsonl" \
	--limit 1200 --max-new-tokens 320 --batch-size 8 \
	2>&1 | tee "$ADAPT_RUN/logs/corpus.log"
```

检查语料旁的 `.manifest.json` 中 `requested`、`written` 与哈希；空回答可能被跳过，不能把请求数直接写成有效语料数。

```bash
"$TRAIN_PYTHON" "$ADAPT_SOURCE/train_drafter.py" \
	--target "$MODEL_ROOT/target" --adapter "$ADAPT_RUN/out/adapter" \
	--drafter "$MODEL_ROOT/draft" --data "$ADAPT_RUN/data/corpus.jsonl" \
	--output "$ADAPT_RUN/out/drafter" --epochs 2 --limit 1200 \
	--anchors-per-sequence 8 --block 8 --max-length 1024 --gamma 7 \
	--lr 1e-4 --weight-decay 0.0 --warmup-fraction 0.05 --drafter-dtype float32 \
	--train-selector --selector-weight 1.0 --seed 20260908 \
	2>&1 | tee "$ADAPT_RUN/logs/drafter-training.log"
```

检查 `TRAIN=PASS`，其中 `checkpoint_reloads=true` 是保存后重新加载的权重相等检查。`out/drafter/training-history.json` 分开保存 `history` 与 `selector_history`；loss 下降不能替代下一步留出集和服务验证。英文另两个训练种子分别为 `1`、`2`，须写入不同输出目录，不能覆盖本次 checkpoint。

#### 6. 重载后评估 draft model

两个 draft model 使用同一个 `target.pt`。核对结果中的 cache 哈希和逐提示文本哈希，再计算成对差异；HF 端到端接受长度另行保存。

```bash
"$TRAIN_PYTHON" "$ADAPT_SOURCE/analyze_predictability.py" \
	--target "$MODEL_ROOT/target" --adapter "$ADAPT_RUN/out/adapter" \
	--prompts "$ADAPT_RUN/data/eval_prompts.jsonl" --cache "$ADAPT_RUN/cache/target.pt" \
	--drafter "$ADAPT_RUN/out/drafter" --draft-path selector --label ours \
	--output "$ADAPT_RUN/results/pred-ours.json" \
	2>&1 | tee "$ADAPT_RUN/logs/agreement-ours.log"
"$TRAIN_PYTHON" "$ADAPT_SOURCE/measure_acceptance.py" \
	--target "$MODEL_ROOT/target" --adapter "$ADAPT_RUN/out/adapter" \
	--prompts "$ADAPT_RUN/data/eval_prompts.jsonl" --limit 40 --max-new-tokens 256 \
	--drafter "$MODEL_ROOT/draft" --output "$ADAPT_RUN/results/acc-released.json" \
	--label released 2>&1 | tee "$ADAPT_RUN/logs/acceptance-released.log"
"$TRAIN_PYTHON" "$ADAPT_SOURCE/measure_acceptance.py" \
	--target "$MODEL_ROOT/target" --adapter "$ADAPT_RUN/out/adapter" \
	--prompts "$ADAPT_RUN/data/eval_prompts.jsonl" --limit 40 --max-new-tokens 256 \
	--drafter "$ADAPT_RUN/out/drafter" --output "$ADAPT_RUN/results/acc-ours.json" \
	--label ours 2>&1 | tee "$ADAPT_RUN/logs/acceptance-ours.log"
```

#### 7. 导出服务权重，保留基座原件

draft model 导出工具转换权重键布局；目标 LoRA 合并后保存到新目录，基座权重不移动、不覆盖。

```bash
"$TRAIN_PYTHON" "$ADAPT_SOURCE/export_drafter_for_vllm.py" \
	--source "$ADAPT_RUN/out/drafter" --reference "$MODEL_ROOT/draft" \
	--output "$ADAPT_RUN/served/draft" 2>&1 | tee "$ADAPT_RUN/logs/export-draft.log"
"$TRAIN_PYTHON" - <<'PY'
import os, torch
from pathlib import Path
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel
run = Path(os.environ["ADAPT_RUN"])
base = Path(os.environ["MODEL_ROOT"]) / "target"
tokenizer = AutoTokenizer.from_pretrained(base)
model = AutoModelForCausalLM.from_pretrained(base, dtype=torch.bfloat16, device_map="cuda")
model = PeftModel.from_pretrained(model, run / "out/adapter").merge_and_unload()
model.save_pretrained(run / "served/target", safe_serialization=True, max_shard_size="5GB")
tokenizer.save_pretrained(run / "served/target")
print("MERGED_TARGET_SAVED")
PY
```

检查 `EXPORT_DRAFTER=PASS` 和合并目标目录。服务加载成功与真实请求仍由后续步骤验证。

#### 8. 服务端终端：一次只启动一条路线

训练进程退出后，先设置三条路线共用的参数。服务端保持在前台，**不要把客户端命令接在服务启动命令后面**。

```bash
export VLLM_USE_V2_MODEL_RUNNER=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
ADAPT_SERVER_ARGS=(
	--model "$ADAPT_RUN/served/target" --served-model-name Qwen/Qwen3.8-27B
	--dtype bfloat16 --tensor-parallel-size 1 --max-model-len 8192
	--max-num-seqs 16 --max-num-batched-tokens 16384 --gpu-memory-utilization 0.9
	--no-enable-prefix-caching --kv-cache-dtype auto --attention-backend FLASH_ATTN
	--mamba-ssm-cache-dtype float32 --reasoning-parser qwen3 --generation-config vllm
	--seed 20260909 --limit-mm-per-prompt '{"image":0,"video":0,"audio":0}'
	--host 127.0.0.1 --port 18080
)
```

基线：同一个微调目标，不加载 draft model。

```bash
"$SERVE_PYTHON" -m vllm.entrypoints.openai.api_server "${ADAPT_SERVER_ARGS[@]}" \
	2>&1 | tee "$ADAPT_RUN/logs/server-baseline.log"
```

发布版 draft model：完成基线客户端测量并停止基线服务后，运行此块。

```bash
"$SERVE_PYTHON" -m vllm.entrypoints.openai.api_server "${ADAPT_SERVER_ARGS[@]}" \
	--speculative-config "{\"method\":\"dflash\",\"model\":\"$MODEL_ROOT/draft\",
		\"num_speculative_tokens\":7,\"rejection_sample_method\":\"standard\"}" \
	2>&1 | tee "$ADAPT_RUN/logs/server-released.log"
```

再训练 draft model：停止发布版 draft model 服务后，运行此块。

```bash
"$SERVE_PYTHON" -m vllm.entrypoints.openai.api_server "${ADAPT_SERVER_ARGS[@]}" \
	--speculative-config "{\"method\":\"dflash\",\"model\":\"$ADAPT_RUN/served/draft\",
		\"num_speculative_tokens\":7,\"rejection_sample_method\":\"standard\"}" \
	2>&1 | tee "$ADAPT_RUN/logs/server-ours.log"
```

#### 9. 客户端终端：检查服务并测量

另开同机 Bash 终端，先执行第 1 步打印的三个 `export` 语句，指向同一次运行。每启动一条服务，就运行一次相应客户端；`ROUTE` 依次选 `baseline`、`released`、`ours`，文件不会互相覆盖。

```bash
set -euo pipefail
export ROUTE=baseline
curl --fail http://127.0.0.1:18080/v1/models
"$TRAIN_PYTHON" "$ADAPT_SOURCE/vllm_client_bench.py" \
	--prompts "$ADAPT_RUN/data/eval_prompts.jsonl" --limit 40 --max-tokens 256 \
	--concurrency 1 4 --warmup 2 --label "$ROUTE" \
	--output "$ADAPT_RUN/results/vllm-$ROUTE.json" \
	2>&1 | tee "$ADAPT_RUN/logs/client-$ROUTE.log"
```

模型列表应包含 `Qwen/Qwen3.8-27B`；draft model 服务启动日志应显示 `DFlash2DraftModel`。客户端需完成两档并发并输出 `VLLM_CLIENT_BENCH=PASS`。对比三份 JSON 的 token 数、整组耗时、输出上限和采样参数，服务端 accepted/drafted 计数单独核对，不能混入教师强制指标。

#### 10. 停止服务并保存本次证据

每条路线测完后，在服务端按 `Ctrl+C`，用 `nvidia-smi` 确认服务进程退出，再启动下一条。全部完成后保留 `logs/`、`results/`、数据切分清单、语料 manifest、两个 checkpoint 及训练 history；使用云主机时还需自行停止计费资源。

原仓库的离线验收只验证已发布证据，不会为这次新运行判分。更换硬件、依赖或数据后须重建自己的评测记录；成对 bootstrap 也不能保证结果跨环境不变。

## 测试与离线复算

下面的检查全部在本地离线运行，只读已保存的记录和本文，不会启动服务、发请求或重新评分。

| 检查项 | 命令 | 通过条件 |
|---|---|---|
| 报告与证据一致性 | `validate_report.py` | 输出 `REPORT_GATE=PASS`，每条规则各一行 `RULE ... PASS`，退出码 0 |
| 漂移与拒绝测试 | `unittest discover` | 全部测试通过；每个注入的缺陷（改表格数值、改请求、改图片字节、改归档源码、伪造验收记录、删徽章、折叠必需章节、新增嵌套 Markdown）都被对应错误拦住 |
| 独立重算汇总 | `analyze_results.py --groups` | 重新生成的 `summary.json` 与已发布版本相同 |
| 上一轮实验复算 | `analyze_results.py --root ... --matrix` | 全部 3,100 个请求、冻结题集和评分绑定均能对上 |
| 再适配汇总与表格 | `experiments/20260909-drafter-adaptation/validate_report.py` | 从逐请求记录重算全部再适配数字，成对 bootstrap 只允许在逐字相同的目标文本上运行，扫描私有标识，输出 `ADAPTATION_GATE=PASS` |

前置条件：Python 3.10+ 标准库，在 `Deep-Learning/Speculative-Decoding` 目录执行；上一轮实验的复算需要 Python 3.12。不需要 GPU、网络、凭据或额外依赖。专用 CI 在 Windows 和 Linux 的 Python 3.10、3.12 上执行前两项，在 Python 3.12 上执行上一轮复算。

这些测试不覆盖：新的推理、官方重新评分、GPU kernel 行为，以及需要 Matplotlib 和中文字体、因而手动执行的绘图脚本。

```bash
python experiments/20260906-qwen38/validate_report.py
python -m unittest discover -s experiments/20260906-qwen38 -p "test_*.py"
python experiments/20260909-drafter-adaptation/validate_report.py
python -m unittest discover -s experiments/20260909-drafter-adaptation -p "test_*.py"
```

验收应输出 `REPORT_GATE=PASS` 和 `ADAPTATION_GATE=PASS`，测试全部通过，每条命令的退出码都为 0。它们检查本文表格、已保存的评分和文件哈希是否一致。

要从逐组数据独立核对汇总数字，可执行：

```bash
python experiments/20260906-qwen38/analyze_results.py --groups experiments/20260906-qwen38/data/groups.json --output experiments/20260906-qwen38/regenerated
```

输出目录中的 `summary.json` 可与[已发布汇总](experiments/20260906-qwen38/data/summary.json)对照。程序只读取已保存的评分、计数和计时，不执行生成的答案。

上一轮实验的复算需要 Python 3.12，检查全部 3,100 个请求、固定题集、题目/重复次数和评分绑定；缺题或错配会失败，不会缩小分母后报分：

```bash
python experiments/20260905-quality/src/analyze_results.py --root experiments/20260905-quality --output out/20260905-replayed.json --matrix
```

## 兼容性与边界

**目标模型微调后，draft model 一定要重训吗？不一定。** 本次使用的 `incoai/Qwen3.8-27B-DFlash2` 是为 `Qwen/Qwen3.8-27B` 发布的。换目标模型前，需要核对架构、tokenizer、隐藏特征和输出头是否兼容，再用实际负载验证。同属一个模型系列不能证明兼容；做过微调也不能直接证明不兼容。

原生 MTP 也需要模型架构、MTP 权重和推理引擎共同支持。打开一个服务参数，不等于能为模型生成原本缺失的 MTP 权重。

**DFlash 论文的标题是 DFlash: Block Diffusion for Flash Speculative Decoding。** 它用轻量块扩散模型并行预测多个 token，并读取目标模型的隐藏特征作为条件。微调 draft model 时，老师是实际要部署的目标模型，包括它的 Adapter；学生从已有 draft model 权重开始。问题来自目标业务，老师负责生成回答。训练时老师不动，只更新 draft model。这是对现有 draft model 的继续训练，不是从零训练。

下面概括 [DFlash 论文第 4.2 节和附录 A.1](https://arxiv.org/html/2602.06036v2#S4.SS2) 的方法，不表示本仓库复现了 DFlash 2 的完整训练配方。

- **训练回答**：论文使用约 80 万条 Nemotron Post-Training V2 和 CodeAlpaca 样本，以目标模型生成的回答训练。这是该实验的规模，不是所有再适配任务的最低要求。
- **条件信息**：从目标模型第 2 层到倒数第 3 层之间均匀取 5 层的隐藏状态，拼接后投影一次，注入每个 draft model 层的 key 和 value。
- **分块方式**：从回答中随机采样锚点 token 作为块首，块内其余位置打 mask，并行预测。
- **损失**：交叉熵按块内位置 `k` 加权 `exp(-(k-1)/gamma)`，因为块内靠前的错误会让后面全部作废。
- **共享参数**：目标模型及其 token embedding、语言模型头保持冻结，训练更新的是 draft model。

微调可能改变目标模型的特征和 token 预测，但具体改动取决于训练配方；Adapter 不一定更新 embedding 或输出头的权重。因此需要做对照，不能直接推断“官方 draft model 一定失配”或“再适配一定有提升”。

**三个结果要分开看。** 命中率看“猜得像不像目标模型”；答案质量看“最终回答是否完成任务”；吞吐和延迟看“实际服务是否更快”。训练 loss 下降或命中率上升，不能替代答案评分和服务性能测试。推测解码保持输出分布的理论保证，以正确实现验证和采样为前提，不能代替对具体引擎、精度和缓存路径的验收。

论文给出了一个再适配案例：[第 5.4 节表 4](https://arxiv.org/html/2602.06036v2#S5.SS4) 使用 1,600 条 LongAlign-10K 样本，对 Qwen3.5-27B 的 DFlash draft model 训练 3 个 epoch。在 HotpotQA、16K 上下文下，接受长度从 3.61 变为 6.05。这是作者的长上下文实验，不是本仓库的 Qwen3.8-27B 微调目标实验，也不是通用的时长或成本承诺。

| 情形 | 采用前要验证什么 |
|---|---|
| 已有为目标模型发布的 draft model | 在相同负载、并发和答案质量标准下，对比不开推测解码与使用官方 draft model |
| 目标已微调，已有兼容的官方 draft model | 先测官方 draft model。需要再适配时，两份 draft model 必须对着同一个冻结目标比较；新权重保存并重载后再评测 |
| 没有兼容的 draft model 权重 | 把新建 draft model 作为独立任务；现有权重的微调结果不能证明从零训练能力 |

本仓库分别保留推理对比和 draft model 再适配证据。继续训练已经在本次目标和数据设置下执行并评估，但不是跨模型通用配方；离线测试只核对已保存记录，不授予新环境训练或答案质量的通过结论。

### MTP 和 DFlash 差在哪里

推测解码先由 draft model 提出候选，再由目标模型验证。目标模型仍决定哪些 token 能进入输出。

| 比较项 | 本次 MTP7 | 本次 DFlash 2-7 |
|---|---|---|
| 起草所用权重 | Qwen3.8 模型文件自带的 MTP 权重 | 与目标模型配套的 DFlash 2 权重 |
| 候选怎样生成 | 在本次 vLLM 路径中逐步起草 | 通过块扩散（block diffusion）并行生成一组候选 |
| 每轮候选数量 | 7 个 token | 7 个 token |
| 谁做最终验证 | 同一个 Qwen3.8 目标模型 | 同一个 Qwen3.8 目标模型 |

这里的“7”是候选数量，不是网络层数。一次前向计算也不表示网络只有一层；它仍会经过 draft model 的各层。MTP 权重是否单独发布，随具体模型而异，不能把本次的打包方式当成 MTP 的统一定义。

推测解码的收益取决于两件事：每轮起草与验证花了多久，以及这一轮实际推进了多少个 token。候选越多，不一定越快；接受率也不是答案准确率。采样算法的分布保证，还需要正确的引擎实现，不能替代部署后的质量测试。

机制资料见 [DFlash 论文](https://arxiv.org/abs/2602.06036)和 [vLLM 固定版本源码](https://github.com/vllm-project/vllm/tree/2cf0a6915ce544dc493a0990f2ea38d81601128a)；本次参数见[实验配置](experiments/20260906-qwen38/evidence/configuration.json)。

### 结论适用到哪里

- 可以说明本次固定配置、固定子集中的性能和得分，不能证明统计显著性、分布等价或正式非劣效。
- 吞吐、客户端延迟、正确答案交付速度是不同指标，不能互相替代，也不能据此推断 GPU kernel 的独立性能。
- 本轮换了模型、权重版本和引擎，不能据此认定上一轮 DFlash 的并发故障已修复。两轮结果独立保留。

### 从零训练需要什么

本仓库**没有**从零训练过 DFlash draft model，上面所有结果都从发布版权重出发。唯一一次随机初始化运行是 CPU 上的玩具配置（隐藏维度 128，词表 512），脚本为 [`stage0_gradient_canary.py`](experiments/20260909-drafter-adaptation/source/round4/stage0_gradient_canary.py)：30 步，loss 6.24 → 3.88，来自作者的单次运行，日志未存档。它只说明梯度能到达 draft model 层、目标特征融合投影和归一化层，而冻结的目标模型没有梯度；不说明真实规模的 draft model 能收敛，也不是从零训练能力的证据。

论文配方（[第 5 节与附录 A.1](https://arxiv.org/html/2602.06036v2#A1.SS1)）：约 80 万条来自 Nemotron Post-Training V2 和 CodeAlpaca 的提示，回答由目标模型重新生成；6 个 epoch，AdamW 学习率 6e-4，余弦调度、4% 预热，序列最长 3,072 token，每条序列 512 个锚点通过一个稀疏注意力掩码一次联合训练。论文的消融实验用 10 万条样本达到全量加速的约四分之三（Qwen3-4B 在 MATH-500 上 4.71× 对 6.09×）。论文写明用 H200，但没有给出 GPU 数量和训练小时数。

本仓库的代码距离这套配方还差什么，按成本排序：

1. `dflash` 0.1.0 用 `torch.empty` 构造 `GroupedDynamicCausalConv.base_kernel`。从配置而不是 checkpoint 构建时，第一次前向就是 NaN。CPU 玩具测试把它初始化为恒等抽头；这个修法没有在真实规模上验证过。
2. `train_drafter.py` 每条序列取 8 个锚点、逐块前向。论文每条序列 512 个锚点、一次稀疏注意力前向，等于每次目标前向多出约 64 倍的 draft model 监督。用现在的循环做论文规模，要几千 GPU 小时。
3. DFlash 2 selector 的训练目标是作者自行构造的，只从发布版权重出发跑过。从零试点应当选论文的 DFlash 结构（不带选择器），z-lab 公开了该结构的参考权重可以对照。
4. `generate_responses.py` 用 Hugging Face `generate`、batch 8，在 27B 目标上约每秒 70 个输出 token。论文规模的数据需要服务引擎生成。
5. 只支持单卡，没有数据并行训练。

下面是量级估算，从实测的每步 0.61 秒和上文 vLLM 吞吐外推，并假设第 1–5 项先做完：

| 运行 | 目标模型 | 数据 | GPU | 时间 |
|---|---|---|---|---|
| 最小可辩护的从零试点 | Qwen3-8B，对照 z-lab 公开的 DFlash 权重 | 10 万条提示，自生成回答，6 个 epoch | 1–2 张 H100 级 | 生成半天到一天，单卡训练两到四天 |
| 论文规模 | Qwen3-8B | 80 万条，6 个 epoch | 约 8 张 | 三到四天 |
| 论文规模 | Qwen3.8-27B | 80 万条，6 个 epoch | 至少 8 张、每张显存大于 94 GiB（论文用 H200） | 4 卡生成约 1.5 天，训练约一周；一张 94 GiB 卡在序列长度 1,024 时峰值已到 86 GiB |

这些是估算，不是测量。今天能站住的说法是：再适配路径已实测；训练目标已实现，并证明能提高成对命中率；真实规模的从零训练尚未演示。

### 上一轮实验：Qwen3.6-27B 与首代 DFlash（2026-09-05）

<a id="previous-experiment"></a>

上一轮使用 Qwen3.6-27B、首代 DFlash draft model 和 vLLM 0.21.0，在同一张 H100 NVL 上完成了全部 164 道 HumanEval+ 和 500 道 MATH-500 的完整答案评测。**DFlash15 单请求更快，主分数与基线相近；但并发 4、8 时，相同 32 道代码题和 32 道数学题的答案质量明显回退。** 根因尚未定位，也未完成修复后复测。

两轮的目标模型、draft model、引擎、题目范围和采样设置都不同。本轮新组合没有复现旧组合的严重回退，不能据此认定旧问题已经修好。

#### 主评测：完整题集，每题一次

代码需要同时通过官方 EvalPlus 的基础与增强测试，数学由固定版本的官方 Math-Verify 脚本判分。截断回答仍留在分母内。

| 路线 | HumanEval+ | MATH-500 | 代码基础测试 | 数学长度截断 |
|---|---:|---:|---:|---:|
| Baseline | 152/164 (92.68%) | 489/500 (97.80%) | 159/164 | 4 |
| MTP5 | 155/164 (94.51%) | 494/500 (98.80%) | 162/164 | 2 |
| DFlash15 | 153/164 (93.29%) | 490/500 (98.00%) | 160/164 | 3 |

Baseline、MTP5、DFlash15 的代码请求耗时中位数为 4.393、1.175、0.660 秒，数学为 15.666、4.542、2.980 秒；代码输出速率中位数为 53.61、196.01、367.52 tok/s，数学为 54.05、187.60、281.77 tok/s。计时含预填充和同机客户端开销，不含服务启动。先对每一题计算 MTP5 耗时/DFlash15 耗时再取中位数，代码为 1.861 倍、数学为 1.498 倍。5 与 15 个draft token不代表相同计算预算，也不是各路线调优后的最佳配置。

![上一轮完整答案请求耗时](images/previous-latency-cn.png)

*作者实测，运行编号 dflash-quality-20260905，每路线代码 164 题、数学 500 题，每题一次。图值来自[逐题复算汇总](experiments/20260905-quality/analysis/summary.json)，由[绘图脚本](tools/make_readme_figures.py)生成。统计所有答案的请求总耗时，不是单独解码算子的时间。*

#### 并发质量不能放行

每档使用冻结题目清单的前 32 道代码题和前 32 道数学题，各执行一次。并发数是同机客户端同时在途的请求数，不是每秒到达率。

| 路线 | 并发 | HumanEval+ | 数学子集 | 长度截断（代码/数学） |
|---|---:|---:|---:|---:|
| Baseline | 1 | 32/32 | 32/32 | 0/0 |
| Baseline | 4 | 32/32 | 30/32 | 0/0 |
| Baseline | 8 | 32/32 | 31/32 | 0/0 |
| MTP5 | 1 | 32/32 | 31/32 | 0/0 |
| MTP5 | 4 | 32/32 | 31/32 | 0/0 |
| MTP5 | 8 | 32/32 | 32/32 | 0/0 |
| DFlash15 | 1 | 32/32 | 31/32 | 0/1 |
| DFlash15 | 4 | 11/32 | 13/32 | 8/17 |
| DFlash15 | 8 | 10/32 | 12/32 | 13/20 |

![上一轮相同题目下的并发正确数](images/previous-concurrency-cn.png)

*作者实测，相同 32+32 题，每档一次。原始响应和官方评分在[结果目录](experiments/20260905-quality/results/)，汇总在[分析结果](experiments/20260905-quality/analysis/summary.json)，图由[绘图脚本](tools/make_readme_figures.py)生成。异常只绑定该轮已测组合，曲线不提供根因证明。*

HumanEval/2 是一个具体例子：三个并发档的规范化请求哈希相同，[并发 1](experiments/20260905-quality/results/dflash15/concurrency-1/repeat-0/HumanEval_2.json) 返回正确函数；[并发 4](experiments/20260905-quality/results/dflash15/concurrency-4/repeat-0/HumanEval_2.json) 返回空定义和 JSON 片段；[并发 8](experiments/20260905-quality/results/dflash15/concurrency-8/repeat-0/HumanEval_2.json) 出现无关函数名和重复文本，耗尽 4,096 个词元后截断。这套 DFlash15 配置不能凭单请求结果直接承载并发流量；现有证据也不能把原因归结为某个 vLLM 组件、浮点误差、DFlash 理论、H100 或云平台。

三条主路线各 1,012 份响应（主评测 664、同种子重复 48、流式 48、并发 192、随机采样 48、合成检索 12），加上 DFlash5 的 64 份同窗口结果，合计 **3,100 份响应、25 个路线/场景组合**。DFlash5 代码和数学均为 32/32，其并发未测试。

#### 上一轮的固定方法

| 项目 | 记录值 |
|---|---|
| GPU | 一张 NVIDIA H100 NVL，95,830 MiB；驱动 610.57.04 |
| 目标模型 | `Qwen/Qwen3.6-27B` @ `6a9e13bd6fc8f0983b9b99948120bc37f49c13e9`，BF16 |
| draft model | `z-lab/Qwen3.6-27B-DFlash` @ `0919688658996800f86b895034249700e9481106` |
| 生成环境 | vLLM 0.21.0，PyTorch 2.11.0，transformers 4.57.6 |
| 评分器 | EvalPlus @ `26d6d00bb1fd0fa37f39c99d5290da67891d1c5e`；Math-Verify @ `ba3d3aaff23b3f4cac7a14672b4f6e293d97c98b` |
| 数据集 | HumanEval+ v0.1.10；MATH-500 @ `6e4ed1a2a79af7d8630a6b768ec859cb5af4d3be` |
| 主评测采样 | temperature 0，top_p 1，top_k -1，seed 20260905；`enable_thinking=false` |
| 服务参数 | max_model_len 40960，max_num_seqs 16，max_num_batched_tokens 8192，显存比例 0.9，关闭 prefix caching |
| 输出预算 | 代码 4096，数学 8192 |

模型、配置和分词器的文件哈希及包版本在 [inputs.json](experiments/20260905-quality/metadata/inputs.json)；[公开协议](experiments/20260905-quality/src/experiment.json)仅移除了私有资源管理对象，[投影说明](experiments/20260905-quality/metadata/protocol-public-projection.json)记录变更前后哈希。代码评分在无网络、非特权 Docker 容器内执行。

#### 在新目录重跑上一轮生成

需要 Linux、Python 3.12、兼容的 H100 NVL 环境及容纳两个权重快照和依赖的磁盘空间。评分容器按 UID/GID 1000 执行，宿主需要能用 `sudo -n` 调用 Docker。从 `experiments/20260905-quality` 目录开始，新建运行目录，不覆盖提供的证据。

```bash
set -euo pipefail
SOURCE="$PWD"
export DFLASH_RUN_ROOT="$(mktemp -d "$HOME/quality-replay-XXXXXXXX")"
export DFLASH_CACHE_ROOT="$DFLASH_RUN_ROOT/cache"
export DFLASH_TARGET_PATH="$DFLASH_CACHE_ROOT/target"
[[ "${DFLASH_TARGET_PATH,,}" != *dflash* ]]
mkdir -p "$DFLASH_RUN_ROOT"/{src,data,metadata,logs,results,state,upstream}
cp src/quality_runner.py src/prepare_data.py src/experiment.json "$DFLASH_RUN_ROOT/src/"
cp data/math500.jsonl data/humaneval_plus.jsonl "$DFLASH_RUN_ROOT/data/"
python3.12 -m venv "$DFLASH_CACHE_ROOT/venv"
PYTHON="$DFLASH_CACHE_ROOT/venv/bin/python"
"$PYTHON" -m pip install -r "$SOURCE/metadata/requirements-frozen.txt"
"$DFLASH_CACHE_ROOT/venv/bin/hf" download Qwen/Qwen3.6-27B --revision 6a9e13bd6fc8f0983b9b99948120bc37f49c13e9 --local-dir "$DFLASH_CACHE_ROOT/target"
"$DFLASH_CACHE_ROOT/venv/bin/hf" download z-lab/Qwen3.6-27B-DFlash --revision 0919688658996800f86b895034249700e9481106 --local-dir "$DFLASH_CACHE_ROOT/draft"
curl --fail --location 'https://raw.githubusercontent.com/huggingface/Math-Verify/ba3d3aaff23b3f4cac7a14672b4f6e293d97c98b/evaluate_model_outputs.py' -o "$DFLASH_RUN_ROOT/upstream/math-verify-evaluate.py"
curl --fail --location 'https://github.com/evalplus/mbppplus_release/releases/download/v0.2.0/MbppPlus.jsonl.gz' -o "$DFLASH_RUN_ROOT/upstream/mbpp-plus-v0.2.0.jsonl.gz"
gzip -dc "$DFLASH_RUN_ROOT/upstream/mbpp-plus-v0.2.0.jsonl.gz" > "$DFLASH_RUN_ROOT/upstream/mbpp-plus-v0.2.0.jsonl"
HUMANEVAL_OVERRIDE_PATH="$DFLASH_RUN_ROOT/data/humaneval_plus.jsonl" "$PYTHON" src/prepare_data.py --root "$DFLASH_RUN_ROOT" --cache "$DFLASH_CACHE_ROOT"
sudo -n docker build -f src/Dockerfile.eval -t dflash-quality-eval:20260905 .
"$PYTHON" "$DFLASH_RUN_ROOT/src/quality_runner.py" --phase canary
"$PYTHON" "$DFLASH_RUN_ROOT/src/quality_runner.py" --phase full
"$PYTHON" "$DFLASH_RUN_ROOT/src/quality_runner.py" --phase full --route dflash5
printf '{"phase":"COMPLETE","exit_code":0}\n' > "$DFLASH_RUN_ROOT/state/campaign.json"
"$PYTHON" "$SOURCE/src/analyze_results.py" --root "$DFLASH_RUN_ROOT" --output "$DFLASH_RUN_ROOT/analysis/summary.json" --matrix
```

生成 CLI 只停止自己的模型服务，不会释放宿主机。复跑时应对照[评分依赖版本](experiments/20260905-quality/metadata/evaluator-requirements-frozen.txt)和[原镜像 ID](experiments/20260905-quality/metadata/evaluator-image-id.txt)；即使评分源码 commit 固定，Docker 基础标签和系统包仍可能变化。vLLM 0.21.0 接受 `standard`、不接受 `strict`；目标路径若含 `dflash` 可能触发方法推断误判。

## 工具与证据

| 路径 | 内容 |
|---|---|
| [`experiments/20260906-qwen38/`](experiments/20260906-qwen38/) | 本次实验：逐组记录、数值汇总、证据、执行源码快照、分析程序、验收程序、测试和测试流程图 |
| [`experiments/20260909-drafter-adaptation/`](experiments/20260909-drafter-adaptation/) | draft model 再适配实验：两种漂移边界的逐请求结果、执行脚本快照、来源哈希、分析程序、验收程序和测试 |
| [`experiments/20260905-quality/`](experiments/20260905-quality/) | 上一轮完整答案评测：原始响应、官方评分、逐题对照、分析代码和图 |
| [`images/`](images/) | 本文使用的中文结果图 |
| [`tools/make_readme_figures.py`](tools/make_readme_figures.py) | 从两轮实验的已发布汇总数据重新生成上述中文图，需要中文字体和[固定版本的 Matplotlib](experiments/20260906-qwen38/requirements-figures.txt) |
| [`LICENSE`](LICENSE) | 本目录适用的许可证 |

### 证据与代码

| 入口 | 可核对的内容 |
|---|---|
| [执行程序](experiments/20260906-qwen38/source/campaign_runner.py) | 当时使用的组派发、计时和实验控制代码 |
| [评分接入](experiments/20260906-qwen38/source/scoring.py)、[流式计时](experiments/20260906-qwen38/source/stream_metrics.py) | 官方评分与回答如何绑定，token 如何核对，延迟如何计算 |
| [配置](experiments/20260906-qwen38/evidence/configuration.json)、[请求样例](experiments/20260906-qwen38/evidence/request-examples.json) | 固定参数及两份带哈希的实际请求 |
| [实验记录](experiments/20260906-qwen38/evidence/run.json) | 测试覆盖、加载检查、测量耗时及来源成员哈希 |
| [逐组记录](experiments/20260906-qwen38/data/groups.json)、[数值汇总](experiments/20260906-qwen38/data/summary.json) | 题目 ID、已存评分、计时、计数和配对结果 |
| [分析程序](experiments/20260906-qwen38/analyze_results.py)、[验收程序](experiments/20260906-qwen38/validate_report.py)、[测试](experiments/20260906-qwen38/test_report.py) | 重新汇总数字，检查本文表格、链接、徽章和证据是否一致 |
| [上一轮分析](experiments/20260905-quality/analysis/)、[上一轮结果](experiments/20260905-quality/results/)、[上一轮源码](experiments/20260905-quality/src/) | 2026-09-05 的逐题对照、原始响应、官方评分和分析程序 |
| [再适配结果](experiments/20260909-drafter-adaptation/results/)、[汇总](experiments/20260909-drafter-adaptation/data/summary.json)、[来源清单](experiments/20260909-drafter-adaptation/evidence/provenance.json)、[脚本](experiments/20260909-drafter-adaptation/source/) | 两种边界下逐请求的命中率、接受长度和 vLLM 记录；权重、数据和日志哈希；实际执行的训练与测量脚本 |

这些源码是实际执行版本的归档，不是从零部署 GPU 的完整安装包。**完整原始回答和 SSE 流仍在作者的私有归档中，没有在此重新分发。** 公开文件不含基础设施定位信息或凭据；归档及成员哈希说明来源，但不能独立证明运行行为。

### 官方资料

- [经典推测解码方法](https://proceedings.mlr.press/v202/leviathan23a.html)
- [DFlash 论文](https://arxiv.org/abs/2602.06036)与[项目代码](https://github.com/z-lab/dflash)
- [vLLM 0.28.0](https://github.com/vllm-project/vllm/releases/tag/v0.28.0)
- [EvalPlus](https://github.com/evalplus/evalplus)与 [MATH-500 来源](https://github.com/openai/prm800k#math-splits)
