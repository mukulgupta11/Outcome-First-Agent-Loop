from __future__ import annotations
import math
from collections import defaultdict
from typing import Any
from .db import DB

def _approx_norm_ppf(p: float) -> float:
    """Abramowitz & Stegun rational approximation for normal quantile."""
    if p >= 1.0:
        return 3.5
    if p <= 0.0:
        return -3.5
    if p < 0.5:
        return -_approx_norm_ppf(1.0 - p)
    t = math.sqrt(-2.0 * math.log(1.0 - p))
    c0, c1, c2 = 2.515517, 0.802853, 0.010328
    d1, d2, d3 = 1.432788, 0.189269, 0.001308
    return t - ((c0 + c1 * t + c2 * t * t) / (1.0 + d1 * t + d2 * t * t + d3 * t * t * t))

def _approx_norm_cdf(z: float) -> float:
    """Error function approximation for standard normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

def compute_sample_size(baseline_p: float, relative_mde: float = 0.08,
                        alpha: float = 0.05, power: float = 0.80,
                        num_comparisons: int = 2, n_min_floor: int = 150) -> int:
    """Calculates required sample size per arm for two-sample proportion test.
    Formula: n = [ (z_alpha * sqrt(2*p_bar*(1-p_bar)) + z_beta * sqrt(p1*(1-p1) + p2*(1-p2))) / (p1 - p2) ]^2
    Adjusts alpha via Bonferroni correction: alpha' = alpha / num_comparisons.
    """
    p1 = max(0.01, min(0.99, float(baseline_p)))
    p2 = max(0.01, min(0.99, p1 * (1.0 + relative_mde)))
    delta = abs(p1 - p2)
    if delta < 1e-4:
        return n_min_floor

    alpha_adj = alpha / max(1, num_comparisons)
    z_alpha = _approx_norm_ppf(1.0 - alpha_adj / 2.0)
    z_beta = _approx_norm_ppf(power)

    p_bar = (p1 + p2) / 2.0
    term1 = z_alpha * math.sqrt(2.0 * p_bar * (1.0 - p_bar))
    term2 = z_beta * math.sqrt(p1 * (1.0 - p1) + p2 * (1.0 - p2))
    n_calc = math.ceil(((term1 + term2) / delta) ** 2)
    return max(n_min_floor, n_calc)

def two_proportion_z_test(s1: int, n1: int, s2: int, n2: int, alpha: float = 0.05, num_comparisons: int = 2) -> dict:
    """Performs two-sided two-proportion z-test with Bonferroni correction.
    Returns z_score, p_value, lift, and whether difference is statistically significant.
    """
    if n1 <= 0 or n2 <= 0:
        return {"z_score": 0.0, "p_value": 1.0, "significant": False, "lift": 0.0}
    p1 = s1 / n1
    p2 = s2 / n2
    p_pool = (s1 + s2) / (n1 + n2)
    se = math.sqrt(p_pool * (1.0 - p_pool) * (1.0 / n1 + 1.0 / n2))
    if se < 1e-6:
        return {"z_score": 0.0, "p_value": 1.0, "significant": False, "lift": 0.0}
    z = (p2 - p1) / se
    p_val = 2.0 * (1.0 - _approx_norm_cdf(abs(z)))
    alpha_adj = alpha / max(1, num_comparisons)
    lift = (p2 - p1) / max(p1, 1e-4)
    return {
        "z_score": z,
        "p_value": p_val,
        "significant": p_val < alpha_adj,
        "lift": lift,
        "p1": p1,
        "p2": p2
    }

class StatsTracker:
    """Tracks per-workflow rolling pickup/engagement rates across cohorts, variants, and caller IDs."""

    def __init__(self, db: DB):
        self.db = db
        # Buffer: (workflow_id, cohort_id, dim, val, day) -> [dials, connected, engaged, hallucinations]
        self._deltas: dict[tuple[str, str, str, str, int], list[int]] = defaultdict(lambda: [0, 0, 0, 0])

    def record_call(self, ev: dict, sim_minute: int):
        wid = ev.get("workflow_id")
        cid = ev.get("cohort_id") or "default"
        day = sim_minute // 1440
        conn = 1 if ev.get("connected") else 0
        eng = 1 if ev.get("engaged") else 0
        qa = ev.get("qa") or {}
        halluc = 1 if qa.get("hallucination_flag") else 0

        variant = ev.get("variant", {})
        for dim, val in variant.items():
            if not val:
                continue
            # Cohort-specific stats
            if cid != "*":
                c_key = (wid, cid, dim, str(val), day)
                d = self._deltas[c_key]
                d[0] += 1
                d[1] += conn
                d[2] += eng
                d[3] += halluc

            # Workflow-wide rollup
            w_key = (wid, "*", dim, str(val), day)
            dw = self._deltas[w_key]
            dw[0] += 1
            dw[1] += conn
            dw[2] += eng
            dw[3] += halluc

    def flush(self):
        """Flushes in-memory delta buffers to SQLite rolling_stats table."""
        if not self._deltas:
            return
        rows = [
            (wid, cid, dim, val, day, d[0], d[1], d[2], d[3])
            for (wid, cid, dim, val, day), d in self._deltas.items()
        ]
        self.db.upsert_stats_batch(rows)
        self._deltas.clear()

    def get_cohort_dim_stats(self, workflow_id: str, cohort_id: str, dim: str, min_day: int | None = None) -> dict[str, dict]:
        """Returns aggregated rates per variant option for a cohort."""
        self.flush()
        raw = self.db.query_stats(workflow_id, cohort_id=cohort_id, dim=dim, min_day=min_day)
        res = {}
        for r in raw:
            val = r["val"]
            d = r["dials"]
            c = r["connected"]
            e = r["engaged"]
            h = r["hallucinations"]
            res[val] = {
                "dials": d,
                "connected": c,
                "engaged": e,
                "hallucinations": h,
                "pickup_rate": c / max(d, 1),
                "engagement_rate": e / max(c, 1),
                "hallucination_rate": h / max(c, 1)
            }
        return res

    def get_caller_id_health(self, workflow_id: str, did: str, min_day: int | None = None) -> dict:
        """Returns overall caller ID pickup rate and sample size."""
        self.flush()
        raw = self.db.query_stats(workflow_id, cohort_id="*", dim="caller_id", min_day=min_day)
        for r in raw:
            if r["val"] == did:
                d = r["dials"]
                c = r["connected"]
                return {
                    "dials": d,
                    "connected": c,
                    "pickup_rate": c / max(d, 1)
                }
        return {"dials": 0, "connected": 0, "pickup_rate": 0.0}
