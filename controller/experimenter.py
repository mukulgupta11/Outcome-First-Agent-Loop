from __future__ import annotations
import copy
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any
from .db import DB
from .stats import StatsTracker, compute_sample_size, two_proportion_z_test

logger = logging.getLogger("controller.experimenter")

def compute_impact_score(delta_primary_kpi: float, affected_leads: int, daily_batch_size: int) -> float:
    """Bounded [0, 1] impact score: clamp(|Δprimary KPI| * affected_leads / max(daily_batch, 1), 0, 1)."""
    raw = abs(delta_primary_kpi) * (affected_leads / max(daily_batch_size, 1))
    return max(0.0, min(1.0, float(raw)))

def compute_dry_run(historical_calls: int, historical_successes: int, expected_lift: float, cohort_id: str) -> dict:
    """Counterfactual simulation replay against recent cohort call history."""
    projected = int(historical_successes * (1.0 + expected_lift))
    return {
        "cohort_id": cohort_id,
        "sample_calls_replayed": historical_calls,
        "observed_successes": historical_successes,
        "projected_successes": projected,
        "projected_lift_pct": round(expected_lift * 100, 2),
    }

def clean_config(cfg: dict) -> dict:
    allowed = {"cohorts", "allocations", "prompt_version", "schedule"}
    return {k: copy.deepcopy(v) for k, v in cfg.items() if k in allowed}

@dataclass
class ActiveExperiment:
    workflow_id: str
    cohort_id: str
    dim: str
    baseline_val: str
    candidate_val: str
    start_minute: int
    req_sample_size: int
    baseline_kpi_at_start: float
    guardrail_kpi_at_start: float
    status: str = "RUNNING"  # "RUNNING" | "PROMOTED" | "REVERTED" | "DECAYED"

class ExperimentManager:
    """Manages 2-arm variant experimentation, sample sizing, sequential testing, and decay rollback."""

    def __init__(self, db: DB, stats: StatsTracker):
        self.db = db
        self.stats = stats
        # (workflow_id, cohort_id, dim) -> ActiveExperiment
        self.active_experiments: dict[tuple[str, str, str], ActiveExperiment] = {}
        # (workflow_id, cohort_id, dim) -> set of tested variants
        self.tested_variants: dict[tuple[str, str, str], set[str]] = {}

    def plan_experiment(self, workflow_id: str, wf_spec: dict, current_config: dict, sim_minute: int) -> dict | None:
        """Plans a 70/30 exploration split for an untried variant on voice or welcome_message."""
        options = wf_spec.get("options", {})
        allocations = current_config.get("allocations", {})
        default_alloc = allocations.get("default", {})
        cohorts = current_config.get("cohorts", [])
        cohort_ids = ["default"] + [c["id"] for c in cohorts]

        # Select target dimensions based on primary objective
        primary_kpi_name = wf_spec.get("objective", {}).get("primary", "engagement_rate")
        if primary_kpi_name == "pickup_rate":
            target_dims = ["caller_id"]
        else:
            target_dims = ["voice", "welcome_message"]

        for dim in target_dims:
            available = options.get(dim, [])
            if len(available) <= 1:
                continue

            for cid in cohort_ids:
                exp_key = (workflow_id, cid, dim)
                if exp_key in self.active_experiments:
                    continue  # Already experimenting on this arm

                tested = self.tested_variants.setdefault(exp_key, set())
                c_alloc = allocations.get(cid, default_alloc).get(dim, {})
                current_active = max(c_alloc.keys(), key=lambda k: c_alloc[k]) if c_alloc else available[0]
                tested.add(current_active)

                untested = [v for v in available if v not in tested]
                if not untested:
                    continue

                candidate = untested[0]
                tested.add(candidate)

                # Get baseline stats for sample sizing
                dim_stats = self.stats.get_cohort_dim_stats(workflow_id, cid, dim)
                s_base = dim_stats.get(current_active, {})
                dials = s_base.get("dials", 0)
                conn = s_base.get("connected", 0)
                eng = s_base.get("engaged", 0)

                primary_kpi_name = wf_spec.get("objective", {}).get("primary", "engagement_rate")
                base_primary = (eng / max(conn, 1)) if primary_kpi_name == "engagement_rate" else (conn / max(dials, 1))
                guard_name = "pickup_rate" if primary_kpi_name == "engagement_rate" else "engagement_rate"
                base_guard = (conn / max(dials, 1)) if guard_name == "pickup_rate" else (eng / max(conn, 1))

                req_n = compute_sample_size(
                    baseline_p=base_primary,
                    relative_mde=0.08,
                    num_comparisons=len(available) - 1,
                    n_min_floor=150
                )

                # Build 70/30 proposed config
                new_cfg = clean_config(current_config)
                if cid not in new_cfg["allocations"]:
                    new_cfg["allocations"][cid] = copy.deepcopy(default_alloc)
                new_cfg["allocations"][cid][dim] = {current_active: 0.7, candidate: 0.3}

                # Register active experiment
                self.active_experiments[exp_key] = ActiveExperiment(
                    workflow_id=workflow_id,
                    cohort_id=cid,
                    dim=dim,
                    baseline_val=current_active,
                    candidate_val=candidate,
                    start_minute=sim_minute,
                    req_sample_size=req_n,
                    baseline_kpi_at_start=base_primary,
                    guardrail_kpi_at_start=base_guard
                )

                batch_size = wf_spec.get("daily_batch", {}).get("size", 1000)
                affected_leads = int(batch_size * 0.30)
                impact = compute_impact_score(0.08 * base_primary, affected_leads, batch_size)
                dry_run = compute_dry_run(conn, eng, 0.08, cid)

                return {
                    "proposal_id": str(uuid.uuid4()),
                    "workflow_id": workflow_id,
                    "base_version": current_config.get("version", 1),
                    "proposed_config": new_cfg,
                    "change_summary": f"A/B test {dim} on {cid}: 70% {current_active} vs 30% {candidate}",
                    "diff": {f"allocations.{cid}.{dim}": new_cfg["allocations"][cid][dim]},
                    "affected_leads": affected_leads,
                    "dry_run": dry_run,
                    "impact_score": round(impact, 3),
                    "rationale": f"Sequential test for {dim} {candidate} vs baseline {current_active} (N_target={req_n})",
                    "action_type": "experiment_start",
                    "exp_key": exp_key
                }
        return None

    def evaluate_active_experiments(self, workflow_id: str, wf_spec: dict, current_config: dict, sim_minute: int) -> list[dict]:
        """Evaluates ongoing experiments. Promotes winners (with guardrail gate) or reverts losers."""
        proposals = []
        primary_kpi_name = wf_spec.get("objective", {}).get("primary", "engagement_rate")
        guard_name = "pickup_rate" if primary_kpi_name == "engagement_rate" else "engagement_rate"
        guard_drop_limit = wf_spec.get("objective", {}).get("guardrail", {}).get(f"{guard_name}_max_drop", 0.02)
        batch_size = wf_spec.get("daily_batch", {}).get("size", 1000)

        for exp_key, exp in list(self.active_experiments.items()):
            if exp.workflow_id != workflow_id or exp.status != "RUNNING":
                continue

            dim_stats = self.stats.get_cohort_dim_stats(workflow_id, exp.cohort_id, exp.dim)
            s_base = dim_stats.get(exp.baseline_val, {})
            s_cand = dim_stats.get(exp.candidate_val, {})

            n_base = s_base.get("connected", 0) if primary_kpi_name == "engagement_rate" else s_base.get("dials", 0)
            s1 = s_base.get("engaged", 0) if primary_kpi_name == "engagement_rate" else s_base.get("connected", 0)
            n_cand = s_cand.get("connected", 0) if primary_kpi_name == "engagement_rate" else s_cand.get("dials", 0)
            s2 = s_cand.get("engaged", 0) if primary_kpi_name == "engagement_rate" else s_cand.get("connected", 0)

            # Check if adequate sample size has been accumulated
            reached_target = n_cand >= exp.req_sample_size
            if not reached_target and n_cand < 150:
                continue

            z_res = two_proportion_z_test(s1, n_base, s2, n_cand)
            lift = z_res["lift"]

            # Guardrail check: compare candidate against baseline with statistical significance
            g_dials = s_cand.get("dials", 0)
            g_conn = s_cand.get("connected", 0)
            g_eng = s_cand.get("engaged", 0)
            cand_guard_rate = (g_conn / max(g_dials, 1)) if guard_name == "pickup_rate" else (g_eng / max(g_conn, 1))
            
            base_guard_rate = (s_base.get("connected", 0) / max(s_base.get("dials", 0), 1)) if guard_name == "pickup_rate" else (s_base.get("engaged", 0) / max(s_base.get("connected", 0), 1))
            ref_guard = max(exp.guardrail_kpi_at_start, base_guard_rate)
            guard_drop = ref_guard - cand_guard_rate

            # Test guardrail drop significance
            n_g1 = s_base.get("dials", 0) if guard_name == "pickup_rate" else s_base.get("connected", 0)
            s_g1 = s_base.get("connected", 0) if guard_name == "pickup_rate" else s_base.get("engaged", 0)
            n_g2 = g_dials if guard_name == "pickup_rate" else g_conn
            s_g2 = g_conn if guard_name == "pickup_rate" else g_eng
            g_z_res = two_proportion_z_test(s_g1, n_g1, s_g2, n_g2)
            guard_degraded = (guard_drop > guard_drop_limit) and (g_z_res["z_score"] < -1.645)

            # Winner Promotion Case: statistically significant primary lift AND guardrail respected
            if z_res["significant"] and lift > 0 and not guard_degraded:
                new_cfg = clean_config(current_config)
                if exp.cohort_id not in new_cfg["allocations"]:
                    new_cfg["allocations"][exp.cohort_id] = copy.deepcopy(new_cfg["allocations"].get("default", {}))
                new_cfg["allocations"][exp.cohort_id][exp.dim] = {exp.candidate_val: 1.0}

                exp.status = "PROMOTED"
                exp.baseline_kpi_at_start = z_res["p2"]  # Track winner rate for decay monitoring

                impact = compute_impact_score(z_res["p2"] - z_res["p1"], batch_size, batch_size)
                dry_run = compute_dry_run(n_cand, s2, lift, exp.cohort_id)

                proposals.append({
                    "proposal_id": str(uuid.uuid4()),
                    "workflow_id": workflow_id,
                    "base_version": current_config.get("version", 1),
                    "proposed_config": new_cfg,
                    "change_summary": f"Promote winner {exp.dim}={exp.candidate_val} on {exp.cohort_id} (+{lift*100:.1f}%)",
                    "diff": {f"allocations.{exp.cohort_id}.{exp.dim}": new_cfg["allocations"][exp.cohort_id][exp.dim]},
                    "affected_leads": batch_size,
                    "dry_run": dry_run,
                    "impact_score": round(impact, 3),
                    "rationale": f"Statistically significant winner (p={z_res['p_value']:.4f}, lift={lift*100:.1f}%, guardrail safe)",
                    "action_type": "winner_promotion",
                    "exp_key": exp_key
                })
            elif (reached_target and not z_res["significant"]) or guard_degraded or (z_res["significant"] and lift < 0):
                # Reversion Case: no win after target sample, inferior, or guardrail violated
                new_cfg = clean_config(current_config)
                if exp.cohort_id in new_cfg["allocations"] and exp.dim in new_cfg["allocations"][exp.cohort_id]:
                    new_cfg["allocations"][exp.cohort_id][exp.dim] = {exp.baseline_val: 1.0}

                exp.status = "REVERTED"
                self.active_experiments.pop(exp_key, None)

                proposals.append({
                    "proposal_id": str(uuid.uuid4()),
                    "workflow_id": workflow_id,
                    "base_version": current_config.get("version", 1),
                    "proposed_config": new_cfg,
                    "change_summary": f"Revert {exp.dim} on {exp.cohort_id} to baseline {exp.baseline_val}",
                    "diff": {f"allocations.{exp.cohort_id}.{exp.dim}": new_cfg["allocations"][exp.cohort_id][exp.dim]},
                    "affected_leads": batch_size,
                    "dry_run": {"reverted_due_to": "guardrail_breach" if guard_degraded else "no_lift"},
                    "impact_score": 0.1,
                    "rationale": f"Reverting test arm {exp.candidate_val} after N={n_cand} calls (lift={lift*100:.1f}%, guard_drop={guard_drop:.3f})",
                    "action_type": "experiment_revert",
                    "exp_key": exp_key
                })

        return proposals

    def check_decay_and_rollback(self, workflow_id: str, wf_spec: dict, current_config: dict, sim_minute: int) -> list[dict]:
        """Detects if any active promoted winner has decayed > 10% (p < 0.05), triggering rollback."""
        rollbacks = []
        batch_size = wf_spec.get("daily_batch", {}).get("size", 1000)
        current_day = sim_minute // 1440

        for exp_key, exp in list(self.active_experiments.items()):
            if exp.workflow_id != workflow_id or exp.status != "PROMOTED":
                continue

            dim_stats = self.stats.get_cohort_dim_stats(workflow_id, exp.cohort_id, exp.dim, min_day=max(0, current_day - 2))
            s = dim_stats.get(exp.candidate_val, {})
            conn = s.get("connected", 0)
            eng = s.get("engaged", 0)
            if conn < 100:
                continue

            recent_rate = eng / max(conn, 1)
            baseline = exp.baseline_kpi_at_start
            if baseline > 0 and (baseline - recent_rate) / baseline > 0.10:
                z_res = two_proportion_z_test(int(baseline * 200), 200, eng, conn)
                if z_res["p_value"] < 0.05 and z_res["z_score"] < -1.645:
                    # Decay confirmed! Formulate rollback
                    new_cfg = clean_config(current_config)
                    new_cfg["allocations"][exp.cohort_id][exp.dim] = {exp.baseline_val: 1.0}
                    exp.status = "DECAYED"
                    self.active_experiments.pop(exp_key, None)

                    rollbacks.append({
                        "proposal_id": str(uuid.uuid4()),
                        "workflow_id": workflow_id,
                        "base_version": current_config.get("version", 1),
                        "proposed_config": new_cfg,
                        "change_summary": f"Rollback decayed variant {exp.dim}={exp.candidate_val} to {exp.baseline_val}",
                        "diff": {f"allocations.{exp.cohort_id}.{exp.dim}": new_cfg["allocations"][exp.cohort_id][exp.dim]},
                        "affected_leads": batch_size,
                        "dry_run": {"observed_decay_drop_pct": round((baseline - recent_rate) / baseline * 100, 1)},
                        "impact_score": 0.2,
                        "rationale": f"Decay detected on {exp.candidate_val}: rate dropped {baseline:.3f} -> {recent_rate:.3f} (p={z_res['p_value']:.4f})",
                        "action_type": "decay_rollback",
                        "exp_key": exp_key
                    })

        return rollbacks

    def handle_rejection(self, workflow_id: str):
        """When an approval is rejected or expires, clear unpromoted experiment so it can re-plan with fresh data."""
        for exp_key, exp in list(self.active_experiments.items()):
            if exp.workflow_id == workflow_id and exp.status != "PROMOTED":
                self.active_experiments.pop(exp_key, None)

    def handle_supervisor_edit(self, workflow_id: str, changed_dims: list[str]):
        """Discards active experiments and resets tested state on supervisor-edited dimensions."""
        for exp_key in list(self.active_experiments.keys()):
            wid, cid, dim = exp_key
            if wid == workflow_id and (not changed_dims or dim in changed_dims or "allocations" in changed_dims):
                self.active_experiments.pop(exp_key, None)
                self.tested_variants.pop(exp_key, None)
