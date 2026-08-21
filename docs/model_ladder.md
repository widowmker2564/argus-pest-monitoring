# Model development ladder — raw material for the Final Report

> **Note on script paths.** Sections below describe work done earlier in
> the project. Some of the scripts they name were removed in the
> 2026-08-21 repository cleanup, which kept only production code and the
> current pipeline. The reasoning and the measurements stand; the paths
> are a record of how the work was done, not files you will find here.


_Living file. Purpose: capture the accuracy-improvement journey (process → method →
result) as it happens, so the Final Report's "development" narrative is assembled
from record, not reconstructed from memory. Runzhe wants this depth in the FINAL
report (a plus); weekly reports only need per-point achievement bullets, NOT this.
Update this file whenever a new model version or pipeline stage lands._

## Read this first — the honest framing (must survive into the report)
The F1 numbers below are NOT on one fixed benchmark: each version was scored on a
DIFFERENT test set (the test split grew and changed domain with the training set).
So this is a record of engineering iterations, not a clean monotonic climb on a
held-constant yardstick. The Batch 2 CAG holdout is the closest thing to a stable
deployment-like probe, and it tells the real story (see the caveat at the end):
**the model is strong IN its training domain; the open challenge is DOMAIN TRANSFER
to the Jewel Changi deployment scene, not raw model capacity.**

There are TWO parallel improvement axes. Keep them separate in the report.

---

## Axis A — model / training data

| # | Date | Acct | Method | Training data | F1 (own test set) | Outcome |
|---|------|------|--------|---------------|-------------------|---------|
| 0 | 2026-04-09 | old (396…) | first practice run | 25 images | 0.818 | **Garbage / overfit** — no generalisation. Discarded, never deployed. |
| 1 | 2026-04-28 | old | single-class `armyworm-larva` | 103+21 Roboflow split (`maize-fallarmyworm`) | 0.749 (P 0.833 / R 0.680) | **First real baseline.** End-to-end validated 5 May. Recall (0.68) flagged as the weak axis. |
| 2 | 2026-05-08 | old | **multi-class** (armyworm-larva + other-caterpillar) — "Route 2" | added paid caterpillar set | — | **FAILED.** Shortcut learning: the model discriminated by photo DOMAIN (source), not species. Reverted to single-class. Key lesson recorded. |
| 3 | v3 (old) | old | single-class + more data + CAG anchor | 248 imgs (108 Roboflow + 140 paid + 11 CAG batch-1 + 2 mislabelled "neg") | 0.719 (assumed_threshold 46.2) | Consolidated single-class. Bud false-positives surfaced as a pain point. |
| 4 | **v4** 2026-05-21 | **nbk2 (366…)** | single-class, retrained after account migration | 261 train / 65 test | 0.719 | **Deployed baseline for a long stretch.** Rollback target. Numbers ≈ v3 (migration, not a capability jump). |
| 5 | **v5** 2026-07-07 | nbk2 | **brute-force training-set scale-up** (stop waiting on CAG, use the full purchased set) | 1160 train (108 Roboflow + 1040 purchased + 12 CAG batch-1) / 133 test | **0.852** (v4 on same test: 0.719, +0.13) | **Current live model.** Big jump on the purchased-domain test set. |
| 6 | **v6** 2026-07-15 | nbk2 | **synthetic close-up augmentation** — reclaim the 152 never-used valid-split images + generate 200 worm-centred close-up crops (2.5-4x margin) from purchased positives | 1512 train (v5's 1160 + 352) / 133 test (UNCHANGED for comparability) | 0.839 (**DOWN** from v5) | **REJECTED + rolled back same day.** Tied v5 on the Batch 2 holdout (2/7 @60) and on purchased TEST (9/10 @60); WORSE on batch_1 (10/13 vs 12/13). |

**The v6 negative result is the most instructive rung — put it in the report.**
Hypothesis: feeding close-ups (the scale production cameras actually shoot at)
would lift generalisation. Result: it deepened confidence where the model was
already right (cag_armyworm_104: 75.9%→97.0%) but converted **zero** misses into
hits, and cost accuracy elsewhere. Conclusion: *the same worms at a tighter scale
carry no new domain information*. The CAG gap is a DOMAIN gap (Jewel's foliage,
lighting, optics), and only real CAG close-range photographs can close it —
synthetic augmentation cannot manufacture domain diversity it was never given.

**Interpreting v5's +0.13:** it was earned on the purchased-image domain. The Batch 2
CAG holdout arbitration (2026-07-13) showed v5 TIES v4 at the operational threshold
(both 3/7 @50%), i.e. the F1 gain did NOT transfer to real CAG field photos. Scaling
the purchased set bought purchased-domain accuracy, not CAG-domain accuracy.

## Axis B — inference pipeline (independent of the model weights)

| Stage | Week | Change | Why | Result |
|-------|------|--------|-----|--------|
| 1x whole-image | early | baseline single detect call | — | ~33% recall on small worms |
| **4x crop** | W6 | crop + upscale before detect | small targets have too few pixels | **recall peak at 4x** (1x→33%, 4x→peak, 6x→same/worse). The core recall lever. |
| **Tiling v4.2** | W12 | cloud-side 4×4 grid + 15% overlap + full-frame pass = 17 calls, per-tile ~4x upscale, NMS merge | apply the 4x finding automatically across a wide frame | recovers small/distant worms a single downscaled call misses |
| **Non-veg suppression v4.3** | W15 (2026-07-13) | after detection, DetectLabels pass drops worm boxes sitting on hard objects (person/vehicle/furniture/machinery) | CL OD cannot be trained on negatives; foliage-vs-object done at app layer | kills non-plant FPs (e.g. the jeep-tire box); does not touch plant-on-plant |

**Tiling has a two-edged result (proven 2026-07-13):** it lifts recall on small worms
BUT amplifies foliage/shadow false positives — a wide indoor scene that scores 7-13%
whole-image explodes to 8+ boxes >90% once each foliage patch is zoomed to the
close-up scale the model associates with worms. Precision-vs-recall fork, decide per
how production cameras are actually framed.

---

## Key experiments & lessons (the "what we tried and learned" for the report)
1. **Multi-class shortcut learning (v2, 2026-05-08):** two similar pest classes from
   different photo sources → model learned source, not species. → single-class only.
2. **4x crop sweet spot (W6):** the real recall bottleneck is model blind spots (FN
   patterns) + target pixel count, not scale beyond 4x.
3. **Domain shift destroys detection:** a printed-then-photographed image gave 0
   detections vs 96.6% on the digital original. Production = 4K close-range digital.
4. **Image-level negatives are a dead end on Rekognition CL OD (proven 2026-07-13):**
   built 410 clean no-worm images, appended to TRAIN; they registered `is_labeled:false`
   and are EXCLUDED from training (confirmed vs AWS docs + re:Post). CL OD requires a
   box on every training image. So FP suppression must live OUTSIDE the model.
5. **In-domain generalisation is strong; domain transfer is the real problem
   (proven 2026-07-13):** 10 purchased TEST images (never trained) detected 10/10, mostly
   99%+; same whole-image path on Batch 2 (Jewel wide indoor) = 2/7 real hits. The model
   trained WELL — Batch 2's poor result is the CAG images' quality/domain, not the model.

## The stable-probe caveat (the one number to trust)
Across versions, F1 was measured on shifting test sets. The Batch 2 CAG holdout (7
sacred images, never trained) is the closest to a fixed deployment-like probe, and it
says v4 ≈ v5 (3/7 @50%). Report the ladder as ENGINEERING PROGRESS (methods tried,
platform limits found, pipeline built), and be explicit that the headline F1 climb
(0.749 → 0.719 → 0.852) is not a single-benchmark climb and that CAG-domain transfer
remains the open frontier — pointing to close-range capture geometry and CAG-domain
positive collection as the next real levers.

## Where the numbers come from (traceability for the report)
- v4/v5 F1 + billable training time: `describe_project_versions` +
  `deployer/audit/rekognition__armyworm-detection__project-versions.json`.
- Batch 2 arbitration: `datasets/evaluate_batch2_v5_vs_v4.py`,
  raw `datasets/cag_holdout/batch2_arbitration_20260713_105204.json`.
- In-domain check: `datasets/push_batch1_purchased.py` run (camera `notile_test`).
- Early milestones (4/09, 4/28 baseline): see the earliest entries in `docs/state.md`.
- Detection lessons: `docs/detection.md`. Tiling: `lambda/pest-detection-processor.py`.
