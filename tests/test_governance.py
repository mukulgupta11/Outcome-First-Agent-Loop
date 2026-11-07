from __future__ import annotations
import tempfile
import os
import pytest
from controller.db import DB
from controller.governance import GovernanceClient, compute_changed_dims, compute_max_shift

class MockClient:
    def __init__(self):
        self.approvals = {}
        self.auto_applies = []

    def request_approval(self, body: dict):
        aid = f"apr_{len(self.approvals)+1:03d}"
        self.approvals[aid] = {"status": "pending", "body": body}
        return {"status_code": 200, "data": {"approval_id": aid, "status": "pending"}}

    def get_approval(self, aid: str):
        if aid in self.approvals:
            return {"status_code": 200, "data": {"approval_id": aid, "status": self.approvals[aid]["status"]}}
        return {"status_code": 404, "data": {}}

    def apply_approval(self, wid: str, aid: str):
        if aid in self.approvals and self.approvals[aid]["status"] == "approved":
            return {"status_code": 200, "data": {"version": 2}}
        return {"status_code": 409, "data": {}}

    def auto_apply(self, wid: str, body: dict):
        self.auto_applies.append(body)
        return {"status_code": 200, "data": {"version": 2}}

def test_tv_distance_and_changed_dims():
    old = {
        "cohorts": [],
        "allocations": {"default": {"voice": {"V1": 1.0}, "caller_id": {"D1": 0.5, "D2": 0.5}}},
        "prompt_version": "P1",
        "schedule": {}
    }
    new_cid = {
        "cohorts": [],
        "allocations": {"default": {"voice": {"V1": 1.0}, "caller_id": {"D1": 1.0}}},
        "prompt_version": "P1",
        "schedule": {}
    }
    # Caller ID changed; shift is 0.5 * (|0.5 - 1.0| + |0.5 - 0.0|) = 0.5
    assert compute_changed_dims(old, new_cid) == ["caller_id"]
    assert compute_max_shift(old, new_cid) == 0.5

    new_voice = {
        "cohorts": [],
        "allocations": {"default": {"voice": {"V2": 1.0}, "caller_id": {"D1": 0.5, "D2": 0.5}}},
        "prompt_version": "P1",
        "schedule": {}
    }
    assert compute_changed_dims(old, new_voice) == ["voice"]
    assert compute_max_shift(old, new_voice) == 1.0

def test_governance_approver_hours_and_fatigue():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        db = DB(db_path)
        client = MockClient()
        gov = GovernanceClient(client, db)

        wf_spec = {
            "governance": {
                "approver_hours": [10, 19],
                "max_requests_per_24h": 2,
                "auto_apply": {"enabled": True, "allowed_dims": ["caller_id"], "max_shift": 0.5}
            }
        }
        gov.load_policy("w1", wf_spec)

        # 08:00 IST (minute 480): Outside approver hours
        assert not gov.is_approver_active("w1", sim_minute=480)
        assert not gov.can_submit_request("w1", sim_minute=480)

        # 11:00 IST (minute 660): Inside approver hours
        assert gov.is_approver_active("w1", sim_minute=660)
        assert gov.can_submit_request("w1", sim_minute=660)

        # Submit request 1
        prop1 = {
            "proposal_id": "p1", "workflow_id": "w1", "base_version": 1,
            "proposed_config": {}, "change_summary": "change voice"
        }
        assert gov.submit_proposal(prop1, sim_minute=660)

        # Cannot submit second request while first is pending
        assert not gov.can_submit_request("w1", sim_minute=675)

        # Mark first request approved and apply it
        aid = list(client.approvals.keys())[0]
        client.approvals[aid]["status"] = "approved"
        applied = gov.apply_pending_approvals(sim_minute=700)
        assert applied == ["w1"]

        # Now can submit request 2
        prop2 = {
            "proposal_id": "p2", "workflow_id": "w1", "base_version": 2,
            "proposed_config": {}, "change_summary": "change welcome"
        }
        assert gov.can_submit_request("w1", sim_minute=720)
        assert gov.submit_proposal(prop2, sim_minute=720)

        # Fatigue limit reached: 2 requests in 24h
        aid2 = list(client.approvals.keys())[1]
        client.approvals[aid2]["status"] = "approved"
        gov.apply_pending_approvals(sim_minute=750)
        assert not gov.can_submit_request("w1", sim_minute=760)

        db.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)

def test_caller_id_rotation_with_auto_apply():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        db = DB(db_path)
        client = MockClient()
        gov = GovernanceClient(client, db)

        wf_spec = {
            "options": {"caller_id": ["D1", "D2", "D3"]},
            "daily_batch": {"size": 1000},
            "governance": {
                "approver_hours": [10, 19],
                "max_requests_per_24h": 4,
                "auto_apply": {"enabled": True, "allowed_dims": ["caller_id"], "max_shift": 0.2}
            }
        }
        gov.load_policy("w1", wf_spec)

        cur_cfg = {
            "version": 3,
            "allocations": {"default": {"caller_id": {"D1": 0.5, "D2": 0.5}}},
            "prompt_version": "P1"
        }

        # D2 degraded: should auto-apply 0.2 shift away from D2, then propose full removal
        prop = gov.handle_caller_id_rotation("w1", "D2", wf_spec, cur_cfg, sim_minute=660)
        assert prop is not None
        assert len(client.auto_applies) == 1
        assert client.auto_applies[0]["base_version"] == 3
        # Auto-apply bumped version to 4
        assert prop["base_version"] == 4
        assert prop["diff"]["allocations.default.caller_id"] == {"D1": 0.5, "D3": 0.5}

        # Check audit log recorded auto_applied
        audits = db.query_audit(workflow_id="w1", event_type="auto_applied")
        assert len(audits) == 1
        assert audits[0]["details"]["changed_dims"] == ["caller_id"]

        db.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)

def test_supervisor_mid_flight_edit_and_stale_base_reproposal():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        db = DB(db_path)
        
        class LoopMockClient(MockClient):
            def workflows(self):
                return [{"workflow_id": "w1", "options": {"voice": ["V1", "V2"]}, "governance": {"approver_hours": [10, 19], "max_requests_per_24h": 4, "auto_apply": {"enabled": False}}}]
            def get_config(self, wid: str):
                return {
                    "version": 2,
                    "config": {
                        "cohorts": [],
                        "allocations": {"default": {"voice": {"V1": 1.0}}},
                        "prompt_version": "P2",  # Supervisor updated prompt_version to P2
                        "schedule": {}
                    }
                }
            def advance(self, step: int):
                return {"sim_minute": 600, "new_chunks": [], "finished": False}

        client = LoopMockClient()
        client.approvals["apr_001"] = {"status": "pending"}
        from controller.loop import ControllerLoop
        loop = ControllerLoop(client, db)
        loop.workflows = {"w1": client.workflows()[0]}
        loop.governance.load_policy("w1", loop.workflows["w1"])

        # 1. Controller creates a pending proposal on voice based on version 1
        db.save_pending_approval({
            "approval_id": "apr_001",
            "proposal_id": "prop_001",
            "workflow_id": "w1",
            "base_version": 1,
            "proposed_config": {
                "cohorts": [],
                "allocations": {"default": {"voice": {"V1": 0.7, "V2": 0.3}}},
                "prompt_version": "P1",
                "schedule": {}
            },
            "created_minute": 600,
            "expires_minute": 1200,
            "status": "pending",
            "summary": "A/B test voice on default: 70% V1 vs 30% V2",
            "diff": {"allocations.default.voice": {"V1": 0.7, "V2": 0.3}}
        })

        # 2. Supervisor edits prompt_version mid-experiment, advancing live config to version 2
        loop.handle_event({
            "type": "config_changed",
            "workflow_id": "w1",
            "version": 2,
            "actor": "supervisor",
            "changed_dims": ["prompt_version"]
        })

        # Old approval should be marked superseded
        old_app = db.get_approval("apr_001")
        assert old_app["status"] == "superseded_by_stale_base"

        # Audit log must show stale_base_reproposed
        audits = db.query_audit(workflow_id="w1", event_type="stale_base_reproposed")
        assert len(audits) == 1
        assert audits[0]["details"]["old_approval_id"] == "apr_001"
        assert audits[0]["details"]["new_base_version"] == 2

        # A new approval proposal should have been submitted with base_version = 2
        active = db.get_active_approvals()
        assert len(active) == 1
        assert active[0]["base_version"] == 2
        assert active[0]["approval_id"] != "apr_001"

        db.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)
