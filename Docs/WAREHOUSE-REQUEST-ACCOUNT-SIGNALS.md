# Warehouse request — Enhanced Profile & Event Availability signals

## Why
The per-account scorecard (CS Day-to-Day → Accounts) shows an **Intelligence & Product
Signals** panel to reach parity with JobAdder's native Account dashboard. Two signals are
currently rendered as an honest **"Not connected"** because no warehouse column exists for
them yet:

- **Enhanced Profile**
- **Event Availability** (corporates)

The other two signals in the panel — **Adder Intelligence Match** and **AI Float** — are
already live, sourced from existing boolean columns on the account dimension
(`is_ai_matching_enabled`, `is_floats_enabled`). This request asks the Data Platform team
to add the two missing booleans in the same shape so the app can light them up with a
one-line change each (no schema guessing on our side).

## What we need
Add two boolean columns to the account dimension the platform already reads:

| Table | Column (requested) | Type | Semantics |
|---|---|---|---|
| `marts.snp_jobadder_all_accounts` | `is_enhanced_profile_enabled` | BOOLEAN | Account has the Enhanced Profile product/feature enabled |
| `marts.snp_jobadder_all_accounts` | `is_event_availability_enabled` | BOOLEAN | Corporate account has Event Availability enabled |

Notes:
- **Same table/grain** as the existing AI flags: the SCD2 dimension
  `marts.snp_jobadder_all_accounts`, latest snapshot `dbt_valid_to IS NULL`, keyed on
  `UPPER(pk_instance) || '-' || client_id`.
- **BOOLEAN, nullable.** NULL is fine where unknown — the app renders NULL as "unknown"
  and only shows Enabled/Disabled for a true boolean. Please do **not** coalesce to FALSE;
  a real NULL is more honest than a fabricated "disabled".
- Column names above are our suggestion for consistency with `is_ai_matching_enabled` /
  `is_floats_enabled`. If the source system names them differently, just tell us the final
  names and we'll map to them.
- Event Availability is corporate-only; a NULL for non-corporate accounts is expected and
  the app will treat it as not-applicable.

## App change once the columns exist (for reference)
Small, already-scoped:
1. `plugins/cs-orchestrator/adapters/sources.py` — add the two columns to the
   `dimension()` SELECT and the returned dict (next to `is_ai_matching_enabled`).
2. `platform/engine.py account_performance()` — replace the two hardcoded `not_connected`
   stubs with:
   ```python
   "enhanced_profile": _bool_signal("is_enhanced_profile_enabled", "Enhanced Profile"),
   "event_availability": _bool_signal("is_event_availability_enabled", "Event Availability (Corporates)"),
   ```
No UI change needed — the signal cards already render these keys.

## Status
- Blocked on: the two columns above landing in `marts.snp_jobadder_all_accounts`.
- Not blocked: everything else on the scorecard (identity, Users, performance, benchmark,
  usage, tickets, Adder Intelligence Match, AI Float) is live.
