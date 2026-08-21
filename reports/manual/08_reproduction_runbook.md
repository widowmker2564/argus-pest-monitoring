# Chapter 8 — Reproducing the system from scratch

This chapter is the from-zero runbook: how to stand up the complete ARGUS stack in a NEW AWS account, by the automated path (the ARGUS deployer) or the manual path (console + CLI, resource by resource).

_As of 2026-08-11._

## 8.1 Role in the system

This runbook is no longer theory. On 2026-08-10 it was executed for real: the full stack was deployed into the NP production account `506868652945` (IAM user `Student_QianRunzhe`, CLI profile `prod`) — all 15 stages green in 103 seconds, zero errors — and the days after ran the complete post-deploy sequence: SES identity verification, Cognito user creation, server-side training-data copy, and retraining of both detection models on the new account. The manual therefore describes two accounts:

- **`366356442579`** (`us-east-1`, CLI profile `prod`) — the development/reference account where the system was built and validated. Chapters 1–7 describe it.
- **`506868652945`** — the NP production account being stood up (in progress; the cloud stack and both retrained models are already live there). Device repointing is the main remaining step (Section 8.9.3).

Every account-bound value changes between accounts: account ID, bucket names, API Gateway ID, Cognito IDs, CloudFront domain, and — critically — the Rekognition Custom Labels models, which cannot be copied between accounts at all (Section 8.9).

Two reproduction paths exist and produce the same stack:

- **Path A (recommended): the ARGUS deployer.** One Windows executable runs 15 idempotent stages end to end and writes every generated ID back into the dashboard config before syncing it. This path has been executed for real twice: a full 15/15-stages-green deployment into a fresh, self-registered AWS account (`CAG_Test`, `324908170757`) on 2026-07-16, and the production migration into `506868652945` on 2026-08-10 (Section 8.4.4).
- **Path B: manual.** The same 15 steps done by hand, console click-path plus CLI for each, in the dependency order from `deployer/STACK_MANIFEST.md` §2. Use this if the exe is unavailable, if a stage needs debugging, or to understand exactly what Path A does.

Both paths end at the same place: a live dashboard URL over HTTPS, an empty Rekognition project waiting for training, and a day-1 task list (Section 8.5). The cloud stack is complete and testable without any robot or camera — the dashboard's Test upload exercises the full detection rail. Device provisioning (Go2/Orin, mini PC) is a separate, optional layer (Section 8.8).

## 8.2 Inventory

Paths are relative to the project repository root.

| Component | Location | Purpose |
|---|---|---|
| `ARGUS.exe` | `deployer/dist/ARGUS.exe` | The one-file deployer app (pywebview shell + deploy engine). Built by `build.ps1`. Rebuilt 2026-08-11 with the Bedrock policy, the per-camera watchdog, and the v6.3 Lambda sources. |
| `ARGUS-Setup-1.0.0.exe` | `deployer/dist/installer/ARGUS-Setup-1.0.0.exe` | Inno Setup installer for the exe: Program Files install, shortcuts, uninstaller, auto-installs the WebView2 runtime when missing. Built by `build_installer.ps1`. |
| `deploy.py` | `deployer/deploy.py` | The 15-stage boto3 engine. Also runnable headless (`python deploy.py ...`). Contains `verify()` (post-deploy audit) and `destroy()` (teardown). |
| `training.py` | `deployer/training.py` | The Train-screen engine: YOLO folder → validated Rekognition dataset → training → `custom_model_arn` write-back. |
| `STACK_MANIFEST.md` | `deployer/STACK_MANIFEST.md` | The recreate-level bill of materials, audited from the live account. Path B follows its §2 creation order. |
| `deployer/audit/*.json` | `deployer/audit/` | Raw per-resource config JSONs (IAM policies, table specs, CORS, CloudFront config). Path B reuses them as CLI inputs after rewriting account/region/bucket literals. |
| Lambda code mirror | `lambda/` | Source of 4 functions (`pest-detection-processor.py`, `pest-monitoring-api.py`, `pest-camera-scheduler.py`, `pest-model-watchdog.py`) plus reusable policy files. |
| `kvs-hls-handler` source | `deployer/audit/kvs-hls-handler_src/lambda_function.py` | The fifth function's code. NOT in `lambda/` — it lives under `deployer/audit/`. |
| `lambda/cors.json` | `lambda/cors.json` | API Gateway CORS configuration, reusable as-is for `create-api`. |
| `lambda/ddb-policy.json` | `lambda/ddb-policy.json` | The api role's 6-verb DynamoDB policy (`pest-monitoring-ddb-full-access`). Account ID inside must be rewritten. |
| Bedrock verify policy | `deployer/audit/iam__pest-detection-processor-role__inline__bedrock-verify.json` | Bedrock invoke policy for the LLM verification gate (Sonnet 4.6 + Haiku 4.5 inference profiles). Since 2026-08-11 the deployer attaches it automatically (Section 8.9.4). |
| `REHEARSAL.md` | `deployer/REHEARSAL.md` | The two-round rehearsal protocol and its success criteria; the 2026-08-10 production migration served as Round 2 on the fresh-account axis. |
| Dashboard source | `web/dashboard_v4/` | The 15 static files the deployer templates and syncs to the new dashboard bucket. |
| Deploy state | `deployer/out/deploy_state.json` (dev) or `%LOCALAPPDATA%\ARGUS\out\deploy_state.json` (exe) | Every generated ID from a run. Input to verify/resume/teardown. |
| Migration toolkit | `migration/` | The scripts that executed the 2026-08-10/11 production migration: `copy_training_data.py` (server-side training-data copy + grant/revoke), `train_v9r_on_prod.py` (dataset-export retrain), `poll_training.py` (training-status poller), `migrate_moth.py` (moth model rebuild), `migrate_jewel_records.py` (on-site detection-record migration), `diff_accounts.py` (old-vs-new audit), `prod_baseline_20260810.json` (frozen reference config). |

## 8.3 Prerequisites (both paths)

1. **A Windows machine.** Windows 10/11. The app uses Edge WebView2 (preinstalled on Windows 11 and current Windows 10). Two artifacts exist: the bare `ARGUS.exe` (`deployer/dist/`), which assumes WebView2 is present, and the installer `ARGUS-Setup-1.0.0.exe` (`deployer/dist/installer/`), which auto-installs the WebView2 runtime when missing (bundled evergreen bootstrapper). On an unknown machine, use the installer.
2. **The ARGUS.exe** (Path A), or Python 3.12+ with `boto3` and the AWS CLI v2 (Path B / headless Path A). Rebuild the exe with `powershell -ExecutionPolicy Bypass -File build.ps1` in `deployer/` if needed; the script prebuilds `layer/fyp-pillow.zip` on first run. Close any running ARGUS.exe first — PyInstaller cannot replace a running file.
3. **A fresh AWS account.** Path A's embedded AWS window can guide account signup inside the app (email verification, payment card typed directly into AWS's own page, phone code, Basic plan). AWS signup cannot be automated — CAPTCHA, card, and phone are always manual. An institutionally issued account works the same way: the NP production account `506868652945` arrived with an IAM user (`Student_QianRunzhe`) already in an AdministratorAccess group, so step 4 was already satisfied there.
4. **An IAM user with AdministratorAccess — never root.** Create a user (e.g. `deployer`), attach the AWS-managed `AdministratorAccess` policy, create an access key (CLI type), and keep the secret for the paste step. The ARGUS keys screen walks this end to end and **refuses root keys outright**; it also preflights that the pasted key actually has AdministratorAccess (including via group membership) so a rights-less key fails at paste time, not 8 stages in.
   - Console: **IAM → Users → Create user** → name `deployer` → **Attach policies directly** → `AdministratorAccess` → create → **Security credentials → Create access key → Command Line Interface**.
   - CLI (from any machine already holding credentials for the new account):
   ```
   aws iam create-user --user-name deployer --profile **prod**
   aws iam attach-user-policy --user-name deployer --policy-arn arn:aws:iam::aws:policy/AdministratorAccess --profile **prod**
   aws iam create-access-key --user-name deployer --profile **prod**
   ```
   The secret access key is shown once. Store it in the app (it lands in Windows Credential Manager via DPAPI, never plaintext on disk) or in an AWS CLI profile. Never write it into any file in this repo.
5. **An alert email address** you can open — SES sends a verification link to it during deployment.
6. **Read Section 8.9 (account-portability caveats) before starting.** In particular: no trained model migrates; the new account must retrain.

## 8.4 Path A — automated deployment with ARGUS

### 8.4.1 The wizard flow

Run `ARGUS.exe`. The screens, in order:

1. **Welcome** — the opening screen the app lands on; Start continues to Consent.
2. **Consent** — checkbox + Terms/Privacy folds. Decline exits.
3. **Account** — optional embedded AWS signup (skip if the account exists).
4. **Keys** — the 4-step IAM rail; paste the access key ID + secret. STS liveness check + root-key refusal + AdministratorAccess preflight run here.
5. **Config** — deployment name, region, resource prefix (lowercase, 2–21 chars, `[a-z][a-z0-9-]*`), detection target label (the object class you will later train, e.g. `armyworm-larva`), alert email (required, validated), live-view on/off.
6. **Review** — the plan plus the cost estimate (pricing appears here and nowhere else).
7. **Hold-to-deploy** — the 15 stages stream into the aperture "theater" UI.
8. **Done** — real dashboard URL, accounts panel, Train screen entry, "Run a system check", Danger zone (teardown).

### 8.4.2 The 15 stages (what actually happens)

Stage order is fixed in `deploy.py` (`STAGES`); every stage is idempotent — an existing resource is adopted, not duplicated, so re-running after a failure resumes safely.

| # | Stage | UI label | Creates |
|---|---|---|---|
| 1 | `iam` | Identity & permissions | 5 Lambda execution roles + inline policies (from the rewritten `deployer/audit/` documents) + the `pest-scheduler-invocation-role` (trust: `scheduler.amazonaws.com`, invoke on both `pest-model-watchdog` and `pest-camera-scheduler`). Since 2026-08-11 the processor role also gets the live `bedrock-verify` and `s3-frames-write` inline policies, so the LLM verification gate works on a fresh stack without manual IAM work (Section 8.9.4). |
| 2 | `dynamodb` | Database | 4 tables, PAY_PER_REQUEST: `pest-monitoring-cameras`, `pest-monitoring-detections` (+ GSI `by-pest-time`), `pest-monitoring-system-config`, `pest-monitoring-schedule-logs`. |
| 3 | `s3` | Storage | 3 buckets: `**PREFIX**-frames-**ACCT**`, `**PREFIX**-processed-**ACCT**` (private, versioned, SSE-S3), `**PREFIX**-dashboard-**ACCT**` (public-read website, PAB disabled first — ordering trap). |
| 4 | `layer` | Image-processing library | Publishes the `fyp-pillow` layer (Pillow 12.2.0, cp312, manylinux2014). The exe uses the prebuilt bundled zip; a dev run pip-builds it and asserts `PIL/_imaging*.so` exists (the exact defect that broke layer v1). |
| 5 | `lambda` | Cloud functions | The 5 functions, `pest-camera-scheduler` first (the api's `SCHEDULE_EXECUTOR_ARN` env references it). Retries through IAM eventual consistency. Since 2026-08-10 the processor deploys at the validated production sizing (1024 MB / 600 s — a tiled frame plus up to 120 verify crops measures 24–54 s) with the full detection-tuning env: Sonnet 4.6 verifier, `TILE_MIN_CONFIDENCE=8`, `LLM_VERIFY_ALL_BOXES=true`, `MAX_BOXES=120`, `POST_NMS_IOU/CONTAIN=0.1`, `POST_MAX_BOX_AREA=0.05`, `POST_VERIFY_FLOOR=49`. The watchdog ships from the repo mirror `lambda/pest-model-watchdog.py` (per-camera `max_runtime_min`). |
| 6 | `s3-notification` | Event wiring | `lambda add-permission` for `s3.amazonaws.com` FIRST, then the frames-bucket `s3:ObjectCreated:*` notification on prefix `frames/` → `pest-detection-processor`. |
| 7 | `cognito` | Sign-in service | Pool `pest-dashboard-users` (email sign-in, admin-create-only) + SPA client `dashboard-web` (no secret, USER_PASSWORD_AUTH, 12 h tokens). Zero users seeded. |
| 8 | `apigw` | Secure API | HTTP API `pest-monitoring-api-gateway`: CORS, JWT authorizer `cognito-dashboard`, 2 integrations, 21 routes (JWT on all but `GET /stream/status`), `$default` auto-deploy stage, 2 invoke permissions. |
| 9 | `cloudfront` | Dashboard & delivery | Distribution over the dashboard bucket's **S3 website endpoint** (http-only origin), Managed-CachingDisabled policy, fresh CallerReference. Takes minutes to reach Deployed. |
| 10 | `writeback` | Configuration | Rewrites `js/config.js` (`HTTP_API`, `COGNITO_REGION`, `COGNITO_CLIENT_ID`), uploads all `web/dashboard_v4/` files to the dashboard bucket, sets the frames-bucket CORS to the new website + CloudFront origins. |
| 11 | `kvs` | Live video | Only with live-view enabled: stream `**PREFIX**-cam-stream`, 2 h retention. |
| 12 | `scheduler` | Cost guard | EventBridge Scheduler `pest-model-watchdog-15min`, `rate(15 minutes)`, target = the watchdog via the invocation role. |
| 13 | `rekognition` | Vision engine | Empty Custom Labels project `**PREFIX**-detection`. No model — training is a later, customer-driven step. |
| 14 | `seed` | Initial data | 1 system-config row (`detection_settings`: `email_enabled=true`, `recipient_email`, `auto_capture=true`, `capture_interval=60`) + 2 camera rows: `manual_upload` and `camera-1` (label = deployment name, `custom_model_arn=''` until training). Since 2026-08-10 both rows seed the validated detection config: `min_confidence=10` (the CANDIDATE floor feeding the gate — seeding it high silently strangles recall), `llm_verify_enabled=true`, `post_verify_floor=49`; the real camera also gets `tiling_enabled=true` and `max_runtime_min=45`. The code is the truth if any doc lags. |
| 15 | `ses` | Alerts | `verify-email-identity` for sender + recipient (sends the confirmation email) and prints the sandbox warning. It does not wait for you to click the link. |

Every generated ID is written to `deploy_state.json` as it appears. On the exe the file lives at `%LOCALAPPDATA%\ARGUS\out\deploy_state.json`.

### 8.4.3 Headless equivalent

The same engine runs without the app on any machine with Python + boto3 and an AWS CLI profile for the new account. This is exactly how the 2026-08-10 production migration ran:

```
python deployer/deploy.py --profile **prod** --prefix argus --target-label armyworm-larva --sender-email **alerts@np.edu.sg** --recipient-email **you@np.edu.sg** --deployment-name "Jewel Forest Valley"
```

(Substitute your own profile, emails, and deployment name; add `--live-view` if the KVS live-video layer is wanted — the production run omitted it.)

Useful flags: `--dry-run` (print the plan, change nothing), `--only s3,lambda` (run selected stages; a subset run never writes the completion stamp), `--verify` (read-only audit of an existing deployment: prints `[OK ]`/`[MISS]` per resource, then `N/N present`, plus "deployment is INCOMPLETE" if anything is missing).

### 8.4.4 The verified precedents

**First precedent — `CAG_Test` (2026-07-15/16).** The full Path A flow first ran on a fresh self-registered account (`CAG_Test`, `324908170757`):

- Run 1: 9/15 stages created live (iam through cloudfront), then `writeback` failed — the exe bundle was missing `web/dashboard_v4` (build bug, fixed in `build.ps1`).
- Run 2 (resume): failed at `apigw` — the route-adopt branch stripped a required parameter, a path only reachable on resume. Fixed 2026-07-16.
- Run 3 (resume): **15/15 complete** — the product's first successful end-to-end deployment. Existing resources adopted, the remaining six stages green first time.

Post-deploy management (accounts panel, teardown, verify audit) shipped and was exercised the same day. The `CAG_Test` account was later closed.

**Second precedent — the real production migration (2026-08-10).** The headless command in 8.4.3 deployed the stack into the NP production account `506868652945`: **all 15 stages, 103 seconds, zero errors, first run**. This served as REHEARSAL.md Round 2 on the fresh-account axis (though not the clean-machine axis). What it created:

- Dashboard: `https://d1dtoxef7qmugl.cloudfront.net` (CloudFront `E1YADURLSAVNFA`)
- API: `https://vzfl7s6z00.execute-api.us-east-1.amazonaws.com` (21 routes, JWT on all but `GET /stream/status`)
- Cognito pool `us-east-1_9selFDHpc`, client `6vebotf45bp8u46cnraddiaplv`
- Buckets `argus-frames-506868652945`, `argus-processed-506868652945`, `argus-dashboard-506868652945`
- Empty Rekognition project `argus-detection`; 6 roles, 4 tables, `fyp-pillow` layer v1, 5 Lambdas, the S3→processor notification, the watchdog schedule
- KVS skipped (deployed without `--live-view`)

Post-deploy verification passed on every point that had previously been a gap: the processor came up at 1024 MB / 600 s with the layer attached, the env carried the full Sonnet 4.6 detection tuning, and both camera rows read `llm_verify_enabled=true, min_confidence=10, post_verify_floor=49`. Section 8.5 walks the post-deploy sequence this migration then executed.

## 8.5 Path A — day-1 post-deploy tasks (worked example: the 2026-08-10/11 production migration)

The deployer ends with a live but empty system. Four tasks make it usable. Every one of them has now been executed for real on the production account, so each subsection below pairs the generic procedure with what actually happened there.

### 8.5.1 Confirm the SES identity, note the sandbox

Open the "Amazon Web Services – Email Address Verification Request" email and click the link. Then check:

- Console: **Amazon SES → Identities** — the address must show **Verified**.
- CLI:
```
aws ses get-identity-verification-attributes --identities **alerts@np.edu.sg** --profile **prod** --region **us-east-1**
```

A fresh account is in the **SES sandbox**: max 200 messages/24 h, 1/sec, and mail can only be sent **to verified addresses**. For a demo this is fine (verify every recipient). To lift it, request production access — a manual AWS review, ~1–2 business days:

- Console: **Amazon SES → Account dashboard → Request production access**.
- CLI:
```
aws sesv2 put-account-details --production-access-enabled --mail-type TRANSACTIONAL --website-url **https://your-dashboard-url** --use-case-description "**pest detection alert emails to site staff**" --profile **prod** --region **us-east-1**
```

**Worked example:** the production account's alert identity was verified 2026-08-11 (the owner clicked the link; the identity read `SUCCESS` on the next check). The account remains in the sandbox — the reference account is too, so this is not a regression — meaning alert mail only reaches verified recipients until production access is requested.

### 8.5.2 Create dashboard users (the pool starts empty)

A fresh Cognito pool has zero users and self-signup is disabled, so nobody can log in until an account is admin-created. The ARGUS done screen's **accounts panel** lists/creates/removes users and sets the password as **permanent** (avoiding the FORCE_CHANGE_PASSWORD limbo that the dashboard's login flow does not handle).

- Console: **Amazon Cognito → User pools → pest-dashboard-users → Users → Create user** (email as username; then still set a permanent password via CLI below, or the user is stuck in the temporary-password state).
- CLI:
```
aws cognito-idp admin-create-user --user-pool-id **NEW_POOL_ID** --username **user@np.edu.sg** --message-action SUPPRESS --profile **prod** --region **us-east-1**
aws cognito-idp admin-set-user-password --user-pool-id **NEW_POOL_ID** --username **user@np.edu.sg** --password "**ChosenPassword1**" --permanent --profile **prod** --region **us-east-1**
```

Then open the CloudFront URL from the done screen and sign in.

**Worked example:** the production migration admin-created one user in pool `us-east-1_9selFDHpc` with a permanent password (status `CONFIRMED`, no forced change), and sign-in against `https://d1dtoxef7qmugl.cloudfront.net` was confirmed 2026-08-11. One lesson from that check is worth keeping: the dashboard shows the camera row's `label`, not its `camera_id`. The deployer had written the deployment name into the label, so the operator opened the new dashboard, saw no familiar camera name, and reasonably concluded the row was missing — the backend was fine the whole time. A display name IS functionality when it is the only handle the user has on a resource; set labels to what the operator expects.

### 8.5.3 Train a model (mandatory — no model migrates)

The new account's Rekognition project is empty. Use the ARGUS **Train screen** (done screen → "Train your model"): pick a labeled image folder (YOLOv8, Roboflow export, or flat layout auto-detected), review the analysis report (pairing, normalized-coords check, zero-box exclusion, >4096 px downscale), pass the typed-TRAIN cost gate, and watch the 6-phase rail to `TRAINING_COMPLETED`. The pipeline then **auto-wires the resulting `custom_model_arn` onto every `model_type=custom` camera row** and offers one-click rollback. Training is left STOPPED afterwards by design; start detection from the dashboard when needed (~US$4/hr while running; the watchdog stops idle models). Closing and reopening the app mid-training re-attaches to the watch. (The `ARGUS.exe` on disk was rebuilt 2026-08-11, so the built exe contains the Train screen; if a future rebuild's done screen ever lacks "Train your model", rebuild via `build.ps1`.)

The manual/CLI equivalent of this whole step is the Chapter 3 training pipeline (dataset build → GroundTruth manifests → `create-dataset` → `create-project-version` → write the ARN into the camera row).

**Worked example — how the production migration actually retrained both models.** The migration did not start from a local image folder; it reused the training data already in the reference account's S3, entirely server-side. The sequence generalizes to any account-to-account move:

**(a) Copy the training data server-side** (`migration/copy_training_data.py`). A temporary, prefix-scoped statement is merged into the OLD frames bucket's policy (by Sid, preserving existing statements) granting the NEW account's IAM user `s3:GetObject` on `training-data/v9/*` only, plus a prefix-conditioned `s3:ListBucket`. The new account then `copy_object`s every key under that prefix into `argus-frames-506868652945/training-data/v9/...` — the bytes never touch a laptop. The script also rewrites every manifest `source-ref` to point at the new bucket and verifies object counts. Result on 2026-08-10: **36,641/36,641 objects (4.27 GB) copied, 0 failed, counts match; 36,634 manifest source-refs repointed.** The read grant stays in place until the new model is signed off, then `python migration/copy_training_data.py --revoke` removes it (and only it).

**(b) The trap that would have wasted a multi-hour training run — the dataset, not the manifest, is the source of truth.** Any labels added or corrected **in the Rekognition console** after a manifest was uploaded exist only inside that account's DATASET; the S3 manifest is stale from that moment. Building the new account's dataset from the copied manifest would have silently trained an older labelling state. The correct method, scripted in `migration/train_v9r_on_prod.py`: export the live entries with **`ListDatasetEntries`** (the labelled truth), repoint every `source-ref`, upload the result as new manifests, then `create-dataset` from those. Generalize this: **after ANY console labelling, export the dataset — never trust the original manifest.**

**(c) Armyworm retrain.** With datasets built the correct way (TRAIN 32,986 labelled images, TEST 3,653), `v9r-prod-20260810` — the v9-generation model with added data augmentation (flips, rotations, exposure jitter; the documented 13x build) — was submitted on project `argus-detection` on 2026-08-10. At ~33k images this is a run of many hours; poll with `DescribeProjectVersions` (`migration/poll_training.py`) rather than watching the console. Outcome: TRAINING_COMPLETED 2026-08-12 10:04 SGT, Rekognition test-split F1 0.613 (development-account equivalent 0.599 — same recipe, treat as equivalent), and the version ARN was written into the `worm_cam` camera row. The post-training holdout validation re-push must be repeated: the first 2026-08-12 attempt was invalidated by the processor-role IAM gap fixed the same day (see 9.3.14).

**(d) Moth model rebuild** (`migration/migrate_moth.py`). The predecessor's moth model (`SmartPestProject`) is account-bound and cannot move, but its DATA survived intact in the old account's Rekognition console bucket (`custom-labels-console-us-east-1-d1abc2aed2`): 116 TRAIN + 29 TEST labelled images. The script copies them server-side (same temporary-grant pattern, `--revoke` after), exports the labelled dataset entries (dataset-is-truth again), creates project **`argus-moth-detection`**, trains **`moth-prod-20260811`**, and seeds the `moth_cam` camera row. Trained 2026-08-11 with **F1 = 0.991** vs the original's 0.988 — same data, so treat them as equivalent, and as proof that a Custom Labels capability is fully reconstructible on a new account from data alone. `llm_verify_enabled` is deliberately FALSE on `moth_cam`: the verification prompt describes larvae and this camera's target is adult moths.

When each training completes, write its version ARN into the matching camera row's `custom_model_arn` (the Train screen does this automatically; scripted runs do it with a `file://`-JSON `update-item`).

### 8.5.4 Wire cameras (optional, demo layer)

The stack detects on anything uploaded to `frames/**camera_id**/...` in the frames bucket — the dashboard Test upload already proves the rail. To attach real capture devices (Go2 + SIYI A8 Mini via the Jetson Orin, or the mini PC's Hikvision), provision the devices per Section 8.8 and repoint their config at the new account (Section 8.9.3).

**Worked example:** this is the one step the production migration has NOT yet completed — the Orin and mini PC still point at the reference account. During the migration the production camera rows were also aligned to the reference-account ids the devices and operators expect (`worm_cam`, `moth_cam`, `manual_upload`) with the reference labels and schedule restored, so a repointed device drops straight into a matching row. The KVS live-view layer was deliberately not deployed (Section 8.4.4), so `kvs_stream_name` is empty on the new account; create the stream (Path B Step 11) before repointing any `kvs_controller` if live view is wanted.

## 8.6 Path B — manual resource creation, in order

Follow the order exactly — it is the dependency order from `STACK_MANIFEST.md` §2 and the `deploy.py` stage list. Everything below assumes region **us-east-1**; substitute yours consistently. Placeholders in bold are yours to fill: **YOUR_ACCOUNT_ID**, profile **np**, prefix **vision** (or keep the reference names, e.g. `frames-armyworm-**YOUR_ACCOUNT_ID**` — names must be consistent everywhere, including Lambda env vars).

Before starting, prepare the policy inputs: copy the needed `deployer/audit/iam__*.json` files plus `lambda/ddb-policy.json` to a working folder and rewrite every occurrence of `366356442579` → **YOUR_ACCOUNT_ID**, `us-east-1` → **YOUR_REGION**, and the three reference bucket names → your bucket names. (`deploy.py Ctx.rewrite()` does exactly this mechanically.)

### Step 1 — IAM roles (everything else references these)

Six roles. The 5 Lambda execution roles share one trust policy (`deployer/audit/iam__trust-policy-lambda.json`); each gets `AWSLambdaBasicExecutionRole` attached plus its inline policies.

Console: **IAM → Roles → Create role → AWS service → Lambda** → attach `AWSLambdaBasicExecutionRole` → name → after creation, **Add permissions → Create inline policy → JSON** and paste each rewritten document.

CLI pattern (repeat per role/policy):

```
aws iam create-role --role-name pest-detection-processor-role --assume-role-policy-document file://deployer/audit/iam__trust-policy-lambda.json --profile **prod**
aws iam attach-role-policy --role-name pest-detection-processor-role --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole --profile **prod**
aws iam put-role-policy --role-name pest-detection-processor-role --policy-name pest-detection-processor-policy --policy-document file://**rewritten\iam__pest-detection-processor-role__inline__pest-detection-processor-policy.json** --profile **prod**
```

| Role | Inline policies (rewritten audit file / source) |
|---|---|
| `pest-detection-processor-role` | `pest-detection-processor-policy`, `read-system-config`, `bedrock-verify` (LLM gate — Sonnet 4.6 + Haiku 4.5 inference-profile ARNs, account/region rewritten), `s3-frames-write` |
| `pest-monitoring-api-role` | `pest-monitoring-api-policy`, `pest-monitoring-ddb-full-access` (**reuse `lambda/ddb-policy.json`**, account rewritten), `read-processed-armyworm` |
| `pest-camera-scheduler-role` | `pest-camera-scheduler-policy` |
| `kvs-hls-handler-role` | `kvs-hls-policy` (from the managed `kvs-hls-handler-rolePolicy` audit file; inline is fine) |
| `pest-model-watchdog-role` | `rekognition-db-handler` |

Then the **scheduler invocation role** `pest-scheduler-invocation-role`: trust `scheduler.amazonaws.com` with condition `aws:SourceAccount = **YOUR_ACCOUNT_ID**`; one inline policy `invoke-scheduled-lambdas` allowing `lambda:InvokeFunction` on **both** `pest-model-watchdog(:*)` **and** `pest-camera-scheduler(:*)`. This fixes two live-account name gaps on purpose: the reference account's watchdog schedule uses a console-generated role, and its api policy's `iam:PassRole` points at a role name that does not exist there. In the new account, make sure the rewritten `pest-monitoring-api-policy`'s `iam:PassRole` Resource is this role's ARN.

### Step 2 — DynamoDB (4 tables)

Console: **DynamoDB → Tables → Create table**, On-demand capacity, keys per the table below; on `pest-monitoring-detections` add GSI `by-pest-time` under **Indexes**. Do NOT enable TTL, PITR, or streams.

CLI:

```
aws dynamodb create-table --table-name pest-monitoring-cameras --billing-mode PAY_PER_REQUEST --attribute-definitions AttributeName=camera_id,AttributeType=S --key-schema AttributeName=camera_id,KeyType=HASH --profile **prod** --region **us-east-1**

aws dynamodb create-table --table-name pest-monitoring-detections --billing-mode PAY_PER_REQUEST --attribute-definitions AttributeName=image_id,AttributeType=S AttributeName=detection_time,AttributeType=S AttributeName=pest_type,AttributeType=S --key-schema AttributeName=image_id,KeyType=HASH AttributeName=detection_time,KeyType=RANGE --global-secondary-indexes "[{\"IndexName\":\"by-pest-time\",\"KeySchema\":[{\"AttributeName\":\"pest_type\",\"KeyType\":\"HASH\"},{\"AttributeName\":\"detection_time\",\"KeyType\":\"RANGE\"}],\"Projection\":{\"ProjectionType\":\"ALL\"}}]" --profile **prod** --region **us-east-1**

aws dynamodb create-table --table-name pest-monitoring-system-config --billing-mode PAY_PER_REQUEST --attribute-definitions AttributeName=config_key,AttributeType=S --key-schema AttributeName=config_key,KeyType=HASH --profile **prod** --region **us-east-1**

aws dynamodb create-table --table-name pest-monitoring-schedule-logs --billing-mode PAY_PER_REQUEST --attribute-definitions AttributeName=log_id,AttributeType=S AttributeName=timestamp,AttributeType=S --key-schema AttributeName=log_id,KeyType=HASH AttributeName=timestamp,KeyType=RANGE --profile **prod** --region **us-east-1**
```

### Step 3 — S3 (3 buckets)

**us-east-1 quirk:** `create-bucket` must be called WITHOUT `--create-bucket-configuration` in us-east-1 (passing a LocationConstraint there errors); in any other region you must pass it.

Console: **S3 → Create bucket** ×3; for frames/processed leave Block Public Access ON and enable **Versioning**; set default SSE-S3 encryption on all three. For the dashboard bucket: **Permissions → Block public access → uncheck all** FIRST, then add the public-read bucket policy, then **Properties → Static website hosting** with index and error document both `index.html` (the SPA fallback).

CLI (frames shown; repeat for processed; dashboard differs as noted):

```
aws s3api create-bucket --bucket **vision**-frames-**YOUR_ACCOUNT_ID** --profile **prod**
aws s3api put-bucket-encryption --bucket **vision**-frames-**YOUR_ACCOUNT_ID** --server-side-encryption-configuration "{\"Rules\":[{\"ApplyServerSideEncryptionByDefault\":{\"SSEAlgorithm\":\"AES256\"}}]}" --profile **prod**
aws s3api put-bucket-versioning --bucket **vision**-frames-**YOUR_ACCOUNT_ID** --versioning-configuration Status=Enabled --profile **prod**
aws s3api put-public-access-block --bucket **vision**-frames-**YOUR_ACCOUNT_ID** --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true --profile **prod**
```

Dashboard bucket (ordering trap — PAB off BEFORE the policy or `PutBucketPolicy` fails):

```
aws s3api create-bucket --bucket **vision**-dashboard-**YOUR_ACCOUNT_ID** --profile **prod**
aws s3api put-public-access-block --bucket **vision**-dashboard-**YOUR_ACCOUNT_ID** --public-access-block-configuration BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false --profile **prod**
aws s3api put-bucket-policy --bucket **vision**-dashboard-**YOUR_ACCOUNT_ID** --policy "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Sid\":\"PublicReadDashboard\",\"Effect\":\"Allow\",\"Principal\":\"*\",\"Action\":\"s3:GetObject\",\"Resource\":\"arn:aws:s3:::**vision**-dashboard-**YOUR_ACCOUNT_ID**/*\"}]}" --profile **prod**
aws s3api put-bucket-website --bucket **vision**-dashboard-**YOUR_ACCOUNT_ID** --website-configuration "{\"IndexDocument\":{\"Suffix\":\"index.html\"},\"ErrorDocument\":{\"Key\":\"index.html\"}}" --profile **prod**
```

The frames-bucket CORS and event notification come later (Steps 6 and 10) — they depend on the processor Lambda and the CloudFront domain.

### Step 4 — Lambda layer `fyp-pillow`

No source zip exists in any bucket — the layer is BUILT. On a machine with Python + pip:

```
pip install --platform manylinux2014_x86_64 --implementation cp --python-version 3.12 --only-binary=:all: --target **work\python** Pillow==12.2.0
```

**Before zipping, verify `python/PIL/_imaging*.so` and `python/pillow.libs/` exist** — their absence is the exact defect that made layer v1 in the reference account silently draw no boxes. Zip with `python/` at the archive root, then publish:

```
aws lambda publish-layer-version --layer-name fyp-pillow --zip-file fileb://**work\fyp-pillow.zip** --compatible-runtimes python3.12 --compatible-architectures x86_64 --profile **prod** --region **us-east-1**
```

Capture the returned `LayerVersionArn` — on a fresh account it ends `:1`, not `:2`; never hardcode the reference account's version number. (Shortcut: `deployer/layer/fyp-pillow.zip` is the prebuilt, verified zip the exe bundles — publish that directly.)

Console alternative: **Lambda → Layers → Create layer**, upload the zip, runtime python3.12, arch x86_64.

### Step 5 — Lambda functions (5)

See Section 8.7 for where each function's code comes from and how to zip it. Create `pest-camera-scheduler` FIRST (the api function's env references its ARN). Common settings: runtime `python3.12`, handler `lambda_function.lambda_handler`, arch x86_64. If creation fails right after Step 1 with an invalid-role error, wait ~10 s and retry — IAM eventual consistency.

| Function | Mem / timeout | Role | Layer | Env vars |
|---|---|---|---|---|
| `pest-camera-scheduler` | 128 / 60 | pest-camera-scheduler-role | — | `TABLE_CAMERAS=pest-monitoring-cameras`, `TABLE_SCHEDULE_LOGS=pest-monitoring-schedule-logs` |
| `pest-detection-processor` | 1024 / 600 (the validated production sizing — a tiled frame plus up to 120 verify crops measures 24–54 s; the old 512/60 times out intermittently on real frames) | pest-detection-processor-role | `fyp-pillow:**N**` | `TABLE_CAMERAS`, `TABLE_SYSTEM_CONFIG=pest-monitoring-system-config`, `TABLE_DETECTIONS=pest-monitoring-detections`, `SENDER_EMAIL=**alerts@np.edu.sg**`, `S3_PROCESSED_BUCKET=**vision**-processed-**YOUR_ACCOUNT_ID**`, plus the detection tuning: `TILE_MIN_CONFIDENCE=8`, `LLM_VERIFY_ALL_BOXES=true`, `LLM_VERIFY_MODEL_ID=us.anthropic.claude-sonnet-4-6`, `LLM_VERIFY_MAX_BOXES=120`, `LLM_VERIFY_MAX_TOKENS=300`, `LLM_VERIFY_WORKERS=3`, `LLM_VERIFY_PAD=0.6`, `POST_NMS_IOU=0.1`, `POST_NMS_CONTAIN=0.1`, `POST_MAX_BOX_AREA=0.05`, `POST_VERIFY_FLOOR=49` |
| `pest-monitoring-api` | 256 / 30 | pest-monitoring-api-role | — | `TABLE_CAMERAS`, `TABLE_SYSTEM_CONFIG`, `TABLE_DETECTIONS`, `TABLE_SCHEDULE_LOGS`, `S3_FRAMES_BUCKET=**vision**-frames-**YOUR_ACCOUNT_ID**`, `S3_PROCESSED_BUCKET=**vision**-processed-**YOUR_ACCOUNT_ID**`, `SCHEDULE_EXECUTOR_ARN=arn:aws:lambda:**us-east-1**:**YOUR_ACCOUNT_ID**:function:pest-camera-scheduler` |
| `kvs-hls-handler` | 128 / 30 | kvs-hls-handler-role | — | none |
| `pest-model-watchdog` | 128 / 30 | pest-model-watchdog-role | — | `TABLE_CAMERAS`, `MAX_RUNTIME_MIN=75` |

Console: **Lambda → Create function → Author from scratch** per row, then **Code → Upload from → .zip file**, **Configuration → Environment variables / General configuration / Permissions**; attach the layer on the processor under **Code → Layers → Add a layer → Custom layers**.

CLI example (processor):

```
aws lambda create-function --function-name pest-detection-processor --runtime python3.12 --handler lambda_function.lambda_handler --role arn:aws:iam::**YOUR_ACCOUNT_ID**:role/pest-detection-processor-role --memory-size 1024 --timeout 600 --architectures x86_64 --layers arn:aws:lambda:**us-east-1**:**YOUR_ACCOUNT_ID**:layer:fyp-pillow:**N** --zip-file fileb://**work\pest-detection-processor.zip** --environment "Variables={TABLE_CAMERAS=pest-monitoring-cameras,TABLE_SYSTEM_CONFIG=pest-monitoring-system-config,TABLE_DETECTIONS=pest-monitoring-detections,SENDER_EMAIL=**alerts@np.edu.sg**,S3_PROCESSED_BUCKET=**vision**-processed-**YOUR_ACCOUNT_ID**,TILE_MIN_CONFIDENCE=8,LLM_VERIFY_ALL_BOXES=true,LLM_VERIFY_MODEL_ID=us.anthropic.claude-sonnet-4-6,LLM_VERIFY_MAX_BOXES=120,LLM_VERIFY_MAX_TOKENS=300,LLM_VERIFY_WORKERS=3,LLM_VERIFY_PAD=0.6,POST_NMS_IOU=0.1,POST_NMS_CONTAIN=0.1,POST_MAX_BOX_AREA=0.05,POST_VERIFY_FLOOR=49}" --profile **prod** --region **us-east-1**
```

### Step 6 — S3 → processor event wiring

Permission FIRST or the notification call fails:

```
aws lambda add-permission --function-name pest-detection-processor --statement-id s3-frames-invoke --action lambda:InvokeFunction --principal s3.amazonaws.com --source-arn arn:aws:s3:::**vision**-frames-**YOUR_ACCOUNT_ID** --source-account **YOUR_ACCOUNT_ID** --profile **prod** --region **us-east-1**

aws s3api put-bucket-notification-configuration --bucket **vision**-frames-**YOUR_ACCOUNT_ID** --notification-configuration "{\"LambdaFunctionConfigurations\":[{\"Id\":\"frames-to-processor\",\"LambdaFunctionArn\":\"arn:aws:lambda:**us-east-1**:**YOUR_ACCOUNT_ID**:function:pest-detection-processor\",\"Events\":[\"s3:ObjectCreated:*\"],\"Filter\":{\"Key\":{\"FilterRules\":[{\"Name\":\"Prefix\",\"Value\":\"frames/\"}]}}}]}" --profile **prod**
```

Console: **S3 → the frames bucket → Properties → Event notifications → Create event notification** (All object create events, prefix `frames/`, destination the processor). The console adds the permission for you; emit only one.

### Step 7 — Cognito

```
aws cognito-idp create-user-pool --pool-name pest-dashboard-users --username-attributes email --auto-verified-attributes email --admin-create-user-config AllowAdminCreateUserOnly=true --policies "PasswordPolicy={MinimumLength=8,RequireUppercase=false,RequireLowercase=true,RequireNumbers=true,RequireSymbols=false}" --profile **prod** --region **us-east-1**

aws cognito-idp create-user-pool-client --user-pool-id **NEW_POOL_ID** --client-name dashboard-web --no-generate-secret --explicit-auth-flows ALLOW_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH --id-token-validity 12 --access-token-validity 12 --refresh-token-validity 30 --token-validity-units "IdToken=hours,AccessToken=hours,RefreshToken=days" --profile **prod** --region **us-east-1**
```

Capture **NEW_POOL_ID** and **NEW_CLIENT_ID**. Console: **Amazon Cognito → User pools → Create user pool** (email sign-in, no self-registration) then **App clients → Create app client** (public client, no secret).

### Step 8 — API Gateway (HTTP API)

`lambda/cors.json` is the exact CORS shape:

```
aws apigatewayv2 create-api --name pest-monitoring-api-gateway --protocol-type HTTP --cors-configuration file://lambda/cors.json --profile **prod** --region **us-east-1**

aws apigatewayv2 create-authorizer --api-id **NEW_API_ID** --name cognito-dashboard --authorizer-type JWT --identity-source "$request.header.Authorization" --jwt-configuration Audience=**NEW_CLIENT_ID**,Issuer=https://cognito-idp.**us-east-1**.amazonaws.com/**NEW_POOL_ID** --profile **prod**

aws apigatewayv2 create-integration --api-id **NEW_API_ID** --integration-type AWS_PROXY --integration-uri arn:aws:lambda:**us-east-1**:**YOUR_ACCOUNT_ID**:function:pest-monitoring-api --integration-method POST --payload-format-version 2.0 --profile **prod**
aws apigatewayv2 create-integration --api-id **NEW_API_ID** --integration-type AWS_PROXY --integration-uri arn:aws:lambda:**us-east-1**:**YOUR_ACCOUNT_ID**:function:kvs-hls-handler --integration-method POST --payload-format-version 2.0 --profile **prod**
```

Then the 21 routes. All target the api integration except `GET /video-playback` (the hls integration); all carry `--authorization-type JWT --authorizer-id **AUTH_ID**` except `GET /stream/status`, which must be `--authorization-type NONE` (device pollers hit it unauthenticated). Route list: `GET /cost`, `GET /settings`, `POST /settings`, `GET /history`, `GET /presigned-url`, `POST /detection/verify`, `DELETE /detection`, `GET /model/status`, `POST /model/start`, `POST /model/stop`, `GET /identities`, `POST /identities`, `DELETE /identities`, `GET /schedule`, `POST /schedule`, `DELETE /schedule`, `GET /schedule-logs`, `POST /stream/start`, `POST /stream/stop`, `GET /stream/status`, `GET /video-playback`.

```
aws apigatewayv2 create-route --api-id **NEW_API_ID** --route-key "GET /settings" --target integrations/**INT_API_ID** --authorization-type JWT --authorizer-id **AUTH_ID** --profile **prod**
aws apigatewayv2 create-route --api-id **NEW_API_ID** --route-key "GET /stream/status" --target integrations/**INT_API_ID** --authorization-type NONE --profile **prod**
aws apigatewayv2 create-route --api-id **NEW_API_ID** --route-key "GET /video-playback" --target integrations/**INT_HLS_ID** --authorization-type JWT --authorizer-id **AUTH_ID** --profile **prod**
```

Stage and invoke permissions:

```
aws apigatewayv2 create-stage --api-id **NEW_API_ID** --stage-name "$default" --auto-deploy --profile **prod**
aws lambda add-permission --function-name pest-monitoring-api --statement-id apigw-invoke --action lambda:InvokeFunction --principal apigateway.amazonaws.com --source-arn "arn:aws:execute-api:**us-east-1**:**YOUR_ACCOUNT_ID**:**NEW_API_ID**/*" --profile **prod**
aws lambda add-permission --function-name kvs-hls-handler --statement-id apigw-invoke-videoplayback --action lambda:InvokeFunction --principal apigateway.amazonaws.com --source-arn "arn:aws:execute-api:**us-east-1**:**YOUR_ACCOUNT_ID**:**NEW_API_ID**/*/*/video-playback" --profile **prod**
```

Base URL (no stage suffix): `https://**NEW_API_ID**.execute-api.**us-east-1**.amazonaws.com`. Console path for all of it: **API Gateway → Create API → HTTP API**, then Authorization, Integrations, Routes, and CORS tabs.

### Step 9 — CloudFront

Console: **CloudFront → Create distribution**. Origin domain = the dashboard bucket's **static website endpoint** `**vision**-dashboard-**YOUR_ACCOUNT_ID**.s3-website-**us-east-1**.amazonaws.com` — type it; do NOT accept the console's suggested REST endpoint. Protocol **HTTP only** (website endpoints have no HTTPS; https origin → 502). Viewer protocol redirect-to-HTTPS, allowed methods GET/HEAD, cache policy **CachingDisabled** (AWS-managed, same ID in every account: `4135ea2d-6df8-44a3-9df3-4b5a84be39ad`), default root object `index.html`, no OAC/OAI, no aliases. No CloudFront custom error responses — the SPA 404 fallback is the S3 website ErrorDocument.

CLI: take `deployer/audit/cloudfront__E1YADURLSAVNFA_distribution-config.json`, rewrite the origin DomainName to your website endpoint, set a fresh unique `CallerReference` (CloudFront rejects duplicates), then:

```
aws cloudfront create-distribution --distribution-config file://**rewritten\cf-config.json** --profile **prod**
```

Capture the new distribution ID and `*.cloudfront.net` domain. The distribution takes minutes to reach Deployed.

### Step 10 — Config write-back + dashboard sync + frames CORS

Edit `web/dashboard_v4/js/config.js` (a copy — do not commit the new account's values over the reference ones): set `HTTP_API` to the Step 8 base URL, `COGNITO_REGION` to **us-east-1**, `COGNITO_CLIENT_ID` to **NEW_CLIENT_ID** (the pool ID is not referenced by the dashboard). Then sync:

```
aws s3 sync web/dashboard_v4/ s3://**vision**-dashboard-**YOUR_ACCOUNT_ID**/ --cache-control "no-cache, must-revalidate" --profile **prod**
```

Then the frames-bucket CORS, with the new origins:

```
aws s3api put-bucket-cors --bucket **vision**-frames-**YOUR_ACCOUNT_ID** --cors-configuration "{\"CORSRules\":[{\"AllowedHeaders\":[\"*\"],\"AllowedMethods\":[\"GET\",\"PUT\",\"POST\",\"HEAD\"],\"AllowedOrigins\":[\"https://**NEW_CF_DOMAIN**.cloudfront.net\",\"http://**vision**-dashboard-**YOUR_ACCOUNT_ID**.s3-website-**us-east-1**.amazonaws.com\"],\"ExposeHeaders\":[\"ETag\"],\"MaxAgeSeconds\":3000}]}" --profile **prod**
```

### Step 11 — Kinesis Video Streams (only if live view is wanted)

```
aws kinesisvideo create-stream --stream-name **vision**-cam-stream --data-retention-in-hours 2 --profile **prod** --region **us-east-1**
```

Console: **Kinesis Video Streams → Create video stream**. The HLS data endpoint is per-account/per-stream; `kvs-hls-handler` resolves it at runtime — never hardcode it anywhere.

### Step 12 — EventBridge Scheduler (the cost-guard watchdog)

```
aws scheduler create-schedule --name pest-model-watchdog-15min --schedule-expression "rate(15 minutes)" --flexible-time-window Mode=OFF --state ENABLED --target "{\"Arn\":\"arn:aws:lambda:**us-east-1**:**YOUR_ACCOUNT_ID**:function:pest-model-watchdog\",\"RoleArn\":\"arn:aws:iam::**YOUR_ACCOUNT_ID**:role/pest-scheduler-invocation-role\",\"RetryPolicy\":{\"MaximumEventAgeInSeconds\":86400,\"MaximumRetryAttempts\":0}}" --profile **prod** --region **us-east-1**
```

Console: **Amazon EventBridge → Scheduler → Schedules → Create schedule** (rate 15 minutes, target the watchdog Lambda, execution role = the invocation role, retries 0). Note the mechanism: EventBridge Scheduler invokes Lambda by assuming the role — no `lambda add-permission` is involved (unlike classic EventBridge rules).

### Step 13 — Rekognition Custom Labels project (skeleton only)

```
aws rekognition create-project --project-name **vision**-detection --profile **prod** --region **us-east-1**
```

Console: **Amazon Rekognition → Custom Labels → Projects → Create project**. Capture the ProjectArn (the numeric suffix is AWS-assigned). No model exists yet; training is Section 8.5.3 / Chapter 3. Do not recreate the reference account's `SmartPestProject` (moth demo) or the dead v1.

### Step 14 — DynamoDB seeds

Write the JSON item files and use `file://` inputs — raw inline JSON with colons breaks PowerShell quoting (the same trap as the model-ARN write in Chapter 3).

System config (1 row):

```
aws dynamodb put-item --table-name pest-monitoring-system-config --item file://**seed-config.json** --profile **prod** --region **us-east-1**
```

`seed-config.json`:

```
{"config_key": {"S": "detection_settings"}, "email_enabled": {"BOOL": true}, "recipient_email": {"S": "**you@np.edu.sg**"}, "additional_recipients": {"L": []}, "auto_capture": {"BOOL": true}, "capture_interval": {"N": "60"}}
```

Camera rows (2): `manual_upload` (required by the dashboard Test-upload feature) and one real camera. Fields per row, matching the `deploy.py` seed: `camera_id`, `label`, `default_waypoint_id`, `target_label` = **your training class**, `model_type` = `custom`, `min_confidence` (N) = `10` (the CANDIDATE floor feeding the LLM gate — setting it high silently strangles recall before the LLM ever sees a box; the user-facing threshold is `post_verify_floor`), `llm_verify_enabled` (BOOL) = true, `post_verify_floor` (N) = `49`, `stream_enabled` (BOOL), `model_running` (BOOL) = false, `custom_model_arn` = `""` (empty until training writes it), `kvs_stream_name` (`**vision**-cam-stream` on the real camera if Step 11 ran, else NULL), `schedule` (empty map on `manual_upload`; `{enabled:false, days:[], start_time:"09:00", end_time:"17:00"}` on the camera), and on the real camera `tiling_enabled` (BOOL) = true and `max_runtime_min` (N) = `45` (the watchdog's per-camera auto-stop bound).

### Step 15 — SES identities

```
aws ses verify-email-identity --email-address **alerts@np.edu.sg** --profile **prod** --region **us-east-1**
```

Repeat for the recipient if different. Then Section 8.5.1 (confirm the link, sandbox note). Console: **Amazon SES → Identities → Create identity → Email address**.

**Do not create anything else.** CloudWatch log groups auto-create on first invoke. No SNS, SQS, EC2, classic EventBridge rules, or KMS CMKs are part of this stack.

## 8.7 Path B — Lambda code upload from the local mirror

Each function's zip must contain exactly one file named `lambda_function.py` at the archive root. The mirrors in `lambda/` are named `pest-*.py` — **rename inside the zip**, not on disk.

| Function | Source file |
|---|---|
| `pest-detection-processor` | `lambda/pest-detection-processor.py` |
| `pest-monitoring-api` | `lambda/pest-monitoring-api.py` |
| `pest-camera-scheduler` | `lambda/pest-camera-scheduler.py` |
| `pest-model-watchdog` | `lambda/pest-model-watchdog.py` (the repo mirror — it honours per-camera `max_runtime_min`; do NOT use the older snapshot under `deployer/audit/pest-model-watchdog_src/`, which only knows the global 75-minute cap) |
| `kvs-hls-handler` | `deployer/audit/kvs-hls-handler_src/lambda_function.py` (already correctly named) |

PowerShell zip recipe (per core function):

```
Copy-Item lambda/pest-detection-processor.py **work**\lambda_function.py
Compress-Archive -Path **work**\lambda_function.py -DestinationPath **work**\pest-detection-processor.zip -Force
```

Upload at creation (Step 5) or update later:

```
aws lambda update-function-code --function-name pest-detection-processor --zip-file fileb://**work\pest-detection-processor.zip** --profile **prod** --region **us-east-1**
```

Console: **Lambda → the function → Code → Upload from → .zip file**. Two cautions: the function name and runtime are fixed at creation (changing them means delete + recreate, which wipes the resource policy — re-grant the API Gateway and S3 invoke permissions); and the mirror files contain no secrets, but always deploy from the mirror, never from a console-edited copy, so the repo stays the source of truth.

## 8.8 Device provisioning (checklist level)

The cloud stack works with zero devices — skip this section for a dashboard-only reproduction. The Go2 is a demo/testbed only; the production vision is fixed cameras.

**Jetson Orin (Go2 patrol + SIYI A8 Mini capture) — Chapter 5 has every step:**
- [ ] Ubuntu + ROS 2 Foxy installed; `cyclonedds_ws` workspace built (the Unitree Go2 DDS interface).
- [ ] `setup_go2.sh` environment sourced (network interface + `CYCLONEDDS_URI` / ROS env for the Go2 link).
- [ ] KVS Producer SDK compiled for ARM64; GStreamer passthrough pipeline (`rtspsrc → rtph264depay → h264parse → kvssink`) for the A8 Mini feed — RTSP, or the 2026-07-29 mini-HDMI → USB capture-card path now in use (Chapter 5).
- [ ] Patrol + capture scripts present: `go2_patrol_gated.py` (capture and upload are built into it — `capture_frame`/`upload_frame`) and `capture_4k_hdmi.py` (the 2026-07-29 capture-card path); `kvs_controller.py` service polling `GET /stream/status`.
- [ ] `patrol-scheduler.service` (systemd, `Restart=always`) running `run_patrol_scheduler.sh` / `patrol_scheduler.py`, polling `GET /schedule?camera=worm_cam` and launching `go2_patrol_gated.py` on the dashboard's schedule (Chapter 5, Section 5.13) — **not yet dry-run on real hardware**; confirm the systemd unit starts, and that a scheduled test run actually launches (armed via `~/go2/.patrol_armed`) before relying on it.
- [ ] AWS credentials for the NEW account on the device, and every script's API URL / bucket / camera_id repointed (Section 8.9.3).

**Mini PC Win11 host + Ubuntu VM (fixed Hikvision camera / moth stream) — Chapter 6 has every step:**
- [ ] VM up (VMware, NAT — inbound unreachable by design).
- [ ] `kvs-controller.service` (systemd, `Restart=always`) running `run_kvs_controller.sh`; `CAMERA_ID` set correctly (the systemd unit env overrides home-dir edits — the Chapter 6 gotcha). Capture/upload script `capture_and_upload_v4_armyworm.py` present (repo mirror: `minipc/`).
- [ ] `reverse-tunnel-fyp.service` up (VM → Orin:2222 reverse SSH; self-heals within ~45 s) if remote administration is needed.
- [ ] Same repointing as the Orin: new API base URL for `/stream/status`, new stream name, new account credentials.

## 8.9 Account-portability caveats (critical — read before either path)

Every caveat in this section has now been confronted for real by the 2026-08-10/11 production migration; where a caveat has been resolved or confirmed by that migration, the subsection says so.

### 8.9.1 Rekognition Custom Labels models are account-bound — but their data is portable

There is **no export, import, copy, or cross-account API** for trained Custom Labels models. The armyworm models (current generation: the v9 retrain with added data augmentation) and the predecessor's moth model (`SmartPestProject`, F1 0.988) exist only in account `366356442579` and cannot follow you. On the new account you MUST retrain: the ARGUS Train screen (Section 8.5.3) with a labeled image folder, the Chapter 3 pipeline, or — when the data already lives in the old account's S3 — the executed migration route in Section 8.5.3 (server-side copy + `ListDatasetEntries` export). Training runs from ~1 h (a ~30-image proof run) to many hours at full scale (the ~33k-image armyworm run). After training, the new `ProjectVersionArn` must land in the camera row's `custom_model_arn` — the Train screen does this automatically; manually it is one boto3/`file://`-JSON field write (the ARN contains colons; raw PowerShell `update-item` mangles the quoting).

This caveat is no longer theory: both capabilities were rebuilt on `506868652945` from data alone — armyworm `v9r-prod-20260810` (project `argus-detection`) and moth `moth-prod-20260811` (project `argus-moth-detection`, F1 0.991 vs the original 0.988 on the same data). The model is account-bound; the capability is not, as long as you keep the labelled data.

### 8.9.2 Every hardcoded identifier changes

The account ID is embedded in bucket names, every ARN, and the IAM trust conditions; the API Gateway ID, Cognito pool/client IDs, and CloudFront domain are server-assigned and new. Path A rewrites all of them mechanically and records them in `deploy_state.json` — the production migration confirmed this end to end (the templated `js/config.js` served through the new CloudFront carried the correct new API and client IDs on first check). Path B: the complete inventory of reference-account identifiers — which files hold them and their PROD/DEMO/DEAD classification — is the Chapter 9 registry. Work through it once after deployment and confirm nothing still points at `366356442579`. For a migration between two live accounts, `migration/diff_accounts.py` automates the old-vs-new comparison (Lambda config, camera rows, SES identities, streams) and is how the remaining gaps in Section 8.9.3 were enumerated.

### 8.9.3 Device-side config references the old account

The edge devices carry the old API and bucket coordinates. At minimum, on each device: the API base URL (currently `https://vzfl7s6z00.execute-api.us-east-1.amazonaws.com` → your `https://**NEW_API_ID**...`) used by `kvs_controller.py` for `GET /stream/status`; the frames bucket name in the capture/upload scripts; the KVS stream name; the region; and the device's AWS credentials, which must belong to the new account. Camera IDs must match the rows you seeded. Chapters 5 and 6 list the exact files per device.

**Migration status:** this is the remaining open step of the production migration. For that account the concrete targets are: API base `https://vzfl7s6z00.execute-api.us-east-1.amazonaws.com`, frames bucket `argus-frames-506868652945`, camera ids unchanged (`worm_cam` / `moth_cam` — the rows were aligned to the reference ids, Section 8.5.4), and new-account credentials on each device. KVS was not deployed there, so any `kvs_controller` repointing waits until a stream exists.

### 8.9.4 The LLM verification gate needs one Bedrock step the deployer cannot do

The processor mirror in `lambda/` runs the Bedrock LLM verification gate as part of the standard pipeline (v6.3: tiling → suppression → LLM verify → post-gate cleanup), and since the 2026-08-10/11 deployer fixes a fresh stack comes up with the gate fully wired: the camera rows seed `llm_verify_enabled=true`, the env carries `LLM_VERIFY_MODEL_ID=us.anthropic.claude-sonnet-4-6` (note the `us.`-prefixed inference-profile ID, not the bare foundation-model ID), the processor deploys at 1024 MB / 600 s, and the `bedrock-verify` inline policy (Sonnet 4.6 + Haiku 4.5 profile and foundation-model ARNs) is attached to `pest-detection-processor-role` automatically. Without that policy the fail-closed gate would reject every detection — this was a real deployer defect, found and fixed 2026-08-11.

What no deployer can do: **Bedrock model access is a per-account console grant.** A fresh account gets `AccessDeniedException` on Anthropic models until the use-case form is accepted (**Bedrock → Model access → Modify model access**; approval can lag, so submit it first thing). On the NP production account this cost nothing — Sonnet 4.6 and Haiku 4.5 were already authorized when the account was audited on 2026-08-10 — but never assume that; check before deploying. Details in Chapter 2's processor section and `docs/aws.md`.

### 8.9.5 SES sandbox and other fresh-account frictions

SES starts sandboxed (Section 8.5.1). Cognito starts with zero users (Section 8.5.2). CloudFront takes minutes to deploy. New accounts occasionally carry low default service quotas; none have bitten this stack in practice (neither the `CAG_Test` run nor the production migration needed a quota increase). An institutionally issued account may also not be empty — the NP production account carried a previous student's Amplify relics (13 roles, one bucket); they are harmless and were left untouched. Do not clean up resources you did not create.

### 8.9.6 One known credential in the repo

`datasets/archive/experiments/pre_v3_abandoned/download.py` contains an inline Roboflow API key from an abandoned experiment — credential stored there, not reproduced here. It has nothing to do with reproduction; just do not copy that file into anything you hand over or publish.

### 8.9.7 Cross-account S3 grants must be revoked after sign-off

The server-side data copies in Section 8.5.3 work by adding temporary, prefix-scoped read statements to the OLD account's bucket policies. They are deliberately left in place until the new models are signed off, and each script removes exactly its own statements with `--revoke`. As of 2026-08-11 three grants are outstanding on the reference account: the `training-data/v9/*` grant on the frames bucket (`python migration/copy_training_data.py --revoke`), a `frames/*` grant on the same bucket added for the on-site records migration (Sid `MigrationReadJewelFrames` — remove the statement from the bucket policy directly), and the moth console-bucket grant (`python migration/migrate_moth.py --revoke`). Whoever finishes the migration must run the revokes — a standing cross-account read grant is not something to hand over.

## 8.10 Smoke-test suite (run after either path)

Run these in order on the new account. They are the day-1 customer test from `REHEARSAL.md` Round 2, expanded.

### 8.10.1 Detection rail: upload → DynamoDB record → dashboard card

Sign in to the dashboard (CloudFront URL) → **Settings → Test upload** → upload any image. Within ~10–30 s a record must appear in the Gallery. With no model trained yet this is a **clean record** (zero detections) — that is the expected pass; clean images write a DynamoDB record too, by design. CLI equivalent:

```
aws s3 cp **test.jpg** s3://**vision**-frames-**YOUR_ACCOUNT_ID**/frames/manual_upload/**smoke-1**.jpg --profile **prod**
aws dynamodb query --table-name pest-monitoring-detections --key-condition-expression "image_id = :k" --expression-attribute-values "{\":k\":{\"S\":\"frames/manual_upload/**smoke-1**.jpg\"}}" --profile **prod** --region **us-east-1**
```

If no record appears: check `/aws/lambda/pest-detection-processor` in CloudWatch Logs (console: **CloudWatch → Log groups**); if the log group itself does not exist, the S3 notification (Step 6) never fired.

After training a model and starting it, repeat with a known-positive image and expect a card with boxes.

### 8.10.2 KVS live check (only if live view was deployed)

With a producer streaming (device or the GStreamer test pipeline):

- Console: **Kinesis Video Streams → **vision**-cam-stream → Media playback** shows live video.
- CLI:
```
aws kinesisvideo get-data-endpoint --stream-name **vision**-cam-stream --api-name GET_HLS_STREAMING_SESSION_URL --profile **prod** --region **us-east-1**
```
Then the dashboard Live tab must play (it calls `GET /video-playback` → `kvs-hls-handler`). With no producer, the correct behavior is a clean "stream not active" state, not an error page.

### 8.10.3 Scheduler fire (cost guard)

The watchdog must run every 15 minutes from minute zero:

```
aws scheduler get-schedule --name pest-model-watchdog-15min --profile **prod** --region **us-east-1**
```

Expect `"State": "ENABLED"`. Then confirm real invocations: console **CloudWatch → Log groups → /aws/lambda/pest-model-watchdog** — log streams appearing on a 15-minute cadence. This is the guard that stops a forgotten Rekognition endpoint (per-camera `max_runtime_min` — 45 min on the seeded real camera; the env `MAX_RUNTIME_MIN=75` is the fallback for rows without one), so prove it before ever starting a model.

### 8.10.4 Alert email

Prerequisites: SES identity verified, a trained model RUNNING, `email_enabled=true` in system config (the seed default). Upload a known-positive image via Test upload; a detection at/above the camera's `min_confidence` sends an SES alert to `recipient_email`. In sandbox the recipient must itself be verified — if the mail does not arrive, check **SES → Account dashboard** send statistics and the processor's CloudWatch log before suspecting the pipeline.

### 8.10.5 Full-stack audit

ARGUS done screen → **Run a system check**, or headless:

```
python deployer/deploy.py --profile **prod** --region **us-east-1** --prefix **vision** --target-label **x** --sender-email **x@x** --verify
```

Every line must read OK; the summary must read all resources present. (`--verify` is read-only; the dummy label/email arguments are required by the parser but unused.)

## 8.11 Operations — cost profile, idle check, teardown

### 8.11.1 What the stack costs

| Item | Billed when | Approximate rate | Notes |
|---|---|---|---|
| Rekognition Custom Labels **inference endpoint** | Only while RUNNING | ~US$4/hour per model | The dominant cost. The watchdog auto-stops running endpoints after the camera row's `max_runtime_min` (45 min as seeded; env fallback 75). |
| Rekognition Custom Labels **training** | Per training run | billed per hour; a v5-scale run ≈ 3.5 h; a ~30-image proof run ≈ 1 h ≈ US$4 | One-off per model version. |
| One-model duty cycle | Daily | ≈ US$2/day (30 min warm/day) → **≈ US$60/month** | The project's headline figure; roughly 2× if both models run. |
| S3 (3 buckets) | Storage | cents/month at a few GB | Free tier covers the first 12 months at this scale. |
| DynamoDB (4 tables, on-demand) | Per request | negligible | Detection volume is hundreds of rows/week. |
| Lambda (5 functions) | Per invoke | free tier covers it | Watchdog = 2,880 invokes/month; the rest are event-driven. |
| API Gateway HTTP API | Per request | ~US$1/million requests | Dashboard polling stays far below meaningful spend. |
| CloudFront | Per GB/request | negligible / free tier | Static dashboard files only. |
| Kinesis Video Streams | Per GB ingested | zero when no producer streams | 2 h retention keeps storage minimal. |
| Cognito | Per MAU | free tier (50,000 MAU) | A handful of users. |
| SES | Per message | fractions of a cent; sandbox caps 200/day | |
| Bedrock (the LLM verification gate — on by default since 2026-08-10) | Per call | a few US$/month at FYP volume on Sonnet 4.6 | Section 8.9.4. |
| EventBridge Scheduler | Per invocation | negligible | |

### 8.11.2 Idle-cost check

A correctly deployed stack idles at effectively **$0/day**: nothing bills by the hour except a RUNNING Rekognition endpoint, and the watchdog kills those. The acceptance test (REHEARSAL.md Round 2, step 7): leave the fresh account untouched for 24 h and read the bill — console **Billing and Cost Management → Bills**; expect ~$0. If it is not ~$0, the usual culprit on the reference account's history was exactly a running model endpoint or stray legacy schedules — check **Rekognition → Projects → your project → Use model** for anything RUNNING, and `aws scheduler list-schedules --profile **prod** --region **us-east-1**` for anything unexpected.

### 8.11.3 Teardown

Full removal is one guarded action in ARGUS (**Danger zone → Delete deployment**): reverse-dependency deletion of everything (schedule, Rekognition versions + project, KVS, CloudFront disable→wait→delete, API, Cognito pool, 5 Lambdas, layer versions, 3 emptied buckets, 4 tables, 6 roles, SES identities), followed by the same `verify()` audit in reverse expectation — success means "0/N resources remain", independently confirmed, not assumed. Headless: `deploy.destroy(ctx)` in `deploy.py`; it refuses to run if the stored state belongs to a different account than the credentials. Closing the AWS account itself is always a manual root console action (Account → Close account; 90-day reopen window). Full teardown detail is Chapter 7.

## 8.12 Cross-references

- Chapter 1 — architecture overview and the two-camera model; Section 1.10.7 is the short form of this chapter.
- Chapter 2 — what each cloud resource does once it exists; the processor version history and the Bedrock gate this chapter's caveat 8.9.4 refers to.
- Chapter 3 — the training pipeline (the manual equivalent of the Train screen), the model history, and the ARN write-back procedure.
- Chapter 4 — dashboard modules, Cognito user management in depth, the redeploy command with the cache-control flag.
- Chapter 5 — Jetson Orin / Go2 provisioning in full (the checklist in 8.8 expanded).
- Chapter 6 — mini PC VM provisioning in full, including the reverse tunnel and the systemd `CAMERA_ID` gotcha.
- Chapter 7 — the ARGUS deployer internals: `deploy.py`, `app.py`, `training.py`, the stack manifest, rehearsal protocol, and teardown.
- Chapter 9 — the registry of every ID, ARN, IP, and endpoint that must change on a new account, plus the PROD/DEMO/DEAD classification.
