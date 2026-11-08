# Outcome-First Agent Loop (Formi)
Autonomous, self-improving controller loop for regulated outbound voice agents across Indian BFSI workflows.
- **Run Controller**: `./run.sh <SIM_URL>` (advances simulation, ingests NDJSON events, evaluates cohorts, and applies governed changes).
- **Submission & Audit**: See [`SUBMISSION.md`](./SUBMISSION.md) for full design, stakeholder memos, audit reports, and benchmarks.
- **Test & Reports**: Run unit tests via `PYTHONPATH=. .venv/bin/pytest tests/` and inspect benchmark reports in [`reports/`](./reports/).
