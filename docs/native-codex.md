# Native Codex adapter

`ukrainian_llm_eval.native_codex` is a fail-closed, subscription-authenticated
adapter for native Codex CLI evaluation. It has no login flow, no API-key
fallback, no provider substitution, and no dependency on Learn Ukrainian or
fleet configuration.

Windows is unsupported. The installed npm launcher forms and the invocation
cleanup both rely on POSIX executable and process-group behavior; Windows npm
shims and process groups have no implementation in this adapter.

The explicit `"codex_tool_policy": "reference-only"` configuration supports
both `closed-book` and controlled `sources` with the same restricted model
catalog. The legacy configuration remains closed-book only. Both require
current, hash-bound local control evidence; neither implies live admission.

The public `preflight` and `run` commands select this adapter with
`"adapter": "codex"` and read the absolute provisioning directory only from
`UKRAINIAN_LLM_EVAL_CODEX_PROVISIONING_DIR`. The directory is never part of a
serialised route, run identity, or public result. Legacy `sources` requests fail
before any native invocation. Local mock controls establish execution boundaries;
live subscription readiness and scored-study admission remain separate gates.

## Reference-only paired policy

Use this policy for the GPT-6 Astra paired evaluation. Native model metadata can
select tools independently of feature-disable flags. The adapter reads the
installed binary's **bundled, offline catalog**, selects the exact requested
model, and changes only five tool fields: `tool_mode`, `multi_agent_version`,
`apply_patch_tool_type`, `experimental_supported_tools` and
`supports_search_tool`. Model aliases, instructions, supported efforts, context
limits and all other fields stay unchanged. The original entry, restricted
entry and restriction policy are hashed into the evidence. Code Mode and its
host remain disabled; asynchronous input, delegation, patching and tool search
are absent from the verified surface.

Closed-book advertises no tools. Sources advertises the configured reference
tools plus three native resource helpers. The controller **rejects all resource
methods without forwarding them**. It also rejects unlisted tools and call-cap
overflow. Permitted calls consume the controller's budget before network I/O,
including calls that fail. The model cannot reset that counter. Tool schemas,
including descriptions and annotations, are pinned in the control receipt and
checked before inference and again when the required MCP server initializes.
Missing tools, changed schemas and changed server identity fail closed.

Example private runtime configuration:

```json
{
  "schema": "zno-nmt.config.v1",
  "adapter": "codex",
  "model": "gpt-6-astra",
  "effort": "low",
  "codex_tool_policy": "reference-only",
  "tools": ["verify_word", "verify_stress"],
  "corpus_id": "live-sources",
  "timeout_seconds": 120,
  "max_output_tokens": 4096,
  "max_tool_calls": 20,
  "repeats": 3
}
```

After installing the package in the environment that will run the evaluator,
set `SOURCES_URL` through the operator's private endpoint workflow, then run:

```sh
.venv/bin/python -m ukrainian_llm_eval.codex_reference_controls \
  --config .runtime/codex-reference-config.json \
  --sources-url-env SOURCES_URL \
  --output .runtime/codex-reference-controls-001
```

This reads live tool/server metadata, then runs nine credential-free local
cases against synthetic Responses and MCP fixtures: empty closed-book surface,
permitted reference call, denied resource listing/templates/read, call-cap
overflow, an unlisted tool, schema drift and a missing tool. The last two must
fail before any model request reaches the fixture. Unknown tool surfaces stop
tool injection. No provider inference or scored exam is used. Capture controls
separately for each exact model/effort/tool-list/cap configuration. The capture
command supports caps up to 100 to keep deterministic probes bounded.
Successful control cases also emit a progress message before the final message
and verify that the CLI's explicit final-output file contains only the latter.

A passing capture produces `reference-control.json`. Keep its immutable capture
files at their recorded locations, and provision exactly these owner-only files:

```text
private-codex-reference/
  auth.json
  reference-control.json
```

Use the same sanitized subscription-auth shape described below. Set
`UKRAINIAN_LLM_EVAL_CODEX_PROVISIONING_DIR` to this directory. Use the same JSON
configuration in both conditions; omit the Sources URL entirely for closed-book.
The existing public preflight/run commands dispatch the new policy. Never copy
a legacy receipt into this provisioning directory or relabel a failed capture.
Implementation, native binary, catalog entry, bridge entrypoint, model, effort,
allowlist, cap and artifact drift invalidate the receipt.

Native MCP events must reconcile with the controller's append-only journal.
The restricted policy uses `--output-last-message` for the candidate answer and
checks that file against the final streamed agent message. Progress messages
remain in the raw evidence and are not concatenated with the final answer.
An absent or inconsistent final artifact fails execution; a malformed final
answer remains a candidate failure, even if an earlier message contained JSON.
Wrong or malformed answers retain their session, usage and tool-call evidence
as candidate failures. Interrupted controller evidence is retained before an
execution failure is returned. `tool_calls` counts completed native reference
attempts, including cap-denied calls; controller records separately identify
which attempts were forwarded. Resource denials remain in controller evidence.

The server identity hash here binds MCP `serverInfo`, **not a frozen corpus
snapshot**. Live corpus changes and contamination remain limitations. Backend
model identity, accepted/effective effort and a hard output-token bound remain
unknown when the native protocol does not attest them. A synthetic control
receipt does not waive the all-model, both-condition readiness gate before any
scored study. No scored results are established by these controls.

## Legacy closed-book policy

The remainder documents the original policy, used when `codex_tool_policy` is
omitted. Its historical descriptor captures must be regenerated against the
installed runtime; a newer catalog can cause them to fail. Use the paired policy
above for reference access.

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
