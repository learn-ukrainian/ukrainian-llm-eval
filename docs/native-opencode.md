# Gemma through native OpenCode

The `opencode` adapter evaluates `google/gemma-4-31b-it` through OpenCode's
OpenRouter provider. Reasoning is explicitly off or on; `effort` is null.
A direct `chat-http` run is a different harness and does not establish this
route's readiness.

Each attempt creates a fresh home, workspace and session. It imports no
OpenCode authentication file, plugins, agent definitions, rules or previous
sessions. The CLI uses `--pure`, an explicit evaluation agent, a fixed title,
disabled title/summary agents and disabled automatic compaction. Permissions
deny every tool except `StructuredOutput` and configured Sources references in Sources mode.
The CLI starts an authenticated loopback server; its native session API receives
the packet schema and uses `StructuredOutput` for the final answer.

The parent reads the provider credential from the configured environment
variable. OpenCode receives only a temporary local gateway credential. Its
OpenRouter provider constructs the requests; the gateway forwards them only
after checking model, endpoint provider, reasoning toggle, output limit and
permitted tool names. It rejects duplicate requests, auxiliary requests in
closed-book mode, unexpected fields and excess rounds. Redirects are rejected.
Real provider credentials never enter the child home.

The gateway buffers each provider stream until it verifies completion, model
and provider identity, authoritative usage and the tool-call allowance. It
forwards only calls matching that response to the filtered reference bridge.
The bridge also enforces the total call cap. Raw native assistant messages and provider
streams are retained through the existing private evidence callback.

Answers must satisfy the packet's original JSON response contract. Markdown
fences, missing IDs and invalid answers are failures; the adapter does not
strip fences or repair responses. The schema-validated native final answer must
agree with the provider's terminal `StructuredOutput` arguments. Every native
reference call must be completed and match the gateway count.
`StructuredOutput` is a final-answer channel, so it does not consume the
reference-call allowance or create another provider request. It must occur
exactly once, without a reference call in the same response; any further
request is rejected. All response tokens and provider cost still count. Requested reasoning and effective reasoning
remain distinct: the latter is unknown without provider attestation.

## Configuration

```json
{
  "schema": "zno-nmt.config.v1",
  "adapter": "opencode",
  "opencode_bin": "opencode",
  "model": "google/gemma-4-31b-it",
  "provider": "openrouter",
  "effort": null,
  "timeout_seconds": 90,
  "max_output_tokens": 4096,
  "max_tool_calls": 1,
  "repeats": 1,
  "tools": ["verify_word"],
  "corpus_id": "live-sources",
  "endpoint_env": "EVAL_OPENROUTER_ENDPOINT",
  "key_env": "EVAL_OPENROUTER_KEY",
  "openrouter": {
    "provider_endpoint": "venice/bf16",
    "expected_provider_name": "Venice",
    "reasoning_enabled": false,
    "max_price": {"prompt": "0.12", "completion": "0.36", "request": "0"}
  }
}
```

Endpoint/key environment values are private runtime inputs. Verify current
endpoint availability and price ceilings before freezing a study. The sample
is not an entitlement, pricing guarantee or permission to spend.

Use the ordinary study execution path with a validated request-budget
mechanism and spending authorization. Native OpenCode supports the same
request commitments and settlement controller as the HTTP adapters. Direct
live calls without a request-budget controller fail before CLI launch;
credential-free loopback fixtures are supported without paid accounting.
This adapter does not purchase credits or fall back to another provider.

## Verification and limits

Deterministic tests cover route drift, output limits, unexpected tools,
duplicate requests, stream completeness, provider identity, tool-call matching,
call limits, private environment isolation, native structured-answer parsing and budget
ordering. The installed OpenCode CLI was separately exercised against local
provider and MCP fixtures for reasoning off/on in both conditions. All four
cases returned the exact answer; Sources cases each made one allowed call and
a second model request carrying its result. Closed-book cases each made one
model request and no tool calls. These checks establish mechanism behavior,
not live model quality or benchmark scores.

No OS sandbox is claimed. Isolation combines a fresh home/workspace, native
permissions and the parent transport/reference gate. Study admission, a live
semantic canary, independent review and the all-model readiness gate remain
separate requirements before scored exams.

Configuration references: [OpenCode agents](https://opencode.ai/docs/agents/),
[OpenCode configuration](https://opencode.ai/docs/config/),
[OpenCode structured output API](https://opencode.ai/docs/sdk/#structured-output).
