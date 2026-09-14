# Qira live benchmark console

[English](README.md) | [中文](README-CN.md)

A browser console that runs the model comparison **live** and plots the result
while it happens, instead of handing the room another long report.

The written studies next to this folder are the record: they are exact,
reproducible and hash-verified. They are also 1,000+ lines each, and nobody
reads them in a workshop. This console drives the *same* measurement code
against the *same* deployments, but a stakeholder can pick two models, click
once, and watch time-to-first-token, decode speed and cost appear as bars.

Related: [scenario benchmark](../qira-scenario-model-benchmark/README.md) ·
[router validation](../qira-model-router-validation/README.md) ·
[throughput recalibration](../qira-followup-throughput-recalibration/README.md) ·
[production readiness](../qira-production-readiness/README.md)

---

## 1. What it measures

Per arm (a deployment, optionally pinned to a reasoning effort):

| Metric | Meaning |
|---|---|
| TTFT p50 / p90 | Time to first token: how long the user waits before anything appears |
| TPOT p50 / p90 | Milliseconds per output token once text is flowing |
| tok/s p50 / p90 | Decode rate, the same measurement expressed as speed |
| E2E p50 / p90 | Whole-request latency |
| Prompt / reasoning / emitted tokens | Mean token composition per request |
| USD per 1,000 requests | List-price estimate from the tokens this run actually consumed |
| USD per 1,000 emitted tokens | Cost of a unit of *useful* output, so a verbose model cannot look cheap |
| Errors, truncations, bursts | The things an average would otherwise hide |
| Served model split | For Model Router arms: which underlying model actually answered |

Per run, a totals strip reports what the session itself consumed and cost:
prompt tokens sent, output tokens received, how many of those were reasoning
tokens the user never sees, total tokens, and the list-price cost of that one
session. If any arm in the run has no confirmed price the figure is marked
partial rather than quietly under-reported.

Six charts: TTFT, decode speed, cost per 1,000 requests, cost against latency
(the bargain-corner scatter), token composition, and router model split.

### The models and scenarios, matched to the report

The console offers the same arms and prompts the written scenario study used,
so a live number can be put next to a printed one:

- **Three candidate models**, flagged *in the report*: `gpt-4o-mini-bench`,
  `gpt-5-mini`, `gpt-5.6-luna`. Every other deployment in the registry is
  shown separately as a baseline, a router, or a deployment kept for one of
  the other studies.
- **Every reasoning effort is its own arm.** Effort is picked with chips, not
  a dropdown, because a reasoning model is not one arm but one arm per effort
  and comparing those permutations is the point. The **README matrix** preset
  selects exactly the study's shape: 11 model×effort arms across every Qira
  prompt.
- **Six Qira product scenarios**, 17 prompts, shown under their report names:
  Next Move, Write For Me, Catch Me Up, Pay Attention, Live Interaction,
  Creator Zone. The last three carry a *text proxy* marker: speech-to-text,
  the realtime voice path and image generation itself were out of scope, and
  a text score is not a voice-latency result.
- **Each prompt keeps its own answer budget** (80–900 tokens). The request cap
  is that budget plus a uniform reasoning headroom (8192 by default), which is
  exactly how the study runs it: a reasoning model gets room to think without
  being truncated, and every arm answers the same prompt under the same rules.

Two prompts are missing by design — see §4.

### Run history

Every completed run is written to `history/` as one self-contained JSON file
and appears in the **Past runs** list, newest first, alongside the recorded
study runs. Opening one restores its charts, table and totals exactly as they
were. Runs are kept separate rather than merged, so "what did we measure last
Tuesday" and "what is this doing right now" are both answerable, and any past
run can still be exported to CSV or deleted.

`history/` is git-ignored: those runs belong to whoever operated the console,
not to the repository. The recorded study runs ship in `replay/replay_pack.json`.

When the console drives the same-region runner, a run executes in Sweden
Central while the history the room reads lives on the portal VM. Capturing it
does not depend on anyone watching: submitting a run starts a background worker
that polls the runner until the record is mirrored locally, so closing the tab,
losing Wi-Fi or starting the run from the API all still land in **Past runs**.
A lost browser does not stop the measurement either — the run belongs to the
room, and **Stop** is how you end one. Listing history also backfills any runner
run the portal never saw, which recovers the history a portal restart would
otherwise have missed, and it is best effort: an unreachable runner never stops
the local history from being served. Mirroring is keyed on the run id, so these
paths can race without duplicating a row, and deleting a run leaves a marker so
the next sync does not bring it back.

### What it deliberately does not do

- **No web search and no tools.** Every request carries a system message and
  the prompt, nothing else. This is native model capability only, which is the
  condition the whole study set is built on. Adding a search tool would make
  the models incomparable.
- **No quality scoring.** Quality needs a blind judge over many answers; that
  lives in the written studies. This console measures speed, tokens and cost.
- **No invoice.** Cost is a list-price estimate computed from measured tokens.

---

## 2. Quick start

### On the benchmark VM (the only way to get trustworthy latency)

```bash
git clone https://github.com/david-xinyuwei/david-share.git
cd david-share/Agents/Model-And-Router-Benchmark/qira-live-benchmark-console
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env      # fill in your endpoint; leave the key empty to use Entra ID
./run_on_vm.sh 8080
```

Then, from your laptop, forward the port instead of opening one:

```bash
ssh -L 8080:127.0.0.1:8080 <user>@<vm-ip>
```

and open <http://localhost:8080/>.

> Run the console on a VM **in the same region as the deployments**. Driving it
> from a laptop measures your office wifi and your distance from the region,
> and reports both as model latency. A first call from another continent in
> testing showed a 23-second TTFT for an answer the VM returns in about one.

### Registered portal deployment

The delivered deployment separates presentation from measurement:

```text
browser
  -> Linux Work VM nginx /qira-benchmark/ (Basic Auth)
  -> portal service 127.0.0.1:8513
  -> reverse-tunnel listener 127.0.0.1:8514
       <- encrypted SSH initiated by the Sweden Central runner
  -> Sweden Central runner 127.0.0.1:8513
  -> Sweden Central Azure OpenAI resource
```

The portal and its durable history live on the Linux Work VM, where it is
registered alongside the other demo applications. The request-timing process
lives on the Sweden Central benchmark VM, so moving the UI to East Asia does
not turn geography into model latency. No runner port is opened to the
internet: the runner initiates a reverse SSH tunnel to the Work VM's existing
SSH endpoint. Both applications and the forwarded port listen on loopback
only. The SSH server host key is pinned from the Azure control plane, and the
dedicated tunnel account is restricted to listening on `127.0.0.1:8514`. The
nginx route reuses the portal's existing Basic Auth.

Ports are deliberately unique on the Work VM: `8513` for the portal and
`8514` for the reverse tunnel. See `deploy/` for the portal, runner and
reverse-tunnel systemd units plus the nginx location. Both services use
`StateDirectory=qira-benchmark`; no writable path under `/opt` is required.
State-changing endpoints require JSON from the same origin, and the runner
accepts at most two active paid runs at once.

### In-page sign-in instead of the browser popup

`deploy/portal-gate/` turns the Demo Portal's Basic-Auth dialog into a themed
sign-in page. nginx still enforces access, but through `auth_request` against a
small local service on `127.0.0.1:8515`:

```text
anonymous request -> nginx auth_request -> 401 -> 302 /portal-login (themed form)
signed in         -> nginx auth_request -> 204 -> the portal or the console
```

The gate verifies credentials against the same htpasswd files nginx used, so
existing portal passwords keep working; Apache `apr1` hashes are checked in
pure Python and bcrypt entries are delegated to the `htpasswd` binary with the
password supplied on stdin, never as a command-line argument that `/proc`
would expose. A successful sign-in issues an HMAC-SHA256 session cookie that is
`HttpOnly`, `SameSite=Lax` and time limited, and one sign-in covers the portal
home page and every proxied application. Redirect targets are restricted by an
anchored allowlist of URL characters, and failed sign-ins are held to a fixed
deadline so they do not reveal whether a user name exists.
`nginx-default.deployed.conf` records the configuration currently running on
the Work VM; the configuration it replaced is kept on the VM at
`/etc/nginx/backups/default.pre-portal-gate`.

Copy the three `deploy/*.env.example` files to the paths named by the systemd
units. In particular, `portal.env` must list the exact HTTP and/or HTTPS
browser origins in `QIRA_ALLOWED_ORIGIN`; the server never trusts the request's
`Host` header to establish an allowed origin.

`deploy/demo-portal-card.html` is the canonical card for the Linux Work VM
Demo Portal. Keep it as the first card in the portal grid and increment the
portal's active-service count when registering a fresh installation.

### On a laptop, with no credentials

Start it anyway:

```bash
python server.py --port 8080
```

With no `AZURE_OPENAI_ENDPOINT` the console starts in **replay mode**: the run
button is disabled, and `replay/replay_pack.json` feeds the same charts from
the recorded study runs. Useful for rehearsing, and for the moment the
conference-room wifi dies. Replayed views are labelled as such on screen.
The pack embeds its own model/scenario catalog and is deliberately not in Git
LFS, so replay mode also works in a clone made without `git lfs pull`.

---

## 3. How a run is built

1. **Pick arms.** Any mix of direct deployments and Model Router deployments.
   Reasoning models expose their probed `supported_efforts`; an unsupported
   value is rejected before any request is sent, not discovered as a 400.
2. **Pick prompts.** The Qira scenario prompts, grouped by surface
   (notification management, catch me up, writing, and so on), or the router
   study's difficulty-tiered prompts.
3. **Warm-up.** By default one pass per prompt per arm is measured and
   discarded. The first call to a cold deployment carries connection setup,
   Entra token acquisition and scheduler placement that no steady-state user
   ever pays.
4. **Sequencing.** Arms run one after another, never simultaneously. Two arms
   on the same resource would compete for the same tokens-per-minute quota,
   and neither number would be a property of the model. Within an arm,
   concurrency drives several prompts at once.
5. **API surface.** Model Router deployments only work on Chat Completions and
   only expose their routing trace there. If a router is selected, **every**
   arm in that run moves to Chat Completions, so an API-surface difference is
   never mistaken for router overhead.
6. **Same region, by construction.** The console talks to exactly one
   `AZURE_OPENAI_ENDPOINT`, and an Azure OpenAI resource is regional, so every
   arm is served from one region. The region each deployment was verified in
   is shown under the model list and recorded in every saved run. Note that
   GlobalStandard and DataZoneStandard SKUs may serve from a wider geography
   than the resource's own region; each deployment's SKU is shown next to it.

A single run is capped at 400 requests. The estimate under the form shows the
exact count before you start.

---

## 4. The honesty rules built into the charts

These exist because the obvious implementation of each one is misleading.

- **A burst is not a decode rate.** If an answer's first and last text chunk
  arrive less than 50 ms apart, it did not stream — it was delivered whole.
  Dividing tokens by that window produces numbers like 10,000 tok/s. The
  follow-up study established this threshold and showed the effect is an
  artefact of the delivery path. Burst records are therefore excluded from
  TPOT and decode-rate statistics, counted in their own `Burst` column, and
  named under the decode chart. An arm whose answers *all* burst reports no
  decode rate at all rather than a flattering one.
- **A failed call has no latency.** Errors are counted and sampled, never
  averaged into TTFT.
- **A stream without usage is not a success.** A response that completes but
  reports no tokens cannot be billed or compared, so it is not counted as OK.
- **An unpriced model shows no cost.** Rather than guessing, arms without a
  confirmed list price are blank in the cost chart.
- **Cost follows the served model.** For a router arm, cost is computed from
  whichever model actually answered, not from the router deployment name.
- **Two prompts are withheld.** `PA01` and `PA03` reproduced an internal
  meeting transcript and are redacted in the public copy of the datasets. The
  console does not offer them, because sending a redaction marker to a model
  would measure nothing.

---

## 5. Where the numbers come from

The console contains **no request-timing code of its own**. `bench_core.py`
loads `harness.py` from the sibling study folder and calls `run_one()` — the
same function, the same streaming phase split, the same `max_retries=0`, the
same usage requirement that produced the written reports. Pricing and the
model registry are read from the same `config/` files.

The consequence is the point: a number produced live on a projector and a
number printed in the reports mean the same thing. The replay pack is built by
re-aggregating the studies' own raw `.metrics.jsonl` through the same
`summarize_arm()` the live path uses, so replayed and live charts are directly
comparable too.

---

## 6. Limits

- Latency is client-observed and includes the network. Same-region VM,
  always.
- Concurrency here is a closed batch, not a steady-state load generator. For
  sustained-throughput questions use the production-readiness study, which was
  built for exactly that.
- A short live run is a small sample. Three iterations over three prompts is a
  demonstration, not a procurement decision; the written studies carry the
  sample sizes that support the recommendations.
- Cost is list price, excluding any Model Router request fee.

---

## 7. Files

| Path | Purpose |
|---|---|
| `server.py` | Standard-library HTTP server, SSE event stream, CSV export |
| `bench_core.py` | Loads the study harness; planning, execution, aggregation |
| `static/` | Responsive warm-light UI with dark-mode adaptation; dependency-free HTML/CSS/JS and SVG charts |
| `deploy/demo-portal-card.html` | Canonical first-card registration for the Linux Work VM Demo Portal |
| `deploy/portal-gate/` | In-page sign-in gate: service, systemd unit, and nginx `auth_request` wiring |
| `scripts/build_replay_pack.py` | Rebuilds `replay/replay_pack.json`; `--check` verifies it |
| `replay/replay_pack.json` | Recorded study runs for credential-free demonstration |
| `history/` | One JSON per completed run; git-ignored, created on first run |
| `tests/test_console.py` | Offline tests: statistics, planning, execution, history, HTTP surface |
| `run_on_vm.sh` | Starts the console on the benchmark VM, bound to localhost |

Run the tests with:

```bash
python -m unittest discover -s tests
```

They need no network and no credentials.
