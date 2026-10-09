"""Filtered stdio MCP bridge; no corpus material is written to disk.

Invoked by a trusted native candidate harness, never by the candidate itself.
The endpoint is read from an environment variable and never advertised as a tool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

REFERENCE_TOOLS = frozenset({
    "verify_word", "verify_words", "verify_lemma", "verify_stress", "search_text",
    "get_chunk_context", "query_pravopys", "search_style_guide", "search_definitions",
    "search_idioms", "search_synonyms", "check_modern_form", "search_literary",
})
MAX_BYTES = 2_000_000

SMOKE_TOOLS = ["verify_words", "verify_stress", "query_pravopys", "search_style_guide", "search_text"]
SMOKE_CATALOG_SHA256 = "63cb16b7233e298814b0dc5a85e6f27cc27c7b6fd112123a48c365ab11a4fba3"


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def normalized_catalog(tools: list[dict], configured: list[str]) -> list[dict]:
    if not isinstance(tools, list):
        raise ValueError("MCP tool schema listing is invalid")
    indexed = {}
    for tool in tools:
        if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                or not isinstance(tool.get("inputSchema"), dict) or tool["name"] in indexed):
            raise ValueError("MCP tool schema is invalid or duplicated")
        indexed[tool["name"]] = tool
    if not set(configured) <= indexed.keys():
        raise ValueError("Sources MCP does not expose configured tools")
    if set(configured) == set(SMOKE_TOOLS) and configured != SMOKE_TOOLS:
        raise ValueError("frozen Sources schema order drift")
    selected = [indexed[name] for name in configured]
    if configured == SMOKE_TOOLS and hashlib.sha256(canonical(selected).encode()).hexdigest() != SMOKE_CATALOG_SHA256:
        raise ValueError("frozen Sources schema drift")
    return selected


def physical_lines(text: str) -> list[str]:
    """Split serialized records on CR/LF only, preserving Unicode JSON values.

    Match splitlines' empty-input and final-terminator behavior, while keeping
    interior blank records for each reader's existing strictness policy.
    """
    if not text:
        return []
    lines = re.split(r"\r\n|\r|\n", text)
    if lines[-1] == "":
        lines.pop()
    return lines


def strict_json(raw: str) -> object:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate reference JSON key")
            result[key] = value
        return result
    def constant(_value):
        raise ValueError("nonfinite reference JSON value")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def decode_response(raw: bytes) -> dict:
    text = raw.decode("utf-8")
    if text.lstrip().startswith("{"):
        return strict_json(text)
    for event in text.replace("\r\n", "\n").split("\n\n"):
        payload = "\n".join(line[5:].lstrip() for line in physical_lines(event) if line.startswith("data:"))
        if payload:
            result = strict_json(payload)
            if isinstance(result, dict) and ("result" in result or "error" in result):
                return result
    raise ValueError("missing MCP result")


def validate_arguments(value: object, schema: dict) -> None:
    """Validate the JSON-schema vocabulary used by the frozen Sources catalog."""
    supported = {"type", "properties", "required", "items", "additionalProperties", "enum", "description",
                 "title", "default", "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"}
    if set(schema) - supported:
        raise ValueError("unsupported reference argument schema")
    kinds = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
    kind = schema.get("type")
    if kind is not None and (kind not in kinds or type(value) is not kinds[kind]):
        raise ValueError("reference argument type mismatch")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("reference argument enum mismatch")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if not set(schema.get("required", [])) <= value.keys():
            raise ValueError("reference argument required field missing")
        if schema.get("additionalProperties") is False and set(value) - properties.keys():
            raise ValueError("reference argument extra field")
        for key, item in value.items():
            if key in properties:
                validate_arguments(item, properties[key])
            elif isinstance(schema.get("additionalProperties"), dict):
                validate_arguments(item, schema["additionalProperties"])
    if isinstance(value, list) and "items" in schema:
        for item in value:
            validate_arguments(item, schema["items"])
    for lower, upper, measure in (("minimum", "maximum", value),
                                  ("minLength", "maxLength", len(value) if isinstance(value, str) else None),
                                  ("minItems", "maxItems", len(value) if isinstance(value, list) else None)):
        if lower in schema and (measure is None or measure < schema[lower]):
            raise ValueError("reference argument lower bound")
        if upper in schema and (measure is None or measure > schema[upper]):
            raise ValueError("reference argument upper bound")


class Bridge:
    def __init__(self, url: str, allowed: list[str], timeout: float = 20, max_tool_calls: int = 20, *,
                 journal: Path | None = None, deadline: float | None = None):
        if not allowed or len(set(allowed)) != len(allowed) or not set(allowed) <= REFERENCE_TOOLS:
            raise ValueError("invalid reference tool allowlist")
        self.url = url
        self.allowed = allowed
        self.timeout = timeout
        self.max_tool_calls = max_tool_calls
        self.calls = 0
        self.headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        self.schemas: dict = {}
        self.metadata_operations = 0
        self.journal = journal
        self.deadline = deadline if deadline is not None else time.monotonic() + 600

    def charge_metadata(self, method: str, params: dict) -> None:
        if time.monotonic() >= self.deadline or self.metadata_operations >= 8:
            raise ValueError("reference metadata limit or deadline reached")
        if method != "initialize" and params:
            raise ValueError("reference discovery arguments not permitted")
        self.metadata_operations += 1

    def record_exchange(self, request: dict, response: dict | None) -> None:
        if self.journal is not None:
            with self.journal.open("a", encoding="utf-8") as stream:
                stream.write(canonical({"request": request, "response": response}) + "\n")

    def request(self, method: str, params: dict, ident: int | str | None = 1) -> dict:
        data = {"jsonrpc": "2.0", "method": method, "params": params}
        if ident is not None:
            data["id"] = ident
        request = urllib.request.Request(self.url, data=json.dumps(data).encode(), headers=self.headers)
        opener = urllib.request.build_opener(_RejectRedirects())
        with opener.open(request, timeout=self.timeout) as response:
            session = response.headers.get("Mcp-Session-Id")
            if session:
                self.headers["Mcp-Session-Id"] = session
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("MCP response exceeds limit")
            if ident is None or not raw:
                return {}
            result = decode_response(raw)
            if (not isinstance(result, dict) or result.get("jsonrpc") != "2.0"
                    or type(result.get("id")) is not type(ident) or result.get("id") != ident
                    or ("result" in result) == ("error" in result)):
                raise ValueError("MCP response correlation invalid")
            return result

    def handle(self, message: dict) -> dict | None:
        try:
            response = self._handle(message)
        except Exception:
            self.record_exchange(message, {"jsonrpc": "2.0", "id": message.get("id"),
                "error": {"code": -32603, "message": "Reference bridge request failed"}})
            raise
        self.record_exchange(message, response)
        return response

    def _handle(self, message: dict) -> dict | None:
        ident = message.get("id")
        method = message.get("method")
        params = message.get("params", {})
        if not isinstance(params, dict) or time.monotonic() >= self.deadline:
            raise ValueError("reference request or deadline invalid")
        if method in {"initialize", "notifications/initialized", "ping", "tools/list", "resources/list"}:
            self.charge_metadata(method, params)
        if ident is None:
            if method == "notifications/initialized":
                self.request(method, {}, None)
            return None
        if method == "initialize":
            result = self.request("initialize", params, ident)
            if "error" in result:
                raise ValueError("upstream MCP initialization failed")
            version = result["result"]["protocolVersion"]
            self.headers["MCP-Protocol-Version"] = version
            body = {"protocolVersion": version, "capabilities": {"tools": {}},
                    "serverInfo": {"name": "exam-reference-filter", "version": "1"}}
        elif method == "ping":
            body = {}
        elif method == "tools/list":
            upstream = self.request("tools/list", {})
            tools = upstream.get("result", {}).get("tools", [])
            selected = normalized_catalog(tools, self.allowed)
            self.schemas = {tool["name"]: tool for tool in selected}
            body = {"tools": selected}
        elif method == "tools/call":
            if params.get("name") not in self.allowed:
                return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": "Tool not permitted"}}
            if not isinstance(params.get("arguments", {}), dict):
                raise ValueError("tool arguments must be an object")
            if params["name"] not in self.schemas:
                raise ValueError("reference schemas not bound before call")
            validate_arguments(params.get("arguments", {}), self.schemas[params["name"]]["inputSchema"])
            if self.calls >= self.max_tool_calls:
                return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": "Reference call limit reached"}}
            self.calls += 1
            upstream = self.request(method, params, ident)
            if "error" in upstream:
                # Do not forward transport diagnostics containing private endpoints.
                body = {"isError": True, "content": [{"type": "text", "text": "Reference lookup failed"}]}
            else:
                body = upstream["result"]
        else:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Method not permitted"}}
        return {"jsonrpc": "2.0", "id": ident, "result": body}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url-env", required=True)
    parser.add_argument("--tools", required=True, help="JSON array of reference tool names")
    parser.add_argument("--max-tool-calls", type=int, default=20)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--deadline", type=float)
    args = parser.parse_args()
    try:
        if args.max_tool_calls < 1:
            raise ValueError("positive call budget required")
        if args.journal is not None:
            fd = os.open(args.journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        bridge = Bridge(os.environ[args.url_env], json.loads(args.tools), max_tool_calls=args.max_tool_calls,
                        journal=args.journal, deadline=args.deadline)
    except (KeyError, ValueError, TypeError):
        print("Invalid reference bridge configuration", file=sys.stderr)
        return 2
    for line in sys.stdin:
        message = {}
        try:
            if len(line.encode()) > MAX_BYTES:
                raise ValueError("request exceeds limit")
            message = strict_json(line)
            if not isinstance(message, dict):
                raise TypeError("request must be an object")
            response = bridge.handle(message)
        except Exception:  # noqa: BLE001 - sanitize every malformed bridge request
            response = {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                        "error": {"code": -32603, "message": "Reference bridge request failed"}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
