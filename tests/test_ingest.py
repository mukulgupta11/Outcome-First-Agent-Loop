from __future__ import annotations
import os
import tempfile
import pytest
from controller.db import DB
from controller.ingest import Ingestor

class MockSimClient:
    def __init__(self, chunks: dict[int, list[str]], history_chunks: list[int] | None = None):
        self.chunks = chunks
        self.history_chunks = history_chunks or []

    def list_chunks(self, kind: str = "events", after: int = -1):
        if kind == "history":
            return [{"chunk": c, "kind": "history"} for c in sorted(self.history_chunks) if c > after]
        return [{"chunk": c, "kind": "events"} for c in sorted(self.chunks.keys()) if c > after]

    def iter_chunk(self, n: int):
        for line in self.chunks.get(n, []):
            yield line

def test_ingestion_and_dedupe():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        db = DB(db_path)
        events_processed = []

        # Chunk 0 has 2 events, Chunk 1 has 2 events (one replayed event_id=2 and one new event_id=3)
        chunk0 = [
            '{"type": "lead", "lead_id": "l1", "workflow_id": "w1", "event_id": 1}',
            '{"type": "call", "lead_id": "l1", "workflow_id": "w1", "connected": true, "event_id": 2}'
        ]
        chunk1 = [
            '{"type": "call", "lead_id": "l1", "workflow_id": "w1", "connected": true, "event_id": 2}',
            '{"type": "call", "lead_id": "l1", "workflow_id": "w1", "connected": false, "event_id": 3}'
        ]
        client = MockSimClient({0: chunk0, 1: chunk1})
        ingestor = Ingestor(client, db, on_event=lambda ev: events_processed.append(ev))

        # Ingest chunk 0
        ingestor.ingest_new_chunks([{"chunk": 0}], sim_minute=15)
        assert len(events_processed) == 2
        assert db.get_meta_int("last_chunk_id") == 0
        assert db.get_meta_int("max_event_id") == 2

        # Ingest chunk 1 (contains duplicate event_id=2)
        ingestor.ingest_new_chunks([{"chunk": 1}], sim_minute=30)
        assert len(events_processed) == 3  # event_id=2 was skipped!
        assert db.get_meta_int("last_chunk_id") == 1
        assert db.get_meta_int("max_event_id") == 3

        # Restart simulation: new ingestor with same DB should skip already processed chunks
        events_processed.clear()
        restarted_ingestor = Ingestor(client, db, on_event=lambda ev: events_processed.append(ev))
        restarted_ingestor.ingest_new_chunks([{"chunk": 0}, {"chunk": 1}], sim_minute=30)
        assert len(events_processed) == 0  # Zero re-processed events!

        db.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)
