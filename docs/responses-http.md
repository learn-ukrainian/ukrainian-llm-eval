# DeepSeek Responses HTTP adapter

`ukrainian_llm_eval.responses_http.run_responses_http` runs one fresh
Ukrainian exam trial through a provider implementing the DeepSeek Responses API
shape. It is a narrow evaluator adapter for the ZNO/NMT and UA-GEC response
contract. It does not select a model, score answers, or retry a failed request.

The route must be configured with `adapter: "responses-http"`, a model such as
`deepseek-v4-flash`, and environment-variable names for the endpoint and
optional bearer key:

```json
{
  "schema": "zno-nmt.config.v1",
  "adapter": "responses-http",
  "model": "deepseek-v4-flash",
  "effort": "high",
  "timeout_seconds": 60,
  "max_output_tokens": 4096,
  "max_tool_calls": 4,
  "repeats": 3,
  "tools": ["verify_word"],
  "corpus_id": "sources-revision-id",
  "provider": "deepseek",
  "endpoint_env": "ZNO_NMT_RESPONSES_ENDPOINT",
  "key_env": "ZNO_NMT_RESPONSES_KEY",
  "http_response_format": "json_schema"
}
```

Set the endpoint and key only in the private runtime environment. The key is
used as a bearer credential for the POST request and is never included in the
candidate prompt, returned trial identity, or public configuration. A typical
endpoint is `https://api.deepseek.com/responses`; use the exact endpoint
recorded by the route's admission evidence.

DeepSeek documents the Responses API as stateless: each request sends the
complete `input` history, and `previous_response_id` and `conversation` are
unsupported. The adapter therefore sends the initial user prompt on every
request, sets `store` to `false`, and appends the complete provider `output`
items and each `function_call_output` to the next request. Reasoning items are
retained in that history. A response must have an exact requested model,
`object: "response"`, a unique response ID, `status: "completed"`, complete
output items, and authoritative `usage` fields. `incomplete`, `failed`,
missing-status, malformed, or model-drift responses fail the trial without a
retry. The adapter uses non-streaming responses so the final status and full
output object arrive together.

The official Responses API defines `max_output_tokens` as the combined bound
for visible output and reasoning tokens. The adapter passes that field
unchanged and checks `usage.output_tokens` against it. `usage.output_tokens`
and its optional `reasoning_tokens` breakdown are retained in private evidence;
the returned metrics report the aggregate input, output, and total counts.
DeepSeek's documented response usage does not contain a per-request account
charge, so `cost_usd` remains unknown here. A configured request-budget
controller still commits the complete serialized request before transport and
observes the provider usage before any next tool request.

## Closed-book and Sources conditions

For `closed-book`, the request contains no `tools` field and sets
`tool_choice: "none"`. Any function call, web search, or other unsupported
output item is rejected before it can reach a broker.

For `sources`, only the configured allowlisted reference tools are advertised
as Responses `function` tools. A function name must be the corresponding
`mcp__sources__...` name and its arguments must be a strict JSON object. The
controller calls the configured Sources MCP endpoint, records each attempted
call and result privately, and includes the serialized result in the complete
next `input` history. Failed calls count toward `max_tool_calls`; there is no
fallback to web search or another tool surface.

The adapter depends on the shared MCP allowlist and transport in
`ukrainian_llm_eval.adapters`. It does not publish or snapshot the Sources
corpus. A live Sources identity and tool-schema hash belong in private run
evidence and can change between experiments.

## Evidence and limitations

Pass the evaluator's private evidence callback to retain request bodies,
provider response bodies, full reasoning/output records, tool calls, and
request-budget receipts. The transport records raw response bytes as base64 in
that private store; returned failures expose only a normalized reason.

The adapter proves the request and response controls exercised by this
harness. It does not prove that a provider's hidden system framing, training
data, or backend implementation is unchanged. Pin the model, endpoint,
admission evidence, request-budget mechanism, and Sources identity in the
experiment manifest before treating results as comparable.

Authoritative provider semantics:

* [DeepSeek Responses API reference](https://api-docs.deepseek.com/api/create-response/)
* [DeepSeek Responses API guide](https://api-docs.deepseek.com/guides/responses_api/)
