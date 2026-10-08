# Data Platform warehouse access for CS Platform — grants runbook

Status: **applied + live (2026-10).** This runbook records the cross-account access that
makes NDR (#16) and live ML churn (#5) work, so it is reproducible and does not silently
drift. It has two parts: (1) the IAM role (codified in
`cs-platform-churn-reader.tf.example`), and (2) the Redshift object grants (below -
Redshift grants are not IAM/Terraform-managed; they live in the database).

> NOTE (2026-10-07): the `rpt.rpt_account_ndr_monthly` SELECT grant below was found to be
> MISSING in the live warehouse (the churn-reader role could read `marts` but not `rpt`,
> so the whole-book metrics batch returned 0 rows and portfolio NDR + licence utilisation
> showed "no data"). It was applied this session via the Data API and verified
> (`HAS_TABLE_PRIVILEGE` = true; the batch warm then loaded 10,396 accounts). If the
> warehouse is ever rebuilt, re-run ALL grants below - a missing `rpt` grant fails
> silently (0 rows), not loudly.
>
> NOTE (2026-10-08): the SELECT grants were found MISSING AGAIN - this time BOTH
> `rpt.rpt_account_ndr_monthly` and `marts.int_ds_account_churn_scoring`
> (`HAS_TABLE_PRIVILEGE` = false), with portfolio NDR back to "no data". Root cause: the
> two tables are owned by the `dbt` role and are **DROP+recreated on every dbt run**, which
> drops their table-level ACLs. Re-running the one-time GRANTs only fixes it until the next
> dbt build. The DURABLE fix (section 2a below) is `ALTER DEFAULT PRIVILEGES FOR USER dbt`,
> so every FUTURE dbt-created table in `rpt`/`marts` auto-grants SELECT to the reader. Both
> the one-time re-grant and the default privileges were applied + verified this session
> (`HAS_TABLE_PRIVILEGE` = true for both; NDR back to 87.1% live). Schema USAGE survived
> (it is granted on the schema, not the rebuilt tables), so only table SELECT was lost.

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

## 2a. DURABLE grants that survive dbt rebuilds (REQUIRED - 2026-10-08)
The two metrics/churn tables are owned by the `dbt` role and are DROP+recreated on every
dbt run, which drops the table-level SELECT grants from section 2 (observed twice; see the
dated notes at the top). Re-running section 2 only patches it until the next dbt build.

`ALTER DEFAULT PRIVILEGES` makes it self-healing: any table that `dbt` creates in `rpt` or
`marts` from now on automatically grants SELECT to the reader, so a rebuild no longer breaks
NDR/churn. Apply ONCE as a Data Platform admin (default privileges are keyed to the creating
role, hence `FOR USER dbt`):

```sql
ALTER DEFAULT PRIVILEGES FOR USER dbt IN SCHEMA rpt   GRANT SELECT ON TABLES TO "IAMR:cs-platform-churn-reader";
ALTER DEFAULT PRIVILEGES FOR USER dbt IN SCHEMA marts GRANT SELECT ON TABLES TO "IAMR:cs-platform-churn-reader";
ALTER DEFAULT PRIVILEGES FOR USER dbt IN SCHEMA stg   GRANT SELECT ON TABLES TO "IAMR:cs-platform-churn-reader";
```

Notes:
- Default privileges apply to tables created AFTER this statement, so pair it with a
  one-time section-2 re-grant to fix the CURRENTLY-existing tables (both were applied
  together this session). After the next dbt build, section 2 should no longer be needed.
- `dbt` is the live owner of `rpt.rpt_account_ndr_monthly` and
  `marts.int_ds_account_churn_scoring` (verified via `pg_class.relowner`). If the ETL/dbt
  owner role is ever renamed, re-issue these two statements `FOR USER <new-owner>`.
- Verify it stuck: `SELECT has_table_privilege('IAMR:cs-platform-churn-reader',
  'rpt.rpt_account_ndr_monthly','SELECT');` should be `t`, and should REMAIN `t` after a
  dbt rebuild (that is the whole point).
- These are still read-only and additive; nothing here grants write.

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
- **dbt table rebuilds (the common case):** `rpt.rpt_account_ndr_monthly` and
  `marts.int_ds_account_churn_scoring` are DROP+recreated by dbt and lose their section-2
  table grants each build. Section **2a** (`ALTER DEFAULT PRIVILEGES FOR USER dbt`) is the
  standing fix so this self-heals; keep it applied. If NDR/churn ever shows "no data" again,
  first check `has_table_privilege(...)` on both tables - if false, section 2a was lost
  (e.g. dbt owner renamed) and must be re-applied, then re-run section 2 for the current
  tables.
- Nothing here grants write; the reader cannot mutate warehouse data.
- **CRITICAL (verified 2026-10):** modifying the IAM role — *even a tag-only change* — causes
  Redshift to recreate the mapped `IAMR:cs-platform-churn-reader` DB user on the next assume,
  which **drops these object grants**. Any `terraform apply` / role edit MUST be followed by
  re-running section 2 and verifying section 4. This was observed when bringing the role under
  Terraform: the tag flip wiped the grants; they were re-applied and NDR/churn confirmed live
  again (734,867 NDR rows, 10,280 churn, 91% sample). Treat section 2 as a mandatory post-step
  for any role change.
