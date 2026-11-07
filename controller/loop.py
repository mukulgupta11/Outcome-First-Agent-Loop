from __future__ import annotations
import copy
import logging
import uuid
from .audit import AuditStore
from .client import SimClient
from .db import DB
from .detector import DetectorManager
from .experimenter import ExperimentManager
from .governance import AGENT_DIMS, GovernanceClient, clean_config, compute_changed_dims
from .ingest import Ingestor
from .stats import StatsTracker

logger = logging.getLogger("controller.loop")

class ControllerLoop:
    """Outcome-First Controller Loop: advance -> ingest -> decide -> act."""

    def __init__(self, client: SimClient, db: DB):
        self.client = client
        self.db = db
        self.audit = AuditStore(db)
        self.stats = StatsTracker(db)
        self.detector = DetectorManager()
        self.experimenter = ExperimentManager(db, self.stats)
        self.governance = GovernanceClient(client, db)
        self.ingestor = Ingestor(client, db, on_event=self.handle_event)
        self.workflows: dict[str, dict] = {}
        self.queued_proposals: list[dict] = []
        self.active_incident = False
        self.last_config_changed = False

    def bootstrap(self):
        """Fetch workflow metadata, reconcile approvals, and bootstrap pre-D0 history."""
        wfs = self.client.workflows()
        self.workflows = {w["workflow_id"]: w for w in wfs}
        for wid, w in self.workflows.items():
            self.governance.load_policy(wid, w)
        self.reconcile_approvals()
        self.ingestor.bootstrap_history()
        self.stats.flush()
        self.db.set_meta("initialized", "1")

    def handle_event(self, ev: dict):
        """Dispatches ingested events to update loop state and audit logs."""
        etype = ev.get("type")
        if etype == "config_changed":
            wid = ev.get("workflow_id")
            actor = ev.get("actor", "")
            self.last_config_changed = True
            sim_minute = self.db.get_meta_int("sim_minute", 0)
            if actor == "supervisor":
                # Stale base handling: reconcile pending proposals for this workflow
                sup_changed_dims = ev.get("changed_dims", [])
                live_res = self.client.get_config(wid)
                live_cfg = live_res.get("config", {})
                live_ver = live_res.get("version", ev.get("version", 1))

                active = self.db.get_active_approvals()
                for app in active:
                    if app["workflow_id"] == wid and app.get("status") == "pending":
                        prop_cfg = app.get("proposed_config", {})
                        if isinstance(prop_cfg, str):
                            try:
                                import json
                                prop_cfg = json.loads(prop_cfg)
                            except Exception:
                                prop_cfg = {}
                        diff_obj = app.get("diff") or {}
                        diff_str = str(diff_obj) + " " + str(app.get("summary") or "")
                        prop_dims = [d for d in ("prompt_version", "voice", "welcome_message", "caller_id", "schedule", "cohorts") if d in diff_str]
                        if not prop_dims:
                            prop_dims = compute_changed_dims(live_cfg, prop_cfg)

                        conflict = any(d in sup_changed_dims for d in prop_dims)
                        if conflict:
                            self.db.update_approval_status(app["approval_id"], "cancelled_by_supervisor")
                            self.db.log_audit(
                                sim_minute=sim_minute,
                                event_type="supervisor_override_cancelled",
                                workflow_id=wid,
                                details={
                                    "approval_id": app["approval_id"],
                                    "reason": f"Supervisor edited conflicting dimensions: {sup_changed_dims}"
                                }
                            )
                        else:
                            self.db.update_approval_status(app["approval_id"], "superseded_by_stale_base")
                            merged_cfg = copy.deepcopy(live_cfg)
                            for dim in prop_dims:
                                if dim == "prompt_version":
                                    merged_cfg["prompt_version"] = prop_cfg.get("prompt_version")
                                elif dim == "schedule":
                                    merged_cfg["schedule"] = copy.deepcopy(prop_cfg.get("schedule", {}))
                                elif dim in AGENT_DIMS:
                                    for cohort_name, alloc in prop_cfg.get("allocations", {}).items():
                                        if dim in alloc:
                                            merged_cfg.setdefault("allocations", {}).setdefault(cohort_name, {})[dim] = copy.deepcopy(alloc[dim])

                            new_pid = str(uuid.uuid4())
                            new_prop = {
                                "proposal_id": new_pid,
                                "workflow_id": wid,
                                "base_version": live_ver,
                                "proposed_config": clean_config(merged_cfg),
                                "change_summary": f"[Re-proposed on v{live_ver}] Pending change preserved after supervisor edit",
                                "diff": {d: merged_cfg.get(d) for d in prop_dims},
                                "affected_leads": 1000,
                                "impact_score": 0.5,
                                "rationale": f"Re-proposal following supervisor edit to v{live_ver}",
                                "action_type": "stale_base_reproposal"
                            }
                            submitted = self.governance.submit_proposal(new_prop, sim_minute)
                            if not submitted:
                                self.queued_proposals.insert(0, new_prop)

                            self.db.log_audit(
                                sim_minute=sim_minute,
                                event_type="stale_base_reproposed",
                                workflow_id=wid,
                                details={
                                    "old_approval_id": app["approval_id"],
                                    "new_proposal_id": new_pid,
                                    "old_base_version": app.get("base_version"),
                                    "new_base_version": live_ver,
                                    "reason": "Supervisor edit mid-flight bumped base version"
                                }
                            )
                self.experimenter.handle_supervisor_edit(wid, sup_changed_dims)
            self.db.log_audit(
                sim_minute=sim_minute,
                event_type="config_changed",
                workflow_id=wid,
                details=ev
            )
        elif etype == "approval_decided":
            aid = ev.get("approval_id")
            status = ev.get("status", "")
            wid = ev.get("workflow_id")
            sim_minute = self.db.get_meta_int("sim_minute", 0)
            if aid:
                local_app = self.db.get_approval(aid)
                if local_app and local_app.get("status") in ("superseded_by_stale_base", "cancelled_by_supervisor", "stale"):
                    self.db.update_approval_status(aid, f"ignored_{status}")
                    self.db.log_audit(
                        sim_minute=sim_minute,
                        event_type="stale_approval_ignored",
                        workflow_id=wid,
                        details={"approval_id": aid, "decision": status, "local_status": local_app.get("status")}
                    )
                    return
                self.db.update_approval_status(aid, status)
                if status == "approved" and wid:
                    apply_res = self.client.apply_approval(wid, aid)
                    if apply_res["status_code"] == 200:
                        self.db.update_approval_status(aid, "applied")
                        self.db.log_audit(
                            sim_minute=sim_minute,
                            event_type="approval_applied",
                            workflow_id=wid,
                            details={"approval_id": aid, "applied_version": apply_res["data"].get("version")}
                        )
                elif status in ("rejected", "expired") and wid:
                    self.experimenter.handle_rejection(wid)
            self.db.log_audit(
                sim_minute=sim_minute,
                event_type="approval_decided",
                workflow_id=wid,
                details=ev
            )
        elif etype == "call":
            sim_minute = self.db.get_meta_int("sim_minute", 0)
            self.stats.record_call(ev, sim_minute)
            self.detector.process_call(ev)

    def reconcile_approvals(self):
        """Reconcile pending approvals with simulator after startup/restart."""
        active = self.db.get_active_approvals()
        for app in active:
            aid = app["approval_id"]
            res = self.client.get_approval(aid)
            if res["status_code"] == 200:
                current_status = res["data"].get("status")
                if current_status != "pending":
                    self.db.update_approval_status(aid, current_status)
            elif res["status_code"] == 404:
                self.db.update_approval_status(aid, "unknown")

    def determine_advance_step(self) -> int:
        """Adaptive advance: 15m if pending approval, config change, or incident; else 60m."""
        has_pending = len(self.db.get_active_approvals()) > 0
        if has_pending or self.last_config_changed or self.active_incident:
            self.last_config_changed = False
            return 15
        return 60

    def decide_step(self):
        """Anomaly detection and candidate hypothesis evaluation."""
        sim_minute = self.db.get_meta_int("sim_minute", 0)
        current_day = sim_minute // 1440

        # 1. Check for hallucination spikes (requires 2 consecutive windows)
        halluc_reports = self.detector.check_hallucinations(self.workflows)
        for rep in halluc_reports:
            self.active_incident = True
            self.db.log_audit(sim_minute, "anomaly_detected", rep.workflow_id, {
                "anomaly_type": rep.anomaly_type,
                "target": rep.target,
                "evidence": rep.evidence
            })
            cur_cfg_res = self.client.get_config(rep.workflow_id)
            cur_cfg = cur_cfg_res.get("config", {})
            cur_cfg["version"] = cur_cfg_res.get("version", 1)
            fix_prop = self.governance.handle_hallucination_fix(
                rep.workflow_id, rep.target, cur_cfg, sim_minute
            )
            if fix_prop:
                self.queued_proposals.insert(0, fix_prop)

        # 2. Check for degraded caller IDs (dials >= 60, >30% drop)
        did_reports = self.detector.check_caller_ids(self.workflows, self.stats, min_day=max(0, current_day - 2), current_day=current_day)
        for rep in did_reports:
            self.db.log_audit(sim_minute, "anomaly_detected", rep.workflow_id, {
                "anomaly_type": rep.anomaly_type,
                "target": rep.target,
                "evidence": rep.evidence
            })
            cur_cfg_res = self.client.get_config(rep.workflow_id)
            cur_cfg = cur_cfg_res.get("config", {})
            cur_version = cur_cfg_res.get("version", 1)
            cur_cfg["version"] = cur_version
            did_prop = self.governance.handle_caller_id_rotation(
                rep.workflow_id, rep.target, self.workflows[rep.workflow_id], cur_cfg, sim_minute
            )
            if did_prop:
                self.queued_proposals.insert(0, did_prop)

        # 3. Experimentation lifecycle: decay rollbacks -> evaluations -> new plans
        active_app_wids = {a["workflow_id"] for a in self.db.get_active_approvals()}
        queued_wids = {p["workflow_id"] for p in self.queued_proposals}
        for wid, wf_spec in self.workflows.items():
            if wid in active_app_wids or wid in queued_wids:
                continue  # Do not propose concurrent changes while one is pending or queued

            cur_cfg_res = self.client.get_config(wid)
            cur_cfg = cur_cfg_res.get("config", {})
            cur_version = cur_cfg_res.get("version", 1)

            # Check decay rollback first
            rollbacks = self.experimenter.check_decay_and_rollback(wid, wf_spec, cur_cfg, sim_minute)
            if rollbacks:
                for r in rollbacks:
                    r["base_version"] = cur_version
                self.queued_proposals.extend(rollbacks)
                continue

            # Evaluate active experiments for winner promotion
            eval_props = self.experimenter.evaluate_active_experiments(wid, wf_spec, cur_cfg, sim_minute)
            if eval_props:
                for ep in eval_props:
                    ep["base_version"] = cur_version
                self.queued_proposals.extend(eval_props)
                continue

            # If no active experiment on workflow, plan an experiment
            new_exp = self.experimenter.plan_experiment(wid, wf_spec, cur_cfg, sim_minute)
            if new_exp:
                new_exp["base_version"] = cur_version
                self.queued_proposals.append(new_exp)

        if not halluc_reports and not self.detector._open_incidents:
            self.active_incident = False

    def act_step(self):
        """Applies approved requests and submits eligible batched proposals."""
        sim_minute = self.db.get_meta_int("sim_minute", 0)

        # 1. Apply any pending approvals that have been approved
        applied_wids = self.governance.apply_pending_approvals(sim_minute)
        for wid in applied_wids:
            # Reconcile local workflow cache
            pass

        # 2. Submit queued proposals respecting approver hours and 24h fatigue limits
        remaining_queue = []
        for prop in self.queued_proposals:
            wid = prop["workflow_id"]
            if self.governance.can_submit_request(wid, sim_minute):
                # Ensure base_version and untouched dimensions are fresh against live config
                cur_cfg_res = self.client.get_config(wid)
                live_cfg = cur_cfg_res.get("config", {})
                prop["base_version"] = cur_cfg_res.get("version", 1)
                
                # Protect supervisor edits on other dimensions
                diff_keys = str(prop.get("diff", {}))
                if "prompt_version" not in diff_keys:
                    prop["proposed_config"]["prompt_version"] = live_cfg.get("prompt_version")
                if "schedule" not in diff_keys:
                    prop["proposed_config"]["schedule"] = copy.deepcopy(live_cfg.get("schedule", {}))
                if "cohorts" not in diff_keys:
                    prop["proposed_config"]["cohorts"] = copy.deepcopy(live_cfg.get("cohorts", []))

                submitted = self.governance.submit_proposal(prop, sim_minute)
                if not submitted:
                    remaining_queue.append(prop)
            else:
                remaining_queue.append(prop)
        self.queued_proposals = remaining_queue

    def run_once(self) -> bool:
        """Executes one tick/cycle of the loop. Returns True if scenario is finished."""
        step = self.determine_advance_step()
        adv = self.client.advance(step)

        sim_minute = adv.get("sim_minute", 0)
        self.db.set_meta("sim_minute", str(sim_minute))

        new_chunks = adv.get("new_chunks", [])
        self.ingestor.ingest_new_chunks(new_chunks, sim_minute)
        self.stats.flush()
        self.decide_step()
        self.act_step()

        return bool(adv.get("finished", False))

    def run(self):
        """Runs the loop until the scenario finishes."""
        self.bootstrap()
        finished = False
        while not finished:
            finished = self.run_once()
        self.db.set_meta("finished", "1")
