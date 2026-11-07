from __future__ import annotations
import tempfile
import os
import pytest
from controller.db import DB
from controller.audit import AuditStore, parse_time_spec, format_sim_minute

def test_time_parsing():
    assert parse_time_spec("D0 00:00") == 0
    assert parse_time_spec("D3 14:00") == 3 * 1440 + 14 * 60  # 5160
    assert parse_time_spec("5160") == 5160
    assert format_sim_minute(5160) == "D3 14:00"

def test_audit_lifecycle_and_explanation():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        db = DB(db_path)
        audit = AuditStore(db)

        # 1. Detect at D3 10:00 (minute 4920)
        audit.record_step(
            sim_minute=4920,
            step="detect",
            workflow_id="nw_renewal",
            details={"observation": "Voice V2 achieved statistically significant lift (+15.2%, p=0.002)"}
        )

        # 2. Propose at D3 11:00 (minute 4980)
        audit.record_step(
            sim_minute=4980,
            step="propose",
            workflow_id="nw_renewal",
            details={
                "proposal_id": "prop_001",
                "approval_id": "apr_100",
                "summary": "Promote Voice V2 on senior_ta cohort",
                "diff": {"allocations.senior_ta.voice": {"V2": 1.0}},
                "impact_score": 0.45,
                "rationale": "High engagement lift with stable pickup guardrail"
            }
        )

        # 3. Approve at D3 13:30 (minute 5130)
        audit.record_step(
            sim_minute=5130,
            step="approve",
            workflow_id="nw_renewal",
            details={
                "approval_id": "apr_100",
                "status": "approved",
                "approver": "client_supervisor (human approval)",
                "reasons": []
            }
        )

        # 4. Apply at D3 14:00 (minute 5160)
        audit.record_step(
            sim_minute=5160,
            step="apply",
            workflow_id="nw_renewal",
            details={
                "approval_id": "apr_100",
                "proposal_id": "prop_001",
                "version": 4,
                "changed_dims": ["voice"],
                "actor": "controller_approved"
            }
        )

        # 5. Outcome at D3 18:00 (minute 5400)
        audit.record_step(
            sim_minute=5400,
            step="outcome",
            workflow_id="nw_renewal",
            details={
                "version": 4,
                "observed_engagement": 0.68,
                "pre_change_engagement": 0.55,
                "realized_lift": "+23.6%",
                "verdict": "beneficial"
            }
        )

        # Query explanation for time D3 14:00
        explanation = audit.explain("nw_renewal", "D3 14:00")
        assert explanation["workflow_id"] == "nw_renewal"
        assert explanation["change_applied_at"] == "D3 14:00"
        assert explanation["applied_change"]["version"] == 4
        assert "Voice V2" in explanation["proposal"]["summary"]
        assert explanation["decision"]["status"] == "approved"
        assert explanation["outcome"]["verdict"] == "beneficial"

        report = audit.format_explanation(explanation)
        assert "Compliance Audit Report: nw_renewal @ D3 14:00" in report
        assert "Who Approved It" in report

        db.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)
