"""Synchronous persistent JSONL logging, without crash-durability guarantees."""

from dataclasses import fields
import json
from pathlib import Path

from . import Event


class JsonlRecorder:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._failed = False

    def record(self, event: Event) -> None:
        if self._failed:
            raise RuntimeError("recorder is unusable after a recording failure")
        try:
            if type(event) is not Event:
                raise TypeError("expected Event")
            values = {field.name: getattr(event, field.name) for field in fields(Event)}
            line = (json.dumps(values, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
            with self._path.open("ab") as stream:
                if stream.write(line) != len(line):
                    raise OSError("short event write")
                stream.flush()
        except BaseException:
            self._failed = True
            raise
