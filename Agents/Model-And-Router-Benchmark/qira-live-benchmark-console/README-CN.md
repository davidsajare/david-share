# Qira 实时基准测试控制台

[English](README.md) | [中文](README-CN.md)

一个浏览器控制台：**当场**跑模型对比，边跑边出图，而不是再给客户一份长报告。

本目录旁边的几份书面研究才是正式记录——它们精确、可复现、带哈希校验，但每份都有
上千行，工作坊现场没人会读。这个控制台调用**同一套**测量代码、打到**同样的**部署上，
但业务方可以自己勾两个模型、点一次，然后看着首 token 时延、解码速度和成本变成柱状图。

相关：[场景基准](../qira-scenario-model-benchmark/README-CN.md) ·
[Router 验证](../qira-model-router-validation/README-CN.md) ·
[吞吐校准](../qira-followup-throughput-recalibration/README-CN.md) ·
[生产就绪](../qira-production-readiness/README-CN.md)

---

## 1. 测什么

每个实验组（一个部署，可选固定某档 reasoning effort）：

| 指标 | 含义 |
|---|---|
| TTFT p50 / p90 | 首 token 时延：用户在看到任何内容之前要等多久 |
| TPOT p50 / p90 | 开始出字之后，每个输出 token 的毫秒数 |
| tok/s p50 / p90 | 解码速率，同一测量换成速度表达 |
| E2E p50 / p90 | 整个请求的端到端时延 |
| 输入 / 推理 / 可见输出 token | 每次请求的平均 token 构成 |
| 每 1,000 次请求美元 | 按本轮实际消耗的 token，用标价估算 |
| 每 1,000 可见输出 token 美元 | 一单位**有效**输出的成本，避免话多的模型显得便宜 |
| 错误、截断、整段到达 | 平均值会掩盖掉的那些情况 |
| 实际服务模型占比 | Model Router 组：到底是哪个底层模型回答的 |

每次运行还有一条总计条，报告这次会话本身消耗了什么、花了多少钱：发出去的输入
token、收回来的输出 token、其中有多少是用户根本看不到的推理 token、总 token 数，
以及这一次会话的标价成本。如果本轮有任何一个实验组没有确认价格，该数字会标注为
partial，而不是悄悄少算。

六张图：TTFT、解码速度、每千次请求成本、成本对时延（“性价比角落”散点图）、
token 构成、Router 模型分流。

### 模型与场景，与报告一一对应

控制台提供的实验组和题目，与书面场景研究所用的完全一致，因此现场跑出来的数字可以
直接和报告里印的放在一起看：

- **三个候选模型**，标注为 *in the report*：`gpt-4o-mini-bench`、`gpt-5-mini`、
  `gpt-5.6-luna`。注册表里其他部署单独分组显示，它们是基线、Router，或为其他研究
  保留的部署。
- **每一档 reasoning effort 各算一个实验组。** effort 用小标签（chip）多选而不是
  下拉单选，因为推理模型不是一个实验组，而是每档 effort 一个实验组，比较这些排列
  组合正是重点。**README matrix** 预设会精确选出研究的形状：11 个 模型×effort
  实验组 × 全部 Qira 题目。
- **6 个 Qira 产品场景**、17 道题，按报告里的名称显示：Next Move、Write For Me、
  Catch Me Up、Pay Attention、Live Interaction、Creator Zone。后三个带 *text proxy*
  标记：语音转文字、实时语音链路和图像生成本身不在范围内，文本得分不等于语音时延
  结论。
- **每道题保留自己的答案预算**（80–900 token）。请求上限 = 该题预算 **+** 统一的
  推理余量（默认 8192），这正是研究的跑法：推理模型有思考空间而不会被截断，同时
  所有实验组在同一规则下回答同一道题。

有两道题按设计缺失——见第 4 节。

### 历史运行记录

每次跑完的运行都会以一个自包含 JSON 文件写入 `history/`，并出现在 **Past runs**
列表里（最新在前），和已记录的研究运行并列。打开任意一条就能原样恢复它的图表、
表格和总计。各次运行**分开保存**而不是合并，因此“上周二我们测了什么”和“现在这个
正在跑什么”都能回答；任何一条历史运行仍可导出 CSV 或删除。

`history/` 已加入 git 忽略：这些运行属于操作控制台的人，不属于仓库。随仓库交付的
已记录研究运行在 `replay/replay_pack.json` 里。

当控制台驱动同区域 runner 时，运行在 Sweden Central 执行，而现场要看的历史记录存
在 Portal VM 上。回写不依赖有没有人盯着页面：提交运行时会启动一个后台 worker，轮询
runner 直到记录被镜像到本地——因此关掉标签页、Wi-Fi 断线、或直接用 API 发起，结果
都仍会出现在 **Past runs** 里。浏览器断开也不会中止测量：这次运行属于整个会场，要
结束它请用 **Stop**。读取历史列表时还会补齐 Portal 从未见过的 runner 运行，这样
Portal 重启期间本会丢失的记录也能找回；补齐是尽力而为的，runner 不可达时也不会
妨碍本地历史的读取。镜像以 run id 为键，因此这几条路径同时命中也不会产生重复条目；
删除某次运行会留下标记，下次同步不会把它带回来。

### 刻意不做的事

- **不带联网搜索、不带任何工具。** 每次请求只有一条系统消息和题目本身。这是纯模型
  原生能力，也是整套研究成立的前提。挂上搜索工具，几个模型之间就没有可比性了。
- **不做质量打分。** 质量需要盲评法官跑足够多的答案，那部分在书面研究里。这个控制台
  测的是速度、token 和成本。
- **不是账单。** 成本是用实测 token 按标价估算出来的。

---

## 2. 快速开始

### 在压测 VM 上跑（唯一能得到可信时延的方式）

```bash
git clone https://github.com/david-xinyuwei/david-share.git
cd david-share/Agents/Model-And-Router-Benchmark/qira-live-benchmark-console
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env      # 填入 endpoint；key 留空即使用 Entra ID
./run_on_vm.sh 8080
```

然后从笔记本做端口转发，而不是开放入站端口：

```bash
ssh -L 8080:127.0.0.1:8080 <user>@<vm-ip>
```

再打开 <http://localhost:8080/>。

> 控制台必须跑在**与部署同区域**的 VM 上。从笔记本发起，测到的是办公室 WiFi 和你
> 到该区域的距离，却会被当成模型时延报出来。测试中从另一个大洲发起的首次调用，
> TTFT 是 23 秒；同一个回答在 VM 上约 1 秒。

### 注册到门户的部署方式

交付部署把“展示”和“测量”分开：

```text
浏览器
  -> Linux Work VM nginx /qira-benchmark/（Basic Auth）
  -> Portal 服务 127.0.0.1:8513
  -> 反向隧道监听 127.0.0.1:8514
       <- 由 Sweden Central Runner 主动建立加密 SSH
  -> Sweden Central Runner 127.0.0.1:8513
  -> Sweden Central Azure OpenAI 资源
```

Portal 与持久化历史记录位于 Linux Work VM，并和其他 Demo 应用一起注册；请求计时进程
则位于 Sweden Central 压测 VM。因此把 UI 放到 East Asia 不会把地理距离混进模型时延。
Runner 不开放任何公网应用端口：它主动连接 Work VM 已有的 SSH 入口，建立反向隧道。
两个应用进程和映射端口都只监听 loopback。SSH 服务端主机密钥通过 Azure 控制平面固定，
专用隧道账号只能监听 `127.0.0.1:8514`。nginx 路径沿用 Portal 现有的 Basic Auth。

Work VM 上端口明确错开：`8513` 是 Portal，`8514` 是反向隧道。Portal、Runner、
反向隧道三个 systemd 单元和 nginx location 均在 `deploy/`。两个服务均使用
`StateDirectory=qira-benchmark`，不需要写 `/opt`。变更接口只接受同源 JSON，Runner
同时最多接受两个付费运行。

### 页面内登录，取代浏览器弹窗

`deploy/portal-gate/` 把 Demo Portal 的 Basic-Auth 弹窗换成与界面同风格的登录页。
访问控制仍由 nginx 强制执行，只是改用 `auth_request` 询问本机 `127.0.0.1:8515`
上的小服务：

```text
未登录请求 -> nginx auth_request -> 401 -> 302 跳转 /portal-login（登录页）
已登录     -> nginx auth_request -> 204 -> 正常进入 Portal 或控制台
```

网关校验的是 nginx 原来使用的同一批 htpasswd 文件，因此现有 Portal 密码继续有效：
Apache `apr1` 哈希用纯 Python 校验，bcrypt 交给系统 `htpasswd` 程序，并通过标准输入
传递密码，绝不作为命令行参数（否则会经 `/proc` 泄露给本机其他账号）。登录成功后
签发 HMAC-SHA256 会话 Cookie，带 `HttpOnly`、`SameSite=Lax` 并有有效期；一次登录
即可覆盖 Portal 首页和所有反向代理的应用。跳转目标由 URL 字符白名单正则限制；登录
失败统一等待到固定截止时间，避免泄露用户名是否存在。
`nginx-default.deployed.conf` 记录了 Work VM 上当前实际运行的配置，被替换的旧配置
保留在 VM 的 `/etc/nginx/backups/default.pre-portal-gate`。

把三个 `deploy/*.env.example` 复制到 systemd 单元指定的路径。尤其是 `portal.env`
必须在 `QIRA_ALLOWED_ORIGIN` 中明确列出实际使用的 HTTP 和/或 HTTPS 浏览器 Origin；
服务端绝不根据请求的 `Host` header 推导允许来源。

`deploy/demo-portal-card.html` 是 Linux Work VM Demo Portal 的标准卡片。新安装时应将
其放在 Portal 网格第一位，并同步增加 active-service 计数。

### 在笔记本上、没有凭据时

照样可以启动：

```bash
python server.py --port 8080
```

没有 `AZURE_OPENAI_ENDPOINT` 时，控制台进入**回放模式**：运行按钮禁用，
`replay/replay_pack.json` 用已记录的研究数据喂同样的图表。适合彩排，也适合
会议室 WiFi 掉线的那一刻。回放视图在界面上有明确标注。
回放包内嵌自己的模型/场景目录，并且明确不进入 Git LFS，因此没有执行
`git lfs pull` 的 clone 同样可以使用回放模式。

---

## 3. 一次运行是怎么组织的

1. **选实验组。** 直连部署和 Model Router 部署可以任意组合。推理模型会列出探测过的
   `supported_efforts`；不支持的取值在发出任何请求之前就被拒绝，而不是等着收 400。
2. **选题目。** Qira 场景题（按通知管理、追进度、写作等场景分组），或 Router 研究的
   难度分级题。
3. **预热。** 默认每个实验组对每道题先跑一次并丢弃。冷部署的第一次调用包含建连、
   Entra 取 token 和调度落位，这些开销稳态用户根本不会付。
4. **排队方式。** 实验组之间严格串行，绝不同时跑。同一资源上的两个组会争抢同一份
   TPM 配额，那样两边的数字都不再是模型自身的属性。组内部则按并发数同时发多道题。
5. **API 路径。** Model Router 部署只能走 Chat Completions，也只有在那里才吐路由
   trace。因此只要选了 Router，本轮**所有**实验组都切到 Chat Completions，避免把
   API 路径差异误读成 Router 开销。
6. **同区域是结构性保证。** 控制台只连一个 `AZURE_OPENAI_ENDPOINT`，而 Azure
   OpenAI 资源是区域性的，所以所有实验组都由同一个区域提供服务。每个部署被验证时
   所在的区域显示在模型列表下方，并记录进每一条保存的运行里。注意 GlobalStandard
   与 DataZoneStandard SKU 的服务地理范围可能大于资源自身区域；每个部署的 SKU 就
   标在它旁边。

单次运行上限 400 个请求。表单下方的预估会在开始前给出准确数量。

---

## 4. 图表里内建的几条“诚实规则”

这些规则之所以存在，是因为每一条的“想当然实现”都会误导人。

- **整段到达不是解码速率。** 如果一个回答的首个和最后一个文本块间隔不到 50 ms，
  它根本没有流式输出，而是整段一次交付。用 token 除以这个窗口会得到一万 tok/s 这种
  数字。跟进研究确立了这个阈值，并证明该现象是交付路径造成的假象。因此整段到达的
  记录被排除在 TPOT 和解码速率统计之外，单独计入 `Burst` 列，并在解码图下方点名。
  某个组如果**全部**都是整段到达，就干脆不报解码速率，而不是报一个好看的假值。
- **失败的调用没有时延。** 错误会被计数并抽样展示，但绝不平均进 TTFT。
- **没有 usage 的流不算成功。** 状态完成却没报 token 的回答无法计费、无法比较，
  因此不计入 OK。
- **没有确认价格的模型不显示成本。** 与其猜，不如在成本图上留空。
- **成本按实际服务模型算。** Router 组的成本按真正回答的那个模型计，而不是按 Router
  部署名。
- **两道题被撤下。** `PA01` 与 `PA03` 复刻了一段内部会议记录，在公开版数据集中已做
  脱敏。控制台不提供这两题——把脱敏占位符发给模型什么也测不出来。

---

## 5. 数字从哪来

控制台**自己不含任何请求计时代码**。`bench_core.py` 从同级研究目录加载
`harness.py` 并调用 `run_one()`——同一个函数、同一套流式阶段拆分、同样的
`max_retries=0`、同样“必须收到 usage”的要求，正是产出书面报告的那段代码。
价格与模型注册表也读同一批 `config/` 文件。

这样做的意义正在于此：投影仪上当场跑出的数字，和报告里印着的数字，是同一个含义。
回放包也是把研究自己的原始 `.metrics.jsonl` 用实时路径同一个 `summarize_arm()`
重新聚合出来的，所以回放图和实时图之间同样可直接比较。

---

## 6. 局限

- 时延是客户端观测值，包含网络。所以必须同区域 VM。
- 这里的并发是闭合批次，不是稳态压力发生器。稳态吞吐的问题请看生产就绪研究，
  那份研究就是为此而做的。
- 一次短运行是小样本。三道题跑三轮是演示，不是采购决策；支撑结论的样本量在书面
  研究里。
- 成本是标价，且不含 Model Router 自身可能的请求费用。

---

## 7. 文件

| 路径 | 用途 |
|---|---|
| `server.py` | 纯标准库 HTTP 服务、SSE 事件流、CSV 导出 |
| `bench_core.py` | 加载研究 harness；计划、执行、聚合 |
| `static/` | 响应式暖色浅色界面并自动适配深色；零依赖 HTML/CSS/JS 与 SVG 图表 |
| `deploy/demo-portal-card.html` | Linux Work VM Demo Portal 第一张卡片的标准注册片段 |
| `deploy/portal-gate/` | 页面内登录网关：服务、systemd 单元与 nginx `auth_request` 配置 |
| `scripts/build_replay_pack.py` | 重建 `replay/replay_pack.json`；`--check` 做校验 |
| `replay/replay_pack.json` | 已记录的研究运行，用于无凭据演示 |
| `history/` | 每次跑完的运行一个 JSON；已 git 忽略，首次运行时自动创建 |
| `tests/test_console.py` | 离线测试：统计、计划、执行、历史记录、HTTP 接口 |
| `run_on_vm.sh` | 在压测 VM 上启动控制台，只绑定 localhost |

运行测试：

```bash
python -m unittest discover -s tests
```

不需要网络，也不需要凭据。
