import argparse
import asyncio
import json
import os
from time import time
from urllib.request import Request, urlopen


async def read_quota() -> dict[str, object]:
    server = await asyncio.create_subprocess_exec(
        "codex", "app-server", "--listen", "stdio://",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )
    assert server.stdin is not None and server.stdout is not None
    try:
        async def exchange() -> dict[str, object]:
            async def send(message: dict[str, object]) -> None:
                server.stdin.write((json.dumps(message) + "\n").encode())
                await server.stdin.drain()

            async def result(request_id: int) -> dict[str, object]:
                while line := await server.stdout.readline():
                    message = json.loads(line)
                    if message.get("id") == request_id:
                        if "error" in message:
                            raise RuntimeError("The Codex app server rejected the request")
                        return message["result"]
                raise RuntimeError("The Codex app server closed its output")

            await send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "litellm_quota_monitor", "version": "0.1.0"},
            }})
            await result(1)
            await send({"method": "initialized", "params": {}})
            await send({"id": 2, "method": "account/rateLimits/read"})
            return await result(2)

        return await asyncio.wait_for(exchange(), timeout=30)
    finally:
        if server.returncode is None:
            server.terminate()
        await server.wait()


def snapshots(data: dict, source: str, bucket: str, observation_time: int) -> list[dict]:
    by_id = data.get("rateLimitsByLimitId") or {}
    legacy = data.get("rateLimits") or {}
    selected = by_id.get(bucket) or (legacy if legacy.get("limitId") in (bucket, None) and bucket == "codex" else {})
    return [
        {
            "source": source,
            "window": name,
            "used_percent": window["usedPercent"],
            "resets_at": window["resetsAt"],
            "observed_at": observation_time,
        }
        for name in ("primary", "secondary")
        if isinstance(window := selected.get(name), dict)
        and isinstance(window.get("resetsAt"), int)
        and window["resetsAt"] > observation_time
        and isinstance(window.get("usedPercent"), (int, float))
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--bucket", default="codex")
    parser.add_argument("--proxy-url", help="Omit to print the quota snapshots")
    options = parser.parse_args()
    values = snapshots(asyncio.run(read_quota()), options.source, options.bucket, int(time()))
    if not values:
        raise SystemExit("No current subscription windows were reported")
    if not options.proxy_url:
        print(json.dumps(values))
        return
    key = os.environ["LITELLM_QUOTA_ADMIN_KEY"]
    for value in values:
        request = Request(
            options.proxy_url.rstrip("/") + "/subscription-quota/snapshot",
            data=json.dumps(value).encode(),
            method="PUT",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        with urlopen(request, timeout=10):
            pass


if __name__ == "__main__":
    main()
