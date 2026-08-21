# DECISIONS — why the system is shaped this way

Each entry is a fork the project actually reached: what was chosen, what was
rejected, and what the evidence was. Read this before proposing a change — most
"obvious improvements" are on the rejected list, with a reason.

Chronology and per-version detail live in [`model_ladder.md`](model_ladder.md)
and [`state.md`](state.md).

---

## Architecture

### One Lambda holds the whole detection pipeline
`lambda/pest-detection-processor.py` does tiling, detection, verification,
suppression, box cleanup and the DynamoDB write in a single S3-triggered
function.

**Why:** the pipeline is a strict sequence with no fan-out and no step that
benefits from independent scaling. Step Functions or a queue between stages
would have added operational surface, cold starts and failure modes for no
gain at this volume.

**Cost:** the function is large and its stages can only be tested together.
Accepted deliberately. A v6.3 pass stripped dead code (3,266 → 1,545 lines)
rather than splitting the function up.

### Detector finds, LLM verifies
Rekognition Custom Labels runs as a **high-recall front end**: tiling on,
candidates gathered down to `TILE_MIN_CONFIDENCE = 8`. Claude Sonnet on Bedrock
then judges every surviving box, and precision comes from that gate plus
post-gate cleanup.

**Why:** the measurement that forced it — raw tiled coverage found *every*
worm in the holdout. The detector was never the recall bottleneck; the
confidence floors were. The worms sat at 10–17% confidence, below every
operating floor in use at the time.

**Rejected:** raising detector precision by training. Four model versions tried
and failed. Also rejected: an LLM-only pipeline with no detector — the arena
runs showed the LLM is a good judge of a proposed crop and a poor localiser of
a whole frame.

**Consequence:** the detector's own F1 stopped describing system behaviour.
This is why the report leads with recall on real-scene sets, not F1.

### Tiling is a per-camera switch, not a separate camera
Earlier builds modelled "tiled" and "whole-image" as different camera rows.
That doubled every configuration surface. It is now one boolean
(`tiling_enabled`) on the camera row, exposed as a "Zoom scan" toggle in the
dashboard.

### The dashboard is vanilla JS ES modules with no build step
Thirteen modules load straight into the browser. No bundler, no framework, no
package manager.

**Why:** the deliverable has to be handed to a maintainer who may not have a
Node toolchain, and it has to be deployable by copying files to S3. A build
step is a dependency on tooling that will rot faster than the code.

**Rejected:** a Flutter Web rewrite. This was believed to be a requirement for
several weeks based on an early miscapture, and was disavowed once the claim
was checked against its supposed source. Do not resurrect it.

**Cost:** `main.js` exposes exactly 40 names on `window` for the inline
`onclick` markup. Every other module stays private.

### Auth is Cognito with the JWT authorizer as the real boundary
Every backend call goes through `api.js`, which attaches the Cognito ID token
as a Bearer header. The HTTP API's JWT authorizer is the enforcement point.
The browser never holds an AWS key.

### Detection boxes are stored as normalised coordinates, never drawn into the image
DynamoDB holds normalised box coordinates; the browser paints
percentage-positioned rectangles over the original frame.

**Why:** this is what lets an operator flag a false positive and have that box
disappear from the card, the modal and the analytics count at once. Burning
boxes into a JPEG makes every downstream correction impossible.

---

## Model and data

### Training data comes only from sources the project controls
The supply is the purchased corn(DST1105) fall-armyworm-larva class plus the
moth-zldog Roboflow larva classes, augmented 13× (flips, rotations, exposure
jitter), 13 variants per source image.

**Why:** the original plan depended on the client supplying site photographs.
After about six weeks with nothing delivered, that dependency was written off.
A project cannot be scheduled around a data source it does not control.

### Generic caterpillar imagery was tested and rejected
A large set of generic black caterpillars was added in v7.2 and made things
**worse**: false positives rose sharply (one probe image went 76.7% → 96.2%)
while light-coloured worms still missed.

**Why it hurt:** it reinforced a "plant implies worm" prior. Three model
versions independently confirmed that domain, not volume, was the wall.

**Consequence:** do not add generic caterpillar data again. v7.2 is kept as a
report-grade negative result.

### The holdout is fixed and small, and that is the point
The scored holdout is `batch_2` 102–109, `CAG_Jewel_1/2`, and the 4
field-realistic photos. Recall is quoted from that set and nothing else.

**Why fixed:** a holdout that changes between model versions cannot compare
them. The ladder harness scores every arm against one frozen answer key
with one matching rule.

**Known limit, stated rather than hidden:** the holdout contains no true
negatives, so an image-level false-positive rate is not measurable on it. Any
claim of an FP rate on this set is unsupported.

### There is no automatic retraining loop
Wrong detections are labelled by hand.

**Why:** an auto-retrain loop on a system whose false positives outrank its
true positives in confidence would train on its own mistakes. The flag button
exists to correct the record an operator sees, not to feed a pipeline.

### The confidence floor was tuned twice, and the second value is live
The threshold study settled on **49** on the development account. After the
model was retrained on production, 49 was discarding real worms, so it was
refitted to **33** on 2026-08-13. Both the live Lambda and the camera row read
33.

**Why the report still says 49:** the study is correct for the build it was run
on. This is the single most likely question to be asked about the numbers, and
the honest answer is that a threshold is a property of a specific trained model,
not a universal constant.

### Area cap at 5% of the frame
`POST_MAX_BOX_AREA = 0.05`. The largest hand-labelled worm is 4.39% of frame,
so the headroom is only 1.14×.

**Known risk, written down:** a genuine close-up worm may exceed 5% and be
silently suppressed. This is the first thing to suspect if a large obvious worm
produces no box.

---

## Deployment and operations

### The Go2 quadruped is a demo and testbed, not the production shape
Production is fixed cameras at each waypoint.

**Why:** the robot proves the capture geometry and makes the system
demonstrable, but a quadruped patrolling a public airport garden is not an
operable deployment. Both capture paths write to the same S3 prefix and the same
pipeline, so the cloud side is identical either way.

### The whole stack ships as a one-click installer
`deployer/` builds ARGUS, a Windows app that stands the entire AWS stack up on
a fresh account in 15 stages (measured at 103 seconds on the production
migration).

**Why:** the system has to be handed over to people who will not run a
15-command CLI sequence correctly. The deployer is also the executable
specification of the stack — if a resource is not in `deploy.py`, it is not part
of the system.

**Rule learned the hard way:** the deployer must reproduce the *validated*
stack, not an idealised one. It once shipped a stale threshold, which would have
silently regressed detection for anyone who re-ran it.

### Credentials never touch project files
The deployer stores the IAM key in the OS credential store. Device credentials
live in the environment or `~/.aws/credentials` on the device. Card and password
entry happens on AWS's own pages, which the app does not read.

### The account migration retrained rather than copied
Custom Labels models are account-bound, so moving to the production account
meant retraining both detectors there and rewiring every ARN. The frozen
pre-migration configuration baseline was frozen to a JSON snapshot so drift
could be diffed against it.

---

## What the project can and cannot claim

**There are no worms at Jewel.** This is the constraint that shapes every
claim.

The project can show:
- that the model detects, by offline evaluation against the fixed holdout;
- that the robot patrols autonomously with the full cloud chain live;
- that the stack deploys reproducibly onto a clean account.

The project cannot show:
- that a real worm will be caught on any given patrol.

Live detection is demonstrated through the dashboard's Test upload panel.
Printed targets were tried early and do not work. Stating this limitation
plainly is a deliberate choice — the alternative is a claim the evidence does
not support.
