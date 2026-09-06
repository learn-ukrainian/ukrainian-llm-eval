# Native Codex adapter

`ukrainian_llm_eval.native_codex` is a fail-closed, subscription-authenticated
adapter for native Codex CLI evaluation. It has no login flow, no API-key
fallback, no provider substitution, and no dependency on Learn Ukrainian or
fleet configuration.

Windows is unsupported. The installed npm launcher forms and the invocation
cleanup both rely on POSIX executable and process-group behavior; Windows npm
shims and process groups have no implementation in this adapter.

It currently supports **only `closed-book`**. `sources` is explicitly
unsupported: the existing Codex control captures do not establish an isolated
MCP route, so callers must not label a Sources run as closed-book.

The public `preflight` and `run` commands select this adapter with
`"adapter": "codex"` and read the absolute provisioning directory only from
`UKRAINIAN_LLM_EVAL_CODEX_PROVISIONING_DIR`. The directory is never part of a
serialised route, run identity, or public result. A `sources` request fails
before any native invocation. This wiring exposes the adapter for local
validation; it does not turn the recorded local mock control evidence into an
admission or provider-readiness claim.

The runtime-only `private_env_path` is an absolute, owner-only provisioning
directory with exactly the files the adapter needs:

```text
private-codex/
  auth.json                    # ChatGPT subscription tokens; never committed or logged
  closed-book-control.json     # operator attestation bound to local mock artifacts
```

`auth.json` follows the installed Codex ChatGPT shape: `auth_mode` must be
`"chatgpt"`; `OPENAI_API_KEY` must be null or empty; and `tokens` must contain
nonempty `access_token`, `refresh_token`, `id_token`, and `account_id` strings.
The adapter rejects API-key, personal-access-token, external-header, or any
other auth shape before creating a child process. It parses and canonically
rewrites only that narrow shape into the fresh home, without logging or hashing
credential values.

The adapter copies `auth.json` into a fresh temporary `CODEX_HOME` and invokes
Codex with `--ignore-user-config`, `--ignore-rules`, `--ephemeral`, a fresh
neutral cwd, a packet-derived strict response schema, the exact model, and the
exact requested effort. The schema is exactly
`adapters.response_schema(packet)`, which closes every response object and
requires the packet's opaque question keys (and matching row keys). It does not
mount a normal user home. It requests no project
instructions, skills, hooks, plugins, memories, or resume state, and requires
a mock capture to prove handler effects instead of assuming those flags work.

The control receipt is mandatory and uses `native-codex-control.v2`. It binds
the invoking entrypoint SHA-256, the actual native runtime SHA-256, the CLI
version, the SHA-256 of the local control-probe implementation, and the
generated request-shape hash. That shape includes the exact
requested model and effort, so a receipt captured for one model cannot attest
another model. A direct native executable is supported. For the supported
installed Node launcher, the adapter derives the platform vendor executable
from the known `@openai/codex` package layout and hashes that executable too.
Unknown wrappers and missing vendor binaries fail closed; the adapter never
treats a wrapper hash as the runtime identity.

For every visible descriptor
(`functions.exec`, `functions.wait`, `functions.request_user_input`, and
delegation), it must name a local mock-capture artifact, its SHA-256, and the
same request-shape hash. The adapter checks that each artifact still matches.
The receipt is an **operator attestation** of handler effects, not independent
proof produced by the adapter; its artifact binding prevents a later report
swap. A descriptor is acceptable when the attestation says its hash-bound
capture proved it inert. Any unknown or usable handler makes preflight fail.

The reproducible local probe injects the exact fresh, noninteractive invocation
shape into a loopback-only Responses fixture. The first request still advertises
`functions.exec`, `functions.wait`, and `functions.request_user_input`, so the
evidence is inert behavior rather than surface removal. A custom `exec` call
and a schema-valid function-call `wait` both return the disabled code-mode-host
diagnostic. A schema-valid input request returns unavailable in Default mode.
The injected collaboration spawn call is neither advertised nor accepted.
These results apply only to that exact fresh noninteractive CLI mode. They do
not establish behavior for interactive or Plan-mode hosts. The earlier
permissive code-mode capture did not prove its synthetic file write, so it is
not a positive control for successful tool execution.

## Create a local control receipt

The command below invokes only the installed CLI and a process-local
`127.0.0.1` fixture. It starts each handler case with an empty temporary HOME,
CODEX_HOME, and cwd; passes no auth file or token; disables retries; bounds
request/process output; and terminates a timed-out process group. It creates a
new owner-only capture directory and will not overwrite a prior capture.

Runtime cleanup signals only the original POSIX process group. A child that
creates a separate session is outside that group: the adapter closes its own
nonblocking pipe descriptors and returns a bounded failure, but cannot claim
to terminate that escaped process.

```sh
umask 077
mkdir -p .runtime
.venv/bin/python -m ukrainian_llm_eval.codex_controls \
  --output "$(pwd)/.runtime/codex-control-001" \
  --codex-bin codex \
  --model gpt-5.6-luna \
  --effort medium
```

The command sends four synthetic tool requests: a `functions.exec` custom
envelope, schema-valid `functions.wait` and `functions.request_user_input`
function-call envelopes, and an unadvertised `collaboration.spawn_agent`
custom envelope. It writes immutable per-handler captures, `report.json`, and
`closed-book-control.json`. It refuses to write the receipt unless the first
request advertises the expected Functions tools, excludes delegation, and each
second request contains the expected inert result. Failure captures and the
report remain available for diagnosis, but no receipt is created.

The capture directory must remain in place: the receipt binds each external
capture by absolute path and SHA-256. It is deliberately outside the native
provisioning directory, whose exact two-file shape remains:

```sh
mkdir -m 700 /absolute/private-codex
# Place a subscription-only auth.json here through the operator's secure
# credential workflow. Do not copy a live Codex home or API-key credentials.
install -m 600 .runtime/codex-control-001/closed-book-control.json \
  /absolute/private-codex/closed-book-control.json
export UKRAINIAN_LLM_EVAL_CODEX_PROVISIONING_DIR=/absolute/private-codex
```

With a separately supplied owner-only `auth.json`, public commands use that
runtime directory without serialising its path or credential content:

```json
{
  "schema": "zno-nmt.config.v1",
  "adapter": "codex",
  "provider": "managed:codex-subscription",
  "model": "gpt-5.6-luna",
  "effort": "medium",
  "timeout_seconds": 600,
  "max_output_tokens": 32768,
  "max_tool_calls": 1,
  "repeats": 1,
  "tools": [],
  "corpus_id": null
}
```

Save this as `.runtime/zno-nmt-demo/codex-config.json`; its model and effort
must exactly match the receipt capture. `max_output_tokens` remains recorded
configuration metadata, not a proven native output limit.

```sh
.venv/bin/python -m ukrainian_llm_eval preflight \
  --config .runtime/zno-nmt-demo/codex-config.json \
  --condition closed-book
.venv/bin/python -m ukrainian_llm_eval run \
  --questions .runtime/zno-nmt-demo/questions.json \
  --config .runtime/zno-nmt-demo/codex-config.json \
  --condition closed-book \
  --output .runtime/zno-nmt-demo/codex-closed-book.json
```

This capture is scoped local control evidence. It does not attest a backend
model, accepted/effective effort, native output limit, provider result, or
behavior in interactive and Plan-mode hosts.

An operator control receipt must bind those exact local captures before a real
closed-book canary. A real canary must then verify the semantic answer, fresh
session, zero emitted tool events, and ambient-instruction resistance. The
installed JSONL protocol does not emit backend model or accepted/effective
effort, so these remain unknown.

```python
from ukrainian_llm_eval import native_codex

native_codex.preflight_codex(config, "closed-book", private_env_path="/private/codex")
trial = native_codex.run_codex(
    packet, config, "closed-book", sources_url=None, prompt=prompt,
    evidence=store_private_raw_event, private_env_path="/private/codex",
)
```

`requested_effort`, `accepted_effort`, and `effective_effort` remain separate.
The installed JSONL protocol currently exposes only the requested route, the
fresh session id, and input/output token counters; backend model and effort are
recorded as `"unknown"`. Native CLI backends may map an `ultra` request to
`xhigh`; the adapter never fabricates that result from the request. Raw CLI
JSONL is passed unchanged to the evidence callback. Missing session/usage, tool
events, malformed structured output, or timeout are strict failures.

Each invocation event and run identity retain the validated control receipt
SHA-256 plus entrypoint and native-runtime SHA-256 values, using the same
canonical digest as preflight. This lets the caller compare the evidence used
for preflight and execution. Invocation evidence and every envelope-verified
run also retain `response_schema_sha256` for the exact packet-derived schema
passed to `--output-schema`. If only the answer payload is malformed, the
adapter records a failed `candidate_response_error` with an all-null response
map while retaining its verified fresh session, zero tool calls, usage, and
identity; it does not repair the answer or discard that independent repeat.
Preflight has no packet, so its settings and request-shape hashes deliberately
describe only configuration-level controls and do not claim a response-schema
hash.

`max_output_tokens` is retained as configuration metadata but is currently
**not forwarded as a native output limit**. Its effective value is unknown;
this prototype cannot establish a hard token bound for benchmark admission.
