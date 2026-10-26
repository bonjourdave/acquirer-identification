"""Append-only JSONL traces, one line per span.

Field names follow OpenTelemetry GenAI conventions where one exists, so the file could be shipped
to a tracing backend later without renaming.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

_RENAMES = {
    "model": "gen_ai.request.model",
    "response_model": "gen_ai.response.model",
    "input_tokens": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
}


class Tracer:
    def __init__(self, path: Path, run_id: str, attributes: dict | None = None) -> None:
        self.path = path
        self.run_id = run_id
        self.attributes = attributes or {}
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def span(self, name: str, context: dict, **fields: object) -> str:
        span_id = uuid.uuid4().hex[:16]
        record = {
            "trace_id": self.run_id,
            "span_id": span_id,
            "name": name,
            "timestamp": datetime.now(UTC).isoformat(),
            **self.attributes,
            **{_RENAMES.get(k, k): v for k, v in (context | fields).items()},
        }
        line = json.dumps(record, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return span_id
