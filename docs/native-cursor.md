# Native Cursor adapter

New wire answers use the shared answer-first envelope: each packet ID maps to
`answer` followed by a separate nonblank `explanation`. Only answer values
enter saved runs and scoring. The native terminal result text is the order
source, checked against the last assistant segment when present; source
disagreement fails. Concatenated progress or malformed terminal text cannot
be repaired from an earlier valid answer or explanation. Raw stdout and
`candidate_submission.stdin` remain private evidence strings, preserving
presentation before canonical sorting. This observes native text; model-token
ordering before runtime serialization remains unverified.

`ukrainian_llm_eval.native_cursor` is a fail-closed adapter for headless
`cursor-agent` evaluation on a Cursor **subscription login**. It does not use
the Cursor SDK, does not prefer `CURSOR_API_KEY`, and does not import Learn
Ukrainian fleet code.

## Native isolation and staging

Each attempt creates one private root containing a fresh HOME, workspace,
TMPDIR and all four XDG directories. Help, version, status and inference use
that same environment and workspace. Parent profiles, settings, MCP entries,
cache/session paths, OAuth overrides, API keys and proxy variables are excluded.
The child runs with an owner-only creation mask; this is not an OS sandbox.
Managed configuration and native inventories still require runtime proof.

Set the runtime-only `UKRAINIAN_LLM_EVAL_CURSOR_PROVISIONING_DIR` to an
already operator-staged absolute directory. It must be current-user `0700`
with trusted nonsymlink ancestors. Its `auth.json` must be a bounded, nonempty,
current-user `0600` regular file with one hard link. Checked no-follow reads
reject special files, duplicate JSON keys and concurrent replacement/mutation.
Only the authentic `accessToken` and `refreshToken` pair is copied to the child
XDG config's `cursor/auth.json`; staged API keys and Bedrock credentials are
excluded. The child uses the supported `AGENT_CLI_CREDENTIAL_STORE=file`.
No expiry field or refresh token is invented. Installed source-contract hashes
are checked; a changed contract stops execution. Status is a token-presence
probe, not server-validity proof. Inference and model attestation remain required.

Missing/rejected staging or pre-model authentication failure yields
`auth_unavailable` / `NOT_RUN`, without a candidate retry. Auth diagnostics
contain only a reason, return code and timeout flag. Credential changes are
compared privately; only a rotation boolean/operator alert is recorded. The
original staged bytes are never changed. Genuine post-model output/thinking
and timeout evidence remain private diagnostic evidence.

Closed-book writes no Sources entry and omits MCP approval and force flags.
Sources writes only the evaluator-owned allowlisted proxy into the fresh
workspace configuration. Ambient user MCP configuration is never imported.

`--approve-mcps` and `--force` for closed-book
- for Sources, writes only allowlisted `sources` into
  `{workspace}/.cursor/mcp.json` and passes `--approve-mcps --force`
- excludes ambient user MCP configuration through the private child HOME

`--approve-mcps` auto-approves MCP *servers*. Headless ask mode still rejects
non-readonly MCP *tool runs* unless `--force` is also set. Sources therefore
uses both flags. Closed-book never passes `--force`.

Cursor may emit a `getMcpTools` discovery call before Sources MCP tools. The
adapter allows that meta-tool only for Sources, with scoped arguments and an
independent eight-operation metadata limit. Starts and genuine completions are
recorded separately from the twenty content calls. Controller operations are
counted only if they actually reach the proxy; native-only discovery creates
no invented controller receipt. Content completions must preserve arguments
and match the actual controller result. MCP names may arrive as `sources-verify_word`; they are
normalized to bare reference tool ids.

Cloud account state, effective backend effort/output limits and managed
configuration remain runtime limitations; fresh directories do not prove their
absence. A thinking-only timeout remains failed, with no fabricated answer or
automatic retry. Completion speed is not repaired by diagnostic capture.

## Configuration

```json
{
  "schema": "zno-nmt.config.v1",
  "adapter": "cursor",
  "provider": "managed:cursor-subscription",
  "model": "grok-4.7-high",
  "effort": "high",
  "timeout_seconds": 180,
  "max_output_tokens": 4096,
  "max_tool_calls": 20,
  "repeats": 3,
  "tools": ["verify_word", "verify_stress"],
  "corpus_id": "operator-live-sources",
  "cursor_bin": "cursor-agent"
}
```

`effort` is recorded for study accounting. The CLI has no separate `--effort`
flag; pin effort inside the model id (for example `grok-4.7-high`).

Prefer the unambiguous `cursor-agent` binary. A generic `agent` on `PATH` may
be Grok Build TUI and will misfire.

## Operator prerequisites

1. Provide existing authorized operator-staged subscription authentication;
   the evaluator does not log in, mint/refresh tokens or discover host stores.
2. Keep staging and raw evidence private. Successful native inference remains
   unverified until the authorized runtime smoke.
3. Provide a Sources URL only for Sources cells through the private endpoint
   workflow; never commit it.

## Public API

- `validate_config` / `validate_cursor_config` / `validate_options`
- `preflight` / `preflight_cursor`
- `run` / `run_cursor`

The shared runner reads only the runtime provisioning variable above.
Receipts keep subscription authentication distinct from actual inference:
`auth_path: "cli-login"` records the native route, and `api_key_source` must be
positively attested as `login` by the native stream.

## Explicit native output defaults (#67)

For Grok, set the required `max_output_tokens` field to the exact string
`"native-default"` to select the native runtime default in either condition.
Positive integers remain accepted as numeric metadata and are not forwarded
as output overrides. No token-limit switch is added. New preflight and run
identities disclose the configured selection and mechanism; effective numeric
ceilings remain unknown. The existing 2,000,000-byte stdout safety bound is
reported separately as `capture_stdout_max_bytes`. Usage is never clamped to
a configured integer and does not attest a per-request ceiling. See the
[shared selection contract](running.md#native-output-limit-selection-67)
for validation, failures and pairing. Historical captures do not renew proof
for this runtime revision.
