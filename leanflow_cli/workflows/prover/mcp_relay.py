"""Relay native-runtime MCP stdio messages to one parent-owned job broker."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    """Forward protocol messages; credentials identify exactly one job capability."""
    endpoint = os.environ["LEANFLOW_BRIDGE_URL"]
    token = os.environ["LEANFLOW_BRIDGE_TOKEN"]
    # The capability belongs to a local broker and must never traverse a user's
    # configured HTTP proxy.
    local_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for line in sys.stdin:
        message: object = None
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("MCP messages must be JSON objects")
            if "id" not in message:
                continue
            request = urllib.request.Request(
                endpoint,
                data=json.dumps(message).encode(),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            )
            with local_http.open(request) as response:
                result = json.load(response)
        except (OSError, ValueError, urllib.error.URLError) as error:
            result = {
                "jsonrpc": "2.0",
                "id": message.get("id") if isinstance(message, dict) else None,
                "error": {"code": -32603, "message": str(error)},
            }
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
