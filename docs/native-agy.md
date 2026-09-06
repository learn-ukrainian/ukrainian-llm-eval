# Gemini through native AGY CLI

The `agy` adapter runs the existing Antigravity subscription route. It supports
`gemini-3.8-flash-low`, `gemini-3.8-flash-medium` and `gemini-3.8-flash-high`,
with a matching `effort`. It does not use the Gemini API or add a paid fallback.

Each attempt creates an empty home, neutral Git workspace and one conversation.
Only a supplied OAuth token is copied into that home. Existing settings, rules,
plugins, skills, conversations and API-key environment variables are excluded.
The generated settings disable G1 credit fallback. No login, credit purchase,
or original-home change is performed.

The native profile exposes `finish` and, in Sources mode, the native MCP
dispatcher. AGY's initial inventory can list additional built-in descriptors;
it is not proof that those actions are callable. An installed `PreToolUse`
hook permits only `finish` and configured `sources` reference calls. It denies
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

## Provisioning and configuration

Authenticate in your normal AGY session first. Create an owner-only private
provisioning directory containing `antigravity-oauth-token`, copied from your
existing `~/.gemini/antigravity-cli/antigravity-oauth-token`. The directory must
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
