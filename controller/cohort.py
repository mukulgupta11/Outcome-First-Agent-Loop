from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)
class Cohort:
    """One shared cohort specification with demographic feature predicates."""
    id: str
    where: dict[str, list[str]] = field(default_factory=dict)

    def matches(self, features: dict[str, Any]) -> bool:
        """Evaluates whether features satisfy the cohort predicates.
        AND across feature keys, OR within the list of values for each key.
        """
        for feature_key, allowed_values in self.where.items():
            val = str(features.get(feature_key, ""))
            if allowed_values and val not in allowed_values:
                return False
        return True


class CohortEvaluator:
    """Shared evaluator matching leads to cohorts in priority order."""

    def __init__(self, cohorts: list[Cohort]):
        self.cohorts = cohorts

    @classmethod
    def from_config(cls, config: dict) -> CohortEvaluator:
        raw_cohorts = config.get("cohorts", [])
        parsed = [
            Cohort(id=c["id"], where=c.get("where", {}))
            for c in raw_cohorts
            if "id" in c
        ]
        return cls(parsed)

    def evaluate(self, features: dict[str, Any]) -> str:
        """Evaluates lead features against defined cohorts in order; defaults to 'default'."""
        for cohort in self.cohorts:
            if cohort.matches(features):
                return cohort.id
        return "default"
