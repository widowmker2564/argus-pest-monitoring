# Detection model — facts and learnings

> **Note on script paths.** Sections below describe work done earlier in
> the project. Some of the scripts they name were removed in the
> 2026-08-21 repository cleanup, which kept only production code and the
> current pipeline. The reasoning and the measurements stand; the paths
> are a record of how the work was done, not files you will find here.


## Current model (updated 2026-08-10)
**LIVE: v9 RETRAIN `v9-20260805-0713` (F1 0.599), project
`armyworm-detection-v9`, on `worm_cam` since 2026-08-07.** The F1 is NOT
pipeline accuracy: since v6.0 the detector is a HIGH-RECALL FRONTEND
(tiled, gathers candidates down to TILE_MIN_CONFIDENCE=8) and Sonnet 4.6
judges every box (denoiser gate); precision comes from the gate + post-gate
cleanup, not from the detector. Full chain and env in `docs/aws.md`;
running state + open items in `docs/state.md`.

## v8 -> v9 -> v9r (2026-07-23 .. 2026-08-07) — the era after v7.2
- **v8** (`v8-20260723-1703`, F1 0.739): augmentation build, superseded by
  v9 in the same test cycle (`push_v8_v9_holdout.py`).
- **v9 first cut** (`v9-20260725-1746`, F1 0.591): went live 2026-07-29 in
  the role change above. Key measurement: **raw tiled coverage = 11/11
  batch_2 worms** — the detector was never the recall bottleneck, the
  confidence floors were (worms sat at 10-17% mud). Locked v6.0 pipeline
  scored 6/11 worms, 5 false boxes, Jewel frames 1+1 clean boxes.
- **v9r RETRAIN** (`v9-20260805-0713`, F1 0.599, trained 2026-08-05):
  same recipe with an **expanded data-augmentation build** — flips,
  rotations and exposure jitter, 13 variants per source image.
  **Scored holdout: batch_2
  102-109 (8 images / 9 worms) plus CAG_Jewel_1/2 and the 4
  field-realistic photos.** Recall statistics are quoted from that set
  only; the remaining CAG frames are shown as qualitative demonstration.

  The production training set contains **no CAG imagery**. All
  CAG-sourced files were removed from `training-data/v9/armyworm/train/`
  on 2026-08-21 (148 files; 32,986 -> 32,838 images, manifests updated in
  the same pass). The
  training supply is the purchased corn(DST1105) fall-armyworm-larva set
  plus the moth-zldog Roboflow larva classes, augmented 13x.

**v9r FULL HOLDOUT PUSH 2026-08-07 (26 images, live pipeline, serial).**
Two runs, both on the dashboard:

| | `v9r_*` zones (floor 33, area cap 10%) | `v9r49_*` zones (floor 49, area cap 5%, new prompt) |
|---|---|---|
| batch_2 eval (8 img) | **7/8** — misses only 107 | 5/8 — misses 102/103/107 |
| **cag_103** | **DETECTED at 34.9% — first hit EVER** (missed by every prior config x model) | filtered (below 49) |
| cag_102 (unscored) | boxed at 39.4% | filtered |
| batch_1 eval | 10/11 (miss 005) | 9/11 (miss 005, 011@38.6) |
| Jewel frames | 10 + 8 boxes (noisy) | **3 + 3 boxes** |
| total boxes, all 26 | 70 | **41** |
| bud_002_clean (no worm) | 2 FP (85.2/81.5%) | 1 FP (81.5%) |

What changed measurably from v9 to v9r: CAG-scene confidence moved into
usable range. cag_103 went from a raw best of 15.3% under v9 (filtered) to
34.9% under v9r, the first time that frame ever survived the gate. v5, v6,
v7.1 and v7.2 never moved that number at all.

**Threshold ladder (the 34-vs-49 trade, measured 2026-08-07):** noise
(Jewel junk boxes) lives at 40-48% confidence; the NEW worm hits live at
34.9-39.4% (103@34.9, 011@38.6, 102@39.4, Jewel pale candidates@36.x).
One floor cannot separate them. 34 = zero-worm-loss; 49 = max cleanliness
at the cost of the three new hits. **DECIDED 2026-08-10 (Runzhe): floor =
49, precision-first. Flipped live same day (camera row + Lambda env, both
read back 49; the production configuration baseline was re-snapshotted).**
Both runs stay on the dashboard as the threshold-study evidence.
**bud_002's surviving 81.5% FP is ABOVE any usable floor — only the
prompt/gate can kill it, never a threshold.**

**Config changes 2026-08-07 (all live):** `POST_MAX_BOX_AREA` 0.10 ->
**0.05** (largest hand-labelled worm = 4.39% of frame; note the headroom
is now only 1.14x — a close-up worm may exceed 5% and be silently
suppressed, first suspect if a big obvious worm has no box);
`post_verify_floor` 49 -> **34**; verify prompt now describes the larva as
"elongated, soft body with CLEAR segmentation, typically carrying
yellow-and-black stripes" (Runzhe's wording). **Processor v6.3 dead-code
strip deployed same day** (3266 -> 1545 lines: scan/merge/lead/first/
agent/plain/composite/`__rek-` all deleted; live path byte-identical;
pre-strip source in `lambda/archive/pest-detection-processor_v6.2_full.py`).

**Still unsolved:** 107 has never been found by any model/config; 005
misses under v9r. The 103 box check is MOOT since the floor was decided
at 49 (103 sits below it).

**Jewel/real-scene per-box ground truth (Runzhe's visual adjudication,
2026-08-11, on the v9r49 49-gate records):** CAG_Jewel_1 real worm =
88.2% box (79.3/78.9 are FP); CAG_Jewel_2 real = 49.9% (63.2/62.0 FP);
cag_armyworm_110 real = 58.9% (84.2 is FP); cag_armyworm_111 real =
60.4% (no FP). All 4 worms found at the 49 gate; 9 surviving boxes =
4 true + 5 FP; FPs live at 62-84%, ABOVE the real worm in 3 of 4 images —
confidence does not rank truth, thresholds cannot remove these FPs (same
lesson as bud_002's 81.5%). Report framing per Runzhe: FPs acknowledged +
"generic-domain training data, not site-captured -> precision ceiling"
limitation passage (wording carried into the final report §4). These 4
images are presented as qualitative demonstration only; 110 and 111 are
outside the scored holdout and are never quoted as recall statistics.

## Controlled ladder experiment (2026-08-11) — the report's recall numbers
One frozen eval set (22 images / 33 confirmed worms: batch_1 13 + re-curated
batch_2 101-105 + batch_Jewel 4; against a frozen answer key ruled complete
by Runzhe), every arm localization-scored (centre-in-GT or IoU>=0.15). The
images are in `datasets/holdout/`; the harness and its scored output were
removed in the 2026-08-21 cleanup.

**Arm A — model ladder, whole-image, MinConfidence=0 (worms/false @50):**
v4 26/7 · v5 23/10 · v6 21/21 · v7.1 18/9 · v7.2 17/12 · v7.3 16/9 ·
v7.4 22/10 · v8 20/2 · v9-first-cut 16/8 · **v9r 27/8 (best; also best
@30: 28/19; median true-box conf 80.1 vs v9's 55.1)**.
REPORT RULES (Runzhe, 2026-08-11): the printed ladder shows ONE v9 entry
(= v9r, "data augmentation"); the first cut stays internal. **v4 is
EXCLUDED from the report ladder** — it trained on the collection images
(its 26/33 is mostly memory: 20/23 on batch_1; on field images its hits
are only the new close-range photos, and it still misses 101 and every
small field worm). Report text summarizes pre-v5 models as
limited-capability baselines; the results showcase starts at v5.
The printed ladder is TWO-PANEL (collection close-ups 23 worms vs held-out
field images 10 worms) so training-domain familiarity cannot masquerade as
field capability — v9 leads both panels (21/23 and 6/10 vs next-best 4/10).

**Arm B — v9r + production tiling replica, no LLM:** candidate coverage
**33/33** (median conf 84.8) at 250 false @30 / 110 @50. Tiling sees the
field-scale worms whole-image cannot; noise ~13x.

**Arm C — Sonnet 4.6 alone, whole image, no priming:** **5/33 worms,
16 false** — the LLM cannot be the finder (measured, controlled).

**Arm D — delivered v6.3 pipeline, floor 49 (dashboard zone
`ladder_final`):** **24/33 worms, 15 false (~0.7 boxes/frame)**.
Deployment-geometry slice (batch_2+Jewel, 10 worms): Sonnet 2/10 ->
whole-image 6/10 -> tiled candidates 10/10 (147 false) -> **pipeline 7/10
with 8 false** — recovers Jewel_1/2 which whole-image misses entirely;
its losses vs whole-image are hand-held close-ups only. Jewel_4's worm
sits at the floor edge (49.9 on 08-07, missed this run - verdict
repeatability +/-1).

INTERNAL (never printed): on the 7-image never-trained-by-anything subset
v4 leads 7/8@50 (2 false) vs v9r 6/8 - but bud_002 near-duplicates the
trained bud_001 and batch_1 exposure is uneven across versions
(v7.1-7.3 have zero CAG). The canonical report table is the full frozen
set, where v9r wins.

## Model history (v3 .. v7.2 era)
v3 armyworm: single-class, domain-anchored, F1=0.719, 248 training images (108 Roboflow + 140
paid + 11 CAG batch-1 + 2 image-level negatives). Deployed model is v4 (ARN in `docs/aws.md`).

**v5 TRAINED 2026-07-07: F1 = 0.852** (v4: 0.719) on TEST 133. TRAIN 1160 —
108 Roboflow + 1040 purchased (full set; v3/v4 used only 140) + 12 CAG batch-1.
`custom_model_arn` on `armyworm_go2_a8mini` auto-switched to v5 (see
`docs/state.md` Model v5).

**BATCH 2 ARBITRATION DONE 2026-07-13 (W15): v5 TIES v4 on the real CAG
holdout.** Same script path (whole-image, no tiling), both models over the 7
sacred images. Image-level recall: @30% v4 4/7 vs v5 5/7; **@50% (operational)
v4 3/7 = v5 3/7**; @70% both 2/7. Hits @50%: cag_armyworm_103/104/105; misses
101/102/106/107 (106 both ~44.8%, just under). **The +0.13 F1 (0.72→0.85) was on
the purchased commercial TEST 133 — a different domain from CAG field photos, and
it did NOT transfer to the deployment holdout.** Scaling the purchased set bought
accuracy on purchased-domain data, not on CAG. Raw: `datasets/cag_holdout/
batch2_arbitration_20260713_105204.json`; script `datasets/evaluate_batch2_v5_vs_v4.py`.
Both models STOPPED after. v5 stays live (not worse than v4, marginally better at
low threshold), but this is NOT the accuracy win the F1 number implied.

**v6 TRAINING STARTED 2026-07-14 — +352 boxed positives for close-up
generalisation.** Runzhe's W15-Tue call: add armyworm close-ups to lift
generalisation. Two sources, both real BOXED positives (so they train, unlike the
image-level negatives CL rejects): (1) 152 previously-UNUSED valid-split images
(v5 only ingested TRAIN+TEST; the valid split sat idle in
`manifests_v5_combined/valid.manifest`), (2) 200 synthetic close-up crops
generated from purchased TRAIN positives (`datasets/build_closeups_v6.py`:
worm-centered 2.5-4x-margin square crops, MIN_EDGE 320, labels recomputed in
crop coords, crops with a PARTIALLY-cut box skipped to avoid training suppression;
284 boxes total). Appended to the TRAIN dataset (LabeledEntries 1160→1512,
verified). TEST held IDENTICAL so v5→v6 F1 is comparable. Version
`v6-2026-07-14`; watcher `datasets/v6_watch.py`. HONEST EXPECTATION: the close-ups
are purchased-domain (same worms, tighter scale), so this should help the TEST-set
+ close-range recall, but does NOT add CAG-domain data — Batch 2 may still lag
(that gap needs real CAG close-range photos, still unavailable). Arbitrate v6 vs
v5 on Batch 2 + the purchased TEST after training.
**v7 PLAN (Dr. Li + Runzhe, 2026-07-16) — the corrected direction.** Root cause
of ALL current misses, seen clearly at last: the false negatives are LIGHT-COLOURED
worms (cag_armyworm_003/004, most of batch_2), while the training set is dominated
by DARK/black generic caterpillars. The v6 "close-up" augmentation made this WORSE
— audit of the 200 crops showed mixed SPECIES (a black spiny non-armyworm larva),
blur, and scale that never actually reached close-up — diluting the armyworm concept
and losing 003/004. Lesson (report-grade): synthetic augmentation must first verify
SPECIES consistency + real scale, or it is contamination, not augmentation.
- **v7.1**: add ~500 REAL light-coloured armyworm images, shot at ~2 m (the CAG
  deployment distance), CAG-domain-similar; REDUCE the black generic-caterpillar
  set to ~800. Hypothesis: fixes light-worm FN AND cuts shadow FP simultaneously.
  BLOCKER: sourcing the ~500 light-coloured set (web search in flight 2026-07-16).
  Candidate species for "light" larvae: Spodoptera litura (SE-Asia/Singapore
  relevant), S. exigua, Mythimna separata, early-instar S. frugiperda.
- **v7.2**: ONLY after v7.1 shows the approach works — data augmentation on the
  existing set (flip, scale, light/exposure re-synthesis) for generalisation.
- Data source is SELF-SOURCED only (CAG is dead — see below); purchased/public
  datasets + our own capture.

**v7 SOURCE AUDIT 2026-07-17 — every candidate source inspected visually
(contact sheets, boxes drawn). Two findings invalidate the v7.1 plan as
written.**
1. **The Roboflow light-worm source has 36 images, not ~500.**
   `universe.roboflow.com/kantharaju/dataset-18-classes` (v10, 17 classes despite
   the name) holds `Pest-(Spodoptera litura)` = **36 unique source images**
   (train 64 / valid 7 / test 4 are Roboflow AUGMENTATIONS of 25 / 7 / 4
   originals). `Pest-(Spodoptera exigua)` adds ~37 more, with many visible
   near-duplicates. Species + colour check PASSES (real Spodoptera, cream/tan
   with dark lateral spots = the missing phenotype); **scale check FAILS** —
   they are extreme macro, several on studio white backgrounds, not the ~2 m
   deployment geometry. Some boxes are loose. Downloaded to
   `datasets/pest18-classes-10/`.
2. **The purchased "caterpillar" set (1300 imgs, 89% of v5's TRAIN) is NOT
   armyworm.** Visual audit: black spiny temperate larvae (nettle/willow/alder
   scenes, European foliage), plus fuzzy tussock-moth larvae; many images blurry,
   several shot upward against sky, worms often a few pixels in wide cluttered
   frames, and some boxes contain no visible worm. This is the SAME "black spiny
   non-armyworm" contaminant the v6 close-up audit flagged — v6 merely
   concentrated it. **This is the root cause of the foliage/bud FP prior**
   ("plant close-up ⇒ armyworm") and of the light-worm FN, and it explains why
   F1 0.852 on the purchased TEST never transferred to CAG: that F1 measures
   fidelity to a wrong-species set.
3. **The maize set is the best asset we have and v5 drowned it.**
   `maize-fallarmyworm-1` = 133 images with a `fall-armyworm-larva` box
   (108 train / 22 valid / 3 test). Audit: real *S. frugiperda*, **LIGHT tan/cream**,
   shot in real maize at **medium distance** (worm small in frame, natural
   clutter/lighting) — the closest thing on disk to CAG deployment geometry AND
   the target colour. v5 used 108 of them = **9% of TRAIN**, against 1040
   purchased. v5's valid split (22 larva images) was never ingested.
4. **CAG batch_2 re-audit (all 7 opened): 5 of 7 carry hand-drawn circles** —
   101 blue, 102 pink, 103 green (the "416" jeep is in-frame = the known FP
   source), 106 red, 107 blue — the same contamination that excluded
   `cag_bud_002` from v5. Only **104 and 105 are clean** (unmarked, close-range,
   worm sharp). So batch_2 was never a 7-image benchmark; it is a 2-image one.
5. **SUPPLY SOLVED same day — the maize set is a slice of a ~1,100-image CC0
   mother lode.** 8-modality source hunt (33 agents, adversarial verify) found our
   133 maize images are a ~9% subset of **KaraAgro AI Maize** (Harvard Dataverse
   DOI `10.7910/DVN/CXUMDS`, same Ghana 2021-22 campaign, CC0, boxes included,
   ~1,100 net-new, field scale). Tropical top-ups for domain match: iNat S. litura
   Asia, GBIF Taiwan Moth (CC-BY), IP102 classes 86/23, + a pale-tan Spodoptera
   photographed on shell-ginger AT JEWEL (iNat obs 294780558). Full ranked plan +
   composition + risks + 48h steps: **`datasets/v7_1_acquisition_plan.md`**. The
   two live risks it stresses: DOMAIN (KaraAgro is maize field, not Jewel — same
   non-transfer trap as v5's purchased set) and SCALE (reject macro even when
   light — the v6 lesson).

**v7.1 TRAINED + JUDGED ON CAG 2026-07-21 — REJECTED, KEEP v5. The light-colour
hypothesis did NOT transfer; DOMAIN is the binding constraint, not colour.**
v7.1 = 1503 corn(DST1105 FAW) + moth-zldog (Mythimna/S.litura larva), single class,
NO black set, NO CAG (both batches held out). In-domain test F1 0.744 (corn+moth,
meaningless for CAG). CAG holdout arbitration (whole-image, `evaluate_v7_1_vs_v5.py`,
raw `datasets/holdout/cag/v7_1_vs_v5_20260721_021123.json`):
- Overall recall @50%: **v5 16/20 vs v7.1 12/20** (@30% 18 vs 15; @70% 14 vs 10) —
  but batch_1 (13) is v5's TRAIN (memorised) vs v7.1 unseen, so it is NOT a fair
  comparison; it inflates v5.
- **batch_2 (7, never trained by either = the only fair set): TIE at every
  threshold** — both 3/7 @50, 5/7 @30, 2/2 @70. v7.1 wins 102 (new hit) and is far
  more confident on 104 (75.9→**93.1**), but loses 105 (78.1→43.0). Net wash.
- **The thesis image test:** the light worms the whole v7 pivot targeted —
  cag_armyworm_003/004 — v7.1 (generalising, never trained on them) scores
  **003 = 9.3%, 004 = 24.2%**; v5 memorised them (88/62). So corn/moth light worms
  did NOT teach the model CAG light worms. maize-field + rice ≠ Jewel indoor garden;
  the colour signal drowned in the domain gap — the plan's own #1 risk, realised.
- FP prior UNCHANGED by dropping the black set: both still spray 100-270 raw boxes;
  bud_002 (drawn circle) still fires ~91% on BOTH. Expected — corn/moth are also
  plant-background positives, and CL still cannot train on negatives.
- **Consequence / direction:** more out-of-CAG-domain positives (any colour) will
  NOT close the gap — v5, v6, v7.1 all confirm it. The only levers left are
  (a) DEPLOYMENT GEOMETRY — production cameras shoot CLOSE-RANGE vegetation, which
  IS the model's working domain (in-domain recall is ~10/10), so the demo/product
  should lean on close-range capture, not wide indoor scenes; (b) real CAG-domain
  close-range boxed positives, which we do not have and cannot get (CAG dead). No
  further blind retrain is warranted. v7.1 kept as a clean report-grade negative.

**v7.2 TRAINED + JUDGED 2026-07-21 - the black-set test. REJECTED: adding 500 generic black caterpillars HURT; 3rd model to confirm domain is the wall.** v7.2 = corn(full) + moth-zldog larva(full) + top-500 quality-ranked purchased BLACK, single class, strict 80:20 by unique stem. In-domain F1 0.794 (up from 0.744 - black test images are easier, meaningless for CAG). CAG holdout (`datasets/holdout/cag/v7_2_vs_v5_20260721_054531.json`):
- **batch_2 (fair) @50%: v7.2 2/7 vs v7.1 3/7 vs v5 3/7 - WORSE than both.** Overall @50% v5 16 / v7.1 12 / v7.2 11.
- **FALSE POSITIVES got worse (predicted):** bud_001 76.7%(v7.1)->96.2%(v7.2), bud_002 91%->97.4%. The purchased black set is the wrong-species 'plant=>armyworm' FP source; re-adding it re-strengthened that prior.
- **Light worms still mostly miss:** 003 27% / 005 20% miss; only 004 improved (24%->92.5%), 1 of 3.
- **Locked lesson:** out-of-CAG-domain positives do not help regardless of colour (v7.1) or volume (v7.2 +500); the black set specifically is NET NEGATIVE. Keep v5; do not re-add the black set. Levers remain deployment geometry + real CAG close-range data (unavailable).

**v6 VERDICT 2026-07-15: REJECTED and ROLLED BACK to v5. Synthetic close-up
augmentation does NOT transfer to the deployment domain — this is a clean,
report-grade negative result.** (Re-confirmed 2026-07-16 with a fixed test
harness: v5 batch_1 12/13 vs v6 10/13 — v6 lost the two LIGHT worms 003/004;
batch_2 + purchased tied. The earlier "v5 vs v6" table was corrupted by a
DynamoDB sort-key bug reading stale records — fixed: newest-record + since-window
+ model_arn provenance check in `push_v6_all.py`.) v6 (trained 13828s, purchased-TEST F1 = **0.839**
vs v5's 0.852) was trialled live on `worm_cam`, then all three probe sets were
pushed through the LIVE pipeline (v6, whole-image, `datasets/push_v6_all.py`).
**Threshold trap to remember when reading old numbers: the v5 probe runs used
min_conf 30, the v6 run used 60 — raw counts are NOT comparable.** Recomputed at
an identical 60 from the recorded max-confidences:

| probe | v5 @60 | v6 @60 | verdict |
|---|---|---|---|
| batch_2 (sacred holdout — the real signal) | 2/7 | 2/7 | tie |
| purchased TEST (in-domain) | 9/10 | 9/10 | tie |
| batch_1 (12/13 trained on — biased) | 12/13 | **10/13** | v6 WORSE |
| purchased-TEST F1 | **0.852** | 0.839 | v6 worse |

So the +352 close-ups bought NOTHING on the deployment domain and regressed two
images v6 had actually trained on (cag_armyworm_003 88.2%→<60, 004 61.7%→<60).
The ONE real effect: close-range confidence deepened (cag_armyworm_104
75.9%→**97.0%**) — it made the already-detected more certain but converted zero
misses into hits. **Lesson: same worms at a tighter scale ≠ new domain
information; the CAG gap needs REAL CAG close-range photos, not synthetic crops.**
Bud FP persists (bud_001 84.6%, bud_002 82.6%). v6 kept (not deleted) for the
record; endpoint stopped. Rollback executed = one ARN write (see `docs/aws.md`).

**IN-DOMAIN GENERALISATION IS STRONG — the problem is DOMAIN, not the model
(proven 2026-07-13, whole-image no-tiling via camera `notile_test`).** 10 purchased
TEST images (never trained on) detected 10/10, most at 99%+ (99.8/99.7/99.6...).
CAG batch_1 detected 13/13 (but 12/13 were trained on = memory check, biased).
Contrast: same whole-image path on Batch 2 (Jewel wide indoor) = 2/7 real hits.
So the model + training direction are RIGHT; the failure is domain mismatch —
purchased/close-range = model's domain (works great), Jewel wide-indoor with a
tiny distant worm = out of domain. This validates Dr. Li's steer (2026-07-13):
CAG's own images are too few/poor, judge direction on the purchased set first.
Implication: to make real CAG scenes work, either (a) deployment cameras shoot
CLOSE-RANGE (enter the model's domain) or (b) add CAG-domain close-range boxed
positives; swapping to YOLO fixes background handling but NOT the domain gap.
Note bud FP persists: cag_bud_002 (the excluded drawn-circle bud) still fired
armyworm at 91.4% — the old bud-false-positive that image-level negatives were
meant to fix but CL OD won't accept (see below).

**FP root cause is confirmed (2026-07-13), and it is NOT tiling-blur or classic
overfitting.** At MinConfidence=0 with NO tiling, both models spray 120-300 raw
boxes per image (v4 sprayed 143 on cag_armyworm_101; v5 sprayed 218) — almost all
1-8% junk filtered by threshold. So the box-spray is the model's own disposition,
not the crop pipeline. The surviving FPs are 90%+ CONFIDENT (not soft/mushy),
which rules out upscale-blur as the driver. Real cause = the training set has ZERO
true negatives (the converters skip zero-box images), so the model's learned prior
is "plant close-up ⇒ armyworm" and it fires on any foliage (observed again
2026-07-13 on an indoor greenhouse scene: 8 boxes >90%, zero worms). Tiling
AMPLIFIES this: 17 passes = 17 chances to FP on foliage at the close-up scale that
matches training positives, and NMS only removes overlapping duplicates, not
distinct per-tile FPs.

**TRAINING-NEGATIVES ARE A DEAD END ON THIS PLATFORM — proven 2026-07-13.**
Rekognition Custom Labels OBJECT DETECTION cannot train on image-level negatives
(no-box images). Confirmed three ways: (a) empirical — appended 410 clean
empty-annotation entries to the TRAIN dataset via `update_dataset_entries`; they
registered `cl-metadata.is_labeled:false`, `LabeledEntries` stayed 1160, and an
explicit `is_labeled:true` in the manifest was IGNORED on import; (b) AWS OD
manifest docs define `annotations` as required per detected object, with no
empty/negative form; (c) AWS re:Post + community: bounding boxes are mandatory,
true-negative images must be REMOVED. Because CL excludes unlabeled entries from
training, a v6 trained on these negatives would be byte-for-byte equal to v5 — a
guaranteed-zero-payoff run, so it was NOT trained. **This RETRACTS the earlier
(2026-07-13) reframe that the old "negatives limitation" note rested on thin
n=2 evidence — the old note was correct; it is an architectural limit, not a
sample-size problem.** The 410 negative images live at `training-data/negatives/`
(healthy-maize + hard-neg crops from purchased positives) and 410 unlabeled
entries remain in the TRAIN dataset (harmless — CL ignores them; removing needs a
dataset recreate). Build/upload/append scripts: `datasets/build_negatives_v6.py`,
`upload_negatives_v6.py`, `append_negatives_v6.py`; census `census_negatives.py`.

CONSEQUENCE — the model-side levers on Rekognition CL are asymmetric: you can
raise RECALL (add more BOXED positives, esp. close-range CAG-domain) but you
CANNOT suppress FPs by feeding negatives. FP suppression must be done OUTSIDE the
model. Fixes, cheapest first: (1) deployment geometry (production cameras see
close-range vegetation only — the wide indoor-showcase scenes that FP worst never
occur); (2) app-layer suppression in the processor (DetectLabels pass; drop custom
boxes overlapping Person/Vehicle/Furniture/Machinery — kills the jeep-tire/people
FP type, e.g. cag_armyworm_103's 65% box on the "416" jeep wheel; does NOT fix
plant-on-plant FPs); (3) a two-class worm-vs-background retrain COULD teach the
model to reject foliage, but repeats the v2 multi-class shortcut-learning risk —
open, not recommended without careful same-image background boxing + validation;
(4) strategic: a self-hosted detector (YOLO on the Jetson Orin) trains natively on
backgrounds/negatives and drops the $4/hr CL hosting cost — the platform limit is
a real reason to reconsider CL for the detector.

Correction to the v3 record above: the "2 image-level
negatives" (cag_bud_001/002) were never negatives — both carry an `armyworm-larva`
box. Visual audit 2026-07-07: bud_001 = real larva on the lotus flower (KEPT in v5);
bud_002 = hand-drawn blue circle around the bud (EXCLUDED from v5 — drawn markings
contaminate training; v4 trained with it, a possible bud-FP contributor).
Multi-class (Route 2, tried as v2) **failed** — shortcut learning, discriminated by photo
domain not species. Reverted to single-class.

## Hard-won learnings
- **4x crop is the sweet spot**: 1x → ~33% recall, 4x → peak, 6x → same or worse. A8
  deployment zoom 2–4x. The real bottleneck is model blind spots (FN patterns), not scale —
  fix via training-data augmentation, not more cropping.
- **Domain shift destroys detection**: a printed-then-photographed image gave 0 detections vs
  96.6% digital. Production case = 4K close-range digital capture.
- Image-level negatives are insufficient for bud false-positives — a Rekognition CL OD service
  limitation; address externally.
- **Out-of-domain input → confident garbage (observed 2026-07-07, v5)**: pointed at an
  indoor showcase scene, v5 boxed people and a robot arm as armyworm-larva at 90-99%.
  NOT overfitting — the train set is 100% plant-domain positives (the converters skip
  zero-box images, so there are no true negatives at all); the model has never seen
  "not a worm". Same lesson as domain-shift above. Fixes, cheapest first: application-layer
  suppression in the processor (DetectLabels Person/Machinery overlap filter), true
  negatives in v6 (limited effect per the line above), and deployment geometry
  (production cameras point at vegetation only — this input never occurs).
- Rekognition is called at a 30% threshold to preserve full label data for frontend filtering.
- **LLM CROP-VERIFICATION IS LIVE (processor v4.5, deployed 2026-07-22, Haiku
  4.5, HYBRID GATE — not annotate-only anymore).** After reviewing the A/B data
  below, Runzhe changed the rule: a Rekognition box at/above the camera's
  `min_confidence` is trusted outright and the LLM is never even called; a box
  BELOW it is adjudicated by Haiku — explicit reject drops it, anything else
  (confirm, or any failure) keeps it, fail-open. Per-camera opt-in
  (`llm_verify_enabled` in DynamoDB) so this never runs on `moth_cam` (adult
  moths, wrong prompt domain). Full architecture + two bugs an adversarial
  review caught before this reached production: `docs/aws.md` v4.5 entry.
  Pipeline order — **Rekognition first, LLM second. Never the
  reverse:**
  `whole frame (uncropped, unscaled) -> Rekognition v5 -> v4.3 hard-object
  suppression -> per-box crop+upscale (fed ONLY to Bedrock) -> Haiku 4.5 verdict
  -> verified_by_llm/llm_reason on the box -> dashboard`.
  Rekognition FINDS, the LLM VERIFIES. The reverse order (LLM pre-screens the whole
  frame, then hands regions to Rekognition) was tested and is dead — see the A/B
  below. **Rekognition still only ever sees a whole, unscaled frame**, so the W15
  shadow-filtering ruling is not violated; the crop exists only downstream, as
  Bedrock's input.
- **A/B RESULT 2026-07-21 — whole-image vs crop, same 20 CAG images, same model
  (Haiku 4.5). Crop wins decisively.** `datasets/verify_llm_crop.py` runs both arms
  in one pass over an identical image set with per-arm prompts and pre-registered
  decision thresholds.

  | | ARM A whole-image | ARM B crop |
  |---|---|---|
  | CLEAN recall (n=14) | **6/14 (43%)** | **13/14 (93%)** |
  | discordant A-yes/B-no | — | **0** |
  | discordant A-no/B-yes | — | **7** |
  | jeep-wheel control (must reject) | — | PASS |
  | cag_armyworm_104 control (must accept) | FAIL (missed) | PASS |

  **Whole-image is not "slightly worse", it is systematically blind.** It missed
  `cag_armyworm_009` — an unmistakable striped larva curled on bare concrete — and
  reported "concrete surface with black cable and hook, no vegetation or larva
  visible". This matches the mechanism predicted before the run: **Haiku 4.5 is
  Bedrock's STANDARD resolution tier (1568px long edge / 1568 image tokens)**, not
  the high-res tier (2576px/4784 — Fable 5/Mythos 5/Opus 4.7/4.8/Sonnet 5 only). A
  target under ~0.5% of frame area survives Bedrock's downscale as ~2-3 visual
  patches; under ~0.1% as roughly one token. Backed by ICLR 2025 "MLLMs Know Where
  to Look", which also shows crop+upscale is what recovers the accuracy. **No model
  swap fixes the whole-image arm — it is an input-resolution limit, not model
  quality.**
- **Model choice matters enormously for the CROP arm** (unlike the whole-image arm,
  where nothing helps). Same three crops, both models: Nova Lite false-rejected
  `cag_armyworm_104`'s large, high-contrast striped larva ("No larva visible");
  Haiku 4.5 accepted it correctly ("Striped caterpillar larva visible on green leaf
  surface") and still rejected the jeep wheel. Haiku's rejections are
  **species-level discriminations**, not blanket refusals: "a moth or dragonfly",
  "a snail or mollusk", "a spider or arachnid", "vintage truck wheel", "too blurry".
  Nova Lite rejected 38/41; Haiku rejected 12/48 with a stated alternative ID on
  nearly every one.
- **KNOWN FALSE REJECTION — light-coloured worms are still the weak class.** Haiku
  rejected `cag_armyworm_003`'s 88.2% box as "a moth or insect on orange flower, not
  a larva". Visual check: it is unambiguously a **pale grey armyworm larva** —
  segmented, with a row of black spiracle dots along the flank, curled on a hibiscus
  petal. It is the single CLEAN image ARM B missed, and it falls in exactly the
  light-worm class that v7.1 was built to fix and failed to. **The verifier inherits
  the project's oldest blind spot; it does not cure it.**
- **Limits of this result, state them in the report:** (1) the pre-registered third
  gate — box-level precision >= 0.50 — could NOT be evaluated, because that needs
  human ground truth on all 48 crops and only 6 were spot-checked; the KEEP decision
  rests on 2 of 3 gates. (2) n=20 with essentially no larva-free images, so the
  false-alarm rate that dominates production (most frames are empty) is unmeasured
  here. (3) Not a random sample of Jewel conditions.
- **What the earlier Nova Lite crop-verify run measured (processor
  v4.4, 2026-07-21, superseded by the Haiku A/B above).** Architecture: Rekognition
  generates boxes -> each box
  is cropped with 0.6 padding and upscaled to 672px -> a Bedrock multimodal model is
  asked "is this a larva?". Over the full 20-image CAG holdout the crops were correct
  (inspected by eye) but **Nova Lite rejected 38 of 41 boxes**, including
  `cag_armyworm_104`'s 75.9% box, which shows a large, high-contrast striped
  caterpillar filling the frame. It does reject true non-worms correctly (the
  `cag_armyworm_103` jeep wheel), so it is discriminating, just far too trigger-happy
  on REJECT. **A verifier that false-rejects an obvious larva is worse than no
  verifier**, which is why the stage shipped annotate-only at the time
  (`LLM_VERIFY_DROP=false`; that env var no longer exists — v4.5 replaced
  annotate-only with the hybrid gate once Haiku 4.5 became available, see above).
  Claude Haiku 4.5 — the model the selection research recommended for exactly this
  yes/no-plus-reason job — is untested: Bedrock blocks it on this account pending the
  Anthropic use-case form. Until that is submitted and re-tested, "LLM verify helps"
  is UNPROVEN, not disproven. Separately confirmed by the same test: a multimodal
  model asked to find worms in the WHOLE wide frame found none in 7/7 batch_2 images
  — reinforcing that the LLM must go second, never first.

## Two detection paths — never compare numbers across them
**STATUS 2026-07-21: the two paths have CONVERGED — tiling is OFF in production.**
The W15 ruling (`docs/project_timeline.md`) settled it: "whole-image detection is
the deployment mode. Tiling/cropping damages Rekognition's shadow filtering and
increases foliage/shadow false positives." `worm_cam.tiling_enabled = false`;
verified live 2026-07-21. Do not turn it back on. The tiled path below is kept as
the record of what v4.2 did and why the historical numbers differ.
- Pipeline path (processor, used by patrol + dashboard Test upload): TILED —
  4x4 grid + 15% overlap + full-frame pass = 17 calls, NMS-merged. Live since
  v4.2 (well before model v5; the v4→v5 switch changed the model only).
  **RETIRED by the W15 whole-image ruling — code still present, switch is off.**
- Script path (`datasets/evaluate_cag_*.py`): whole-image single
  `detect_custom_labels`, NO tiling.
- v4's historical Batch 2 numbers are script-path. Any v4-vs-v5 arbitration must
  run both models through the SAME path (the script is the intended arbiter).

## Data hygiene
- **Batch 2 (7 CAG images) is a sacred holdout — never train on it.**
- ~~Ask CAG for clean, unmarked photos~~ **RETIRED 2026-07-15 — CAG is written off
  as a data source** (nothing supplied in ~1.5 months; Runzhe's call). Training
  data now comes ONLY from sources we control: purchased/public datasets and our
  own capture. The drawn-circle contamination lesson still stands for any image we
  ever do receive (see cag_bud_002), but do NOT plan around CAG deliveries.
- No auto-retrain loop; wrong detections are labeled manually.
