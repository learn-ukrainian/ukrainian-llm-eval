# Gemini through native AGY CLI

The `agy` adapter runs the existing Antigravity subscription route. It supports
`gemini-3.8-flash-low`, `gemini-3.8-flash-medium` and `gemini-3.8-flash-high`,
with a matching `effort`. It does not use the Gemini API or add a paid fallback.

Each attempt creates one private root with fresh HOME, CWD, TMPDIR and all
four XDG directories before capability probes. Probes and inference share
that exact child environment/workspace; the candidate has one conversation.
Only a supplied OAuth token is copied into that home. Existing settings, rules,
plugins, skills, conversations and API-key environment variables are excluded.
The generated settings disable G1 credit fallback. No login, credit purchase,
or original-home change is performed.

The native profile exposes `finish` and, in Sources mode, the native MCP
dispatcher. AGY's initial inventory can list additional built-in descriptors;
it is not proof that those actions are callable. An installed `PreToolUse`
hook permits `finish`, configured `sources` reference calls, and native
`list_resources` with exactly `ServerName=sources` in Sources cells. Discovery
has an independent eight-operation limit; native-only discovery has no
invented controller receipt. The proxy truthfully returns unsupported
`resources/list` (-32601) without forwarding it or advertising resources.
Unsupported metadata is capability disclosure, not successful content coverage.
The hook denies
all other tool actions and enforces the total reference-call cap under a file
lock. A separate authenticated parent MCP bridge filters schemas and calls and
enforces the same cap. The trusted catalog is supplied in the prompt because
AGY normally discovers MCP schemas through filesystem tools, which are denied.

The candidate receives one question-only user event over stdin. Native
`--json-schema` constrains the final `finish` output. The adapter verifies one
successful native result, exact model/agent/schema, completed steps, the final
hook receipt and the returned answer. Every reference call must match across
the hook, completed native step and parent bridge, including the result text.
A denied or failed tool, extra turn, unfinished step, missing hook, malformed
answer or evidence mismatch fails the attempt. Answers are never repaired.
Raw events and reference receipts remain in private evidence.

The shared runner prompt includes the complete ordered reference catalog and
all task instructions before recording its SHA-256. AGY does not list tools
again to append a private task suffix. In both conditions its only input-frame
constants are `INPUT_FRAME_PREFIX = '{"event":"user","message":{"content":'`
and `INPUT_FRAME_SUFFIX = '}}\n'`, enclosing the JSON-encoded logical prompt.
These constants contain transport syntax only. The private
`runtime_scaffolding` event records them, the condition and the SHA-256 of their
concatenated UTF-8 bytes; `candidate_submission.stdin` retains the actual frame.
Any additional task byte fails submission equality.

Each item now submits `answer` first and a separate nonblank `explanation`.
The raw stdout result line and raw finish-hook args are both decoded with
ordered key pairs before evidence-store canonicalization. Answer, explanation
contents and observed field order must agree. `agy_hook_receipts_raw.text`
preserves the hook log verbatim; only answer values enter the saved run.
The hook and CLI serialize JSON objects, so this proves their observed
presentation, not the ordering of pre-runtime model tokens. Missing producer
events remain failures; the adapter never synthesizes terminal evidence.

## Provisioning and configuration

Use an already operator-staged owner-only private provisioning directory
containing `antigravity-oauth-token`. The evaluator never logs in, discovers
host credential stores, mints tokens or changes the original staging. The directory must
have mode `0700`, and the token must be a regular file with mode `0600`.
Point `UKRAINIAN_LLM_EVAL_AGY_PROVISIONING_DIR` at that directory. It is a
runtime-only input; never commit it or token contents. Other files in that
directory are not imported. Use an absolute path outside the repository.

```json
{
  "schema": "zno-nmt.config.v1",
  "adapter": "agy",
  "agy_bin": "agy",
  "provider": "managed:antigravity-subscription",
  "model": "gemini-3.8-flash-low",
  "effort": "low",
  "timeout_seconds": 90,
  "max_output_tokens": 4096,
  "max_tool_calls": 1,
  "repeats": 1,
  "tools": ["verify_word"],
  "corpus_id": "live-sources"
}
```

Use the ordinary `preflight`, `run`, `pair` and study commands. Sources mode
also needs the runtime Sources URL. Request-level paid-HTTP budgeting is not
available for this native subscription adapter; do not configure it as a
metered API route. Unknown subscription usage or cost is not zero cost.

## Verified controls and remaining limits

Installed native tests used dummy credentials and local provider/MCP fixtures.
At each of three efforts, they checked valid closed-book and Sources output,
excess reference-call denial, forbidden MCP denial and native task-action
denial: 15 cases. Separate native subscription canaries checked all six
model/condition combinations against a synthetic question. No scored exam or
benchmark-quality claim follows from those checks.

`max_output_tokens` records the study's requested setting. AGY exposes no
verified CLI control enforcing it, so the receipt explicitly reports the
effective output cap as unknown. Local request inspection observed a native
65,536-token ceiling. Do not describe the sample's 4,096 as an enforced cap.
The process has a wall-clock deadline; oversized captured output is rejected.

AGY can make an auxiliary request to generate a conversation title. A local
marker test established that the title response was not supplied to the
candidate model. This is native harness metadata overhead, not a second
candidate answer. The native result may not account for its usage. Effective
backend reasoning and subscription account identity remain unverified.

No OS sandbox is claimed. These controls rely on the installed native profile,
hooks and evaluator bridge. Re-run installed controls when the CLI changes.
Preflight records the binary and controls hashes, and execution checks them
again. Independent cross-family review and study admission remain separate
requirements before release or scored evaluation.

References: [AGY headless mode](https://antigravity.google/docs/cli/headless/),
[hooks](https://antigravity.google/docs/hooks/),
[MCP configuration](https://antigravity.google/docs/cli/mcp/),
[credit fallback settings](https://antigravity.google/docs/cli/credits/).

## Same-attempt diagnostic capture (#65)

The supported `--log-file` mechanism binds a unique prelaunch log beneath the
trusted private attempt root. Exactly one unambiguous created/found conversation
binds its transcript. Only that log, transcript and producer result pointers
inside the bound conversation's steps directories are retained. Each artifact
has a 16 MiB byte bound and checked no-follow FD-relative reads; symlinks,
hard links, special files, escaping pointers, mutation, incomplete JSONL and
ambiguous conversation bindings fail closed. Full bytes and digests are saved
privately before temporary cleanup on success or failure. No HOME/auth archive,
latest/prompt/time lookup, truncation, FIFO pairing or metadata stripping occurs.
Closed-book does not start a reference proxy.

Capture is diagnostic only. The supported producer contract does not yet prove
exact call/step/arguments/full-result correlation for supplementary transcript
results, so those bytes never backfill inline results or synthesize events.
Missing native DONE output remains failed. The observed missing-output producer
cause and issue #65 closure require further evidence owned by the driver;
fixture tests and retained bytes are not native runtime success.

## Explicit native output defaults (#67)

For Flash, set the required `max_output_tokens` field to the exact string
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
