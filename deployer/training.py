#!/usr/bin/env python3
"""
training.py - ARGUS in-app model training pipeline.
Design: docs/deployer_training_pipeline.md (spec'd 2026-07-16, built 2026-07-17).

Takes the customer's LOCAL YOLO-labeled image folder and turns it into a
trained, WIRED-IN Rekognition Custom Labels model, end to end:

    analyze folder -> validate -> convert (YOLO -> Ground Truth manifest)
    -> upload to S3 -> create/append Rekognition datasets -> train
    -> watch to completion -> write custom_model_arn onto every custom camera.

Architecture ruling (2026-07-16): deterministic API automation only — no LLM
ever holds credentials or mutates cloud resources. The converter math is the
production-proven datasets/convert_to_manifest.py logic (v3-v6 all trained
through it), ported here.

Every AWS-touching function takes the deploy.Ctx built by the app shell, so
naming, state and persistence stay single-sourced with deploy.py. Events
stream through the same emitter pattern; all kinds are train_* so the UI can
route them without colliding with deploy events.

Hard-won constraints honored here:
  - Rekognition CL object detection IGNORES zero-box images (the v6 negatives
    lesson) — they are excluded up front and reported, never uploaded.
  - Images with any dimension > 4096 px are rejected by CL — downscaled here
    (boxes derive from normalized YOLO coords x the FINAL dims, so scaling
    is lossless for labels).
  - Model ARNs contain colons; camera-row updates go through boto3 only.
  - Training is billed by AWS (~US$4/training-hour; the 1,148-image reference
    run took 3.5 h). The UI cost-gates before start_training is ever called.
"""

import json
import re
import shutil
import time
from pathlib import Path

import botocore

import deploy  # same-dir sibling: Ctx, TABLES, OUT

# Pillow is required (image dims + the 4096 downscale). It is a hard runtime
# dependency of this module only — deploy.py must keep working without it.
try:
    from PIL import Image
except ImportError:  # surfaced as a friendly analyze()/run error, not a crash
    Image = None

MAX_EDGE = 4096          # Rekognition CL rejects any dimension > 4096
MIN_PER_DATASET = 10     # CL minimum images per dataset (train and test)
TEST_FRACTION = 0.15     # auto split when the folder has no test/ split
UPDATE_CHUNK_BYTES = 4_000_000   # update_dataset_entries hard cap is 5 MB
IMG_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff")
NATIVE_EXTS = (".jpg", ".jpeg", ".png")   # CL-supported as-is; others re-encode
CAMERAS_TABLE = deploy.TABLES["cameras"]

PHASES = [
    ("validate", "Check the data"),
    ("convert", "Convert labels"),
    ("upload", "Upload images"),
    ("datasets", "Load the vision engine"),
    ("train", "Train"),
    ("wire", "Wire into cameras"),
]


class TrainError(Exception):
    """Expected, user-facing pipeline failure (bad data, AWS refusal, ...)."""


# A project version whose training finished can move on through the endpoint
# lifecycle (customer starts/stops the model) — ALL of these mean "training
# succeeded", not "still training" and not "failed". Treating STOPPED as a
# failure (or RUNNING as in-progress) would mis-handle a model that completed
# while the app was closed and was then started from the dashboard/console.
TRAINED_OK_STATUSES = ("TRAINING_COMPLETED", "STARTING", "RUNNING",
                       "STOPPING", "STOPPED")
TRAIN_DEAD_STATUSES = ("TRAINING_FAILED", "FAILED", "DELETING")


# ---- event streaming (same pattern as deploy.py, separate global) ----------
_EMIT = None


def set_emitter(fn):
    global _EMIT
    _EMIT = fn


def _emit(kind, **payload):
    if _EMIT:
        try:
            _EMIT(kind, payload)
        except Exception:
            pass


def _log(line, ok=None):
    print(f"[train] {line}", flush=True)
    _emit("train_log", line=str(line), ok=ok)


def _phase(name, status):
    for i, (n, label) in enumerate(PHASES):
        if n == name:
            _emit("train_phase", index=i, total=len(PHASES), name=n,
                  label=label, status=status)
            return


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# =============================================================================
# 1. Analyze — fast, local, no AWS, no image decoding (label text only)
# =============================================================================
def _find_splits(root):
    """Auto-detect the dataset layout. Returns (layout_name, {split: (img_dir,
    lbl_dir)}) or (None, {}). Recognized:
      yolov8   root/images/{split}/ + root/labels/{split}/
      flat     root/images/ + root/labels/          (single split)
      roboflow root/{split}/images/ + root/{split}/labels/
    """
    root = Path(root)
    if (root / "images").is_dir() and (root / "labels").is_dir():
        subs = sorted(d.name for d in (root / "images").iterdir() if d.is_dir())
        splits = {s: (root / "images" / s, root / "labels" / s)
                  for s in subs if (root / "labels" / s).is_dir()}
        if splits:
            return "yolov8", splits
        return "flat", {"train": (root / "images", root / "labels")}
    splits = {}
    for s in ("train", "valid", "val", "test"):
        if (root / s / "images").is_dir() and (root / s / "labels").is_dir():
            splits[s] = (root / s / "images", root / s / "labels")
    if splits:
        return "roboflow", splits
    return None, {}


def _class_names(root):
    """Best-effort class names from data.yaml (no yaml dependency; handles the
    inline-list, block-list and id-map forms Roboflow/YOLO emit)."""
    p = Path(root) / "data.yaml"
    if not p.exists():
        p = Path(root) / "data.yml"
    if not p.exists():
        return {}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    m = re.search(r"^names:\s*\[([^\]]*)\]", text, re.M)
    if m:
        items = [x.strip().strip("'\"") for x in m.group(1).split(",")]
        return {i: n for i, n in enumerate(items) if n}
    names, in_block, next_idx = {}, False, 0
    for line in text.splitlines():
        if re.match(r"^names:\s*$", line):
            in_block = True
            continue
        if in_block:
            m = re.match(r"^\s+(\d+)\s*:\s*(.+?)\s*$", line)
            if m:
                names[int(m.group(1))] = m.group(2).strip("'\"")
                continue
            m = re.match(r"^\s+-\s*(.+?)\s*$", line)
            if m:
                names[next_idx] = m.group(1).strip("'\"")
                next_idx += 1
                continue
            if line.strip():
                break
    return names


def parse_yolo_line(line):
    """(class_id, cx, cy, w, h) normalized, or None. Detection (5 numbers) and
    segmentation (1 + 2N numbers -> reduced to the bounding box), verbatim from
    datasets/convert_to_manifest.py."""
    parts = line.strip().split()
    if len(parts) < 5:
        return None
    try:
        cls_id = int(parts[0])
        nums = list(map(float, parts[1:]))
    except ValueError:
        return None
    if len(nums) == 4:
        cx, cy, w, h = nums
    elif len(nums) >= 6 and len(nums) % 2 == 0:
        xs, ys = nums[0::2], nums[1::2]
        x_min, x_max, y_min, y_max = min(xs), max(xs), min(ys), max(ys)
        cx, cy = (x_min + x_max) / 2, (y_min + y_max) / 2
        w, h = x_max - x_min, y_max - y_min
    else:
        return None
    return cls_id, cx, cy, w, h


def _pairs_for_split(img_dir, lbl_dir):
    """{stem: (img_path, lbl_path)} for images that have a label file."""
    imgs = {}
    for p in img_dir.iterdir():
        if p.is_file() and p.suffix.lower() in IMG_EXTS:
            imgs[p.stem] = p
    pairs, orphan_lbls = {}, 0
    for lbl in lbl_dir.glob("*.txt"):
        if lbl.stem in imgs:
            pairs[lbl.stem] = (imgs[lbl.stem], lbl)
        else:
            orphan_lbls += 1
    return imgs, pairs, orphan_lbls


def analyze(folder):
    """Local, fail-fast dataset report. No AWS calls, no image decoding.
    Returns a JSON-safe dict the UI renders directly: layout, per-split
    counts, per-class stats (with per-split image counts so the UI can plan
    the train/test division), warnings, and hard blockers."""
    if Image is None:
        return {"ok": False,
                "error": "Pillow is not installed in this build — reinstall ARGUS."}
    root = Path(folder or "")
    if not root.is_dir():
        return {"ok": False, "error": "Folder not found."}
    layout, splits = _find_splits(root)
    if not splits:
        return {"ok": False, "error":
                "No YOLO dataset layout recognized here. Expected images/ + "
                "labels/ folders (YOLOv8) or train/images + train/labels "
                "(Roboflow export)."}

    names = _class_names(root)
    classes = {}   # id -> {"boxes": n, "images": {split: set(stems)}}
    per_split = {}
    warnings, blockers = [], []
    orphan_total = unlabeled_imgs = malformed = zero_box = 0
    box_total = out_of_range = 0

    for s, (img_dir, lbl_dir) in splits.items():
        imgs, pairs, orphans = _pairs_for_split(img_dir, lbl_dir)
        orphan_total += orphans
        unlabeled_imgs += len(imgs) - len(pairs)
        boxed_here = 0
        for stem, (_ip, lbl) in pairs.items():
            try:
                lines = lbl.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                malformed += 1
                continue
            file_classes = set()
            for line in lines:
                parsed = parse_yolo_line(line)
                if parsed is None:
                    if line.strip():
                        malformed += 1
                    continue
                cid = parsed[0]
                box_total += 1
                # YOLO coords are normalized [0,1]; anything clearly beyond
                # (>1.5 tolerates rounding slop) means pixel-unit labels —
                # they would clamp to zero-size boxes deep in convert, so
                # catch them HERE, in the fail-fast validate step.
                if any(v > 1.5 for v in parsed[1:]):
                    out_of_range += 1
                c = classes.setdefault(cid, {"boxes": 0, "images": {}})
                c["boxes"] += 1
                c["images"].setdefault(s, set()).add(stem)
                file_classes.add(cid)
            if file_classes:
                boxed_here += 1
            else:
                zero_box += 1
        per_split[s] = {"images": len(imgs), "labeled": len(pairs),
                        "boxed": boxed_here}

    class_list = []
    for cid in sorted(classes):
        c = classes[cid]
        class_list.append({
            "id": cid,
            "name": names.get(cid, f"class {cid}"),
            "boxes": c["boxes"],
            "images": sum(len(v) for v in c["images"].values()),
            "splits": {s: len(v) for s, v in c["images"].items()},
        })

    if not class_list:
        blockers.append("No readable YOLO boxes found in any label file.")
    if out_of_range:
        if out_of_range > box_total / 2:
            blockers.append(
                "The label coordinates are not normalized (values far above "
                "1.0) — these look like pixel-unit labels, which YOLO format "
                "does not use. Re-export with normalized coordinates.")
        else:
            warnings.append(f"{out_of_range} box(es) have out-of-range "
                            "coordinates — they will be dropped.")
    if orphan_total:
        warnings.append(f"{orphan_total} label file(s) have no matching image "
                        "— they will be skipped.")
    if unlabeled_imgs:
        warnings.append(f"{unlabeled_imgs} image(s) have no label file — they "
                        "will be skipped (the engine cannot learn from "
                        "unlabeled photos).")
    if zero_box:
        warnings.append(f"{zero_box} labeled image(s) contain no boxes — "
                        "skipped for the same reason.")
    if malformed:
        warnings.append(f"{malformed} unreadable label line(s)/file(s) ignored.")

    return {"ok": True, "path": str(root), "layout": layout,
            "splits": per_split, "classes": class_list,
            "warnings": warnings, "blockers": blockers}


def plan_for_class(report, class_id):
    """Given an analyze() report and a chosen class, decide the train/test
    division and the cost estimate. Pure function — the UI shows this on the
    cost gate; run_training() recomputes the same plan for real."""
    cls = next((c for c in report.get("classes", [])
                if c["id"] == int(class_id)), None)
    if not cls:
        return {"ok": False, "error": "That class is not in the report."}
    by_split = dict(cls.get("splits", {}))
    explicit_test = by_split.pop("test", 0)
    train_n = sum(by_split.values())
    total = train_n + explicit_test
    if explicit_test >= MIN_PER_DATASET:
        test_n = explicit_test
    else:
        # fold any tiny explicit test back in and carve our own
        train_pool = total
        test_n = max(MIN_PER_DATASET, round(TEST_FRACTION * train_pool))
        train_n = train_pool - test_n
    blockers = []
    if train_n < MIN_PER_DATASET or test_n < MIN_PER_DATASET:
        blockers.append(
            f"Not enough images of '{cls['name']}': the engine needs at least "
            f"{MIN_PER_DATASET} training and {MIN_PER_DATASET} test images "
            f"(you have {total} usable in total). Label more photos first.")
    # Uncapped, deliberately: a 10k-image set really does train for a day and
    # the cost gate must say so (reference: 1,148 images -> 3.5 h billable).
    est_hours = max(1.0, round(0.8 + train_n / 400, 1))
    return {"ok": True, "train": train_n, "test": test_n, "total": total,
            "est_hours": est_hours, "est_cost_usd": round(est_hours * 4),
            "blockers": blockers,
            "auto_split": explicit_test < MIN_PER_DATASET}


# =============================================================================
# 2. Convert + stage — YOLO -> Ground Truth entries, oversize images downscaled
# =============================================================================
def _staged_name(stem, suffix, used):
    name = stem + suffix
    k = 2
    while name in used:
        name = f"{stem}_{k}{suffix}"
        k += 1
    used.add(name)
    return name


def _convert(root, splits, class_id, class_name, bucket, s3_prefix, staging,
             exclude_stems=None):
    """Walk every (image, label) pair, keep boxes of class_id, and produce:
       uploads   [(local_path, s3_key), ...]
       manifests {"train": [entry, ...], "test": [entry, ...]}
    Images that need work (oversize or non-JPEG/PNG format) are re-encoded
    into staging/ first; everything else uploads untouched. Box pixel coords
    are computed from the FINAL saved dimensions. exclude_stems = image stems
    already in the engine from PREVIOUS versions — re-picking an old folder
    must not duplicate entries or leak former TRAIN images into TEST."""
    exclude_stems = exclude_stems or set()
    # ---- gather labeled pairs with boxes of the chosen class ----
    items = []            # (source_split, stem, img_path, [(cx,cy,w,h), ...])
    skipped_no_box = skipped_dup = 0
    for s, (img_dir, lbl_dir) in splits.items():
        _imgs, pairs, _orph = _pairs_for_split(img_dir, lbl_dir)
        for stem in sorted(pairs):
            img_path, lbl = pairs[stem]
            if stem in exclude_stems:
                skipped_dup += 1
                continue
            boxes = []
            for line in lbl.read_text(encoding="utf-8",
                                      errors="replace").splitlines():
                parsed = parse_yolo_line(line)
                if parsed and parsed[0] == class_id:
                    boxes.append(parsed[1:])
            if boxes:
                items.append((s, stem, img_path, boxes))
            else:
                skipped_no_box += 1
    if skipped_dup:
        _log(f"convert: skipped {skipped_dup} image(s) already in the engine "
             "from an earlier training version")
    if skipped_dup and not items:
        raise TrainError(
            "Every image in this folder is already in the engine from an "
            "earlier training run. Pick a folder of NEW photos.")
    if skipped_no_box:
        _log(f"convert: skipped {skipped_no_box} image(s) without "
             f"'{class_name}' boxes")

    # ---- final split assignment (deterministic, mirrors plan_for_class) ----
    explicit_test = [it for it in items if it[0] == "test"]
    rest = [it for it in items if it[0] != "test"]
    if len(explicit_test) >= MIN_PER_DATASET:
        assign = {"test": explicit_test, "train": rest}
    else:
        pool = sorted(items, key=lambda it: (it[0], it[1]))
        test_n = max(MIN_PER_DATASET, round(TEST_FRACTION * len(pool)))
        if len(pool) - test_n < MIN_PER_DATASET:
            raise TrainError(
                f"Not enough usable images ({len(pool)}) — the engine needs "
                f"at least {MIN_PER_DATASET} training and {MIN_PER_DATASET} "
                "test images.")
        idx = {round(i * len(pool) / test_n) for i in range(test_n)}
        idx = sorted(min(j, len(pool) - 1) for j in idx)
        test_set = set(idx)
        assign = {"test": [pool[i] for i in sorted(test_set)],
                  "train": [pool[i] for i in range(len(pool))
                            if i not in test_set]}
        _log(f"convert: no test split supplied — carved "
             f"{len(assign['test'])} of {len(pool)} images for testing")

    # ---- per image: dims (re-encoding if needed) + pixel boxes + GT entry ----
    uploads, manifests = [], {"train": [], "test": []}
    ts = _now_iso()
    used = {"train": set(), "test": set()}
    bad_images = 0
    total = sum(len(v) for v in assign.values())
    done = 0
    for final_split, group in assign.items():
        for _s, stem, img_path, boxes in group:
            done += 1
            if done % 25 == 0 or done == total:
                _emit("train_progress", phase="convert", done=done, total=total)
            try:
                with Image.open(img_path) as im:
                    im.load()
                    w, h = im.size
                    needs_work = (max(w, h) > MAX_EDGE
                                  or img_path.suffix.lower() not in NATIVE_EXTS)
                    if needs_work:
                        out_dir = staging / final_split
                        out_dir.mkdir(parents=True, exist_ok=True)
                        name = _staged_name(stem, ".jpg", used[final_split])
                        dst = out_dir / name
                        im = im.convert("RGB")
                        if max(w, h) > MAX_EDGE:
                            sc = MAX_EDGE / max(w, h)
                            w, h = int(round(w * sc)), int(round(h * sc))
                            im = im.resize((w, h), Image.LANCZOS)
                        im.save(dst, "JPEG", quality=90)
                        local = dst
                    else:
                        name = _staged_name(stem, img_path.suffix.lower(),
                                            used[final_split])
                        local = img_path
            except Exception as e:
                bad_images += 1
                _log(f"convert: unreadable image skipped: {img_path.name} "
                     f"({str(e)[:60]})")
                continue
            annotations = []
            for cx, cy, bw, bh in boxes:
                x0 = min(max(cx - bw / 2, 0.0), 1.0)
                y0 = min(max(cy - bh / 2, 0.0), 1.0)
                x1 = min(max(cx + bw / 2, 0.0), 1.0)
                y1 = min(max(cy + bh / 2, 0.0), 1.0)
                left, top = int(round(x0 * w)), int(round(y0 * h))
                bw_px = int(round((x1 - x0) * w))
                bh_px = int(round((y1 - y0) * h))
                if bw_px >= 1 and bh_px >= 1:
                    annotations.append({"class_id": 0, "top": top, "left": left,
                                        "width": bw_px, "height": bh_px})
            if not annotations:
                bad_images += 1
                continue
            key = f"{s3_prefix}/{final_split}/images/{name}"
            uploads.append((local, key))
            manifests[final_split].append({
                "source-ref": f"s3://{bucket}/{key}",
                "bounding-box": {
                    "image_size": [{"width": w, "height": h, "depth": 3}],
                    "annotations": annotations,
                },
                "bounding-box-metadata": {
                    "objects": [{"confidence": 1.0} for _ in annotations],
                    "class-map": {"0": class_name},
                    "type": "groundtruth/object-detection",
                    "human-annotated": "yes",
                    "creation-date": ts,
                    "job-name": "argus-training",
                },
            })
    if bad_images:
        _log(f"convert: {bad_images} image(s) dropped (unreadable or "
             "degenerate boxes)")
    for split in ("train", "test"):
        if len(manifests[split]) < MIN_PER_DATASET:
            raise TrainError(
                f"After conversion only {len(manifests[split])} usable "
                f"{split} images remain — the engine needs at least "
                f"{MIN_PER_DATASET}. Check the warnings above.")
    return uploads, manifests


# =============================================================================
# 3. S3 — Rekognition read grant + image/manifest upload
# =============================================================================
_POLICY_SIDS = ("ArgusRekognitionBucketRead", "ArgusRekognitionObjectRead",
                "ArgusRekognitionOutputWrite")


def _grant_rekognition_access(ctx, bucket):
    """Bucket policy so the Rekognition SERVICE can read training images and
    write evaluation output (mirrors the policy the CL console generates).
    Idempotent: our Sids are replaced, other statements preserved."""
    s3 = ctx.client("s3")
    try:
        pol = json.loads(s3.get_bucket_policy(Bucket=bucket)["Policy"])
    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Code"] not in ("NoSuchBucketPolicy",):
            raise
        pol = {"Version": "2012-10-17", "Statement": []}
    stmts = [st for st in pol.get("Statement", [])
             if st.get("Sid") not in _POLICY_SIDS]
    stmts += [
        {"Sid": _POLICY_SIDS[0], "Effect": "Allow",
         "Principal": {"Service": "rekognition.amazonaws.com"},
         "Action": ["s3:GetBucketAcl", "s3:GetBucketLocation"],
         "Resource": f"arn:aws:s3:::{bucket}"},
        {"Sid": _POLICY_SIDS[1], "Effect": "Allow",
         "Principal": {"Service": "rekognition.amazonaws.com"},
         "Action": ["s3:GetObject", "s3:GetObjectAcl", "s3:GetObjectVersion",
                    "s3:GetObjectTagging"],
         "Resource": f"arn:aws:s3:::{bucket}/training-data/*"},
        {"Sid": _POLICY_SIDS[2], "Effect": "Allow",
         "Principal": {"Service": "rekognition.amazonaws.com"},
         "Action": "s3:PutObject",
         "Resource": f"arn:aws:s3:::{bucket}/training-output/*",
         "Condition": {"StringEquals":
                       {"s3:x-amz-acl": "bucket-owner-full-control"}}},
    ]
    pol["Statement"] = stmts
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(pol))
    _log("s3: vision engine granted read access to training data")


def _upload(ctx, bucket, uploads, manifests, manifest_keys):
    s3 = ctx.client("s3")
    total = len(uploads)
    for i, (local, key) in enumerate(uploads):
        s3.upload_file(str(local), bucket, key)
        if (i + 1) % 20 == 0 or i + 1 == total:
            _emit("train_progress", phase="upload", done=i + 1, total=total)
    for split, key in manifest_keys.items():
        body = "\n".join(json.dumps(e) for e in manifests[split]) + "\n"
        s3.put_object(Bucket=bucket, Key=key, Body=body.encode("utf-8"),
                      ContentType="application/json")
    _log(f"upload: {total} images + 2 manifests in s3://{bucket}")


# =============================================================================
# 4. Rekognition datasets — create from manifest, or append on iteration
# =============================================================================
def _project_datasets(rek, project_arn):
    for p in rek.describe_projects().get("ProjectDescriptions", []):
        if p["ProjectArn"] == project_arn:
            return {d["DatasetType"]: d["DatasetArn"]
                    for d in p.get("Datasets", [])}
    raise TrainError("The Rekognition project is missing — run a system "
                     "check, then Resume deployment to rebuild it.")


def _wait_dataset(rek, dataset_arn, want_prefix, timeout=900, after_ts=None):
    """Poll to the terminal status. after_ts (epoch seconds): ignore a stale
    terminal status recorded BEFORE our own submission — without it, a
    multi-chunk append could read the previous chunk's UPDATE_COMPLETE and
    fire the next chunk into a dataset still ingesting."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = rek.describe_dataset(DatasetArn=dataset_arn)["DatasetDescription"]
        st = d["Status"]
        fresh = True
        if after_ts is not None:
            lu = d.get("LastUpdatedTimestamp")
            if lu is not None and lu.timestamp() < after_ts - 5:
                fresh = False
        if st == f"{want_prefix}_COMPLETE" and fresh:
            return d
        if st.endswith("_FAILED") and fresh:
            raise TrainError(f"dataset {want_prefix.lower()} failed: "
                             f"{d.get('StatusMessage', st)[:200]}")
        time.sleep(8)
    raise TrainError(f"dataset {want_prefix.lower()} timed out")


def _load_datasets(ctx, rek, project_arn, bucket, manifests, manifest_keys):
    existing = _project_datasets(rek, project_arn)
    for split, ds_type in (("train", "TRAIN"), ("test", "TEST")):
        entries = manifests[split]
        if ds_type not in existing:
            arn = rek.create_dataset(
                ProjectArn=project_arn, DatasetType=ds_type,
                DatasetSource={"GroundTruthManifest": {"S3Object": {
                    "Bucket": bucket, "Name": manifest_keys[split]}}},
            )["DatasetArn"]
            _log(f"datasets: creating {ds_type} ({len(entries)} entries)")
            d = _wait_dataset(rek, arn, "CREATE")
        else:
            arn = existing[ds_type]
            _log(f"datasets: appending {len(entries)} entries to {ds_type}")
            chunk, size = [], 0
            flush_batches = []
            for e in entries:
                line = json.dumps(e)
                if size + len(line) + 1 > UPDATE_CHUNK_BYTES and chunk:
                    flush_batches.append(chunk)
                    chunk, size = [], 0
                chunk.append(line)
                size += len(line) + 1
            if chunk:
                flush_batches.append(chunk)
            for batch in flush_batches:
                blob = ("\n".join(batch) + "\n").encode("utf-8")
                t_submit = time.time()
                rek.update_dataset_entries(DatasetArn=arn,
                                           Changes={"GroundTruth": blob})
                d = _wait_dataset(rek, arn, "UPDATE", after_ts=t_submit)
        stats = d.get("DatasetStats", {})
        errs = stats.get("ErrorEntries") or 0
        if errs:
            _log(f"datasets: {ds_type} reports {errs} error entries — the "
                 "engine skips those images at training", ok=False)
        _log(f"datasets: {ds_type} ready — "
             f"{stats.get('LabeledEntries', '?')} labeled entries")


# =============================================================================
# 5. Train + watch + wire-in
# =============================================================================
def _next_version_number(ctx, rek, project_arn):
    """train:next_n is persisted at RUN START (not on success), so a retry
    after any failure re-uses the same n — same S3 keys, so re-uploaded
    images REPLACE their earlier dataset entries instead of duplicating
    them (update_dataset_entries matches on source-ref)."""
    n = int(ctx.state.get("train:next_n") or 0)
    if n < 1:
        vers = rek.describe_project_versions(
            ProjectArn=project_arn).get("ProjectVersionDescriptions", [])
        n = len(vers) + 1
    return n


def _existing_stems(rek, project_arn, bucket, current_prefix):
    """Image-name stems already present in the project's datasets, EXCLUDING
    entries under current_prefix (a retry of the same version must replace
    its own earlier upload, not be deduped against it). Best-effort: a
    listing failure returns what was gathered so far."""
    stems = set()
    own = f"s3://{bucket}/{current_prefix}/"
    for arn in _project_datasets(rek, project_arn).values():
        token = None
        while True:
            kwargs = {"DatasetArn": arn, "MaxResults": 100}
            if token:
                kwargs["NextToken"] = token
            try:
                resp = rek.list_dataset_entries(**kwargs)
            except botocore.exceptions.ClientError:
                return stems
            for line in resp.get("DatasetEntries", []):
                try:
                    ref = json.loads(line).get("source-ref", "")
                except Exception:
                    continue
                if ref.startswith(own):
                    continue
                name = ref.rsplit("/", 1)[-1]
                stems.add(name.rsplit(".", 1)[0])
            token = resp.get("NextToken")
            if not token:
                break
    return stems


def _start_version(ctx, rek, project_arn, n, bucket):
    version_name = f"v{n}-{time.strftime('%Y%m%d-%H%M', time.gmtime())}"
    try:
        resp = rek.create_project_version(
            ProjectArn=project_arn, VersionName=version_name,
            OutputConfig={"S3Bucket": bucket,
                          "S3KeyPrefix": f"training-output/v{n}"})
    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Code"] == "LimitExceededException":
            raise TrainError(
                "This project has hit AWS's model-version limit. Delete old "
                "versions in the Rekognition console, then train again.")
        raise
    arn = resp["ProjectVersionArn"]
    # the local-attempt stamp hands over to the cloud-side pending record
    ctx.state.pop("train:attempt", None)
    ctx.save("train:pending", {"version_name": version_name,
                               "version_arn": arn, "n": n,
                               "started_at": _now_iso()})
    _log(f"train: {version_name} submitted — typically 1-4 hours; you can "
         "close this app, training continues in AWS")
    _emit("train_started", version=version_name)
    return version_name, arn


def watch_and_wire(ctx, poll_seconds=60):
    """Poll the pending training run to completion, then wire the model in.
    Restartable: everything it needs lives in deploy state + AWS, so a
    relaunched app can re-attach after a crash or an overnight run."""
    pending = ctx.state.get("train:pending")
    if not pending:
        return {"ok": False, "error": "No training run is in progress."}
    project_arn = ctx.state.get("rekognition:project")
    rek = ctx.client("rekognition")
    _phase("train", "running")
    last = None
    consec_fail = 0
    while True:
        # A 1-4 h poll loop WILL see transient network/API errors; one blip
        # must not be reported as a training failure. train:pending survives
        # a give-up, so the Train screen can always re-attach.
        try:
            descs = rek.describe_project_versions(
                ProjectArn=project_arn,
                VersionNames=[pending["version_name"]]
            ).get("ProjectVersionDescriptions", [])
        except Exception as e:
            consec_fail += 1
            if consec_fail >= 30:
                raise TrainError(
                    "Lost contact with AWS for ~30 minutes while watching "
                    "training. Training itself continues in the cloud — "
                    "reopen the Train screen to re-attach.")
            if consec_fail == 1:
                _log(f"watch: transient AWS error, retrying "
                     f"({str(e)[:80]})", ok=False)
            time.sleep(poll_seconds)
            continue
        consec_fail = 0
        if not descs:
            raise TrainError("The training run vanished from the project — "
                             "it may have been deleted in the console.")
        d = descs[0]
        st = d["Status"]
        elapsed = 0
        if d.get("CreationTimestamp"):
            elapsed = int(time.time() - d["CreationTimestamp"].timestamp())
        _emit("train_status", status=st, elapsed=elapsed,
              version=pending["version_name"])
        if st != last:
            _log(f"engine: {st}")
            last = st
        if st in TRAINED_OK_STATUSES:
            _phase("train", "done")
            f1 = d.get("EvaluationResult", {}).get("F1Score")
            billable = d.get("BillableTrainingTimeInSeconds")
            return _wire_in(ctx, pending, f1, billable)
        if st in TRAIN_DEAD_STATUSES:
            ctx.state.pop("train:pending", None)
            ctx.save("train:last_failure",
                     {"version": pending["version_name"], "status": st,
                      "at": _now_iso()})
            raise TrainError(f"Training ended {st}: "
                             f"{d.get('StatusMessage', 'no detail')[:300]}")
        time.sleep(poll_seconds)


def _wire_in(ctx, pending, f1, billable):
    """THE critical step ('训出来了也是白搭' otherwise): point every custom
    camera row at the new model ARN. boto3 only — ARNs contain colons that
    shell quoting mangles (aws.md)."""
    _phase("wire", "running")
    arn = pending["version_arn"]
    prev = ctx.state.get("train:active_arn") or ""
    table = ctx.session.resource("dynamodb").Table(CAMERAS_TABLE)
    wired = 0
    scan_kwargs = {}
    while True:
        page = table.scan(**scan_kwargs)
        for row in page.get("Items", []):
            if row.get("model_type") == "custom":
                table.update_item(
                    Key={"camera_id": row["camera_id"]},
                    UpdateExpression="SET custom_model_arn = :a",
                    ExpressionAttributeValues={":a": arn})
                wired += 1
        if "LastEvaluatedKey" not in page:
            break
        scan_kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    ctx.state.pop("train:pending", None)
    ctx.state.pop("train:last_failure", None)   # a success clears old news
    ctx.state[f"train:v{pending['n']}"] = {
        "arn": arn, "f1": f1, "billable_s": billable,
        "version_name": pending["version_name"], "completed_at": _now_iso()}
    ctx.state["train:rollback_arn"] = prev
    ctx.state["train:active_f1"] = f1
    ctx.state["train:next_n"] = pending["n"] + 1
    ctx.save("train:active_arn", arn)   # save() persists the whole state dict
    if wired:
        _log(f"wire: custom_model_arn set on {wired} camera row(s)", ok=True)
    else:
        _log("wire: NO camera row has model_type='custom' — the model is "
             "trained but not attached to any camera", ok=False)
    _phase("wire", "done")
    _emit("train_done", version=pending["version_name"], f1=f1, arn=arn,
          wired=wired, billable_s=billable, rollback=bool(prev))
    return {"ok": True, "f1": f1, "arn": arn, "wired": wired}


def rollback(ctx):
    """One-click rollback: swap the camera rows back to the previous model.
    Codifies the v6->v5 hand-rollback of 2026-07-15."""
    prev = ctx.state.get("train:rollback_arn")
    cur = ctx.state.get("train:active_arn")
    if not prev:
        return {"ok": False, "error": "No previous model to roll back to."}
    table = ctx.session.resource("dynamodb").Table(CAMERAS_TABLE)
    wired = 0
    scan_kwargs = {}
    while True:
        page = table.scan(**scan_kwargs)
        for row in page.get("Items", []):
            if row.get("model_type") == "custom":
                table.update_item(
                    Key={"camera_id": row["camera_id"]},
                    UpdateExpression="SET custom_model_arn = :a",
                    ExpressionAttributeValues={":a": prev})
                wired += 1
        if "LastEvaluatedKey" not in page:
            break
        scan_kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    # recover the restored model's F1 from its version record, best-effort
    f1 = None
    for k, v in ctx.state.items():
        if k.startswith("train:v") and isinstance(v, dict) \
                and v.get("arn") == prev:
            f1 = v.get("f1")
    ctx.state["train:rollback_arn"] = cur
    ctx.state["train:active_f1"] = f1
    ctx.save("train:active_arn", prev)
    _log(f"rollback: {wired} camera row(s) restored to the previous model",
         ok=bool(wired))
    return {"ok": True, "wired": wired, "arn": prev, "f1": f1}


def status(ctx):
    """One-call summary for the UI. Makes a single describe call only when a
    run is pending; otherwise answers from local state."""
    out = {"ok": True,
           "active_arn": ctx.state.get("train:active_arn") or "",
           "active_f1": ctx.state.get("train:active_f1"),
           "rollback": bool(ctx.state.get("train:rollback_arn")),
           "last_failure": ctx.state.get("train:last_failure"),
           "aborted_attempt": bool(ctx.state.get("train:attempt"))}
    pending = ctx.state.get("train:pending")
    if not pending:
        out["status"] = "trained" if out["active_arn"] else "none"
        return out
    try:
        descs = ctx.client("rekognition").describe_project_versions(
            ProjectArn=ctx.state.get("rekognition:project"),
            VersionNames=[pending["version_name"]]
        ).get("ProjectVersionDescriptions", [])
    except Exception as e:
        out.update(status="unknown", error=str(e)[:140],
                   version=pending["version_name"])
        return out
    if not descs:
        # submitted but vanished — clear so the UI doesn't wedge
        ctx.state.pop("train:pending", None)
        ctx.save("train:last_failure", {"version": pending["version_name"],
                                        "status": "MISSING", "at": _now_iso()})
        out["status"] = "trained" if out["active_arn"] else "none"
        return out
    st = descs[0]["Status"]
    elapsed = 0
    if descs[0].get("CreationTimestamp"):
        elapsed = int(time.time() - descs[0]["CreationTimestamp"].timestamp())
    if st in TRAINED_OK_STATUSES:
        # finished while the app was closed; watch_and_wire() completes instantly
        out.update(status="needs_wire", version=pending["version_name"],
                   elapsed=elapsed)
    elif st in TRAIN_DEAD_STATUSES:
        ctx.state.pop("train:pending", None)
        ctx.save("train:last_failure", {"version": pending["version_name"],
                                        "status": st, "at": _now_iso()})
        out.update(status="trained" if out["active_arn"] else "none",
                   failed=st)
    else:
        out.update(status="in_progress", version=pending["version_name"],
                   elapsed=elapsed, engine_status=st)
    return out


# =============================================================================
# Orchestrator — the whole pipeline, blocking; run it in a background thread
# =============================================================================
def run_training(ctx, folder, class_id, class_name=None):
    """analyze -> convert -> upload -> datasets -> train -> watch -> wire.
    Raises TrainError for expected failures (the app surfaces the message)."""
    if Image is None:
        raise TrainError("Pillow is not installed in this build.")
    if ctx.state.get("train:pending"):
        raise TrainError("A training run is already in progress.")
    project_arn = ctx.state.get("rekognition:project")
    bucket = ctx.state.get("bucket:frames") or ctx.frames_bucket
    if not project_arn:
        raise TrainError("No deployment on file — deploy the stack first.")
    class_id = int(class_id)

    _phase("validate", "running")
    report = analyze(folder)
    if not report.get("ok"):
        raise TrainError(report.get("error", "Could not read that folder."))
    if report["blockers"]:
        raise TrainError(" ".join(report["blockers"]))
    cls = next((c for c in report["classes"] if c["id"] == class_id), None)
    if not cls:
        raise TrainError(f"Class id {class_id} has no boxes in this folder.")
    class_name = (class_name or cls["name"] or "target").strip() or "target"
    # class-map names feed Rekognition label names — keep them tame
    class_name = re.sub(r"[^A-Za-z0-9_.\- ]", "-", class_name)[:64]
    for wmsg in report["warnings"]:
        _log(f"validate: {wmsg}")
    _phase("validate", "done")

    rek = ctx.client("rekognition")
    n = _next_version_number(ctx, rek, project_arn)
    # Persist n NOW: a retry after ANY failure re-uses it, so the same images
    # land on the same S3 keys and REPLACE their dataset entries instead of
    # duplicating them. The attempt stamp lets a relaunched app say "an
    # earlier attempt was interrupted" if we get hard-killed before submit.
    ctx.state["train:attempt"] = {"n": n, "at": _now_iso()}
    ctx.save("train:next_n", n)
    s3_prefix = f"training-data/v{n}"
    manifest_keys = {s: f"training-data/manifests/v{n}/{s}.manifest"
                     for s in ("train", "test")}
    staging = deploy.OUT / "train_staging" / f"v{n}"

    try:
        _phase("convert", "running")
        shutil.rmtree(staging, ignore_errors=True)   # stale earlier attempt
        exclude = _existing_stems(rek, project_arn, bucket, s3_prefix)
        _, splits = _find_splits(folder)
        uploads, manifests = _convert(Path(folder), splits, class_id,
                                      class_name, bucket, s3_prefix, staging,
                                      exclude_stems=exclude)
        _log(f"convert: {len(manifests['train'])} train / "
             f"{len(manifests['test'])} test images ready")
        _phase("convert", "done")

        _phase("upload", "running")
        _grant_rekognition_access(ctx, bucket)
        _upload(ctx, bucket, uploads, manifests, manifest_keys)
        shutil.rmtree(staging, ignore_errors=True)   # staged copies now in S3
        _phase("upload", "done")

        _phase("datasets", "running")
        _load_datasets(ctx, rek, project_arn, bucket, manifests, manifest_keys)
        _phase("datasets", "done")

        _start_version(ctx, rek, project_arn, n, bucket)
    except Exception as e:
        # pre-submit failure: nothing is running in the cloud. Record it so
        # the next visit to the Train screen can say so honestly.
        ctx.state.pop("train:attempt", None)
        ctx.save("train:last_failure",
                 {"status": "INCOMPLETE", "message": str(e)[:300],
                  "at": _now_iso()})
        raise
    return watch_and_wire(ctx)
