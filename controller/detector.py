from __future__ import annotations
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Any
from .stats import StatsTracker, two_proportion_z_test

@dataclass
class AnomalyReport:
    """Structured report for a detected population-level anomaly."""
    workflow_id: str
    anomaly_type: str  # "hallucination_spike" | "caller_id_degraded" | "variant_decay"
    target: str        # e.g., prompt_version "P1", caller_id "D4", or variant option "V2"
    evidence: dict[str, Any]

class DetectorManager:
    """Threshold-based population anomaly detection without single-call triggers."""

    def __init__(self, window_size: int = 40, hallucination_threshold: float = 0.05):
        self.window_size = window_size
        self.hallucination_threshold = hallucination_threshold
        # workflow_id -> {"connected": int, "hallucinations": int, "elevated_windows": int, "current_prompt": str}
        self._halluc_windows: dict[str, dict] = defaultdict(
            lambda: {"connected": 0, "hallucinations": 0, "elevated_windows": 0, "current_prompt": ""}
        )
        # Track currently open incidents so we don't re-trigger continuously
        self._open_incidents: set[tuple[str, str, str]] = set()

    def process_call(self, ev: dict):
        """Streams call QA data into the rolling hallucination window."""
        if not ev.get("connected"):
            return
        wid = ev.get("workflow_id")
        if not wid:
            return

        variant = ev.get("variant", {})
        prompt = variant.get("prompt_version", "")
        qa = ev.get("qa") or {}
        is_halluc = 1 if qa.get("hallucination_flag") else 0

        w_state = self._halluc_windows[wid]
        # If prompt changed, reset window counters
        if w_state["current_prompt"] != prompt:
            w_state["current_prompt"] = prompt
            w_state["connected"] = 0
            w_state["hallucinations"] = 0
            w_state["elevated_windows"] = 0

        w_state["connected"] += 1
        w_state["hallucinations"] += is_halluc

        # Evaluate window when threshold count is reached
        if w_state["connected"] >= self.window_size:
            rate = w_state["hallucinations"] / w_state["connected"]
            if rate > self.hallucination_threshold:
                w_state["elevated_windows"] += 1
            else:
                w_state["elevated_windows"] = max(0, w_state["elevated_windows"] - 1)
            # Reset counters for the next window
            w_state["connected"] = 0
            w_state["hallucinations"] = 0

    def check_hallucinations(self, workflows: dict[str, dict]) -> list[AnomalyReport]:
        """Triggers incident only when 2 consecutive windows exceed hallucination threshold."""
        reports = []
        for wid, w_state in self._halluc_windows.items():
            if w_state["elevated_windows"] >= 2:
                prompt = w_state["current_prompt"]
                incident_key = (wid, "hallucination_spike", prompt)
                if incident_key not in self._open_incidents:
                    reports.append(AnomalyReport(
                        workflow_id=wid,
                        anomaly_type="hallucination_spike",
                        target=prompt,
                        evidence={
                            "elevated_windows": w_state["elevated_windows"],
                            "window_size": self.window_size,
                            "threshold": self.hallucination_threshold
                        }
                    ))
                    self._open_incidents.add(incident_key)
        return reports

    def check_caller_ids(self, workflows: dict[str, dict], stats: StatsTracker,
                         min_day: int | None = None, current_day: int | None = None) -> list[AnomalyReport]:
        """Detects degraded caller IDs with sample size >= 60 dials showing > 30% pickup drop."""
        reports = []
        for wid, wf_spec in workflows.items():
            options = wf_spec.get("options", {}).get("caller_id", [])
            health_by_did = {}
            
            # Prioritize today's dials for early detection if volume >= 60
            evaluated_day = min_day
            if current_day is not None:
                today_health = {did: stats.get_caller_id_health(wid, did, min_day=current_day) for did in options}
                today_eligible = [h for h in today_health.values() if h["dials"] >= 60]
                if len(today_eligible) >= 2:
                    health_by_did = today_health
                    evaluated_day = current_day

            if not health_by_did:
                for did in options:
                    h = stats.get_caller_id_health(wid, did, min_day=min_day)
                    health_by_did[did] = h

            # Find best healthy pickup rate among DIDs with adequate volume
            eligible = [h for h in health_by_did.values() if h["dials"] >= 60]
            if len(eligible) < 2:
                continue

            best_pickup = max(h["pickup_rate"] for h in eligible)
            if best_pickup < 0.10:
                continue

            for did, h in health_by_did.items():
                if h["dials"] >= 60:
                    relative_drop = (best_pickup - h["pickup_rate"]) / best_pickup
                    if relative_drop > 0.30 or h["pickup_rate"] < 0.15:
                        incident_key = (wid, "caller_id_degraded", did)
                        if incident_key not in self._open_incidents:
                            reports.append(AnomalyReport(
                                workflow_id=wid,
                                anomaly_type="caller_id_degraded",
                                target=did,
                                evidence={
                                    "dials": h["dials"],
                                    "connected": h["connected"],
                                    "pickup_rate": round(h["pickup_rate"], 3),
                                    "best_pickup": round(best_pickup, 3),
                                    "relative_drop": round(relative_drop, 3),
                                    "evaluated_day": evaluated_day
                                }
                            ))
                            self._open_incidents.add(incident_key)
        return reports

    def check_decay(self, workflow_id: str, cohort_id: str, dim: str, active_val: str,
                    baseline_rate: float, stats: StatsTracker, current_day: int) -> AnomalyReport | None:
        """Detects if an active winning variant has degraded > 10% relative to baseline (p < 0.05)."""
        dim_stats = stats.get_cohort_dim_stats(workflow_id, cohort_id, dim, min_day=max(0, current_day - 2))
        s = dim_stats.get(active_val)
        if not s or s["connected"] < 100:
            return None

        recent_rate = s["engagement_rate"]
        if baseline_rate > 0 and (baseline_rate - recent_rate) / baseline_rate > 0.10:
            z_res = two_proportion_z_test(
                s1=int(baseline_rate * 200), n1=200,
                s2=s["engaged"], n2=s["connected"]
            )
            if z_res["p_value"] < 0.05 and z_res["z_score"] < -1.645:
                incident_key = (workflow_id, "variant_decay", active_val)
                if incident_key not in self._open_incidents:
                    self._open_incidents.add(incident_key)
                    return AnomalyReport(
                        workflow_id=workflow_id,
                        anomaly_type="variant_decay",
                        target=active_val,
                        evidence={
                            "cohort_id": cohort_id,
                            "dim": dim,
                            "baseline_rate": round(baseline_rate, 3),
                            "recent_rate": round(recent_rate, 3),
                            "drop_pct": round((baseline_rate - recent_rate) / baseline_rate * 100, 1),
                            "sample_size": s["connected"],
                            "p_value": round(z_res["p_value"], 4)
                        }
                    )
        return None

    def clear_incident(self, workflow_id: str, anomaly_type: str, target: str):
        self._open_incidents.discard((workflow_id, anomaly_type, target))
        if anomaly_type == "hallucination_spike":
            self._halluc_windows[workflow_id]["elevated_windows"] = 0
