from __future__ import annotations
import json
import logging
from typing import Callable, Any
from .client import SimClient
from .db import DB

logger = logging.getLogger("controller.ingest")

class Ingestor:
    """Backlog-safe, idempotent, restartable event ingestion."""

    def __init__(self, client: SimClient, db: DB, on_event: Callable[[dict[str, Any]], None] | None = None):
        self.client = client
        self.db = db
        self.on_event = on_event

    def bootstrap_history(self):
        """Ingests pre-D0 history chunks if not already processed."""
        if self.db.get_meta("history_bootstrapped") == "1":
            return
        
        last_chunk_id = self.db.get_meta_int("last_chunk_id", -1)
        max_event_id = self.db.get_meta_int("max_event_id", 0)
        
        history_chunks = self.client.list_chunks(kind="history", after=-1)
        for ch in sorted(history_chunks, key=lambda x: x["chunk"]):
            chunk_no = ch["chunk"]
            if chunk_no <= last_chunk_id:
                continue
            max_event_id = self._process_chunk(chunk_no, max_event_id, is_history=True)
            self.db.record_cursor(chunk_no, max_event_id, sim_minute=0)
            last_chunk_id = chunk_no
            
        self.db.set_meta("history_bootstrapped", "1")
        logger.info(f"History bootstrapped: max_event_id={max_event_id}")

    def ingest_new_chunks(self, new_chunks: list[dict], sim_minute: int):
        """Processes chunks provided by sim advance or catches up on missing chunks."""
        last_chunk_id = self.db.get_meta_int("last_chunk_id", -1)
        max_event_id = self.db.get_meta_int("max_event_id", 0)

        # Reconcile if there are gaps (e.g. after crash recovery)
        if not new_chunks:
            # Query simulator for any unseen event chunks
            unseen = self.client.list_chunks(kind="events", after=last_chunk_id)
            chunks_to_process = sorted(unseen, key=lambda x: x["chunk"])
        else:
            chunks_to_process = sorted(new_chunks, key=lambda x: x["chunk"])

        for ch in chunks_to_process:
            chunk_no = ch["chunk"]
            if chunk_no <= last_chunk_id:
                continue
            max_event_id = self._process_chunk(chunk_no, max_event_id, is_history=False)
            self.db.record_cursor(chunk_no, max_event_id, sim_minute)
            last_chunk_id = chunk_no

    def _process_chunk(self, chunk_no: int, current_max_event_id: int, is_history: bool) -> int:
        """Streams chunk NDJSON, deduplicates by event_id, dispatches events, and flushes leads."""
        leads_buffer = []
        max_event_id = current_max_event_id

        for line in self.client.iter_chunk(chunk_no):
            ev = json.loads(line)
            eid = ev.get("event_id")
            if eid is not None:
                if eid <= max_event_id:
                    continue  # Deduplication: already processed
                max_event_id = max(max_event_id, eid)

            if ev.get("type") == "lead":
                feat = ev.get("features", {})
                leads_buffer.append((
                    ev["lead_id"],
                    ev.get("person_id", ""),
                    ev.get("workflow_id", ""),
                    ev.get("campaign_id", ""),
                    feat.get("lang"),
                    feat.get("city_tier"),
                    feat.get("age_band"),
                    feat.get("value_band")
                ))
                if len(leads_buffer) >= 1000:
                    self.db.insert_leads_batch(leads_buffer)
                    leads_buffer = []

            if self.on_event:
                self.on_event(ev)

        if leads_buffer:
            self.db.insert_leads_batch(leads_buffer)

        return max_event_id
