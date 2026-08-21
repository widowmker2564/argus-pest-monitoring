# Chapter 3 — Detection models and the training pipeline

This chapter covers the two AWS Rekognition Custom Labels models (moth and armyworm), the armyworm version ladder, the CAG holdout discipline, every dataset-tooling script, the manifest format, the full train / evaluate / wire / rollback procedure, and the August 2026 retraining of both models on the NP production account.
_As of 2026-08-14._ (Facts with a different date are marked with that date.)

## 3.1 Role in the system

ARGUS detects pests with AWS Rekognition Custom Labels, a managed object-detection service. We do not train neural networks ourselves. We prepare labeled images, hand Rekognition a manifest, and it trains and hosts a model. The `pest-detection-processor` Lambda (Chapter 2) calls that hosted model on every frame that lands in S3, whether the frame came from the SIYI A8 Mini on the Unitree Go2, from a scheduled camera, or from the dashboard's Test upload panel.

Two models exist. The moth model was inherited from the predecessor project and is stable. The armyworm model is this project's own work and went through fourteen training iterations (v0 through the v9 retrain in the ladder below). On this platform the model is a black box: the only levers are the training data, the split, and the class list, so every improvement attempt was a data experiment. This chapter records what each iteration was, what it was measured on, and what the measurements actually mean. The short version: the model is strong inside its training domain; the long-standing gap to Jewel Changi imagery was narrowed in the v9 era by a role change (the detector became a high-recall front end with an LLM verification gate behind it) plus added data augmentation, and the residual limit is precision on field scenes, not recall.

The system now runs on the NP production account **506868652945** (IAM user `Student_QianRunzhe`, CLI profile `prod`), region **us-east-1**. The stack was deployed there 2026-08-10 by the ARGUS deployer (Chapter 7). The system was built and validated in the development account **366356442579** (CLI profile `nbk2`); that account is retired history, and every dev-account name or ARN in this chapter is kept only as the record of how each iteration was built. Models are account-bound: they cannot be exported or migrated, so both detectors were retrained on the production account from migrated data — the moth model as `argus-moth-detection` / `moth-prod-20260811` (3.4), the armyworm model as `argus-detection` / `v9r-prod-20260810` (Rekognition test-split F1 0.613), both wired into the production camera rows and verified live end to end 2026-08-12 (3.17). Copying an ARN across accounts is never an option. The handover snapshot `argus-repo-snapshot-20260813.zip` was published 2026-08-13.

## 3.2 Inventory

The two `(prod)` rows are the operative models. Every row naming account 366356442579 or a `nbk2`-era project is development history on the retired dev account, kept for the iteration record only.

| Component | Location | Purpose |
|---|---|---|
| Moth model (prod, operative) `moth-prod-20260811` | `arn:aws:rekognition:us-east-1:506868652945:project/argus-moth-detection/version/moth-prod-20260811/1786412382515` | Rebuilt on the production account from the recovered moth training set, F1 0.991 (3.4). Wired into the production `moth_cam` row. |
| Armyworm model (prod, operative) `v9r-prod-20260810` | `arn:aws:rekognition:us-east-1:506868652945:project/argus-detection/version/v9r-prod-20260810/1786377372187` | The v9-retrain recipe retrained on the production account. Trained (Rekognition test-split F1 0.613) and wired into the production `worm_cam` + `manual_upload` rows; verified live 2026-08-12 (3.17). |
| Moth model (dev, history) `SmartPestProject.2026-02-15` | Rekognition, retired dev account 366356442579 | Adult moth detection, label `Moths`, F1 0.988. Inherited from Wilbur Teo. |
| Armyworm v9 retrain (dev, history) | `arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection-v9/version/v9-20260805-0713/1785913987295` | The settled model on the dev account's `worm_cam` from 2026-08-07 until the migration, label `armyworm-larva`. F1 0.599 — a front-end figure, not pipeline accuracy (3.5). Its recipe and data are what `v9r-prod-20260810` retrained on production. |
| Armyworm v5 | `arn:aws:rekognition:us-east-1:366356442579:project/armyworm-detection/version/v5-2026-07-07/1783394123547` | Production model 2026-07-07 → 2026-07-29, F1 0.852 in-domain (health warning in 3.5). Endpoint STOPPED. |
| Armyworm v4 (rollback) | `...project/armyworm-detection/version/armyworm-detection.2026-05-21T12.46.19/1779338780450` | Older rollback target, F1 0.719. |
| Armyworm v6 (rejected) | `...project/armyworm-detection/version/v6-2026-07-14/1784011732429` | Kept for the record only. Endpoint stopped. |
| Armyworm v7.1 (rejected) | project `armyworm-detection-v7`, version `v7-1-20260720-0604` | Light-worm experiment. Rejected. |
| Armyworm v7.2 (rejected) | project `armyworm-detection-v7-2`, version `v7-2-20260721-0301` | Black-set experiment. Rejected. |
| Armyworm v8, v9 first cut | projects `armyworm-detection-v8`, `armyworm-detection-v9` | The augmentation era before the v9 retrain (3.5). Superseded; endpoints stopped. |
| CAG holdout | `datasets/holdout/cag/` (`batch_1/`, `batch_2/`) | The only real CAG-domain evaluation signal. batch_2 is never trained on. |
| Training sources | `datasets/sources/corn_leaf/`, `datasets/sources/moth_zldog/`, plus the vendor-licensed offline image set obtained from the project owner (kept outside the repo) | Raw labeled image supply. |
| `convert_to_manifest.py` | `datasets/current/` | YOLO (Roboflow maize set) to Ground Truth manifest. |
| `convert_other_caterpillar_new.py` | `datasets/current/` | YOLO (the vendor-licensed offline set) to manifest, relabeled. |
| `merge_manifests.py` | `datasets/current/` | Concatenates per-source manifests into the v5 combined set. |
| `upload_images.py` | `datasets/current/` | Uploads only manifest-referenced images to S3, idempotent. |
| `rank_purchased.py` | `datasets/current/` | Quality-ranks the purchased set, writes `purchased_ranked.csv`. |
| `build_v7_2.py` | `datasets/current/` | Assembles the v7.2 set with a strict leak-proof 80:20 split. |
| `build_v7_4.py` | `datasets/current/` | Assembles the fair v7.4 composition (the v8/v9 base) with the batch_2 MD5 abort guard (3.11). |
| `augment_build_v8.py` | `datasets/current/` | The 13x TRAIN augmentation build (v8): exact geometric box transforms + photometric jitter + arbitrary rotations, self-tested (3.11). |
| `build_v9_91.py` | `datasets/current/` | Re-splits the v7.4 pool 90:10 by unique source stem and augments BOTH splits 13x (v9) (3.11). |
| `build_answer_key.py` | `datasets/current/` | Renders the adjudicated ground-truth answer key + confirmation sheets for the 2026-08-11 controlled evaluation (3.12). |
| `train_v7_2.py` | `datasets/current/` | boto3 orchestrator: project + datasets + training + watcher. |
| `evaluate_v7_1_vs_v5.py`, `evaluate_v7_2_vs_v5.py` | `datasets/current/` | Whole-image CAG-holdout arbitration between two models. |
| `evaluate_batch2_v5_vs_v4.py` | `datasets/archive/experiments/one_off_scripts/` | The original batch_2 arbitration (archived; points at pre-reorg paths). |
| `migration/copy_training_data.py` | `migration/` | Server-side cross-account copy of the training data + manifest repoint (3.17). |
| `migration/train_v9r_on_prod.py` | `migration/` | Retrains the armyworm model on the production account from the live dataset entries (3.17). |
| `migration/poll_training.py` | `migration/` | Network-fault-tolerant training watcher (3.17). |
| `migration/migrate_moth.py` | `migration/` | Rebuilds the moth detector on the production account (3.4). |
| Manifest backups | `datasets/current/manifests_v5_*` through `manifests_v9/` | Frozen manifest sets per version. |
| Model wiring | DynamoDB `pest-monitoring-cameras`, attribute `custom_model_arn` | The single runtime pointer to the live model (per account). |
| Training data on S3 | `s3://argus-frames-506868652945/training-data/...` (production, operative); `s3://frames-armyworm-366356442579/training-data/...` (retired dev) | Images + manifests Rekognition trains from. |

## 3.3 Rekognition Custom Labels primer

Skip this section if you already run Custom Labels. Everything later depends on these facts.

**Object model.** A **project** is a named container. Inside it live **datasets** (one TRAIN, one TEST) and **project versions**. A project version is one trained model: immutable, with its own ARN, its own F1 score, and its own hosting endpoint. "Retraining" always means creating a new version. Old versions stay and can be re-hosted at any time, so rollback is trivial.

**Lifecycle.** `create-project-version` starts training (`TRAINING_IN_PROGRESS` → `TRAINING_COMPLETED`, typically 1–4 hours here). A completed model serves nothing until you host it: `start-project-version` (`STARTING` → `RUNNING`, 10–20 minutes). Only a RUNNING model answers `detect-custom-labels`. `stop-project-version` shuts the endpoint down.

**Cost.** Hosting bills about **$4/hour per inference unit while RUNNING**, whether or not you call it. Training bills separately by `BillableTrainingTimeInSeconds`. Standing discipline in this project: start, evaluate, STOP, and verify the stop. Note two endpoints bill at once when both the moth model and an armyworm model are up.

**The ARN colon rule.** A version ARN looks like:
`arn:aws:rekognition:us-east-1:506868652945:project/argus-detection/version/v9r-prod-20260810/1786377372187`
It contains colons. A raw `aws dynamodb update-item` typed in PowerShell mangles the quoting around it. When writing an ARN into DynamoDB, use boto3 (see `datasets/archive/experiments/one_off_scripts/migrate_camera_ids.py` for the style; the 2026-07-20 datasets reorg moved it there) or pass the item as a `file://` JSON document. Never inline the ARN in a PowerShell command string.

**Account-bound.** Models cannot leave the account they were trained in. This stopped being hypothetical in August 2026: standing the system up on the NP production account required retraining both models there and updating every hardcoded account id, bucket name, and ARN. 3.17 records how it was actually done.

**One structural limit that shapes the whole system.** Custom Labels object detection cannot train on negative (zero-box) images. Every training image must carry at least one bounding box. The consequences are in 3.16.

**Console entry point:** AWS console → Amazon Rekognition → Use Custom Labels (left menu) → Projects. **CLI:** all commands are `aws rekognition ...` with `--profile prod --region us-east-1` on the NP production account. (The retired development account used `--profile nbk2`; that profile appears below only in historical records and in scripts that were run against the dev account.)

## 3.4 The moth model (inherited)

`SmartPestProject.2026-02-15`. **F1 0.988**, single label **`Moths`**. This model is the work of the predecessor FYP student, **Wilbur Teo**, whose moth-detection v1 this project inherited and extended. It is native in the development account (366356442579). No retraining was done on it during the main project; the production-account rebuild below recreated it from its own data.

The `moth_cam` camera row is served today by the production rebuild `moth-prod-20260811` (below); on the retired dev account the inherited model played the same role. Either way the model's ARN sits in that row's `custom_model_arn` like any other model (3.15). It participates in the same processor pipeline, except that the LLM crop-verification gate never runs for it: the verify prompt asks about larvae and `moth_cam` targets adult moths, so `llm_verify_enabled` is false on that camera (Chapter 2).

Nothing in the rest of this chapter's ladder or tooling applies to the moth model. It is a finished, stable, inherited asset.

**Production-account rebuild (2026-08-11).** The model itself is account-bound and could not follow the migration, but its data survived intact: **116 TRAIN + 29 TEST labelled images** (537 + 127 boxes) in the old account's Rekognition console bucket `custom-labels-console-us-east-1-d1abc2aed2`. `migration/migrate_moth.py` recovered them end to end: merge a scoped cross-account read grant into the console bucket's policy, copy the images server-side into `argus-frames-506868652945/training-data/moth/`, export the labelled dataset entries with `ListDatasetEntries` (the dataset, not any S3 manifest, is the source of truth for labels — the same lesson as 3.17), repoint every `source-ref`, then create project **`argus-moth-detection`**, both datasets (TRAIN 116/116, TEST 29/29, CREATE_COMPLETE), and version **`moth-prod-20260811`**. It trained the same day at **F1 0.991** — Wilbur's original scored 0.988 on the same data, so treat the two as equivalent, not as an improvement. The new ARN is wired into the production `moth_cam` row, and **`llm_verify_enabled` is false on that row on purpose**: the verify prompt targets larvae and this camera's target is adult moths. Revoke the console-bucket read grant with `python migration/migrate_moth.py --revoke` once the migration is signed off.

## 3.5 The armyworm model ladder

Read the health warning after the table before quoting any number from it.

| Ver | Date | Training composition | F1 | F1 measured on | Verdict |
|---|---|---|---|---|---|
| v0 | 2026-04-09 | 25 images (first practice run, old account 396278862184) | 0.818 | its own tiny split | Garbage / overfit. Discarded, never deployed. |
| v1 | 2026-04-28 | 103 train Roboflow `maize-fallarmyworm`, single class | 0.749 (P 0.833 / R 0.680) | 21-image Roboflow test split | First real baseline. Recall was the weak axis. |
| v2 | 2026-05-08 | v1 + paid caterpillar set as a second class ("Route 2") | — | — | FAILED. Shortcut learning: the model discriminated by photo source, not species. Reverted to single class. |
| v3 | May 2026 | 248 images (108 Roboflow + 140 paid + 11 anchor images + 2 mislabelled "negatives") | 0.719 | its own test split | Consolidated single-class. Bud false positives surfaced. |
| v4 | 2026-05-21 | 261 train / 65 test, retrained after migration to nbk2 | 0.719 | its own 65-image test split | Long-running deployed baseline. Still the older rollback target. |
| v5 | 2026-07-07 | 1160 train (108 Roboflow + 1040 purchased full set + 12 anchor images) | **0.852** | purchased-domain TEST 133 | Production model 2026-07-07 → 2026-07-29. See the health warning below. |
| v6 | 2026-07-14 | v5 + 352 close-ups (152 idle valid-split + 200 synthetic worm-centred crops) = 1512 train | 0.839 | same TEST 133 (held identical) | REJECTED, rolled back to v5 next day. Close-up crops added no domain information; lost two previously-detected light worms. |
| v7.1 | 2026-07-20 | 1503 images total (1103 corn DST1105 FAW + 400 moth-zldog larva); no black set | 0.744 | its own corn+moth test split | REJECTED, keep v5. Light-colour hypothesis did not transfer; batch_2 tie. |
| v7.2 | 2026-07-21 | v7.1 sources + top-500 quality-ranked purchased black caterpillars | 0.794 | its own corn+moth+black test split | REJECTED. batch_2 got worse (2/7 vs 3/7 @50%, original 7-image set); foliage false positives strengthened. |
| v7.3 | 2026-07-22 | v7.1 corn+moth base + the full ~1300 purchased black set (project `armyworm-detection-v7-3`, version `v7-3-20260722-0610`) | — | its own split | REJECTED. Confirmed the black set is net negative at full volume, not just at top-500. |
| v7.4 | 2026-07-23 | the consolidated "fair" composition: corn + moth-zldog + maize supply + the established anchor images (project `armyworm-detection-v7-4`, version `v7-4-20260723-0259`) | — | its own split | Pulled level with v5 on the fair holdout. Kept as the fair yardstick and the base composition for v8/v9. |
| v8 | 2026-07-23 | v7.4 composition with TRAIN augmented ~13x (flips, rotations, exposure jitter; 2251 → ~29k images); TEST held at v7.4's un-augmented 567 | 0.739 | v7.4's test split | Augmentation build. Superseded by v9 in the same test cycle. |
| v9 | 2026-07-25 | same supply, re-split 9:1 by unique source stem, both sides augmented 13x (train 32,981 / test 3,653) | 0.591 | its own augmented test split | Went LIVE 2026-07-29 together with the role change below: high-recall front end + LLM verification gate. |
| v9r (v9 retrain) | 2026-08-05 | v9 with added data augmentation (the documented 13x build: flips, rotations, exposure jitter). Version `v9-20260805-0713` | 0.599 | its own test split | **The settled model.** Live on the dev account's `worm_cam` from 2026-08-07; retrained on production as `v9r-prod-20260810` (F1 0.613), the operative model since 2026-08-12 (3.17). Best of the ladder on the never-trained holdout (see below). |

**Standing health warning — do not quote 0.852 as accuracy.** Each F1 in the table was measured on a DIFFERENT test set that grew and changed domain with the training set. This is a record of engineering iterations, not a monotonic climb on one benchmark. Specifically, v5's 0.852 is **in-domain only**: it was earned on the purchased commercial TEST 133 and **did not transfer to CAG imagery**. On the batch_2 CAG holdout, v5 ties v4 at the operational threshold (both 3/7 @50%, arbitrated 2026-07-13; scored on the original 7-image batch_2, 101-107 — the historical arbitration set, not the fixed 102-109 eval set; see 3.6). Symmetrically, do not read v9's 0.591 / v9r's 0.599 as a regression: from processor v6.x onward the detector is deliberately run as a high-recall front end (tiled, candidates gathered down to `TILE_MIN_CONFIDENCE = 8`) with the Sonnet 4.6 verification gate supplying precision, so the detector's stand-alone F1 no longer describes pipeline behaviour.

**Domain, not colour, is the binding constraint.** Three controlled follow-ups confirmed it:
1. **v6 (scale test):** the same worms at a tighter scale bought nothing — zero misses converted to hits, confidence merely deepened where the model was already right (cag_armyworm_104: 75.9% → 97.0%).
2. **v7.1 (colour test):** real light-coloured Spodoptera positives from corn and rice domains did not teach the model CAG light worms. The two thesis images the pivot targeted, cag_armyworm_003/004, scored 9.3% and 24.2% under v7.1 (internal batch_1 spot-check numbers — not part of the outward-quotable eval set).
3. **v7.2 (volume test):** re-adding 500 black-caterpillar positives made batch_2 worse (2/7 vs 3/7 @50%). The bud_002 confidence rise (91% → 97.4%) (internal batch_1 numbers) was recorded at the time as false-positive evidence, but a later ground-truth correction (see 3.6) found bud_002 contains a real larva, so that delta is not evidence of a false-positive prior.

Locked lesson from the v5–v7.3 experiments: out-of-CAG-domain positives do not close the gap regardless of colour or volume. In-domain generalisation is strong (10/10 on never-trained purchased TEST images, most at 99%+, proven 2026-07-13). The levers that remained were deployment geometry (close-range capture is the model's working domain) and the pipeline changes of the v9 era below. v7.1 and v7.2 are kept as clean, report-grade negative results.

**The v8 → v9 → v9r era (2026-07-23 to 2026-08-07).** After v7.2/v7.3 the direction changed from sourcing new positives to (a) data augmentation on the consolidated supply and (b) changing the detector's JOB. v8 was the first 13x augmentation build; v9 superseded it in the same test cycle. v9 went live 2026-07-29 together with the architecture change: the detector became a high-recall front end (tiling on, candidates gathered down to `TILE_MIN_CONFIDENCE = 8`) and Sonnet 4.6 judges every box (Chapter 2). The measurement behind the change: raw tiled candidate coverage found every batch_2 worm — the detector was never the recall bottleneck; the worms sat at 10–17% confidence, below the old operating floors. **v9r**, trained 2026-08-05 with added data augmentation, replaced v9 on `worm_cam` on 2026-08-07 and is the settled model. On the never-trained holdout benchmark (batch_2 102–109; see 3.6) the full v9r pipeline scores **7/8 at the 34% zero-worm-loss floor and 5/8 at floor 49** (the 2026-08-10 decision; the live production floor was later refitted to 33 — next paragraph), and `cag_armyworm_103` — missed by every earlier model and configuration — was detected for the first time (34.9%). Still unsolved: `cag_armyworm_107` has never been detected by any model or configuration.

**The threshold ladder and the floor-49 decision (2026-08-07 → 2026-08-10).** The full 26-image holdout was pushed through the live pipeline twice: once at post-verify floor 34 / area cap 10% (`v9r_*` dashboard zones) and once at floor 49 / area cap 5% with the tightened verify prompt (`v9r49_*` zones). The two runs mapped the confidence axis: Jewel junk boxes cluster at 40–48%, while the newly-reachable worm hits sit at 34.9–39.4%. **No single floor separates them.** Floor 34 is the zero-worm-loss setting; floor 49 is the maximum-cleanliness setting at the cost of those low-confidence hits (batch_2 eval 5/8 instead of 7/8; Jewel patrol frames 3+3 boxes instead of 10+8; 41 total boxes across the 26 images instead of 70). **Decision (Runzhe, 2026-08-10): floor = 49, precision-first.** Flipped live the same day — the camera row's `post_verify_floor` and the processor env `POST_VERIFY_FLOOR` both read back 49. Both runs stay on the dashboard as the threshold-study evidence. **The floor is per-build, not a constant of the system: the current live value on production is 33 (refitted 2026-08-13).** The production retrain `v9r-prod-20260810` scores some of the same worms lower than the dev-account v9r did (one holdout image's top box fell from 77.2% to 14.3% across the two trainings), and the curated gallery batch was produced at floor 33, so on 2026-08-13 the live floor was set to 33 (camera row + Lambda env) so that a live Test upload draws the same boxes the gallery shows. Verified against the production `worm_cam` row 2026-08-14: `post_verify_floor` 33, `min_confidence` 10, `tiling_enabled` true, `llm_verify_enabled` true, `max_runtime_min` 45. The floor-49 story above stays as the recorded threshold study and the report-quoted configuration. One boundary the study fixed permanently: a persistent false positive survives at 81.5% confidence, ABOVE any usable floor — thresholds cannot remove that class of error; only the verify prompt/gate can. Companion config changes, live since 2026-08-07: `POST_MAX_BOX_AREA` 0.10 → **0.05** (the largest real worm measured in the holdout imagery is 4.39% of frame, so the headroom is only 1.14x — a close-up worm exceeding 5% of frame would be silently suppressed; first suspect if a big obvious worm has no box), and the verify prompt was tightened to describe the larva as elongated and soft-bodied with clear segmentation, typically carrying yellow-and-black stripes (widened again on 2026-08-12 for pale young larvae — 3.16). Processor v6.3 (dead experimental code stripped, 3266 → 1545 lines) deployed the same day.

**Field-realistic qualitative check (adjudicated 2026-08-11).** The 4 field-realistic images (3.6) were adjudicated box by box against the floor-49 records: all 4 worms are found (true boxes at 49.9–88.2%), alongside 5 false positives at 62–84% — above the real worm's confidence in 3 of the 4 images. Confidence does not rank truth. The false positives trace to generic-domain training data rather than site-captured data; that is the delivered system's precision ceiling, and it is stated as a limitation, not hidden. These 4 images are presented as qualitative demonstration only, never as a recall statistic.

Traceability: F1 and billable training time per version come from `describe-project-versions` and `deployer/audit/rekognition__armyworm-detection__project-versions.json`. Arbitration raws: `datasets/holdout/cag/batch2_arbitration_20260713_105204.json`, `v7_1_vs_v5_20260721_021123.json`, `v7_2_vs_v5_20260721_054531.json`. The 2026-08-07 threshold-study runs live on the dashboard zones `v9r_*` and `v9r49_*`.

## 3.6 The CAG holdout discipline

`datasets/holdout/cag/` holds the only real CAG-domain photographs in existence. They are the arbiter for every model-version decision.

- **`batch_1/`** — 13 images: `cag_armyworm_001.jpg` … `cag_armyworm_011.jpg` plus `cag_bud_001.jpg` and `cag_bud_002.jpg`. batch_1 is used for internal spot checks only — outward-facing evaluation numbers are quoted solely from never-trained images (batch_2 102-109, Jewel_1/2, the 4 field-realistic photos; see the subsets below). `cag_bud_002` carries a hand-drawn blue circle (drawn markings make an image unusable for dataset work). **Ground-truth correction (recorded in `docs/state.md`):** both `cag_bud_001` and `cag_bud_002` contain a clearly visible striped larva (bud_002's is the one circled in blue). Both are POSITIVES, not false-positive probes. Consequence: the CAG holdout contains NO true negatives at all, so image-level false-positive rate is not measurable on it, and any past or future claim of a false-positive rate on this holdout is unsupported. Do not cite bud_00x confidence rises as false-positive evidence.
- **`batch_2/`** — the **sacred holdout. Never train on it.** The original benchmark set was `cag_armyworm_101.jpg` … `cag_armyworm_107.jpg` (7 images, Jewel wide indoor scenes). A 2026-07-17 re-audit found 5 of those 7 carry hand-drawn circles from CAG staff (101 blue, 102 pink, 103 green with the "416" jeep in frame, 106 red, 107 blue); only 104 and 105 are clean. So treat the original batch_2 as a 2-clean-image benchmark with 5 contaminated companions, not a pristine 7.

The `batch_2/` folder had grown to 11 files (`cag_armyworm_101`–`111`) by 2026-07-28. All arbitrations recorded up to 2026-07-21 used the original 7 (101–107). Provenance of the additions (per `docs/state.md`): `cag_armyworm_108` and `109` arrived 2026-07-23 — both CLEAN (no hand-drawn circles) and real Jewel domain, 108 a clear patterned armyworm on granite plus leaf, 109 two small dark low-contrast worms. `cag_armyworm_110` and `111` are Runzhe's own phone photos taken 2026-07-28 (source files IMG_1574 and IMG_1577), stored EXIF-upright at 3024x4032 with Orientation stripped so the pipeline and browser see the same pixels.

**The benchmark subsets behind the 3.5 numbers (fixed early August 2026, before the 2026-08-11 re-curation below):**
- **Quantitative recall: `cag_armyworm_102`–`109` (8 images) — the "batch_2 eval" set.** All 2026-08 model comparisons and the threshold study (3.5) score this subset.
- **Qualitative field demonstration: the 4 field-realistic images** — the two Jewel patrol frames `CAG_Jewel_1/2` (captured by the Go2 on patrol) plus `cag_armyworm_110/111` (shot at the Go2's capture angle and resolution). Adjudicated box by box (3.5); presented as demonstration, never as a recall statistic.
- Evaluation claims in any outward-facing document are quoted ONLY from these images.

**Re-curation (2026-08-11) — the current evaluation set.** For the controlled model-ladder evaluation, Runzhe re-curated the holdout. The evaluation set is now **22 images: batch_1 (13, unchanged) + batch_2 `101`–`105` + `batch_Jewel/Jewel_armyworm_1`–`4`**, with the adjudicated ground truth in `datasets/current/answer_key/answer_key.json` (33 confirmed worms; the build script is covered in 3.12). File identity was MD5-checked against S3 ETags: `Jewel_armyworm_1/2` are the former `cag_armyworm_110/111`, `Jewel_armyworm_3/4` are the former `CAG_Jewel_1/2`, and **the current batch_2 `102`–`105` are DIFFERENT photographs from the files the earlier arbitrations and the 2026-08-07 threshold study scored** — the 2026-07-28 hand labels for 104/105 are retired. So none of the recorded numbers above can be reproduced by re-running on today's tree; each is tied to the files as they stood on its run date. On disk, `batch_2/` still holds 13 files (101–111 plus the two Jewel patrol frames), `clean/` holds circle-removed copies, and `_pre_rebuild_backup/` / `_corrupted_20260811/` are recovery snapshots. The 22-image answer-key set, not the raw folder contents, is the operative benchmark.

The evaluate scripts glob `batch_1/*.jpg` + `batch_2/*.jpg`, so a naive re-run today would score 26 files, not the 20 the recorded arbitrations used — and four of today's batch_2 files are different photographs (above). Never compare such a run against the recorded numbers.

**Whole-image evaluation protocol.** All holdout arbitration uses one `detect_custom_labels` call per image on the whole, untiled, unscaled frame, with `MinConfidence=0` so the full confidence distribution is captured, and thresholds (30 / 50 / 70, headline 50) applied locally afterwards. The production pipeline historically had a tiled path; numbers from the two paths must never be compared (see `docs/detection.md`, "Two detection paths"). Any A-vs-B model comparison must push both models through this same script path.

**Leak prevention is mechanical, not honor-system.** `build_v7_2.py` computes the MD5 of every holdout image and drops any training candidate whose hash matches (3.11). Keep that check in every future build script.

**The holdout tree has no backup but S3.** On 2026-08-12 nine holdout files (106–111, the two Jewel patrol frames, and one cleaned copy) were found missing from the local tree — deleted at some point after 2026-08-07, not moved. All nine were recovered from the old account's S3 push history (`migration/restore_holdout.py` scans every `frames/worm_cam/*` zone and pulls the newest copy; integrity was verified by image dimensions, not assumed). The lesson for any successor: the S3 push history is the only backup the holdout has, and the local tree is not versioned — do not rely on either alone.

## 3.7 `convert_to_manifest.py` — YOLO to Ground Truth manifest

**Purpose.** Converts the Roboflow `maize-fallarmyworm-1` YOLOv8 export into SageMaker Ground Truth object-detection manifests, one per split, in the format Rekognition Custom Labels imports. Output: `manifests_v5_roboflow/{train,valid,test}.manifest`.

**Configuration (constants at top of file).** `DATASET_ROOT` = `datasets/current/maize-fallarmyworm-1`; `S3_BUCKET` = `frames-armyworm-366356442579`; `S3_PREFIX` = `training-data/armyworm`; `LARVA_CLASS_ID = 2` (the fall-armyworm-larva class in the source's class map); `OUTPUT_CLASS_NAME` = `armyworm-larva`. All paths are anchored to the script's own folder (`Path(__file__).resolve().parent`), so the working directory does not matter. As checked in, `S3_BUCKET` still names the retired dev bucket; any production re-run must set it to `argus-frames-506868652945` first (3.17), or every emitted `source-ref` points at a dead bucket.

**Functions and algorithm.**
- `parse_yolo_line(line)` — parses one YOLO label line. Two formats are accepted. Detection format (`class_id cx cy w h`, 5 numbers) is taken as-is. Segmentation format (`class_id x1 y1 x2 y2 ...`, 1 + 2N numbers, N ≥ 3) is reduced to its axis-aligned bounding box: split the numbers into xs (even indices) and ys (odd indices), take min/max of each, then `cx = (x_min+x_max)/2`, `cy = (y_min+y_max)/2`, `w = x_max−x_min`, `h = y_max−y_min`. Anything else (odd count, non-numeric) returns None and is skipped silently.
- `yolo_to_sagemaker_bbox(cx, cy, w, h, img_w, img_h)` — converts normalized YOLO centre-format to absolute-pixel corner-format: `left = (cx − w/2) * img_w`, `top = (cy − h/2) * img_h`, `width = w * img_w`, `height = h * img_h`, each rounded to int, with `left`/`top` clamped at 0. `class_id` is always emitted as 0 (single output class).
- `process_split(split_name)` — for each `.txt` label in the split's `labels/` dir: find the matching image (`.jpg`/`.jpeg`/`.png`), parse all lines, keep only class `LARVA_CLASS_ID` boxes, read the true image dimensions with PIL, build the annotations list, and write one manifest JSON line pointing at `s3://<bucket>/<prefix>/<split>/images/<filename>`. **Images with zero kept boxes are skipped entirely** — they never enter the manifest. A label with no matching image prints a warning and is skipped.

The conversion function is small enough to quote whole — this is the exact math every v5-era manifest box went through:

```python
def yolo_to_sagemaker_bbox(cx, cy, w, h, img_w, img_h):
    """Convert normalized YOLO bbox to absolute-pixel SageMaker bbox."""
    abs_w = w * img_w
    abs_h = h * img_h
    left  = (cx - w / 2) * img_w
    top   = (cy - h / 2) * img_h
    return {
        "class_id": 0,
        "top":      max(0, int(round(top))),
        "left":     max(0, int(round(left))),
        "height":   int(round(abs_h)),
        "width":    int(round(abs_w)),
    }
```

Note the asymmetry: only `top`/`left` are clamped (at 0); `width`/`height` are trusted as given, so a box overrunning the right or bottom edge survives unclamped. The later build scripts (3.11) clamp both corners into [0,1] before scaling.

**Data in/out.** In: YOLO labels + images under `DATASET_ROOT/{train,valid,test}/{labels,images}`. Out: one JSONL manifest per split plus a printed kept/skipped/box-count summary.

**Gotchas.**
- The zero-box skip is deliberate but has a system-wide consequence: **the training set contains no true negatives at all.** That, combined with the platform limit in 3.16, is the root cause of the foliage false-positive prior.
- The image size in the manifest must be the real pixel size — the script reads it from the file, never from the label. Keep it that way; a wrong `image_size` silently corrupts every box.
- The class filter is hardcoded; if the source dataset's class map changes, `LARVA_CLASS_ID` must be re-verified against its `data.yaml`.

## 3.8 `convert_other_caterpillar_new.py` — the purchased-set variant

Same algorithm as 3.7 (identical `parse_yolo_line` and `yolo_to_sagemaker_bbox`), modified for the purchased set's layout. Differences only:
- `DATASET_ROOT` points at the vendor-licensed offline image set obtained from the project owner — the purchased master copy deliberately lives outside the repo.
- YOLOv8 default folder shape: `images/{split}/` and `labels/{split}/`, and the middle split is named `val`, not `valid`.
- `SOURCE_CLASS_ID = 0` (the set's only class) and it is **relabeled to `armyworm-larva`** on output. This is the v5 single-class doctrine: after the v2 multi-class failure, all positives merge into one class.
- `S3_PREFIX` = `training-data/other-caterpillar`; output to `manifests_v5_purchased/`.

Gotcha carried from the 2026-07-17 audit: this purchased set (about 1300 images, 89% of v5's TRAIN) is largely **not armyworm** — black spiny temperate larvae plus tussock-moth larvae, some blurry, some sky-shot, some boxes with no visible worm. The converter faithfully converts whatever it is given. Quality control lives in `rank_purchased.py` (3.10), not here.

## 3.9 `merge_manifests.py` and `upload_images.py` — combine and ship

### `merge_manifests.py`

**Purpose.** Concatenates the Roboflow and purchased manifests into `manifests_v5_combined/{train,valid,test}.manifest`, the set v5 actually trained from.

**Algorithm.** For each output split, read each source manifest, drop blank lines, `json.loads` every line as a validation gate (a malformed line raises and aborts the merge), concatenate, and write. Then a per-class census of the merged train manifest is printed from each entry's `class-map`.

**Configuration.** The `MERGES` list pairs each output name with its ordered source list. Note the split-name mapping: Roboflow `valid.manifest` merges with purchased `val.manifest`. The frozen v3 multi-class merge config is kept in comments as the historical record.

**Gotcha.** The anchor images are intentionally NOT merged here — they were appended directly to the Rekognition dataset by hand. So the combined manifest counts do not equal the trained dataset's `LabeledEntries`. Do not "fix" the discrepancy.

### `upload_images.py`

**Purpose.** Uploads exactly the images the merged manifests reference — nothing else. There is no bucket constant: the destination bucket is parsed from each manifest line's `source-ref`, so uploads follow whatever bucket the converters wrote (the retired dev bucket in the checked-in v5 manifests; `argus-frames-506868652945` once manifests are regenerated for production). Idempotent: safe to re-run after a partial failure.

**Functions and algorithm.**
- `parse_s3_uri(uri)` — splits `s3://bucket/key` into bucket and key.
- `s3_object_exists(bucket, key)` — `head_object`, treating 404/NoSuchKey/NotFound as absent, re-raising anything else.
- `upload_from_manifest(manifest_path)` — per manifest line: take `source-ref`, split off the filename, map the S3 directory to a local directory via the `S3_TO_LOCAL` dict, then skip-if-missing-locally, skip-if-already-on-S3, else `upload_file`. Counters: uploaded / skipped_exists / skipped_missing / failed.

**Configuration.** `S3_TO_LOCAL` maps the six S3 prefixes (`training-data/armyworm/...` and `training-data/other-caterpillar/...`) to the Roboflow folder inside the repo and the vendor-licensed offline set's folders outside it. An unmapped S3 directory logs a warning and counts as failed — that is the signal the map is stale after a layout change.

**Gotcha.** The HEAD check makes re-runs cheap, but it also means a corrupted earlier upload is never repaired automatically; delete the S3 object first to force a re-upload.

## 3.10 `rank_purchased.py` — quality-rank the purchased set

**Purpose.** Scores every image in the purchased set on objective proxies for the exact defects the 2026-07-17 audit found (blur, featureless boxes, few-pixel specks, sky shots) and writes `purchased_ranked.csv`, ranked best-first. Downstream builds take the top N instead of sampling randomly (Runzhe's ruling, 2026-07-17).

**Design point:** all scores are computed on the **labelled box region**, not the whole frame — what matters is whether the WORM is usable.

**Functions and algorithm.**
- `laplacian_var(gray)` — variance of the Laplacian over the box crop, the standard blur proxy, implemented with a numpy sliding window (no scipy dependency).
- `sky_fraction(rgb)` — fraction of pixels that look like sky (blue-dominant bright, or blown-out white), computed on the upper half of the frame only.
- `parse_boxes(lbl_path)` — same YOLO detection/segmentation parsing as the converters.
- Per image, the **best** box (highest sharpness) supplies `box_sharp`, `box_contrast` (std-dev inside the box), and `box_frac` (box area / image area).
- Combination: percentile-rank sharpness and contrast across the whole set; normalise `box_frac` against a saturation point of 8% of frame (bigger boxes score higher up to 8%; beyond that there is no extra reward); turn `sky_frac` into a penalty saturating at 25%. Final `score = 0.40*sharp_rank + 0.25*contrast_rank + 0.25*frac_norm − 0.30*sky_penalty`.

**Edge behaviour.** Only label rows of class `0` count (`CLASS_ID = "0"`). A box whose pixel crop is under 4 px on either side is ignored; if every box in an image is ignored, or PIL cannot open the image, the image is skipped with a log line and never ranked. `laplacian_var` returns 0.0 for crops smaller than 3x3 instead of raising. `sky_fraction` is evaluated on the upper half only, so a bright floor in the lower half cannot trigger the sky penalty.

**Data out.** `purchased_ranked.csv` with rank, score, split, stem, per-axis values, and the absolute image path, plus printed distribution stats (sky-heavy count, speck count, soft count).

## 3.11 The build scripts — leak-proof dataset assembly (v7.2 → v7.4 → v8 → v9)

Four build scripts assembled the v7.2 → v9 datasets. They share DNA: MD5 leak guards against the holdout, the unique-source-stem split rule, deterministic hash-ordered splits, and clamped pixel-annotation manifest emission. All four are local-only (nothing uploaded, nothing trained) and, as checked in, write `source-ref`s against the retired dev bucket `frames-armyworm-366356442579` — retarget `S3_PREFIX` to `argus-frames-506868652945` before any production rebuild (3.17).

### `build_v7_2.py` — the leak-proof template (v7.2)

**Purpose.** Assembles the v7.2 dataset: the cleaned v7.1 corn + moth supply plus the top-500 ranked black images, single class `armyworm-larva`, with a strict leak-proof 80:20 train/test split. Local only — it uploads and trains nothing. It is also the template for any future build: the holdout guard and the stem rule below must survive into every successor.

**Configuration.** `V71` = `datasets/v7_1_worm` (source images + `provenance.csv`); `RANKED` = `purchased_ranked.csv`; `HOLDOUT` = `datasets/holdout/cag`; `OUT` = `datasets/v7_2_worm`; `MANI` = `manifests_v7_2`; `S3_PREFIX` = `s3://frames-armyworm-366356442579/training-data/v7_2/armyworm`; `N_BLACK = 500`; `TEST_FRAC = 0.20`; `CLASS_NAME = "armyworm-larva"`.

**Algorithm, in order.**
1. **Gather candidates.** Corn and moth images come from `v7_1_worm/provenance.csv` (source, filename, split already known; labels already YOLO class 0). Black images are the top-500 rows of the ranked CSV, output filenames prefixed `black_` to avoid collisions.
2. **Hash the holdout.** MD5 every image under `holdout/cag/`.
3. **Filter and group.** Per candidate: drop if the image file is missing; drop boxes that are full-frame junk (`keep_boxes` removes any box with both w ≥ 0.98 and h ≥ 0.98 normalized) and drop the image if no box survives; **drop if its MD5 matches a holdout image** (the hard leak guard); drop MD5 duplicates within the build. Survivors group by `(source, stem)`.
4. **The stem rule — Roboflow-augmentation leak prevention.** `stem_of()` strips Roboflow's augmentation suffix `_jpg.rf.<hex-hash>` from corn/moth filenames, so every augmented copy of one photograph shares a single stem. The split is then decided **per unique source stem, not per file**: a rotated or recoloured twin can never land on the other side of the train/test line. This is the difference between a real 80:20 split and a self-graded one.
5. **Stratified deterministic split.** Within each source, stems are sorted by the MD5 of `source|stem` (a stable pseudo-random order, no RNG state), the first 20% become test, the rest train. Stratifying per source keeps the test set's source mix equal to the train set's.
6. **Materialise.** Copy each image into `v7_2_worm/{split}/images/`, convert YOLO boxes to pixel annotations (clamped to [0,1] before scaling, boxes under 1 px discarded), write YOLO labels alongside, append the manifest entry, and record a provenance row.
7. **Report.** Writes `ASSEMBLY_REPORT.md` (drop counts, per-split per-source image/box table, achieved test fraction) and `provenance.csv`. Holdout collisions are reported explicitly; the report proves the count.

**Gotchas.** The black images' labels are found by string-replacing the `images` path segment with `labels` in the ranked CSV's path — this only works while the vendor-licensed offline set keeps the YOLOv8 layout. The `creation-date` in the manifest metadata is hardcoded; cosmetic only.

### `build_v7_4.py` — the fair composition (the v8/v9 base)

**Purpose.** Assembles v7.4, the consolidated "fair" composition every later version trains from: the cleaned v7.1 corn + moth-zldog base, plus the pHash-deduped Roboflow maize-FAW larva images (real localized boxes only), plus CAG batch_1 — so v7.4 holds the same CAG-domain data v5 had and the two can be compared fairly. Single class `armyworm-larva`, strict 80:20 split by unique source stem, `TEST_FRAC = 0.20`. Local only.

**Algorithm, where it differs from `build_v7_2.py`.**
- **Three candidate sources.** Corn + moth come from `v7_1_worm/provenance.csv` as before. The new larva come from `v7_4_new_larva.txt`, a tab-separated list emitted by `dedup_roboflow_larva.py` (pHash-deduped against corn/moth and the CAG holdout); only their class-2 boxes are kept (`read_yolo(lbl, keep_class=2)`) and filenames are prefixed `rf_`. batch_1 comes from the frozen v5 manifest `manifests_v5_combined/cag_batch1_from_v4.manifest`: its pixel boxes are converted back to normalized YOLO (centre = corner + size/2, divided by image size), filenames are prefixed `cag_`, and `bud_002` is excluded outright as the inked contaminant.
- **Full-frame junk drop inside the parser.** `read_yolo` drops any box with w ≥ 0.98 AND h ≥ 0.98 normalized — this is what reduces the full-frame-labelled "fall-armyworm" Roboflow set to its 3 real-box images.
- **The leak gate is an ABORT, not a drop.** `build_v7_2.py` silently drops holdout collisions and reports the count. v7.4 hashes only `batch_2/` (batch_1 is deliberately IN this build) and refuses to continue on any hit:

```python
if dropped["batch2_hit"]:
    raise SystemExit(f"ABORT: {dropped['batch2_hit']} training images collide "
                     f"with the sacred batch_2 holdout")
```

A collision means the candidate list itself is contaminated; a silent drop would hide that. Expected output is the report line "Sacred batch_2 collisions: 0 (asserted)."
- **Name-collision rename.** Files from different sources can share a filename; a second use of a name becomes `{stem}_{md5[:8]}_{name}` instead of overwriting.
- Everything else follows the template: the `_jpg.rf.<hex>` stem rule for corn/moth, the per-source deterministic MD5-ordered 80:20 split, MD5 dedup within the build, clamped pixel-annotation emission (sub-1-px boxes discarded; an image whose every box collapses is deleted). Outputs: `datasets/v7_4_worm/{train,test}`, `manifests_v7_4/`, `ASSEMBLY_REPORT.md`, and `provenance.csv` — which both v8 and v9 consume.

### `augment_build_v8.py` — the 13x augmentation build (v8)

**Purpose.** Turns v7.4's TRAIN split into the v8 training set by augmenting every image ~13x (2251 → ~29k). Only TRAIN is augmented; TEST reuses v7.4's un-augmented `test.manifest` unchanged (augmenting the test set would inflate the reported F1). batch_2 is never read. This script's transform primitives are also the engine of the v9 build below — and the concrete meaning of "added data augmentation" wherever that phrase appears in this project: flips, rotations, exposure jitter.

**The exact geometric transforms (the `GEOMS` table).** Eight orientation ops. Each pairs a PIL image transpose with a closed-form transform of the normalized YOLO box `(cx, cy, w, h)` — image op and box op MUST agree, so both live in one table row. Two examples show the style:

```python
def t_hflip(b):     cx, cy, w, h = b; return (1 - cx, cy, w, h)
def t_rot90(b):     cx, cy, w, h = b; return (1 - cy, cx, h, w)   # 90deg CW
```

The eight are identity, hflip, vflip, rot90, rot180, rot270, transpose, transverse — the last two composed as `rot90(hflip)` / `rot270(hflip)`. These are exact: no resampling error ever reaches the labels.

**`photometric(im)`** — random brightness (0.72–1.28), contrast (0.75–1.30), colour (0.80–1.25) jitter. The box is untouched.

**`rotate_arbitrary(im, boxes, deg)`** — rotates on an expanded canvas, then recomputes each box as the axis-aligned bounds of its four rotated corners (rotate each corner about the old image centre, re-origin on the new centre, take min/max, clamp to the canvas). A box is dropped if its new centre leaves the frame or either side falls below 0.002 normalized. The axis-aligned re-fit makes rotated boxes slightly loose — accepted, since the labels were loose to begin with.

**`variants_for(im, boxes)`** — the per-image plan, greedy: all 8 exact orientations first; then photometric jitter on randomly chosen orientations until 11 variants exist; then two arbitrary rotations at random +/-{15, 20, 25} degrees (also photometric-jittered); truncate to `VARIANTS_PER_IMAGE = 13`. A variant whose boxes all vanish is skipped.

**Safety rails.** `selftest()` runs before EVERY build, not only under `--selftest`: it asserts the transform algebra (hflip is an involution, rot90 twice equals rot180, rot90 then rot270 is identity, a known corner case maps exactly, an arbitrary rotation keeps a centred box centred). A silent box-math bug would corrupt ~30k labels, so the asserts come first. `cap_size` resizes to a 1280 px long edge (LANCZOS) — avoids Rekognition's `ERROR_INVALID_IMAGE_DIMENSION` rejection of >4096 px images and keeps the upload sane. `clamp_boxes` clamps centres into [0,1] and drops sub-0.001 slivers. `manifest_entry` converts to clamped pixel corners and returns None (variant skipped) if no box survives. `random.seed(1105)` makes the whole run deterministic; JPEG quality 88. Unreadable images and zero-box label files are skipped with a log line.

**Data in/out.** In: `datasets/v7_4_worm` train split, driven by its `provenance.csv`. Out: `datasets/v8_worm/train/{images,labels}` (each variant named `{stem}__{suffix}.jpg`) + `manifests_v8/train.manifest`.

### `build_v9_91.py` — the 90:10 re-split, both sides augmented (v9)

**Purpose.** v8's train:test ratio came out 29,263 : 567 (98:2). Runzhe's 2026-07-26 call: make it a conventional 9:1 with the TEST side augmented too. v9 therefore pools ALL v7.4 source images (2,818), re-splits 90:10 by unique source stem (`TEST_FRAC = 0.10`), and augments BOTH splits 13x — train ~32,981 / test ~3,653, the counts in the 3.5 ladder.

**How it works.**
- **Reuses v8's proven primitives instead of copying them:** it compiles `augment_build_v8.py` up to (but excluding) `def main()` into a module object, so `selftest`, `GEOMS`, `variants_for`, `cap_size`, and `read_yolo` are shared, not forked. `selftest()` still runs before anything is written.
- **The MD5 split.** Per source, unique stems are ordered by the MD5 of `"{source}|{stem}"` and the first 10% become test:

```python
ordered = sorted(stems, key=lambda s: hashlib.md5((src + "|" + s).encode()).hexdigest())
n_test = round(len(ordered) * TEST_FRAC)
test_stems = set(ordered[:n_test])
```

MD5 of a stable string is a deterministic pseudo-random shuffle: no RNG state, the same split on every run, stratified per source — and because the unit is the SOURCE STEM, all 13 augmented variants of one photograph land on the same side of the line.
- **The closing leakage assertion.** After writing everything, the script re-checks that no `(source, stem)` pair was actually written to both splits and exits with `SystemExit` if any was — a belt-and-braces check on the assignment logic itself, printed as "stem overlap train/test = 0 (asserted, no leakage)".
- Same per-split manifest emission as v8 (`job-name` `v9_aug_91`), same 1280 px cap, same seed 1105.

**Honest caveat, from the script's own docstring:** an augmented test set measures "handles rotated/flipped variants of held-out photos", which reads slightly higher than a raw held-out set would. The real verdict for this project has always been the CAG holdout through the live pipeline, not this in-domain F1 — one more reason 3.5's health warning treats v9's 0.591 / v9r's 0.599 as front-end figures.

## 3.12 The evaluate scripts — whole-image holdout arbitration

`evaluate_v7_1_vs_v5.py` and `evaluate_v7_2_vs_v5.py` are the same harness with different `MODELS` dicts. They are the reference implementation of the arbitration protocol; any future A-vs-B comparison should copy this file and change only the ARNs — and, on the production account, set `PROFILE = "prod"` (the checked-in scripts ran against the retired dev account).

**Purpose.** Run TWO models over the full CAG holdout (batch_1 + batch_2) through the identical whole-image path so the numbers are directly comparable, then print a per-image verdict table and image-level recall at 30/50/70%.

**Configuration.** `PROFILE = "nbk2"`, `REGION = "us-east-1"`. `MODELS` maps a short name to its **project ARN and version ARN** — the compared models live in different projects, so both ARNs are required per model. `THRESHOLDS = (30, 50, 70)`, `HEADLINE = 50.0`, `CLASS = "armyworm-larva"`, `START_TIMEOUT_MIN = 25`.

**Functions and the endpoint discipline.**
- `status(name)` — `describe_project_versions` filtered to the version name (parsed off the ARN), returns its Status.
- `start_both()` — idempotent start: skips a model already RUNNING/STARTING, calls `start_project_version(MinInferenceUnits=1)` on STOPPED/TRAINING_COMPLETED, raises on anything else. Then polls every 20 s until both are RUNNING or the timeout hits.
- `stop_both()` — requests stop on both, printing "VERIFY MANUALLY IN CONSOLE" on any error.
- `detect(name, path)` — one `detect_custom_labels` call on the raw image bytes with **`MinConfidence=0`**, filtered to the target class. Whole image, no tiling, no upscale.
- `main()` — globs `batch_1/*.jpg` + `batch_2/*.jpg`, runs every image through both models **inside a `try/finally` whose `finally` is `stop_both()`** — the models are stopped even if the run crashes mid-way. Saves the full raw label lists plus metadata to `holdout/cag/<pair>_<timestamp>.json`, then prints: per image, each model's max confidence and a verdict (both / A only / B only / both miss at the 50% headline); then image-level recall at each threshold.

**Cost warning, baked into the script and repeated here:** both endpoints bill roughly $4/hr each while RUNNING. If the script dies in a way that skips the finally block (power loss, kill), verify both models are STOPPED in the Rekognition console before walking away.

**Protocol rules the harness encodes.** Capture at MinConfidence=0 and threshold locally, so one run yields every operating point. Judge image-level recall on the max-confidence box (at the time these scripts ran, no CAG ground-truth boxes existed, so box-level precision was not scored; the 2026-08-11 answer key below changed that). Never mix these numbers with tiled-pipeline numbers. The live model stays live regardless of the outcome; the script only reads. The archived original of this pattern is `datasets/archive/experiments/one_off_scripts/evaluate_batch2_v5_vs_v4.py` (it points at pre-2026-07-20 paths; read it, do not run it).

### `build_answer_key.py` — the adjudicated ground truth behind the 2026-08-11 controlled evaluation

**Purpose.** Renders the canonical adjudicated ground truth for the re-curated 22-image evaluation set (3.6), the confirmation sheets Runzhe adjudicated from, and the machine-readable key `datasets/current/answer_key/answer_key.json`. Every controlled-evaluation number from 2026-08-11 onward is scored against this key. Final state: **22 images / 33 confirmed worms / 0 open proposals** — Runzhe ruled every proposed box confirmed except 103's leaf-axil candidate, which was deleted as a false positive.

**Where the boxes come from — three provenance classes, kept distinct in the code.**
- `confirmed(key)` — Runzhe's 2026-07-28 hand labels, read from `datasets/current/cag_ground_truth.json`.
- The `ADJ` dict — Runzhe's 2026-08-11 per-box adjudications for the four `Jewel_armyworm_*` images, with normalized L/T/W/H coordinates recovered from the v9r49 DynamoDB records.
- `proposed(*boxes, note=...)` — boxes proposed by visual inspection on 2026-08-11 for the NEW batch_2 photos (102–105) and the cleaned bud_002; each carries a descriptive note plus the ruling stamp. The old ground truth for the new 102/103/104/105 files is retired (3.6).

**Functions.**
- `render_overlay(name, path, boxes)` — resizes to an 1800 px long edge, draws each box (green = confirmed, orange = proposed) with a `worm k` / `worm k?` tag, adds a black banner stating the counts, and writes `overlay_<name>.jpg`.
- `contact_sheet(images, cols, tile_w, path)` — fixed-width tiles with per-row heights on a dark background; produces `SHEET_A_batch1.jpg` (4 columns) and `SHEET_B_batch2_jewel.jpg` (3 columns).
- `zoom_crops()` — for every box, crops a square 4.5x the box's larger side (minimum 240 px), resizes to a 460 px tile, re-draws the box in crop coordinates, labels it, and grids all crops into `SHEET_C_zoom_crops.jpg`. The zoom sheet is what made per-box adjudication practical on 3024x4032 phone photos.

**Edge points.** Coordinates are normalized fractions throughout; only rendering multiplies by pixel size. `bud_002` deliberately uses the circle-removed file `holdout/cag/clean/cag_bud_002_clean.jpg`, not batch_1's inked original. The verdicts live in the script and in `cag_ground_truth.json`, so re-running redraws the artifacts without changing any ruling.

## 3.13 The manifest line format

A Rekognition Custom Labels object-detection dataset imports a **SageMaker Ground Truth manifest**: a JSONL file, one JSON object per line, one line per image. The exact shape our converters emit:

```json
{
  "source-ref": "s3://argus-frames-506868652945/training-data/armyworm/train/images/IMG_0042.jpg",
  "bounding-box": {
    "image_size": [{"width": 1920, "height": 1080, "depth": 3}],
    "annotations": [
      {"class_id": 0, "top": 412, "left": 806, "height": 63, "width": 118}
    ]
  },
  "bounding-box-metadata": {
    "objects": [{"confidence": 1.0}],
    "class-map": {"0": "armyworm-larva"},
    "type": "groundtruth/object-detection",
    "human-annotated": "yes",
    "creation-date": "2026-07-07T12:00:00",
    "job-name": "armyworm-labeling"
  }
}
```

Field rules:
- `source-ref` — the S3 URI of the image. The image must exist there before dataset creation (hence `upload_images.py` runs first).
- The label-attribute pair — here `bounding-box` and `bounding-box-metadata` — must share the same base name; Rekognition matches them by that convention.
- `image_size` — the real pixel dimensions, a single-element list. Boxes are interpreted against these numbers.
- `annotations` — one object per box: integer pixel `top`/`left`/`height`/`width`, and `class_id` indexing into `class-map`. **At least one annotation is required per line** — an empty list makes the entry unlabeled, and Custom Labels excludes it from training (3.16).
- `objects` — one `{"confidence": 1.0}` per annotation, parallel arrays.
- `type` must be `groundtruth/object-detection`; `human-annotated` is `"yes"`; `creation-date` and `job-name` are free-form provenance.

One malformed line fails only that entry (it shows in the dataset's `ErrorEntries`), but a wrong-but-parseable line (bad `image_size`, swapped width/height) trains silently on garbage. Validate with `merge_manifests.py`'s parse gate and spot-check boxes visually before training.

## 3.14 Training procedure, end to end

This is the full recipe. On the NP production account — the operative target for every future training run — the values are: profile `prod`, region `us-east-1`, bucket `argus-frames-506868652945`, projects `argus-detection` and `argus-moth-detection`. (The retired development account used profile `nbk2` and bucket `frames-armyworm-366356442579`; those values survive only in the historical records and the checked-in dev-era scripts.) On a fresh account, substitute your own. `train_v7_2.py` automates steps 1–5 with boto3 (`get_or_create_project` — which calls `create_project` when the project is absent, covering step 1 — → `ensure_datasets` → `start_training`, then `--watch` runs `watch()`, which polls every 60 s and writes status, F1, and billable seconds into `v7_2_train_state.json`). The manual steps below are what it does, for when you need them one at a time.

**0. Prepare data.** Run the pipeline in order: converter(s) → `merge_manifests.py` (or a `build_*.py` that emits manifests directly) → `upload_images.py`. Then upload the manifests themselves to the bucket, e.g. under `training-data/v7_2/armyworm/manifests/`. Rekognition must be able to read the bucket; in the same account this is normally automatic, and the console offers to attach a bucket policy if not.

**1. Create the project.**
- Console: Amazon Rekognition → Use Custom Labels → Projects → Create project → name it → Create.
- CLI: `aws rekognition create-project --project-name` **your-project-name** `--profile` **your-profile** `--region us-east-1`
Record the returned project ARN.

**2. Create the TRAIN dataset from the manifest.**
- Console: open the project → Create dataset → "Start with an existing dataset" → "Import images labeled by SageMaker Ground Truth" → point at the manifest's S3 location → choose training dataset → Create.
- CLI: `aws rekognition create-dataset --project-arn` **project-arn** `--dataset-type TRAIN --dataset-source file://`**ds-train.json** `--profile` **your-profile** `--region us-east-1`, where **ds-train.json** contains `{"GroundTruthManifest":{"S3Object":{"Bucket":"`**your-bucket**`","Name":"`**path/to/train.manifest**`"}}}`. (Inlining that JSON in PowerShell is fragile — always use the file form.)

**3. Create the TEST dataset** the same way with `--dataset-type TEST` and the test manifest. Wait for both to reach `CREATE_COMPLETE`:
- Console: the project's Datasets pages show status and image counts.
- CLI: `aws rekognition describe-dataset --dataset-arn` **dataset-arn** `--profile` **your-profile** `--region us-east-1`
Check `DatasetStats.LabeledEntries` against your manifest line count and that `ErrorEntries` is 0. A shortfall means unlabeled or malformed entries.

**4. Train.**
- Console: project page → Train model.
- CLI: `aws rekognition create-project-version --project-arn` **project-arn** `--version-name` **v-name** `--output-config '{"S3Bucket":"`**your-bucket**`","S3KeyPrefix":"`**training-output/prefix**`"}' --profile` **your-profile** `--region us-east-1`
Training here has taken roughly 1–4 hours (v6: 13,828 billable seconds).

**5. Watch and read the result.**
- Console: the project page shows status; the model's Evaluate tab shows F1, precision, recall once done.
- CLI: `aws rekognition describe-project-versions --project-arn` **project-arn** `--profile` **your-profile** `--region us-east-1`
Look for `Status: TRAINING_COMPLETED`, `EvaluationResult.F1Score`, and `BillableTrainingTimeInSeconds`.

**6. Host it to test.**
- Console: model page → Use model tab → Start.
- CLI: `aws rekognition start-project-version --project-version-arn` **version-arn** `--min-inference-units 1 --profile` **your-profile** `--region us-east-1`
Wait for RUNNING (10–20 min). About $4/hr from now until stopped.

**7. Judge on the CAG holdout** with an evaluate-script copy (3.12), never only on the model's own test F1. The ladder in 3.5 is the proof of why: three versions raised or held in-domain F1 and still failed the holdout.

**8. STOP the endpoint.**
- Console: Use model tab → Stop.
- CLI: `aws rekognition stop-project-version --project-version-arn` **version-arn** `--profile` **your-profile** `--region us-east-1`
Then re-run `describe-project-versions` and confirm `STOPPED`. Do this even after a crashed run.

**9. Wire it (only if it won)** — next section.

## 3.15 Model wiring and rollback

The live model ARN lives in exactly ONE runtime place per account: the `custom_model_arn` attribute on the camera's row in the DynamoDB `pest-monitoring-cameras` table (`worm_cam` for armyworm, `moth_cam` for the moth model). All three Lambdas resolve it from there at call time. Nothing hardcodes a model ARN. Current values: on the development account, `worm_cam` points at the v9 retrain `v9-20260805-0713` (since 2026-08-07). On the production account the camera rows are `worm_cam`, `manual_upload`, and `moth_cam` (the deployer seeds the template row as `camera-1`; it was re-keyed to `worm_cam` in the 2026-08-11 reconciliation); `moth_cam.custom_model_arn` already carries `moth-prod-20260811`, and the armyworm ARN writeback (`v9r-prod-20260810`) targets `worm_cam` and `manual_upload` (3.17).

Consequences:
- **Deploying a new model = one attribute write.** No code deploy, no Lambda change.
- **Rollback = the same write with the old ARN.** The v6 rollback on 2026-07-15 was exactly this.

How to write it:
- Console: DynamoDB → Tables → `pest-monitoring-cameras` → Explore table items → open **worm_cam** → edit `custom_model_arn` → paste the version ARN → Save.
- CLI/script: the ARN colon rule (3.3) applies. From PowerShell, do NOT inline the ARN into `aws dynamodb update-item`. Either use boto3:

```python
import boto3
t = boto3.Session(profile_name="nbk2").resource("dynamodb", region_name="us-east-1") \
        .Table("pest-monitoring-cameras")
t.update_item(
    Key={"camera_id": "worm_cam"},
    UpdateExpression="SET custom_model_arn = :a",
    ExpressionAttributeValues={":a": "<the-version-arn>"},
)
```

or pass the values as `file://` JSON documents: `aws dynamodb update-item --table-name pest-monitoring-cameras --key file://`**key.json** `--update-expression "SET custom_model_arn = :a" --expression-attribute-values file://`**vals.json** `--profile` **your-profile** `--region us-east-1`.

Remember the endpoint half: the ARN you point at must be a RUNNING version, and the version you just replaced should be STOPPED once traffic is confirmed on the new one, or you pay for two armyworm endpoints.

## 3.16 Inference-side algorithms — where they live and why they exist

The algorithms below run in the `pest-detection-processor` Lambda and are documented operationally in **Chapter 2**. This section records only the model-side facts that explain WHY each stage exists.

**Tiling (small-target recall — back ON in production).** Small worms have too few pixels for reliable detection; 4x crop+upscale was found to be the recall sweet spot (1x about 33% recall, 4x peak, 6x no better). The processor's tiled mode (4x4 grid + 15% overlap + full-frame pass = 17 calls, NMS merge) automated that finding. But tiling also amplifies the false-positive prior — 17 chances to fire on foliage at the close-up scale the model was trained on — and the W15 ruling set `worm_cam.tiling_enabled = false`. That ruling was reversed on Runzhe's own call with new evidence: the 2026-07-23 deploy (`docs/state.md`) set `worm_cam.tiling_enabled = true` with `LLM_VERIFY_ALL_BOXES = true` — Rekognition finds at high recall, the LLM verifies every box. The deployed config (verified live through 2026-08-10) records `TILE_MIN_CONFIDENCE = 8`, which is what feeds the denoiser its volume. Never compare tiled-path numbers with whole-image numbers.

**Application-layer FP suppression (v4.3) — because Custom Labels cannot train on negatives.** This is a platform limit, proven three ways on 2026-07-13: (a) empirically — 410 clean zero-box entries appended to the TRAIN dataset registered `cl-metadata.is_labeled:false`, `LabeledEntries` stayed at 1160, and an explicit `is_labeled:true` in the manifest was ignored on import; (b) the AWS OD manifest spec makes `annotations` mandatory per object, with no negative form; (c) AWS re:Post confirms true negatives must be removed. So the model's learned prior is "plant close-up means armyworm" (its training set holds zero true negatives, see 3.7) and no retrain on this platform can teach it "not a worm". At MinConfidence=0 both v4 and v5 spray 120–300 raw boxes per image, and some surviving FPs are 90%+ confident. Suppression therefore happens OUTSIDE the model: a DetectLabels pass drops worm boxes sitting on hard objects (person / vehicle / furniture / machinery). This killed the jeep-tire FP on cag_armyworm_103; it cannot touch plant-on-plant FPs. The dead-end artifacts are kept for the record in `datasets/archive/experiments/v6_experiment/`: `build_negatives_v6.py`, `upload_negatives_v6.py`, `append_negatives_v6.py`, `census_negatives.py`, and 410 harmless unlabeled entries still sitting in the v5 TRAIN dataset (removing them would need a dataset recreate; Custom Labels ignores them).

**LLM crop verification (v4.5 hybrid gate, Bedrock).** Rationale: with negatives impossible, a second, different judge earns its keep on the marginal boxes. The order is fixed: **Rekognition finds, the LLM verifies — never the reverse.** The 2026-07-21 A/B (`datasets/verify_llm_crop.py`, 20 CAG images, pre-registered thresholds — a methodology A/B of the verifier, not a model benchmark; do not quote these fractions as detection recall) showed why: whole-image LLM screening recalled 6/14 clean positives vs 13/14 for per-box crop+upscale, because the verifier ran on Bedrock's standard 1568px resolution tier and a sub-0.5%-of-frame worm survives the downscale as a couple of visual patches. No model swap fixes that; it is an input-resolution limit. The v4.5 gate as first deployed (2026-07-22) used Claude Haiku 4.5 and trusted any box at or above the camera's `min_confidence` outright (no LLM call); only a sub-threshold box was cropped (0.6 pad), upscaled, and adjudicated — an explicit reject drops it, anything else keeps it, fail-open. **Deployed config has since evolved (verified live 2026-07-29 and 2026-08-10, `docs/aws.md`):** `LLM_VERIFY_MODEL_ID` is `us.anthropic.claude-sonnet-4-6` (not Haiku 4.5); `LLM_VERIFY_ALL_BOXES = true`, so the gate is now a denoiser that judges EVERY box — including boxes above `min_confidence`, which can now be dropped; `LLM_VERIFY_MAX_BOXES = 120` (the old value 5 is historical); Lambda timeout 600 s at 1024 MB. Since 2026-08-07 the gate is followed by post-gate cleanup: NMS/containment merge, an area cap (`POST_MAX_BOX_AREA = 0.05`), and the post-verify floor (`POST_VERIFY_FLOOR = 49` since 2026-08-10 — the threshold decision in 3.5), with the verify prompt tightened to the yellow-and-black-stripes wording. Processor v6.3 (2026-08-07) stripped the dead experimental paths (3266 → 1545 lines); the pre-strip source is archived at `lambda/archive/pest-detection-processor_v6.2_full.py`. Rekognition still only ever sees the whole, unscaled frame; the crop exists only as Bedrock's input. **Known limitation, carried into every claim: light-coloured worms are false-rejected.** Haiku rejected cag_armyworm_003's pale grey larva ("a moth or insect on orange flower, not a larva"). The verifier inherits the training set's oldest blind spot — the light-worm class that v7.1 targeted and missed — and does not cure it.

## 3.17 Operations / reproduction

**Routine operations checklist.**
- Before any model test: check endpoint states with `aws rekognition describe-project-versions --project-arn` **project-arn** `--profile prod --region us-east-1` (console: each model's Use model tab). Know what is RUNNING before you add to it.
- After any test: STOP what you started and confirm STOPPED. Two RUNNING endpoints (moth + one armyworm) is the normal ceiling.
- Every new model version is judged on the CAG holdout via a copy of the evaluate harness (3.12) before any wiring decision. Its own test F1 alone decides nothing.
- Never train on `holdout/cag/batch_2`. Keep the MD5 holdout check from `build_v7_2.py` in every future build script.
- Model switch and rollback are single DynamoDB writes (3.15). Mind the colon rule.
- After a training round, push the holdout through the live pipeline so results show on the dashboard, not only in a terminal table.

**Reproducing on a fresh AWS account — executed for real, 2026-08-10/11.** Models are account-bound; there is no shortcut. This section was first written as a hypothetical recipe; it has now been run for real against the NP production account **506868652945**, and the record of that run follows the generic recipe.

Generic recipe (any fresh account):
1. Create an S3 bucket for training data. Update **S3_BUCKET** in both converters, the bucket and **S3_TO_LOCAL** in `upload_images.py`, **BUCKET**/**S3_PREFIX** in the build and train scripts, and the `MODELS` ARNs in any evaluate copy.
2. Configure a CLI profile for the new account and substitute it everywhere.
3. Run: converters → merge (or build) → upload images → upload manifests → 3.14 steps 1–8.
4. Wire the winning ARN per 3.15 (the rest of the stack must exist first — Chapter 2 and later).
5. Expect the same numbers only if you train from the same manifests; the frozen sets are in `datasets/current/manifests_v5_*` and `manifests_v7_2/`.

**The actual production retraining (2026-08-10/11).** The ARGUS deployer (Chapter 7) had already stood up the stack on the production account, including the empty Rekognition project `argus-detection` and the bucket `argus-frames-506868652945`. Because the full training set already lived on S3 in the old account, the converters were not re-run; three migration scripts did the model side instead:

1. **`migration/copy_training_data.py`** — server-side copy of the entire armyworm training set (**36,641 objects / 4.27 GB** under `training-data/v9/`) from the old bucket to the new one; the bytes never touch the operator's machine. Method: merge a temporary, prefix-scoped read-grant statement into the OLD bucket's policy (existing statements preserved — the statement is merged in by Sid, not overwritten; `--revoke` removes it at sign-off), copy every key with `copy_object` across 32 workers, rewrite every manifest `source-ref` to point at the new bucket, and verify object counts. Result: 36,641/36,641 copied, 0 failed, counts matched, 36,634 manifest source-refs repointed.
2. **`migration/train_v9r_on_prod.py`** — creates the TRAIN/TEST datasets on the new account and submits training (version `v9r-prod-20260810`). Its method carries the migration's most important generic lesson: **after any labelling or label-editing in the Rekognition console, the S3 manifest is stale — the DATASET is the source of truth.** Console edits never write back to S3, so rebuilding a dataset from a copied manifest silently loses every console edit, and on a set this size that mistake costs a multi-hour training run before it is even visible. The correct method, scripted here: export the live entries from the old account's dataset with `ListDatasetEntries` (paginated, `HasErrors=False`), repoint each entry's `source-ref` at the new bucket, upload the result as NEW manifests (`training-data/v9/armyworm/manifests/{train,test}_v9r_prod.manifest`), and create the datasets from those. Both datasets reached CREATE_COMPLETE: TRAIN 32,986 labelled / 32,986 total, TEST 3,653 / 3,653. Training was submitted 2026-08-10 23:56 and completed 2026-08-12 10:04 SGT at **F1 0.613** — the old account's v9r scored 0.599 on the same recipe and data, so treat the two as equivalent, not as an improvement.
3. **`migration/poll_training.py`** — the watcher. Deliberately network-fault tolerant: the first poller died on a DNS error when the laptop dropped its connection, which looked like a training failure in the log but was not. This version retries transient errors indefinitely (logging at the 1st/10th/60th consecutive failure), and only a terminal training status (`TRAINING_COMPLETED`, `TRAINING_FAILED`, `FAILED`, `DELETING`) ends the loop; it then prints the F1 and the version ARN. Poll interval 180 s.

The moth rebuild (`migration/migrate_moth.py`, 3.4) is the same grant → copy → `ListDatasetEntries` → train pattern against the old account's console bucket, and completed same-day. Both version ARNs are now written into their camera rows per 3.15. The remaining proof step, in progress at this chapter's stamp, is the validation re-push: start the endpoint and push the holdout through the new pipeline — the migration is only proven when the same images give the same detections. Then revoke the cross-account read grants (`--revoke` on both migration scripts).

**Local supply caveats.** The vendor-licensed offline image set obtained from the project owner lives outside the repo — copy it onto any new machine before the purchased-set converter or ranker can run. One credential caution: `datasets/archive/experiments/pre_v3_abandoned/download.py` contains a live Roboflow API key inline (credential stored there, not reproduced here). Do not publish or copy that file anywhere public.

## 3.18 Cross-references

- **Chapter 2** — the `pest-detection-processor` Lambda: tiling implementation, v4.3 suppression config and env vars, the v4.5 LLM gate architecture, `llm_verify_enabled` / `tiling_enabled` camera flags, and the DynamoDB record shape the models' outputs land in.
- **Chapter 4 (dashboard frontend)** — the Test upload panel (the live-detection demo path) and how holdout pushes surface in the gallery.
- **Chapter 5 (edge: Go2 + Jetson Orin)** — the SIYI A8 Mini / Unitree Go2 / Jetson Orin capture chain that produces the frames these models consume.
- **Chapter 7 (deployer)** — the ARGUS deployer that created the production-account stack (Rekognition project, buckets, tables, Lambdas) the retraining in 3.17 targeted.
- **Chapter 8 (reproduction runbook)** — the account-wide reproduction sequence that 3.17's model-side steps slot into.
- Source docs behind this chapter: `docs/detection.md` (facts and learnings), `docs/aws.md` (ARNs, accounts, Bedrock), `docs/state.md` (the 2026-08 migration record), `datasets/README.md` (folder map), and the scripts under `migration/`.
