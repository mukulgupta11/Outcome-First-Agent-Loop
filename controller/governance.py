from __future__ import annotations
import copy
import logging
import uuid
from typing import Any
from .client import SimClient
from .db import DB

logger = logging.getLogger("controller.governance")

AGENT_DIMS = ("welcome_message", "voice", "caller_id")
ALL_DIMS = ("cohorts", "welcome_message", "voice", "caller_id", "prompt_version", "schedule")
VALID_CONFIG_KEYS = {"cohorts", "allocations", "prompt_version", "schedule"}

def clean_config(cfg: dict) -> dict:
    """Strips any non-simulator keys (e.g. version) from config object."""
    return {k: copy.deepcopy(v) for k, v in cfg.items() if k in VALID_CONFIG_KEYS}

def _normalize_dist(cfg: dict, cohort_id: str, dim: str) -> dict[str, float]:
    allocs = cfg.get("allocations", {})
    dist = allocs.get(cohort_id, {}).get(dim) or allocs.get("default", {}).get(dim) or {}
    total = sum(dist.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in dist.items()}

def compute_changed_dims(old: dict, new: dict) -> list[str]:
    ch = []
    if old.get("cohorts") != new.get("cohorts"):
        ch.append("cohorts")
    keys = set(old.get("allocations", {}).keys()) | set(new.get("allocations", {}).keys())
    for dim in AGENT_DIMS:
        if any(_normalize_dist(old, k, dim) != _normalize_dist(new, k, dim) for k in keys):
            ch.append(dim)
    if old.get("prompt_version") != new.get("prompt_version"):
        ch.append("prompt_version")
    if old.get("schedule") != new.get("schedule"):
        ch.append("schedule")
    return [d for d in ALL_DIMS if d in ch]

def compute_max_shift(old: dict, new: dict) -> float:
    keys = {"default"} | {c["id"] for c in old.get("cohorts", [])} | {c["id"] for c in new.get("cohorts", [])}
    worst = 0.0
    for k in keys:
        for dim in AGENT_DIMS:
            a, b = _normalize_dist(old, k, dim), _normalize_dist(new, k, dim)
            all_opts = set(a) | set(b)
            tv = 0.5 * sum(abs(a.get(o, 0.0) - b.get(o, 0.0)) for o in all_opts)
            worst = max(worst, tv)
    return worst

class GovernanceClient:
    """Manages approvals lifecycle, fatigue batching, stale-base reconciliation, and auto-apply."""

    def __init__(self, client: SimClient, db: DB):
        self.client = client
        self.db = db
        self.policies: dict[str, dict] = {}
        # workflow_id -> list of submission sim_minutes in the last 24h
        self.request_history: dict[str, list[int]] = {}

    def load_policy(self, workflow_id: str, wf_spec: dict):
        self.policies[workflow_id] = wf_spec.get("governance", {})

    def is_approver_active(self, workflow_id: str, sim_minute: int) -> bool:
        """Checks if current simulated time is within approver working hours."""
        gov = self.policies.get(workflow_id, {})
        approver_hours = gov.get("approver_hours", [10, 19])
        hour_ist = (sim_minute // 60) % 24
        return approver_hours[0] <= hour_ist < approver_hours[1]

    def can_submit_request(self, workflow_id: str, sim_minute: int) -> bool:
        """Enforces approver hours, zero concurrent requests, and 24h fatigue limits."""
        if not self.is_approver_active(workflow_id, sim_minute):
            return False

        # No concurrent pending requests for the same workflow
        active = [a for a in self.db.get_active_approvals() if a["workflow_id"] == workflow_id]
        if active:
            return False

        # Approver fatigue: maximum requests per 24 hours
        gov = self.policies.get(workflow_id, {})
        max_24h = gov.get("max_requests_per_24h", 3)
        history = self.request_history.setdefault(workflow_id, [])
        # Prune older than 24h (1440 min)
        history[:] = [t for t in history if t > sim_minute - 1440]
        return len(history) < min(max_24h, 2)  # Cap at 2 per Ops supervisor requirement

    def try_auto_apply(self, workflow_id: str, cur_cfg: dict, proposed_cfg: dict,
                       rationale: str, evidence: dict | None = None) -> bool:
        """Attempts auto-apply if policy enables it, dimensions are allowed, and TV shift <= max_shift."""
        gov = self.policies.get(workflow_id, {})
        auto_pol = gov.get("auto_apply", {})
        if not auto_pol.get("enabled"):
            return False

        changed = compute_changed_dims(cur_cfg, proposed_cfg)
        if not changed:
            return False

        allowed_dims = auto_pol.get("allowed_dims", [])
        if any(d not in allowed_dims for d in changed):
            return False

        shift = compute_max_shift(cur_cfg, proposed_cfg)
        max_shift = auto_pol.get("max_shift", 0.0)
        if shift > max_shift + 1e-9:
            return False

        # Execute auto-apply
        pid = str(uuid.uuid4())
        body = {
            "proposal_id": pid,
            "base_version": cur_cfg.get("version", 1),
            "proposed_config": clean_config(proposed_cfg),
            "rationale": rationale,
            "evidence": evidence or {}
        }
        res = self.client.auto_apply(workflow_id, body)
        if res["status_code"] == 200:
            self.db.log_audit(
                sim_minute=self.db.get_meta_int("sim_minute", 0),
                event_type="auto_applied",
                workflow_id=workflow_id,
                details={"proposal_id": pid, "changed_dims": changed, "shift": shift, "rationale": rationale}
            )
            return True
        return False

    def submit_proposal(self, proposal: dict, sim_minute: int) -> bool:
        """Submits proposal to /approvals and tracks in SQLite."""
        wid = proposal["workflow_id"]
        clean_prop = {k: v for k, v in proposal.items() if k not in ("action_type", "exp_key")}
        clean_prop["proposed_config"] = clean_config(clean_prop["proposed_config"])
        res = self.client.request_approval(clean_prop)
        if res["status_code"] == 200:
            aid = res["data"]["approval_id"]
            self.db.save_pending_approval({
                "approval_id": aid,
                "proposal_id": clean_prop["proposal_id"],
                "workflow_id": wid,
                "base_version": clean_prop["base_version"],
                "proposed_config": clean_prop["proposed_config"],
                "created_minute": sim_minute,
                "expires_minute": sim_minute + int(self.policies.get(wid, {}).get("proposal_ttl_hours", 24) * 60),
                "status": "pending",
                "summary": clean_prop.get("change_summary", ""),
                "diff": clean_prop.get("diff", {})
            })
            self.request_history.setdefault(wid, []).append(sim_minute)
            self.db.log_audit(
                sim_minute=sim_minute,
                event_type="approval_requested",
                workflow_id=wid,
                details={"approval_id": aid, "summary": clean_prop.get("change_summary")}
            )
            return True
        elif res["status_code"] == 409:
            # Stale base or duplicate proposal id
            logger.warning(f"Proposal rejected 409: {res['data']}")
            return False
        else:
            logger.warning(f"Proposal failed {res['status_code']}: {res['data']}")
            return False

    def apply_pending_approvals(self, sim_minute: int) -> list[str]:
        """Polls and applies any approved requests within the apply window."""
        applied = []
        active = self.db.get_active_approvals()
        for app in active:
            aid = app["approval_id"]
            wid = app["workflow_id"]
            res = self.client.get_approval(aid)
            if res["status_code"] != 200:
                continue

            status = res["data"].get("status")
            if status == "approved":
                apply_res = self.client.apply_approval(wid, aid)
                if apply_res["status_code"] == 200:
                    self.db.update_approval_status(aid, "applied")
                    self.db.log_audit(
                        sim_minute=sim_minute,
                        event_type="approval_applied",
                        workflow_id=wid,
                        details={"approval_id": aid, "applied_version": apply_res["data"].get("version")}
                    )
                    applied.append(wid)
                elif apply_res["status_code"] == 409:
                    self.db.update_approval_status(aid, "stale")
                elif apply_res["status_code"] == 410:
                    self.db.update_approval_status(aid, "lapsed")
            elif status in ("rejected", "expired", "lapsed"):
                self.db.update_approval_status(aid, status)
                self.db.log_audit(
                    sim_minute=sim_minute,
                    event_type=f"approval_{status}",
                    workflow_id=wid,
                    details={"approval_id": aid, "reasons": res["data"].get("reasons")}
                )
        return applied

    def handle_hallucination_fix(self, workflow_id: str, prompt_version: str, cur_cfg: dict, sim_minute: int) -> dict | None:
        """Suggests prompt fixes via LLM, validates them, and packages proposal with evidence."""
        gov = self.policies.get(workflow_id, {})
        min_pass = gov.get("min_validation_pass_rate") or 0.97

        # 1. Ask LLM for suggested fixes (costs 3 credits)
        suggest_res = self.client.suggest_fixes(workflow_id, f"Prompt {prompt_version} has elevated hallucination rate")
        if suggest_res["status_code"] != 200:
            return None

        candidates = suggest_res["data"].get("suggestions", [])
        if not candidates:
            return None

        # 2. Validate candidates with n=100 conversations (costs 1 credit each)
        best_candidate = None
        best_run_id = None
        best_pass = 0.0

        for cand in candidates:
            cand_p = cand.get("prompt_version")
            if not cand_p:
                continue
            val_res = self.client.run_validation(workflow_id, cand_p, n=100)
            if val_res["status_code"] == 200:
                pass_rate = val_res["data"].get("pass_rate", 0.0)
                run_id = val_res["data"].get("validation_run_id")
                if pass_rate >= min_pass and pass_rate > best_pass:
                    best_pass = pass_rate
                    best_candidate = cand_p
                    best_run_id = run_id

        if not best_candidate or not best_run_id:
            return None

        # 3. Formulate proposed config
        new_cfg = copy.deepcopy(cur_cfg)
        new_cfg["prompt_version"] = best_candidate

        batch_size = 1000
        return {
            "proposal_id": str(uuid.uuid4()),
            "workflow_id": workflow_id,
            "base_version": cur_cfg.get("version", 1),
            "proposed_config": new_cfg,
            "change_summary": f"Incident fix: replace hallucinating prompt {prompt_version} with validated {best_candidate}",
            "diff": {"prompt_version": best_candidate},
            "affected_leads": batch_size,
            "dry_run": {"validation_pass_rate": best_pass, "target_prompt": best_candidate},
            "impact_score": 0.8,
            "rationale": f"LLM fix validated with pass_rate={best_pass:.3f} >= {min_pass:.3f}",
            "evidence": {"validation_run_id": best_run_id},
            "action_type": "prompt_incident_fix"
        }

    def handle_caller_id_rotation(self, workflow_id: str, degraded_did: str, wf_spec: dict, cur_cfg: dict, sim_minute: int) -> dict | None:
        """Rotates away from degraded caller ID: auto-applies immediate partial shift up to max_shift, then proposes full removal."""
        options = wf_spec.get("options", {}).get("caller_id", [])
        healthy = [d for d in options if d != degraded_did]
        if not healthy:
            return None

        gov = self.policies.get(workflow_id, {})
        auto_pol = gov.get("auto_apply", {})
        max_shift = auto_pol.get("max_shift", 0.0)

        auto_applied = False
        auto_cfg = None
        base_ver = cur_cfg.get("version", 1)

        # 1. Immediate partial auto-apply within policy max_shift if permitted
        if auto_pol.get("enabled") and "caller_id" in auto_pol.get("allowed_dims", []):
            cur_alloc = cur_cfg.get("allocations", {}).get("default", {}).get("caller_id") or {}
            cur_deg_weight = cur_alloc.get(degraded_did, 0.5)
            if cur_deg_weight > 0.05 and max_shift > 0:
                shift_amount = min(cur_deg_weight, max_shift)
                auto_cfg = clean_config(cur_cfg)
                auto_alloc = copy.deepcopy(cur_alloc)
                auto_alloc[degraded_did] = max(0.0, cur_deg_weight - shift_amount)
                healthy_bonus = shift_amount / len(healthy)
                for h_did in healthy:
                    auto_alloc[h_did] = auto_alloc.get(h_did, 0.0) + healthy_bonus
                tot = sum(auto_alloc.values())
                auto_alloc = {k: v / tot for k, v in auto_alloc.items()}
                auto_cfg["allocations"]["default"]["caller_id"] = auto_alloc
                
                auto_applied = self.try_auto_apply(
                    workflow_id, cur_cfg, auto_cfg,
                    f"Immediate auto-apply: reduce degraded caller-ID {degraded_did} by {shift_amount:.2f}",
                    evidence={"degraded_did": degraded_did}
                )
                if auto_applied:
                    base_ver += 1

        # 2. Package proposal for full rotation to 100% healthy DIDs via approvals
        base_for_new = auto_cfg if (auto_applied and auto_cfg) else cur_cfg
        new_cfg = clean_config(base_for_new)
        new_alloc = {did: round(1.0 / len(healthy), 3) for did in healthy}
        tot = sum(new_alloc.values())
        new_alloc = {k: v / tot for k, v in new_alloc.items()}
        new_cfg["allocations"]["default"]["caller_id"] = new_alloc

        batch_size = wf_spec.get("daily_batch", {}).get("size", 1000)
        return {
            "proposal_id": str(uuid.uuid4()),
            "workflow_id": workflow_id,
            "base_version": base_ver,
            "proposed_config": new_cfg,
            "change_summary": f"Emergency rotation away from degraded caller-ID {degraded_did}",
            "diff": {"allocations.default.caller_id": new_cfg["allocations"]["default"]["caller_id"]},
            "affected_leads": batch_size,
            "dry_run": {"degraded_did": degraded_did, "healthy_dids": healthy, "target_allocation": new_alloc},
            "impact_score": 0.6,
            "rationale": f"Remove degraded caller-ID {degraded_did} and allocate to {healthy}",
            "action_type": "caller_id_rotation"
        }
