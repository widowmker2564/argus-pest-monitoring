# ARGUS Technical Manual — Index

Smart Pest Monitoring System (codename ARGUS). Ngee Ann Polytechnic ECE Final
Year Project, Apr–Aug 2026. Author: Qian Runzhe. Client: Changi Airport Group.
Prepared for Dr. Yan Li, NP staff, and the next student cohort: this manual is
both the technical reference and the project handover document.

_Version 1.1, assembled 2026-08-11. Per-chapter "as of" stamps mark content
freshness. All repository file paths are relative to the project repository
root (the repo that holds `lambda/`, `web/`, `robot/`, `deployer/`,
`datasets/`, `migration/`, `docs/`). AWS resources are located by account,
ARN, console path, or CLI command — never by any one machine's disk layout.
The system runs on one AWS account: the NP production account
`506868652945` (stack deployed 2026-08-10; both detection models trained
there). Every resource name, ARN, and command in this manual points at that
account. The system was originally built and validated on a separate
development account that has since been retired from operation; it appears
only in historical notes._

**Getting the repository.** The project repository is distributed as a
snapshot archive stored in the production account (there is deliberately no
external git hosting — the production account is the single handover point).
The snapshot was published to `handover/` in the frames bucket on
13 August 2026 as `argus-repo-snapshot-20260813.zip` (SHA256-verified after
upload):

- Console: sign in to account `506868652945` → S3 → bucket
  `argus-frames-506868652945` → `handover/` → download the newest
  `argus-repo-snapshot-*.zip`.
- CLI: `aws s3 cp s3://argus-frames-506868652945/handover/argus-repo-snapshot-`**date**`.zip . --profile` **your-profile**

The snapshot holds the code, docs, deployment tooling, dataset scripts and
manifests, the evaluation holdout, and this manual. It does NOT hold: bulk
training images (already in the same bucket under `training-data/`), the
vendor-licensed purchased image set (non-redistributable; obtain from the
project owner), internal working notes and scratch files pruned at packaging
time, this manual's local `backups/` folder, or credentials of any kind.

## How to use this manual

- To understand the system: read Chapter 1, then the chapter for your area.
- To operate the running system: each chapter ends with an Operations section.
- To reproduce the system on a new AWS account: go straight to Chapter 8; it
  points back into Chapters 2–7 where depth is needed.
- Every AWS procedure gives both the console click-path and the CLI command.
  Values you must supply yourself are **bold** in commands.
- The code is ground truth. Where a chapter flags "doc lag", the repository's
  `docs/*.md` file disagreed with the code and the code was followed.

## Chapters

| # | File | Scope |
|---|---|---|
| 1 | `01_system_architecture.md` | The problem, design principles, end-to-end dataflow, deployment topology, component map. Production vision (fixed cameras) vs the Go2 testbed. |
| 2 | `02_cloud_backend.md` | Every AWS resource and its configuration; all Lambdas function by function, incl. the pest-detection-processor pipeline (tiling, hard-object suppression, Bedrock LLM verification). |
| 3 | `03_models_training.md` | Rekognition Custom Labels, the armyworm model ladder, the moth model (inherited from Wilbur Teo), dataset tooling, manifest format, training and evaluation procedure, holdout discipline. |
| 4 | `04_dashboard_frontend.md` | The vanilla-JS dashboard: every module, Cognito auth flow, canvas box drawing, the liquid-glass design system, deploy and user-management runbooks. |
| 5 | `05_edge_go2_orin.md` | The Unitree Go2 + Jetson Orin + SIYI A8 Mini platform: USLAM navigation, map profiles, the gated patrol script, KVS producer, field operations runbook. |
| 6 | `06_edge_minipc.md` | The mini PC + VM moth-camera node: KVS transcode path, systemd services, the self-healing reverse SSH tunnel, access recipes. |
| 7 | `07_deployer.md` | The ARGUS one-click deployer: the pywebview shell, the 15-stage boto3 engine, verify and teardown, the training pipeline, credential security, the exe build. |
| 8 | `08_reproduction_runbook.md` | From-zero reproduction on a new AWS account: the automated (deployer) path and the manual path, device provisioning, account-portability caveats (models are account-bound), smoke tests, costs. |
| 9 | `09_appendix.md` | Registries: every AWS resource id/ARN, device inventory, environment variables, the repository file map, camera settings reference, glossary. |

## Conventions

- English only. Real component names are used throughout (SIYI A8 Mini,
  Unitree Go2, Jetson Orin, AWS Rekognition Custom Labels, USLAM, Kinesis
  Video Streams, CloudFront, Cognito, Bedrock).
- No credential values appear anywhere in this manual. Credential storage
  locations are named instead. Non-secret identifiers (account id, ARNs,
  bucket names, IPs) are included deliberately: they are needed to operate
  and audit the deployed system.
- Open questions that could not be settled from the repository are marked
  inline as short notes (e.g. "not recorded in any repo source"); each names
  the way to settle it.

## Backups

`backups/` holds versioned copies of every chapter (`.v1.md` as first
written, `.v2.md` after the accuracy-audit revisions, `.v3.md` after the
2026-08 refresh). Timestamped zip archives of the whole manual are kept
alongside as `manual_backup_<date>.zip`. The folder is a local working
archive only: the copies predate later cleanups (they still contain
machine-local absolute paths), so `backups/` is excluded from the handover
snapshot and from any distributed manual bundle.
