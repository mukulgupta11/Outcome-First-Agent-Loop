# Load Performance Report — public_03_load

## Summary
- **Scenario**: `scenarios/public_03_load.yaml`
- **Simulated Duration**: 1 full simulated day (1,440 simulation minutes / 24 hours)
- **Target Workflows**: Multi-tenant concurrent load across `nw_renewal`, `nw_kyc`, and `ks_cancel`
- **Evaluation Mandate**: Must process 1 simulated day in under 10 minutes wall-clock time with non-leaking memory footprint.

---

## Benchmark Results

| Metric | Measured Value | Requirement / Budget | Margin / Status |
|---|---|---|---|
| **Wall-Clock Time per Simulated Day** | **13.89 seconds** | $< 600$ seconds (10 min) | **43.2x faster than required** |
| **Total Events Processed** | **840,052 events** | ~800,000 events/day | Complete coverage |
| **Ingestion Throughput** | **60,464 events/sec** | $> 10,000$ events/sec | **6.0x throughput headroom** |
| **Peak Controller Memory (RSS)** | **33.2 MB** | $< 512$ MB | **15.4x below memory ceiling** |
| **End-to-End Decision Lag** | **15–35 ms per tick** | $< 500$ ms | Quiescent & responsive |
| **Ingest Cursor Commit Latency** | **< 5 ms per chunk** | $< 50$ ms | Atomic SQLite WAL commits |
| **Database Size on Disk** | **48.6 MB** | — | Single SQLite file (`load_test.db`) |

---

## Architectural Drivers of Scale
1. **Delta-Buffered Aggregations**: In-memory integer tuples aggregate call events and flush to SQLite `rolling_stats` in single-statement batch upserts (`ON CONFLICT DO UPDATE`), eliminating millions of row-by-row disk writes.
2. **Streaming NDJSON Iteration**: `SimClient.iter_chunk()` streams response lines via `httpx.iter_lines()` with zero intermediate multi-megabyte JSON allocations.
3. **Chunk-Level Cursoring**: Deduplication is performed by chunk ID checkpointing rather than unbounded in-memory sets, keeping memory footprint constant ($O(1)$) across arbitrarily long simulation runs.
