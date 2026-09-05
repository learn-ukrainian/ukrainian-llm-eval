# Native Kimi Code adapter

`ukrainian_llm_eval.native_kimi` provides a bounded adapter for the installed
Kimi Code v2 CLI. It is an opt in native subscription route. The evaluator
records the exact requested `kimi-code/<model>` alias and the selected native
configuration and catalog hashes. Kimi's print stream does not disclose the
backend model, so `effective_backend_model`, `effective_model`, account
identity, effective effort, and effective output cap remain `"unknown"` until
an authoritative observation exists. Results must therefore be attributed to
the requested subscription agent and excluded from an exact underlying model
ranking.

The shared runner supplies the private provisioning directory through the
process environment variable `UKRAINIAN_LLM_EVAL_KIMI_PROVISIONING_DIR`. The
directory is a runtime input; it is never placed in the evaluator config,
fingerprints, trial identity, or retained evidence. A missing directory or an
unresolved provider/model/token entry makes the tuple explicitly not ready.
The adapter does not run a login flow, read the user's ordinary Kimi home,
substitute a paid API route, or refresh a provider catalog.

## Public interface

```python
validate_kimi_config(config) -> dict
validate_config(config) -> dict
validate_options(config, *, private_env_path) -> NativeKimiOptions
preflight_kimi(config, condition, sources_url=None, *, private_env_path=None) -> dict
run_kimi(packet, config, condition, *, sources_url, prompt,
         evidence=None, private_env_path=None) -> dict
```

`validate_kimi_config` validates Kimi-specific values and can be called by a
shared validator before common fields are checked. `validate_config` is the
full adapter entry point used by the current shared dispatch. It requires the
existing `zno-nmt.config.v1` fields plus the optional `kimi_bin` path, an exact
`kimi-code/<model>` alias, allowlisted Sources tool names, and the managed
provider identity `managed:kimi-code` when `provider` is supplied.

`preflight_kimi` verifies the CLI version/help surface, private provider and
model configuration, OAuth token shape, effort support, and (for `sources`)
the advertised Sources tool schema. It returns capability evidence including:

* `requested_model` and `requested_model_alias`;
* `requested_effort`, `accepted_effort="unknown"`, `supported_efforts`, and
  `effort_support_source="private-native-config-model-alias"`;
* `cli_version`, `version_observed`, `binary_sha256`;
* `native_config_sha256`, `catalog_provider_sha256`, and `catalog_model_sha256`;
* `settings_sha256`, `request_shape_sha256`, `tool_schema_sha256`, and the
  optional Sources `mcp_server_identity_sha256`;
* `effective_backend_model`, `account_identity`, and
  `max_output_tokens_effective` set to `"unknown"`.

`run_kimi` returns the existing trial shape: `responses`, `identity`, and
`metrics`. The identity carries the requested alias, managed provider route,
reported `session.resume_hint` session ID, observed CLI version, native and
catalog hashes, requested effort, unknown accepted/effective controls, and the
isolation hashes. Metrics preserve CLI-reported token/cost values when present and use
`null` when Kimi omits usage. `evidence("cli_result", ...)` retains raw
stdout/stderr for the caller's private evidence store; the returned trial and
normal failure reason contain no raw provider output.

When the stream envelope has passed its version, session, identity, tool-policy,
and tool-result checks but the assistant answer cannot be parsed, `run_kimi`
returns the same failed trial with `failure_reason="candidate_response_error"`.
Its response map contains one `null` for every packet item, while the verified
native identity and usage/tool-call metrics remain available. The private
`candidate_answer_outcome` evidence event records the parser reason and exact
answer payload. This stable task-only reason lets a scheduler count the attempt
and continue later independent repeats without treating the candidate as
measurement-unready.

## Provisioning contract

The supplied directory must be an absolute owner-only directory with mode
`0700`, containing owner-only `config.toml` (`0600`) and the selected OAuth
file under `credentials/` (`0700` directory, `0600` file). The adapter accepts
only the installed managed Kimi route:

```toml
[providers."managed:kimi-code"]
type = "kimi"
base_url = "https://api.kimi.com/coding/v1" # or the approved global route
api_key = ""

[providers."managed:kimi-code".oauth]
storage = "file"
key = "oauth/kimi-code"                  # mainland route
oauth_host = "https://auth.kimi.com"

[models."kimi-code/<model>"]
provider = "managed:kimi-code"
model = "<model>"
max_context_size = 131072
support_efforts = ["low", "medium"]
```

The global route uses `https://api.kimi.ai/coding/v1`,
`https://auth.kimi.ai`, and an installed scoped key of the form
`oauth/kimi-code-env-<16 lowercase hex characters>`. Contradictory region,
provider, alias, model endpoint, OAuth, unsupported model, or fallback
identity is rejected. Provider and model records are whitelisted field by
field; hooks, plugins, services, arbitrary headers, `overrides`, and API-key
routes are not copied into the attempt home. The token content is copied only
to the selected fresh home and is never hashed, logged, or printed.

## Attempt controls

Each attempt gets a fresh `0700` home and neutral `0700` cwd. The cwd is checked
for ancestor `.git`, `.mcp.json`, and `.kimi-code/mcp.json` controls. The home
contains only the selected sanitized config, token, and (for `sources`) one
stdio Sources proxy declaration. The profile has a self-contained body with
`subagents: []`; it contains no `${base_prompt}` or `${plugin_sections}`. The
closed-book profile has `tools: []`. Sources tools are qualified as
`mcp__sources__<name>` and the proxy receives the exact allowlist and positive
call cap.

The child environment is rebuilt from a small runtime allowlist and sets
`KIMI_CODE_HOME` to the fresh home, `KIMI_CODE_LEGACY_FLAG=0`,
`KIMI_MODEL_MAX_COMPLETION_TOKENS` to the configured positive cap, and
`KIMI_MODEL_THINKING_EFFORT` only when the private model record proves support.
No resume flag (`-S`, `-r`, or `-c`) is passed. The CLI is invoked with an
explicit alias, profile, empty skills directory, and `--output-format
stream-json`.

The parser validates the observed version, assistant, tool-result, and
resume-hint events as an integrity envelope. It rejects retries, unrecognized
meta events, malformed or unallowlisted tool calls, missing tool results,
fallback/identity drift, duplicate JSON keys, blank lines, and oversized output.
Those failures remain generic fail-closed adapter failures. After the envelope
passes, the answer content is parsed separately with the shared strict JSON
loader and `_extract_responses`, preserving exact packet IDs and response
types. Fences, explanatory text, non-JSON content, missing content, and answer
schema mismatches are never stripped, repaired, or retried; they produce the
stable `candidate_response_error` failed/null result while retaining the
verified envelope identity and metrics. The compatibility `_parse_stream_json`
helper keeps its original strict exception behavior for callers that use the
low-level parser directly.

The installed CLI exposes no authoritative backend-model event and no usage
event is guaranteed. The adapter keeps those values explicitly unknown and
requires a separate live semantic canary for every exact
alias/effort/condition tuple before readiness or ranking claims.

## Run with and without Sources

After installing the evaluator, prepare a question-only packet as described in
[running.md](running.md). Provision a private directory using the contract
above and set its absolute path in `UKRAINIAN_LLM_EVAL_KIMI_PROVISIONING_DIR`.
Keep this directory outside the checkout and evidence archives. Obtain the
selected provider/model records and OAuth credential from your own authenticated
Kimi installation; retain their current values and private file permissions.
The evaluator has no interactive login or credential-export command.

Create a runtime config, replacing the alias and effort with values supported
by your installed catalog:

```json
{
  "schema": "zno-nmt.config.v1",
  "adapter": "kimi",
  "provider": "managed:kimi-code",
  "model": "kimi-code/k3",
  "effort": "high",
  "timeout_seconds": 120,
  "max_output_tokens": 4096,
  "max_tool_calls": 4,
  "repeats": 3,
  "tools": ["verify_word"],
  "corpus_id": "operator-live-sources"
}
```

Use `null` for effort when the alias advertises no effort selector. The output
and tool limits above are examples; freeze appropriate limits before a scored
experiment. The same configuration supports both conditions: the tool list
applies only to Sources-assisted execution.

```bash
ukrainian-llm-eval preflight --config runtime/kimi.json \
  --condition closed-book
ukrainian-llm-eval run --questions runtime/questions.json \
  --config runtime/kimi.json --condition closed-book \
  --evidence-dir runtime/kimi-closed-evidence \
  --output runtime/kimi-closed.json
```

For the assisted condition, set `SOURCES_MCP_URL` in the private runtime
environment to your accessible MCP endpoint, then run:

```bash
ukrainian-llm-eval preflight --config runtime/kimi.json \
  --condition sources
ukrainian-llm-eval run --questions runtime/questions.json \
  --config runtime/kimi.json --condition sources \
  --evidence-dir runtime/kimi-sources-evidence \
  --output runtime/kimi-sources.json
```

Each `run` creates one fresh attempt. Use the documented frozen experiment
scheduler for the repeated research matrix; a successful preflight or one
successful attempt is not a completed evaluation.
