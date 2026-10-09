"""Native AGY PreToolUse gate; invoked as a file in an isolated child home."""
from __future__ import annotations

import fcntl
import json
import sys
import time
from pathlib import Path
from typing import Any


def decide(call: Any, controls: dict[str, Any], count: int, metadata_count: int = 0) -> tuple[bool, bool]:
    """Return (allowed, reference) without executing a candidate tool."""
    if not isinstance(call, dict) or time.monotonic() >= controls["deadline"]:
        return False, False
    name, args = call.get("name"), call.get("args", {})
    if not isinstance(args, dict):
        return False, False
    if name == "finish":
        return True, False
    if name == "list_resources":
        scoped = bool(controls["tools"]) and args == {"ServerName": "sources"}
        return scoped and metadata_count < controls.get("max_metadata_operations", 8), False
    reference = (name == "call_mcp_tool" and args.get("ServerName") == "sources"
                 and args.get("ToolName") in controls["tools"] and isinstance(args.get("Arguments"), dict))
    return reference and count < controls["max_tool_calls"], reference


def main() -> int:
    try:
        path = Path(sys.argv[1])
        controls = json.loads(path.read_text())
        payload = json.loads(sys.stdin.read(2_000_001))
        call = payload.get("toolCall")
        with path.with_suffix(".state").open("a+", encoding="utf-8") as state:
            fcntl.flock(state, fcntl.LOCK_EX)
            state.seek(0)
            counts = state.read() or "0"
            if counts.startswith("{"):
                counts = json.loads(counts)
                count, metadata_count = counts["content"], counts["metadata"]
            else:
                count, metadata_count = int(counts), 0
            allowed, reference = decide(call, controls, count, metadata_count)
            metadata = call.get("name") == "list_resources" if isinstance(call, dict) else False
            if allowed and (reference or metadata):
                state.seek(0)
                state.truncate()
                # Retain the old content-only state for existing native fixtures.
                state.write(json.dumps({"content": count + int(reference), "metadata": metadata_count + int(metadata)})
                            if metadata_count or metadata else str(count + 1))
                state.flush()
            receipt = {"call": call, "decision": "allow" if allowed else "deny", "count_before": count,
                       "metadata_count_before": metadata_count}
            with path.with_suffix(".jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(receipt, ensure_ascii=False, allow_nan=False) + "\n")
            print(json.dumps({"decision": receipt["decision"], "reason": "Evaluator reference allowlist and call cap"}))
        return 0
    except Exception:  # noqa: BLE001 -- malformed input must never grant permission
        print('{"decision":"deny","reason":"Evaluator gate failed"}')
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
