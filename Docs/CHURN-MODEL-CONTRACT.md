# Redshift Churn Model Contract

The CS platform reads a production churn score through the Redshift Data API.
It does not write to Redshift or invoke model training.

## Required view

Publish a business-facing view in the data platform account:

```text
dwh.marts.cs_account_churn_scores
```

Required columns:

| Column | Type | Meaning |
| --- | --- | --- |
| `nk_ja_account` | `varchar` | Canonical `AUx-yyyyy` account identifier |
| `churn_probability` | numeric | Predictive probability from `0.0` to `1.0` |
| `model_version` | `varchar` | Model/version used to produce the score |
| `scored_at` | timestamp | Score generation time |
| `top_driver_1` | `varchar` | Optional primary risk driver |
| `top_driver_2` | `varchar` | Optional secondary risk driver |

The view should expose one or more scores per account. The platform selects the
most recent row by `scored_at`.

The existing `marts.int_ds_account_churn_scoring` view is historical churn
status/features data and is supported in **status mode**. When configured in
status mode, `calculated_churn_status = 'Churned'` creates a Must Protect task;
the platform does not display a fabricated probability. The existing
`marts.int_ds_churn_prediction` view is marked data-science/training-only and
must not be used as the CS production source.

## Platform configuration

Set these non-secret environment variables after the view is published:

```env
AWS_PROFILE=DataPlatform
AWS_REGION=ap-southeast-2
REDSHIFT_DATABASE=dwh
REDSHIFT_WORKGROUP=data-platform-redshift-warehouse-wg-prod
REDSHIFT_CHURN_TABLE=marts.cs_account_churn_scores
REDSHIFT_CHURN_ID_COLUMN=nk_ja_account
```

For the current status view, use these values instead:

```env
REDSHIFT_CHURN_TABLE=marts.int_ds_account_churn_scoring
REDSHIFT_CHURN_ID_COLUMN=nk_ja_account
REDSHIFT_CHURN_MODE=status
REDSHIFT_CHURN_STATUS_COLUMN=calculated_churn_status
```

The AWS identity should have only `redshift-data:ExecuteStatement`,
`redshift-data:DescribeStatement`, and `redshift-data:GetStatementResult`, with
the database user restricted to `USAGE` on `marts` and `SELECT` on this view.

## Acceptance checks

The connection is ready when:

1. The integration page reports `Churn Model (Redshift)` as connected and read-only.
2. An account detail shows `ml_churn_score`, `model_version`, and top drivers.
3. A Strategic account at or above 70% creates a Priority 1 `MUST_PROTECT` task.
4. The platform can still fall back to its transparent weighted-signal score when
   the view is unavailable.