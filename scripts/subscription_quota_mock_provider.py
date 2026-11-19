from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import time
from uuid import uuid4
from contextlib import closing
from datetime import datetime, timedelta, timezone
import os
import sqlite3
import threading
from pathlib import Path
from urllib.request import Request, urlopen

SOURCE = "Test subscription"
USAGE_FILE = Path("/state/mock-usage.sqlite")
WEEKLY_REQUESTS = 2000
GATEWAY = "http://127.0.0.1:18774"


def week_ends_at(now):
    today = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((today + timedelta(days=7 - today.weekday())).timestamp())


def record_usage():
    now = time.time()
    start = week_ends_at(now) - 7 * 86400
    with closing(sqlite3.connect(USAGE_FILE)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        count = db.execute("SELECT COUNT(*) FROM usage WHERE at >= ?", (start,)).fetchone()[0]
        if count >= WEEKLY_REQUESTS:
            return False
        db.execute("INSERT INTO usage VALUES (?)", (now,))
        return True


def publish_usage():
    auth = {"Authorization": "Bearer " + os.environ["LITELLM_MASTER_KEY"], "Content-Type": "application/json"}
    while True:
        now = int(time.time())
        reset = week_ends_at(now)
        start = reset - 7 * 86400
        with closing(sqlite3.connect(USAGE_FILE)) as db, db:
            count = db.execute("SELECT COUNT(*) FROM usage WHERE at >= ? AND at <= ?", (start, now - 5)).fetchone()[0]
            db.execute("DELETE FROM usage WHERE at < ?", (start,))
        payload = json.dumps({
            "source": SOURCE, "window": "Weekly", "used_percent": min(100, count * 100 / WEEKLY_REQUESTS),
            "resets_at": reset, "observed_at": now,
        }).encode()
        try:
            with urlopen(Request(GATEWAY + "/subscription-quota/snapshot", data=payload,
                                 headers=auth, method="PUT"), timeout=7):
                pass
        except OSError as exc:
            print(f"Quota update deferred ({type(exc).__name__})", flush=True)
        time.sleep(20)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not record_usage():
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": "Test subscription quota exhausted",
                                                  "type": "rate_limit_error"}}).encode())
            return
        rid = uuid4().hex
        text = "Это локальный тестовый ответ LiteLLM. Внешняя подписка пока не подключена."
        item = {
            "id": "msg_" + rid, "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type":"output_text", "text":text, "annotations":[]}],
        }
        usage = {
            "input_tokens": 10, "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 20, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 30,
        }
        if self.path.endswith("/responses"):
            response = {
                "id": "resp_" + rid, "object": "response", "created_at": int(time.time()),
                "status": "completed", "model": "gpt-4o-mini", "output": [item], "usage": usage,
                "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
            }
            if payload.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for event in (
                    {"type":"response.created", "sequence_number":0, "response":response | {"status":"in_progress","output":[],"usage":None}},
                    {"type":"response.output_text.delta","sequence_number":1,"item_id":item["id"],"output_index":0,"content_index":0,"delta":text,"logprobs":[]},
                    {"type":"response.completed", "sequence_number":2, "response":response},
                ):
                    self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
                return
        else:
            response = {
                "id": "chatcmpl-" + rid, "object": "chat.completion", "created": int(time.time()), "model": "gpt-4o-mini",
                "choices": [{"index":0, "message":{"role":"assistant","content":text}, "finish_reason":"stop"}],
                "usage": {"prompt_tokens":10,"completion_tokens":20,"total_tokens":30},
            }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())


if __name__ == "__main__":
    with closing(sqlite3.connect(USAGE_FILE)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS usage (at REAL NOT NULL)")
    threading.Thread(target=publish_usage, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 18773), Handler).serve_forever()
