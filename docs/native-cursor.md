# Native Cursor adapter

`ukrainian_llm_eval.native_cursor` is a fail-closed adapter for headless
`cursor-agent` evaluation on a Cursor **subscription login**. It does not use
the Cursor SDK, does not prefer `CURSOR_API_KEY`, and does not import Learn
Ukrainian fleet code.

The first scored route for this adapter is `--model cursor-grok-4.6-high`.
That is an explicit Cursor Grok-4.6 seat. It does **not** claim the reserved
public Grok-4.7 study inventory.

## Isolation limitations

`cursor-agent` has no Codex-style `--ignore-user-config`, `--ephemeral`, or
`--deny-mcp` flags. Auth on macOS uses the operator keychain and therefore
requires the real `HOME` / `USER`. Each attempt still:

- creates a fresh empty `--workspace`
- runs `--mode ask` (read-only)
- omits `--approve-mcps` and `--force` for closed-book
- for Sources, writes only allowlisted `sources` into
  `{workspace}/.cursor/mcp.json` and passes `--approve-mcps --force`
- fails closed when `~/.cursor/mcp.json` exists with any `mcpServers`

`--approve-mcps` auto-approves MCP *servers*. Headless ask mode still rejects
non-readonly MCP *tool runs* unless `--force` is also set. Sources therefore
uses both flags. Closed-book never passes `--force`.

Cursor may emit a `getMcpTools` discovery call before Sources MCP tools. The
adapter allows that meta-tool only under Sources and does not count it toward
`max_tool_calls`. MCP names may arrive as `sources-verify_word`; they are
normalized to bare reference tool ids.

Residual account/config leakage (global rules, model prefs, cloud account
state) remains possible. Document that limitation in study notes; prefer
native sealed adapters when isolation attestation is required.

## Configuration

```json
{
  "schema": "zno-nmt.config.v1",
  "adapter": "cursor",
  "provider": "managed:cursor-subscription",
  "model": "cursor-grok-4.6-high",
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
flag; pin effort inside the model id (for example `cursor-grok-4.6-high`).

Prefer the unambiguous `cursor-agent` binary. A generic `agent` on `PATH` may
be Grok Build TUI and will misfire.

## Operator prerequisites

1. Install Cursor Agent CLI and run `cursor-agent login`.
2. Confirm `cursor-agent status` shows a logged-in subscription.
3. Ensure `~/.cursor/mcp.json` is absent or has empty `mcpServers`.
4. For Sources cells, provide the live Sources URL through the operator's
   private endpoint workflow (never commit it).

## Public API

- `validate_config` / `validate_cursor_config` / `validate_options`
- `preflight` / `preflight_cursor`
- `run` / `run_cursor`

The shared runner dispatches `"adapter": "cursor"` without a private
provisioning directory env var. Subscription identity stays on the operator
machine; identity receipts record `auth_path: "cli-login"` and attested
`api_key_source` when the stream reports it.
