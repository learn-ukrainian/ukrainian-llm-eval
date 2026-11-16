# Ukrainian LLM Eval

Reproducible evaluation of **Ukrainian language** models and agents, with optional reference tools.

This project is under development toward its first release. It does not yet publish a complete model leaderboard. Track the [results TL;DR, leaderboards and training-data lessons](docs/results.md). Follow the [release epic](https://github.com/learn-ukrainian/ukrainian-llm-eval/issues/1).

The engine supports ZNO/NMT multiple-choice and matching questions, ULP proficiency questions, and UA-GEC correction with separate reference custody, preserved execution evidence, native Claude, Kimi, AGY Gemini, controlled Codex execution, Cursor Agent subscription routes, Gemma through native OpenCode/OpenRouter, plus compatible chat-completions endpoints. UA-GEC scoring uses a separately built offline Docker runtime. Additional native adapters and the public evaluation remain tracked work. No other exam subjects are planned.

The [first-study preparation](docs/first-study.md) records the selected model matrix,
complete source denominators, concrete per-suite limits and remaining launch checks.

## Development installation

Requires Python 3.11 or newer. Validation targets Linux and macOS; Windows behavior is unverified. From a clean clone:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m ukrainian_llm_eval --help
.venv/bin/python -m pytest
.venv/bin/python -m ruff check src tests
.venv/bin/python -m build
```

Provider runs require your own authorized CLI subscription or endpoint credentials. Running tests does not invoke paid models. Do not commit credentials, grading keys, raw provider logs or retrievals.

The [AGY runbook](docs/native-agy.md) covers subscription-only Gemini execution
at low, medium and high effort, with or without Sources MCP, including native
tool restrictions and the unverified effective output-token cap.

The [Cursor runbook](docs/native-cursor.md) covers headless `cursor-agent`
subscription runs (including `cursor-grok-4.6-high`), workspace isolation
limits, and Sources MCP mirroring.

The [Kimi runbook](docs/native-kimi.md) explains private subscription provisioning
and execution with or without Sources MCP. Kimi results identify the requested
subscription route; its CLI does not attest the underlying backend model.
The [OpenCode runbook](docs/native-opencode.md) explains the restricted Gemma/OpenRouter route and its request-level spending gate.

The [Codex runbook](docs/native-codex.md) covers the paired reference-only and legacy closed-book native
routes, local control receipts and private runtime provisioning. Live admission
and backend-model attestation remain separate gates.
The [Responses API runbook](docs/responses-http.md) covers DeepSeek's stateless
HTTP route, including complete tool history and reasoning-inclusive output caps.

## Paid direct API runs (Anthropic, OpenAI)

`api_anthropic` (Messages API) and `api_openai` (Responses API) are closed-book adapters with fixed per-call settings, one synchronous request at a time. Install their SDKs with `pip install -e '.[api]'`. The SDKs read `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` from the environment; this package never reads, prints or stores a key.

A configuration uses the usual `zno-nmt.config.v1` fields with `"adapter": "anthropic-api"` or `"openai-api"`, `"tools": []`, and optional `"temperature"`. Claude Opus 5.5 rejects `temperature`, so leave it out for that model; the null value is recorded. `max_output_tokens` also bounds thinking or reasoning. Example `.runtime/api/opus.json`:

```json
{"schema": "zno-nmt.config.v1", "adapter": "anthropic-api", "model": "claude-opus-5-5", "effort": "high",
 "max_output_tokens": 4096, "timeout_seconds": 300, "max_tool_calls": 20, "repeats": 1,
 "tools": [], "corpus_id": null, "provider": null}
```

Add `--dry-run` to print the request count and estimated USD without a key or a call. A paid run requires `--max-usd` and refuses before any request when the estimate is higher. The estimate is conservative: about one token per two UTF-8 bytes of request, plus the full `max_output_tokens` for every request, at list price. The adapters include list prices for `claude-opus-5-5`, `claude-sonnet-5-5` and `gpt-6.1-sol`, checked 2026-11-16. Other models need `"usd_per_mtok": {"input": …, "output": …}`. One command per provider runs a segmented suite:

```sh
.venv/bin/python -m ukrainian_llm_eval.api_anthropic --config .runtime/api/opus.json \
  --questions .runtime/ulp/questions.json --segment-plan .runtime/ulp/segments.json \
  --out-dir .runtime/api/ulp-opus-r1 --max-usd 30
.venv/bin/python -m ukrainian_llm_eval.api_openai --config .runtime/api/sol.json \
  --questions .runtime/ulp/questions.json --segment-plan .runtime/ulp/segments.json \
  --out-dir .runtime/api/ulp-sol-r1 --max-usd 15
```

Use a segment plan. Anthropic structured output allows at most 16 union-typed answers per request, and a request over that limit is refused before any call. Every request gets a row in `rows/` with the model, settings, token usage and estimated cost, even when its answer fails. Malformed response data fails only its own row: a token count above 2^53 − 1 is invalid usage, and a response that UTF-8 JSON cannot hold (a lone surrogate) is kept losslessly as ASCII-escaped JSON in an `api_response_unencodable` evidence event. Each repeat is reassembled into `runs/repeat-NNN.json` and an evidence attempt in `evidence/`. Score them as usual with `score --run` or `score-gec --run-evidence-dir DIR/evidence --attempt-id repeat-001`. The `run` and `pair` commands also accept these configurations, but only with a per-trial cap, `"max_usd_per_trial"`. Requests are never retried: the SDK retry is off, so a retry cannot create a second paid request.

## Evaluation boundaries

A candidate receives questions, while the separate scorer receives the answer key. Hiding a key does not prove public questions were unseen during training. Live Sources MCP is optional; changing reference content limits exact reproducibility. The evaluator does not host, back up or publish that service.

Scores concern the selected tasks and datasets. They do not certify overall fluency or a CEFR level. Requested effort and verified effective effort are distinct, and unsupported tool access is recorded rather than simulated.

See [benchmark preparation and scoring](docs/benchmarks.md), [running an exam](docs/running.md), [segmented research execution](docs/research-scheduling.md), [the delivery plan](docs/delivery-plan.md), [contributing](CONTRIBUTING.md), [security](SECURITY.md) and [release procedure](docs/releasing.md). Code is MIT licensed; datasets and reference material retain their own rights.
