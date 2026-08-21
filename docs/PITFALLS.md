# PITFALLS — traps this project already paid for

Every entry below cost real time. Read the section for your area before you
start debugging, not after.

Format: **the trap** → what actually happens → what to do instead.

---

## Machine learning and evaluation

### In-domain F1 goes up while real-world recall goes down
Three model versions (v5, v7.1, v7.2) each raised or held F1 on their own test
split while performing worse on the real site imagery. v7.2 is the clearest
case: test F1 rose 0.744 → 0.794 and site recall *fell*.

**Why:** the test split is drawn from the same source as the training data, so
it measures "did the model memorise this domain", not "does it transfer".

**Do instead:** judge every model on the fixed holdout through the live
pipeline. Never sign off on a model using its own test F1. See
`docs/model_ladder.md`.

### F1 numbers from different versions are not comparable
Each F1 in the ladder was measured on a *different* test set that grew and
changed domain along with the training set. The ladder is a record of
engineering iterations, not a climb up one benchmark.

**Do instead:** compare models only on the frozen holdout in
`datasets/holdout/`, scored by the same harness with the same matching rule.

### A detector's F1 is not the pipeline's accuracy
From processor v6.0 onward the detector is deliberately a **high-recall front
end** — tiling on, candidates gathered down to `TILE_MIN_CONFIDENCE = 8` — and
the LLM gate supplies precision. Reading the detector's stand-alone F1 as
system accuracy will mislead you in both directions.

### Confidence does not rank truth
In the 2026-08-11 per-box adjudication, false positives sat at 62–84% while
the real worm sat *below* them in 3 of 4 images. One frame's false positive
survives at 81.5%, above any usable floor.

**Consequence:** you cannot fix precision with a threshold. Only the
verification gate or the prompt can remove these. Do not go looking for a
magic floor value.

### Raw detection counts across runs are not comparable
Old probe runs used different `min_confidence` values (30 vs 60). Comparing
raw box counts between them is meaningless.

**Do instead:** recompute both sides at an identical threshold from the
recorded per-box confidences before comparing anything.

### More data at a tighter crop is not new information
Adding 352 close-up crops deepened confidence on images the model already
detected (one went 75.9% → 97.0%) and converted **zero** misses into hits.

**Lesson:** the same subjects at a different scale is not new domain
information. Synthetic crops do not substitute for genuinely different imagery.

### Rekognition Custom Labels silently ignores image-level negatives
The v6 experiment appended negatives with empty annotation lists. Custom Labels
accepted them and ignored them. There was no error and no warning.

**Do instead:** verify what the dataset actually contains with
`ListDatasetEntries` after any append, and assert on the counts.

---

## Amazon Rekognition Custom Labels

### The dataset is the source of truth, not the S3 manifest
Any label added or corrected **in the Rekognition console** lives inside that
account's DATASET only. The manifest in S3 is stale from that moment on.
Building a new dataset from the copied manifest silently trains an older
labelling state — and you do not find out until hours of training have burned.

**Do instead:** export live entries with `ListDatasetEntries`, repoint every
`source-ref`, upload as new manifests, then create the dataset.

### Models are account-bound and cannot be exported
A Custom Labels model version cannot be moved or copied between AWS accounts.
Copying an ARN across accounts is never an option. During the account migration
both detectors had to be **retrained** on the new account from migrated data.

### Endpoints cost money while running and must be stopped explicitly
A running project version bills continuously (the project's own docstrings have
quoted both ~$1/hr and ~$4/hr; `costs.js` reads the real bill). Nothing stops
it for you.

**Do instead:** always wrap test runs in a `finally:` that re-checks every
endpoint you started and stops anything still RUNNING or STARTING. There is
also a watchdog Lambda driven by the camera row's `max_runtime_min`.

### Images and manifest entries must be deleted together
Deleting training images without dropping the matching manifest lines makes the
next retrain 404 on the missing objects.

---

## AWS stack

### A permission gap in a fail-open path produces confident garbage, not an error
The processor's verification step fails open by design. When the IAM policy for
Bedrock was missing, the pipeline kept running and kept writing records — they
were just unverified. Every deployment stage reported success.

**Lesson, and it generalises:** validate a deployment **by result**, never by
"the stages all went green". Push a known image through and check the output.

### S3 public-access-block ordering
On the dashboard bucket, public access block must be turned off *before*
`PutBucketPolicy`, or the policy call fails. See
`reports/manual/08_reproduction_runbook.md`.

### Copying a frame into the frames bucket re-triggers detection
The S3 event fires on the copy, the processor runs again, and a second record
appears for the same image with a fresh `detection_time`. This produced four
junk rows during the migration.

### A bulk S3 delete driven by a substring match will take innocent files with it
A filter string can appear inside unrelated filename hashes. In this project one
such match would have destroyed 13 legitimate Roboflow files.

**Do instead:** match the key **prefix**, and re-list the bucket afterwards to
verify the survivors. Enable versioning so a mistake is recoverable.

### Inline JSON breaks PowerShell quoting
Raw inline JSON containing colons breaks when passed to the AWS CLI from
PowerShell.

**Do instead:** write a JSON file and use `file://` input.

### Bedrock model access needs a use-case form, and the error does not say so clearly
After the IAM policy was correct, the error changed from `AccessDeniedException`
to `ResourceNotFoundException: Model use case details have not been submitted`.
The second error is an account-level form, not a permissions problem — do not go
back to debugging IAM.

---

## Frontend

### ES-module caching survives a plain reload
The dashboard loads ES modules directly with no bundler. A normal refresh keeps
serving the old JS, so a fix appears not to work.

**Do instead:** hard-reload, or bust the cache in the import URL.

### A display name is functionality when it is the operator's only handle
After the migration, the deployer wrote the deployment name into the camera
row's `label`. The dashboard shows `label`, not `camera_id`. The operator opened
the dashboard, saw no familiar camera, and concluded the row was missing. The
backend had been fine the whole time.

### Do not rename a primary key that devices hard-reference
`camera_id` `armyworm_go2_a8mini` is hard-coded in `robot/go2_patrol_gated.py`,
`minipc/capture_and_upload_v4_armyworm.py` and `robot/kvs_controller.py`.
Renaming the PK breaks the live capture chain. The UI hides the id and shows a
friendly label instead.

---

## Edge devices

### systemd `Environment=` overrides what you edited in the home directory
`CAMERA_ID` is set both in the systemd unit's `Environment=` line and by an
`export` in the run script. Editing only the script, or only the unit, gives you
a device that reports as the wrong camera.

**Do instead:** change both, and confirm which one wins on that unit before
assuming.

### UVC capture silently falls back to 1080p
On a USB capture card the MJPG fourcc must be set **before** the resolution, or
the card quietly gives you 1080p while you believe you have 4K.

**Do instead:** read the actual frame size back after opening the device and
never label a frame by what you requested.

### A stale SSH tunnel hangs at banner exchange
If the reverse tunnel is up but connections hang with "banner exchange"
timeouts, kill the stale sshd pids on the Orin; the service rebinds within about
15 seconds.

---

## Working practice

### The local tree is not a backup
Nine holdout images vanished from local disk between 2026-08-07 and 2026-08-12
and had to be recovered by scanning S3 push history. Integrity was confirmed by
image dimensions, not by trusting the filenames.

**Lesson:** the S3 push history was the only backup the holdout had, and it was
never designed to be one. Do not rely on that twice.

### A claim that is never re-verified propagates through every handoff
One requirement was captured wrongly in an early session, copied forward through
handoff after handoff, and shaped work for weeks before anyone checked it
against the source.

**Do instead:** when a document states a requirement, record who said it and
when. Re-verify before building on it.

### Bash heredocs eat one layer of backslashes on Windows
Writing Python or regex containing backslashes through a heredoc on this
toolchain silently corrupts them.

**Do instead:** write the file directly rather than piping it through a shell
heredoc.
