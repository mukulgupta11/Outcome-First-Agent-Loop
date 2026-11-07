# Grade report — public_02_incidents

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.326 | +38.2% | 0.247 | +36.2% | 0.457 | -2.3% | -7% | 127 (348) |
| nw_kyc | 0.474 | +26.9% | 0.303 | +29.2% | 0.587 | -3.6% | 0% | 28 (29) |
| nw_renewal | 0.429 | -0.9% | 0.267 | +1.8% | 0.542 | -2.2% | 3% | 64 (54) |
| ALL | 0.392 | +18.4% | 0.265 | +20.5% | 0.515 | -3.2% | -1% | 219 (431) |

## Changes applied by the controller

18 applied — neutral: 6, beneficial: 8, harmful: 4

- ks_cancel v2 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v3 (controller_approved) dims=['welcome_message'] engageΔ=+0.014 didΔ=+0.000 → **beneficial**
- nw_kyc v2 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.125 → **beneficial**
- nw_renewal v2 (controller_approved) dims=['voice'] engageΔ=-0.030 didΔ=+0.000 → **harmful**
- nw_renewal v3 (controller_approved) dims=['welcome_message'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v4 (controller_approved) dims=['voice'] engageΔ=+0.031 didΔ=+0.000 → **beneficial**
- ks_cancel v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.213 → **beneficial**
- ks_cancel v5 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.310 → **beneficial**
- nw_kyc v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.235 → **beneficial**
- nw_kyc v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.002 → **neutral**
- ks_cancel v7 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.051 → **harmful**
- ks_cancel v8 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.257 → **harmful**
- ks_cancel v9 (controller_approved) dims=['caller_id', 'prompt_version'] engageΔ=-0.051 didΔ=+0.436 → **harmful**
- nw_kyc v5 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.002 → **neutral**
- ks_cancel v10 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v6 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.041 → **beneficial**
- nw_renewal v7 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.046 → **beneficial**
- ks_cancel v11 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**

## Governance

```
{
  "approval_requests": 19,
  "decisions": {
    "approved": 15,
    "rejected": 2,
    "expired": 1
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 2
  },
  "applied_via_approval": 15,
  "auto_applied": 3,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 2,
    "nw_kyc": 2,
    "ks_cancel": 2
  }
}
```

## Incidents

```
[
  {
    "type": "did_flagged",
    "workflow": "ks_cancel",
    "at": "D2 10:00",
    "flagged_did_share_by_day": {
      "2": 0.131,
      "3": 0.0,
      "4": 0.114,
      "5": 0.0,
      "6": 0.0,
      "7": 0.0,
      "8": 0.0,
      "9": 0.0
    }
  },
  {
    "type": "supervisor_edit",
    "workflow": "ks_cancel",
    "at": "D3 11:30",
    "mttr_minutes": 1620,
    "fixed_by": "controller_approved"
  },
  {
    "type": "supervisor_edit",
    "workflow": "nw_renewal",
    "at": "D5 15:00"
  }
]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{}
```

Credits used: 6 · validation runs: 3 · LLM fix calls: 1