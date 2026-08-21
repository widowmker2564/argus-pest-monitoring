# Deployer training pipeline — BUILT 2026-07-17 (spec'd 2026-07-16)

> **Note on script paths.** Sections below describe work done earlier in
> the project. Some of the scripts they name were removed in the
> 2026-08-21 repository cleanup, which kept only production code and the
> current pipeline. The reasoning and the measurements stand; the paths
> are a record of how the work was done, not files you will find here.


**Status: implemented.** Engine = `deployer/training.py`; bridges in
`deployer/app.py` (pick_training_folder / analyze_training_folder /
plan_training / start_training / watch_training / training_status /
rollback_model); UI = the `s-train` screen in `deployer/web/index.html`
(reached from the done screen; resume bar re-attaches to an in-flight run
on relaunch). Pillow added to requirements.txt + build.ps1. Not yet in a
rebuilt exe — rebuild before the rehearsal.

Runzhe's requirements #3 + #4 from the 2026-07-16 review: a customer who has
deployed the stack must be able to (a) upload a YOLO dataset, (b) have it
converted + trained automatically, (c) have the resulting model WIRED IN
(camera rows' `custom_model_arn` updated — "训出来了也是白搭" otherwise), and
(d) iterate: retrain later with new data, one-click, with rollback.

**Architecture ruling (settled 2026-07-16): deterministic automation, NO agent.**
Every step below is a fixed API pipeline; the converter math already exists and
is production-proven (v3-v6 all used it). An LLM holding admin credentials while
mutating cloud resources is a trust/safety liability, and the agent scaffolding
would exceed the pipeline's own code size. The only acceptable LLM touchpoint
(LATER, optional): a read-only "explain this error" helper. Not now.

## Flow (new "Train" screen in ARGUS, available when deployment_status=complete)

1. **Pick data** — pywebview `create_file_dialog(FOLDER_DIALOG)`: the customer
   selects a LOCAL folder. No S3 console, no manual uploads. Expected layouts
   (auto-detected): `images/{train,val,test}/ + labels/...` (YOLOv8) or flat
   `train/images + train/labels` (Roboflow export). `data.yaml` optional; if
   nc>1 ask which class id is the target (single-class doctrine).
2. **Validate locally** (fail fast, before any upload): image/label pairing,
   parseable YOLO lines, coords in [0,1], min counts (warn <100 train imgs),
   image dims ≤ 4096 (downscale like build_negatives did, or reject), warn on
   zero-box images (CL OD ignores them — the negatives lesson).
3. **Convert locally** — port `datasets/convert_to_manifest.py` logic into
   `deployer/training.py` (pure function: yolo dir -> GT manifest lines).
   Auto 85/15 train/test split when no explicit test split exists.
4. **Upload** — images to `s3://{prefix}-frames-{acct}/training-data/v{N}/...`
   + manifests. Progress bar (boto3 upload with callback -> _push events).
5. **Cost gate** — explicit screen: "Training runs ~1-4 h at AWS's training
   rate (~$1/h, billed by AWS)" + typed OK. (Product directive: confirm costs
   before training/model start.)
6. **Create datasets + train** — Rekognition `create_dataset` (TRAIN/TEST from
   manifest) on the deployed project (`rekognition:project` in state) or
   `update_dataset_entries` on iteration; `create_project_version`
   VersionName `v{N}-{date}`, OutputConfig `training-output/v{N}`.
7. **Watch** — poll every 5 min (background thread, survives app close: on
   relaunch `deployment_status` also reports training-in-progress by describing
   project versions). Show elapsed + billable estimate.
8. **Wire in (the critical step)** — on TRAINING_COMPLETED: read F1 from
   EvaluationResult; write `custom_model_arn` on every `model_type=custom`
   camera row (table `pest-monitoring-cameras`); save
   `train:v{N}:{arn,f1,completed_at}` + `train:active_arn` into deploy state.
   Show F1 to the user with plain-language framing.
9. **Iterate (#4)** — "Train a new version" reruns 1-8 with N+1; keep the
   previous ARN as `train:rollback_arn`; UI offers one-click ROLLBACK (swap the
   camera rows back — this codifies the v6→v5 rollback we did by hand on
   2026-07-15). Old training data prefixes are listed with a delete button
   (S3 lifecycle honesty: they cost cents but confuse).

## Pieces that already exist (reuse, do not rewrite)
- YOLO->GT conversion + 4096 handling: `datasets/convert_to_manifest.py`,
  `build_negatives_v6.py` (save_capped)
- dataset load + acceptance check pattern: `datasets/append_negatives_v6.py`
  (LabeledEntries delta check — reuse verbatim)
- training watch: `datasets/v6_watch.py`
- ARN swap: the boto3 one-liner from the v6 trial/rollback (aws.md warns:
  PowerShell mangles ARNs — boto3 only)
- event streaming + theater UI + resume machinery: all in place

## Est. effort
`deployer/training.py` (~300 lines) + Api bridges (~120) + Train screen
(~250 html/js) + state plumbing. One focused session.

## Open decisions — DEFAULTS IMPLEMENTED 2026-07-17 (Runzhe can override)
- Auto-START after training: **NO** (implemented). The done panel says so
  explicitly: start detection from the dashboard, US$4/h, the watchdog stops
  forgotten models.
- Retention: **no auto-delete** (implemented). Old versions stay until the
  customer deletes them in the console; on AWS's version limit the pipeline
  stops with a clear message instead of deleting anything itself.
- Design item 9's "delete old training-data prefixes button" was **dropped on
  purpose**: iteration APPENDS to the same Rekognition datasets, so every past
  vN prefix stays referenced by dataset entries — deleting one would break the
  next retrain. Storage cost is cents; correctness wins.
