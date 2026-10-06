# Data Platform warehouse access for CS Platform — grants runbook

Status: **applied + live (2026-10).** This runbook records the cross-account access that
makes NDR (#16) and live ML churn (#5) work, so it is reproducible and does not silently
drift. It has two parts: (1) the IAM role (codified in
`cs-platform-churn-reader.tf.example`), and (2) the Redshift object grants (below —
Redshift grants are not IAM/Terraform-managed; they live in the database).

Account: Data Platform **503561421603** · region ap-southeast-2
Workgroup: `data-platform-redshift-warehouse-wg-prod`
(id `96c87b2d-6d6d-4003-b919-f5b8d0ee39d0`) · database `dwh`

## 1. IAM role (Terraform)
`cs-platform-churn-reader` in 503561421603, trusting the CS Platform task role
`arn:aws:iam::350067031910:role/cs-platform-task`, with least-privilege Redshift Data API
read + `redshift-serverless:GetCredentials` scoped to the prod workgroup. Apply via the
`.tf.example` stack (copy to `cs-platform-churn-reader.tf`, `AWS_PROFILE=DataPlatform
terraform apply`). It is idempotent with the role that already exists.

**Status (2026-10): the role is now Terraform-managed.** It was `terraform import`ed
(`aws_iam_role.churn_reader` + `aws_iam_role_policy.churn_read`) and applied; the live role
carries `IaC=terraform`. Trust (`cs-platform-task`) and the read policy were verified
unchanged by the apply. CAVEAT: this stack currently uses **local state** (no remote
backend). The Data Platform team should configure a remote backend (S3 + DynamoDB in
503561421603) for durable, shared state; `.gitignore` keeps local state/rendered `.tf` out
of git in the meantime.

## 2. Redshift object grants
Redshift maps the assumed IAM role to a DB user named `IAMR:cs-platform-churn-reader`,
auto-created on first assume. It needs least-privilege read on exactly the objects the
platform queries:

| Object | Why |
| --- | --- |
| `rpt.rpt_account_ndr_monthly` | NDR (current vs prior-year revenue) |
| `marts.int_ds_account_churn_scoring` | ML churn score (a view over `stg`) |
| `stg.stg_jobadder_all_accounts` | underlying table the churn view reads |

Apply once as a Data Platform admin (Redshift Data API or query editor):

```sql
GRANT USAGE  ON SCHEMA rpt   TO "IAMR:cs-platform-churn-reader";
GRANT SELECT ON TABLE  rpt.rpt_account_ndr_monthly        TO "IAMR:cs-platform-churn-reader";
GRANT USAGE  ON SCHEMA marts TO "IAMR:cs-platform-churn-reader";
GRANT SELECT ON TABLE  marts.int_ds_account_churn_scoring TO "IAMR:cs-platform-churn-reader";
GRANT USAGE  ON SCHEMA stg   TO "IAMR:cs-platform-churn-reader";
GRANT SELECT ON TABLE  stg.stg_jobadder_all_accounts      TO "IAMR:cs-platform-churn-reader";
```

Example apply via the Data API (admin creds):
```bash
aws redshift-data execute-statement \
  --profile DataPlatform --region ap-southeast-2 \
  --database dwh --workgroup-name data-platform-redshift-warehouse-wg-prod \
  --sql 'GRANT USAGE ON SCHEMA rpt TO "IAMR:cs-platform-churn-reader";'
# ...repeat per statement above.
```

## 3. CS Platform side (already set)
`REDSHIFT_ASSUME_ROLE_ARN=arn:aws:iam::503561421603:role/cs-platform-churn-reader` is the
default in `infra/variables.tf`; the task role already has `sts:AssumeRole` on it. No
change needed.

## 4. Verify
Run a read-only one-shot ECS task (or the app) and confirm:
- `rpt.rpt_account_ndr_monthly` returns rows with `revenue_for_the_previous_year` populated
  (NDR shows a real % — verified 91% sample);
- `marts.int_ds_account_churn_scoring` returns scored accounts (verified 10,278).

## 5. Drift / least-privilege notes
- These grants are **additive and minimal** — only three objects, read-only. If the churn
  view's underlying `stg` dependency changes, grant SELECT on the new underlying object.
- If the Data Platform ever rebuilds the warehouse users, re-run section 2.
- Nothing here grants write; the reader cannot mutate warehouse data.
