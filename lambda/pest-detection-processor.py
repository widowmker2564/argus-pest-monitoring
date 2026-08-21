"""
=============================================================================
Lambda: pest-detection-processor  (v6.4 - tiling + FP suppression + LLM
                                    denoiser gate + post-gate cleanup +
                                    passthrough cameras)
=============================================================================
Lineage:
  v4.0  nbk2 migration (env vars, new schema, per-camera config, separate
        processed bucket, WebSocket removed).
  v4.1  de-processed-image refactor: stopped generating annotated images; the
        dashboard draws boxes on a client canvas from the stored `bboxes`.
  v4.2  cloud-side tiling/crop for small-target recall.
  v4.3  application-layer NON-VEGETATION FP suppression: after the custom model
        produces boxes, run DetectLabels and drop any box sitting on a hard
        object (person/vehicle/furniture/machinery). Rekognition CL object
        detection CANNOT be taught negatives (proven 2026-07-13, see
        docs/detection.md), so foliage-vs-hard-object discrimination is done
        here at the application layer. Never suppresses on plant labels.
  v4.4  LLM CROP VERIFICATION: each surviving custom box was cropped, upscaled,
        and sent to a Bedrock multimodal model which answered "is this
        actually a larva?". Shipped annotate-only (verdict recorded, never
        removed a box) while an A/B experiment ran.
  v4.5  HYBRID CONFIDENCE GATE (THIS FILE, Runzhe's call 2026-07-22 after
        reviewing the v4.4 A/B data): Rekognition and the LLM now SHARE
        authority instead of the LLM being a pure annotator. A target-label
        box at/above the camera's min_confidence is trusted outright -
        Rekognition's own score is the authority and the LLM is not even
        consulted. A box BELOW min_confidence is adjudicated by the LLM: an
        explicit "not a larva" verdict drops it, anything else (a positive
        verdict, or no verdict at all) keeps it. `min_confidence` therefore
        changed meaning: it used to be "the floor to count as a detection at
        all"; it is now "the point above which Rekognition's word is final".
        `LLM_VERIFY_DROP` is gone - dropping a rejected sub-threshold box is
        now the only behaviour, there is no annotate-only mode left to gate.
  v4.6  WHOLE-FRAME LLM SCAN (THIS FILE, Runzhe's call 2026-07-22): one extra
        Haiku pass over the entire downscaled frame names grid cells that
        contain a visible larva. Its POSITIVE sightings carry authority: a
        positive cell with no Rekognition box becomes a recovery candidate
        (crop-confirmed before it may alert - the class of miss Rekognition
        returns nothing for, e.g. light-coloured worms), and a sub-threshold
        box inside a positive cell is kept without a crop call. Its SILENCE
        carries none: a scan miss alone never deletes (the 2026-07-21 A/B
        measured whole-frame Haiku missing 8/14 larvae a zoomed crop caught),
        so the crop verdict remains the only path that drops a box. The
        authority rule is unchanged: at/above the camera's min_confidence,
        Rekognition's word is final and no model is consulted.
  v4.7  LLM DENOISER MODE (env LLM_VERIFY_ALL_BOXES, Runzhe's call 2026-07-23).
        Reverses the v4.5 authority rule ON PURPOSE, with evidence: v7.2 showed
        that with TILING ON, Rekognition recall is near-total but the noise
        boxes (leaf/shadow/soil/flower) come back at HIGH confidence too -
        tiling maximises recall but destroys Rekognition's own denoising. So
        the strategy flips: keep tiling for recall, and let the LLM judge EVERY
        box (no min_confidence exemption) as a pure denoiser that may delete
        even a high-confidence box. Whole-frame scan becomes redundant (every
        box is already judged) and is turned off (LLM_SCAN=false). Runtime-only
        via env - the v4.5/4.6 authority behaviour is still the default
        (LLM_VERIFY_ALL_BOXES=false). Known risk under test: a confident model
        reject deletes a real worm; the recall-biased prompt ("if unsure, keep")
        is the guard. Model is swappable via LLM_VERIFY_MODEL_ID (Runzhe will
        arena-screen a stronger multimodal model separately; this validates the
        ARCHITECTURE on Haiku, one variable at a time).
  v6.3  DEAD-CODE STRIP (2026-08-07, Runzhe's pre-migration order): removed
        every experimental path production does not run - whole-frame scan
        (v4.6), cluster-merge gate (v4.8), LLM-FIRST (v5.1), LLM-LEAD (v5.2),
        LLM-PLAIN (v5.6), LLM-AGENT (v5.7), picture-in-picture composite
        (v6.1) and the '__rek-' detector override (v5.8). ~1.6k lines gone;
        the live path is unchanged: tiling -> v4.3 suppression -> LLM
        denoiser gate (LLM_VERIFY_ALL_BOXES) -> post-gate cleanup. The
        '__confN' and '__llm-' key overrides stay (the dashboard Test upload
        uses both). Full pre-strip source: lambda/archive/
        pest-detection-processor_v6.2_full.py.
  v6.4  PASSTHROUGH CAMERAS (2026-08-21): a camera row carrying
        detect_enabled=false writes its DynamoDB record and returns
        immediately - no Rekognition, no tiling, no Bedrock, no SES, no EXIF
        download. Added for the navigation handover demo, which needs
        photos on the dashboard and no detection at all. The flag defaults
        to TRUE, so every camera that predates it behaves exactly as before.
        See the block at "--- 2a. PASSTHROUGH CAMERAS" for why the routing
        cannot be done with a second S3 notification instead.

v4.5 hybrid gate (per-camera opt-in via cam_config.llm_verify_enabled, on TOP
of the global LLM_VERIFY switch - mirrors the v4.2 tiling_enabled pattern):
  - Runs AFTER Rekognition and AFTER v4.3 suppression. Rekognition stays the
    box generator; the LLM only judges regions that are already cropped and
    zoomed, which is the task multimodal models are good at. It is never
    asked to find targets in a wide frame, which is the task they are bad at
    (measured 2026-07-21: a whole-image call missed 8 of 14 clean CAG holdout
    larvae, including one plainly visible on bare concrete - Claude Haiku 4.5
    is Bedrock's standard-resolution tier and cannot resolve a sub-1%-of-frame
    target in an unscaled wide photo. See docs/detection.md).
  - PER-CAMERA OPT-IN IS NOT OPTIONAL SAFETY THEATRE - IT PREVENTS A REAL
    CROSS-CAMERA BUG: this Lambda is shared by every custom-model camera.
    `moth_cam` is also model_type=custom (target_label "Moths", i.e. adult
    moths) but the LLM prompt asks "is this a caterpillar or moth LARVA".
    Applying the gate globally to every custom camera would have the LLM
    silently reject adult-moth detections using a prompt for the wrong life
    stage. Only cameras with `llm_verify_enabled: true` in DynamoDB run the
    gate; `worm_cam` has it, `moth_cam` does not and is untouched by any of
    this file's v4.4/v4.5 code.
  - Fails OPEN at every level: no Bedrock permission, model error, malformed
    reply, or unparseable JSON all leave a sub-threshold box KEPT (unverified
    never removes a box - only an explicit negative verdict does).
  - Sub-threshold candidates are capped at LLM_VERIFY_MAX_BOXES per frame
    (highest Rekognition confidence first) and judged in parallel, so cost
    and added latency stay bounded. Boxes at/above min_confidence are never
    sent to Bedrock at all - their fate is already decided, so verifying them
    would only spend money and latency with no possible effect on the outcome.
  - Requires IAM `bedrock:InvokeModel` on pest-detection-processor-policy and
    Bedrock model access enabled for LLM_VERIFY_MODEL_ID in us-east-1.

v4.2 tiling (per-camera opt-in via cam_config.tiling_enabled):
  - For a wide 1920x1080 frame, slice into a cols x rows grid WITH overlap,
    upscale each tile (~4x, the W6-validated crop sweet spot) and run
    detect_custom_labels on each tile's bytes. An optional full-frame pass
    catches large/obvious targets. Tile detections are converted back to GLOBAL
    normalized coords and de-duplicated with NMS (overlap creates duplicates).
  - Tiling produces a list shaped EXACTLY like Rekognition CustomLabels
    (Name, Confidence, Geometry.BoundingBox), so all downstream logic
    (target_detected, bboxes, SES) is unchanged.
  - Tiling is gated to custom models that opt in; moth/general are untouched.
    Any tiling error falls back to a single S3Object detect call.
  - Tuning via env vars (TILE_COLS/TILE_ROWS/TILE_OVERLAP/TILE_UPSCALE_LONG_EDGE
    /TILE_INCLUDE_FULL_FRAME/TILE_MIN_CONFIDENCE/TILE_NMS_IOU/TILE_MAX_WORKERS).
  - Requires the PIL layer (fyp-pillow) re-attached, s3:GetObject on the frames
    bucket (already granted), and ideally 512 MB / 180 s timeout.

Trigger: S3 PutObject on frames-armyworm-{account-id}
Runtime: Python 3.12+, 512 MB recommended, 180 s timeout, PIL layer required
Role:    pest-detection-processor-role
=============================================================================
"""
import json
import os
import re
import threading
import time
import boto3
import urllib.parse
from datetime import datetime, timezone
from io import BytesIO

# -----------------------------------------------------------------------------
# AWS clients & resources (region auto-injected by Lambda runtime)
# -----------------------------------------------------------------------------
AWS_REGION  = os.environ.get("AWS_REGION", "us-east-1")
rekognition = boto3.client("rekognition", region_name=AWS_REGION)
s3_client   = boto3.client("s3", region_name=AWS_REGION)   # tiling downloads the frame
ses         = boto3.client("ses", region_name=AWS_REGION)
dynamodb    = boto3.resource("dynamodb", region_name=AWS_REGION)

# Bedrock client is created lazily on first use so that an account without
# Bedrock access never pays a cold-start cost for a feature it does not run.
_bedrock_runtime = None

# -----------------------------------------------------------------------------
# Configuration via env vars (set in Lambda console after deploy)
# -----------------------------------------------------------------------------
TABLE_DETECTIONS    = os.environ.get("TABLE_DETECTIONS",    "pest-monitoring-detections")
TABLE_CAMERAS       = os.environ.get("TABLE_CAMERAS",       "pest-monitoring-cameras")
TABLE_SYSTEM_CONFIG = os.environ.get("TABLE_SYSTEM_CONFIG", "pest-monitoring-system-config")
SENDER_EMAIL        = os.environ["SENDER_EMAIL"]          # required

# Tiling tuning (global defaults; per-camera opt-in via cam_config.tiling_enabled)
TILING_ENABLED_GLOBAL   = os.environ.get("TILING_ENABLED", "true").lower() == "true"
TILE_COLS               = int(os.environ.get("TILE_COLS", "4"))
TILE_ROWS               = int(os.environ.get("TILE_ROWS", "4"))
TILE_OVERLAP            = float(os.environ.get("TILE_OVERLAP", "0.15"))    # fraction of base tile
TILE_UPSCALE_LONG_EDGE  = int(os.environ.get("TILE_UPSCALE_LONG_EDGE", "1920"))  # zoom knob
TILE_INCLUDE_FULL_FRAME = os.environ.get("TILE_INCLUDE_FULL_FRAME", "true").lower() == "true"
TILE_MIN_CONFIDENCE     = int(os.environ.get("TILE_MIN_CONFIDENCE", "30"))  # per-tile gather floor
TILE_NMS_IOU            = float(os.environ.get("TILE_NMS_IOU", "0.5"))
TILE_MAX_WORKERS        = int(os.environ.get("TILE_MAX_WORKERS", "4"))

# Non-vegetation FP suppression (v4.3; custom models only). A DetectLabels pass
# finds hard-object regions (people/vehicles/furniture/machinery); any custom box
# mostly covered by such a region is dropped as a false positive. Worm boxes on
# plants are never touched (PROTECT list). Env-tunable, off-switchable, and any
# error is non-fatal (detection proceeds unfiltered).
SUPPRESS_ENABLED   = os.environ.get("SUPPRESS_NONVEG", "true").lower() == "true"
SUPPRESS_MIN_CONF  = float(os.environ.get("SUPPRESS_MIN_CONF", "55"))   # DetectLabels floor
SUPPRESS_COVERAGE  = float(os.environ.get("SUPPRESS_COVERAGE", "0.5"))  # frac of worm box inside region
SUPPRESS_LABELS = {s.strip().lower() for s in os.environ.get(
    "SUPPRESS_LABELS",
    "person,human,pedestrian,vehicle,car,truck,jeep,bus,van,motorcycle,bicycle,"
    "wheel,tire,machine,machinery,robot,engine,furniture,chair,couch,table,bench,"
    "desk,cabinet,shelf,appliance,electronics,computer,screen,monitor,tv,"
    "helmet,clothing,shoe,footwear,backpack,handbag,bag,weapon,gun,rifle,"
    "building,wall,floor,railing,fence,sign,road,pavement,brick,metal"
).split(",") if s.strip()}
# Never suppress on these, even if DetectLabels returns a box (worms live on plants).
SUPPRESS_PROTECT = {"plant", "leaf", "leaves", "flower", "flowers", "blossom",
                    "tree", "vegetation", "grass", "foliage", "petal", "bud",
                    "moss", "fern", "herbs", "produce", "soil", "dirt", "ground"}

# LLM hybrid confidence gate (v4.5; per-camera opt-in, see cam_config below).
# A sub-min_confidence candidate box is cropped with padding, upscaled, and
# judged by a Bedrock multimodal model; an explicit rejection drops it.
LLM_VERIFY_ENABLED   = os.environ.get("LLM_VERIFY", "true").lower() == "true"
# Claude Haiku 4.5 on Bedrock is only reachable through a cross-region INFERENCE
# PROFILE (the `us.` prefix). Calling the bare foundation-model id fails with
# "Invocation of model ID ... with on-demand throughput isn't supported".
LLM_VERIFY_MODEL_ID  = os.environ.get("LLM_VERIFY_MODEL_ID",
                                      "us.anthropic.claude-haiku-4-5-20251001-v1:0")

# --- Per-request model routing (v4.9, 2026-07-28) ----------------------------
# An S3 key segment '__llm-<alias>' picks the verification model for THIS run
# only, the same stateless trick the '__confN' override already uses. It lets
# several models run side by side on the same images in one pass instead of
# swapping the Lambda config between arms. Unknown alias -> env default.
LLM_MODEL_ALIASES = {
    "sonnet46": "us.anthropic.claude-sonnet-4-6",
    "haiku45":  "us.anthropic.claude-haiku-4-5-20251001-v1:0",
}
# One-slot holder so the Bedrock call sites stay a plain read. Reset on EVERY
# record - Lambda reuses warm containers and a stale value would silently
# attribute one model's results to another.
_ACTIVE_LLM_MODEL = [LLM_VERIFY_MODEL_ID]


def active_llm_model():
    """Model id for the record being processed right now."""
    return _ACTIVE_LLM_MODEL[0]


def set_active_llm_model(object_key, cam_config=None):
    """Resolve the verification model for THIS record, most specific first:

      1. '__llm-<alias>' in the S3 key   - per-request, what Test upload sends
      2. cam_config['llm_model_id']      - per-camera, the dashboard's model
                                           picker (v6.3); an ALIAS, not a raw
                                           model id, so a typo can never reach
                                           Bedrock as an unknown id
      3. LLM_VERIFY_MODEL_ID env         - account-wide default

    Only aliases in LLM_MODEL_ALIASES are honoured at either level. That is
    deliberate: an unrecognised model id does not fail loudly at the call site,
    it fails as AccessDenied on EVERY crop, which the gate reads as 'no verdict'
    (2026-07-26: a bad id produced 492 AccessDenied calls and 44/44 images
    reporting zero detections). An unknown alias therefore falls through to the
    next level rather than being passed along.
    """
    model_id = LLM_VERIFY_MODEL_ID
    source = "env default"

    cam_alias = str((cam_config or {}).get("llm_model_id", "") or "").strip()
    if cam_alias:
        if cam_alias in LLM_MODEL_ALIASES:
            model_id = LLM_MODEL_ALIASES[cam_alias]
            source = f"camera setting '{cam_alias}'"
        else:
            print(f"[LLMRoute] camera has unknown llm_model_id "
                  f"'{cam_alias}', ignoring it")

    m = re.search(r"__llm-([A-Za-z0-9_.-]+)", object_key or "")
    if m:
        alias = m.group(1)
        if alias in LLM_MODEL_ALIASES:
            model_id = LLM_MODEL_ALIASES[alias]
            source = f"key alias '{alias}'"
        else:
            print(f"[LLMRoute] unknown key alias '{alias}', "
                  f"staying on {model_id}")

    print(f"[LLMRoute] {source} -> {model_id}")
    _ACTIVE_LLM_MODEL[0] = model_id
    return model_id


LLM_VERIFY_MAX_BOXES = int(os.environ.get("LLM_VERIFY_MAX_BOXES", "5"))   # cost/latency cap
LLM_VERIFY_PAD       = float(os.environ.get("LLM_VERIFY_PAD", "0.6"))     # crop padding, frac of box
LLM_VERIFY_LONG_EDGE = int(os.environ.get("LLM_VERIFY_LONG_EDGE", "672")) # crop upscale target
# Padding floor (px) so a tiny box still gets real surrounding context, and an
# upscale cap so a tiny box isn't blown up into pure interpolation noise
# (found 2026-07-21: a 6x4px box with no floor got padded to ~13x9px, then
# upscaled 60x+ to hit LLM_VERIFY_LONG_EDGE - almost no real signal left).
LLM_VERIFY_MIN_CONTEXT_PX = int(os.environ.get("LLM_VERIFY_MIN_CONTEXT_PX", "32"))
LLM_VERIFY_MAX_UPSCALE    = float(os.environ.get("LLM_VERIFY_MAX_UPSCALE", "8.0"))
LLM_VERIFY_WORKERS   = int(os.environ.get("LLM_VERIFY_WORKERS", "4"))
LLM_VERIFY_TIMEOUT   = int(os.environ.get("LLM_VERIFY_TIMEOUT", "12"))    # per-call seconds
# Output token cap for one verdict. Haiku 4.5 answers the JSON in well under 100
# tokens. Thinking-always-on models (Claude Fable 5 / Mythos 5) bill and spend
# reasoning tokens against this same cap, so a 100-token budget is consumed
# entirely by reasoning and the JSON reply never arrives - parse_llm_verdict
# then returns None and the gate fails open on every box, silently doing
# nothing. Raise this alongside LLM_VERIFY_TIMEOUT when pointing at such a model.
LLM_VERIFY_MAX_TOKENS = int(os.environ.get("LLM_VERIFY_MAX_TOKENS", "100"))
# Sampling parameters are REJECTED (400) on Claude Fable 5 / Mythos 5 / Opus 4.7+
# / Sonnet 5, and this task is deterministic enough not to need them. Set
# LLM_VERIFY_TEMPERATURE to a number only for an older model that wants it.
LLM_VERIFY_TEMPERATURE = os.environ.get("LLM_VERIFY_TEMPERATURE", "").strip()
# v4.7 DENOISER MODE (Runzhe 2026-07-23). Default false = the v4.5/4.6 authority
# rule (a box at/above the camera's min_confidence is trusted outright, never
# sent to the model). Set true and the min_confidence exemption is REMOVED:
# EVERY target box is crop-judged and the model may delete even a high-confidence
# box. Rationale: with tiling ON, Rekognition recall is near-total but the noise
# boxes (leaf/shadow/soil/flower) come back at HIGH confidence too, so trusting
# high confidence lets that noise through - the LLM becomes a pure denoiser over
# every box instead of a sub-threshold safety net. Pair with tiling_enabled=true
# and a raised LLM_VERIFY_MAX_BOXES (tiling emits many boxes; the cap must cover them
# or unchecked noise survives fail-open). Known risk being tested: an
# over-confident model reject deletes a real worm.
LLM_VERIFY_ALL_BOXES = os.environ.get("LLM_VERIFY_ALL_BOXES", "false").lower() == "true"

# The prompt is deliberately recall-biased: an uncertain verdict must come back
# as "yes". A missed larva costs a real pest detection; a false alarm costs one
# dashboard click. Kept short so the reply stays inside the token cap.
# v4.8 PRECISION rewrite (Runzhe 2026-07-27). The previous prompt ended with
# "If you are unsure, answer true - a missed real larva is far worse than a
# false alarm" - written for the v4.4 recall era, and the DIRECT cause of the
# noise Runzhe saw surviving on the dashboard (wood planks, dried leaves at
# 30-50% all "confirmed"): the models were obeying that line, not failing.
# In denoiser mode the bias must be the opposite: uncertain = not a larva.
# v5.9 (2026-08-04). The old opening line was "An automated detector flagged
# this region as possibly containing an armyworm or caterpillar larva" - the
# same leading question this project already measured as fatal on whole frames
# (tell the model a worm is present and it confirms 10/10 while locating only
# 2/11). It was doing the same damage here: of 28 surviving false boxes the
# primed prompt killed 2, the neutral wording below killed 5, and 5 of the
# false verdicts literally cited Runzhe's hand-drawn ink ("in circled area",
# "near red circle marker") as their evidence. Do not reintroduce a preamble
# that asserts a larva may be present.
LLM_VERIFY_PROMPT_PRIMED_RETIRED = "see git history - retired 2026-08-04"

LLM_VERIFY_PROMPT = (
    "What is in this photo? Look carefully at the centre of the frame.\n\n"
    "This is a crop from a garden. Most such crops contain only leaves, "
    "stems, wood, soil, shadows or flowers - a larva is uncommon.\n\n"
    "A caterpillar or moth larva has an elongated, soft body with CLEAR "
    "segmentation. Colour varies and is NOT the deciding feature: many carry "
    "yellow-and-black or red-and-black stripes, but young larvae are often "
    "pale grey, cream or off-white with only faint markings. Judge by the "
    "segmented body, not by colour. "
    "Bare wood grain, planks, dried or curled leaves, "
    "twigs, stems, soil, stones, shadows, an empty flower or bud, droppings "
    "and man-made objects are NOT larvae.\n\n"
    "Reply with strict JSON only, no other text:\n"
    '{"is_larva": true or false, "reason": "what you actually see, at most '
    '12 words"}\n'
    "Set is_larva true ONLY if a caterpillar or moth larva is genuinely "
    "present AND occupies the centre of this crop."
)

# Post-gate cleanup (v5.0). Both default to OFF so the behaviour is unchanged
# until they are explicitly configured. Measured best pair on batch_2:
# POST_NMS_IOU=0.3, POST_VERIFY_FLOOR=15.
POST_NMS_IOU         = float(os.environ.get("POST_NMS_IOU", "0"))
POST_VERIFY_FLOOR    = float(os.environ.get("POST_VERIFY_FLOOR", "0"))
# Runzhe's size sanity rule (2026-08-04): a box bigger than this fraction of
# the frame cannot be a larva, whatever the model said. Grounded in his own
# labels - the LARGEST of 34 hand-marked worms covers 4.39% of its frame, so
# 10% sits 2.3x above anything real. Catches the failure the confidence floor
# cannot: a frame-spanning box the verifier confidently described as a
# caterpillar (img_101 at 43% of frame). 0 = rule off.
POST_MAX_BOX_AREA    = float(os.environ.get("POST_MAX_BOX_AREA", "0"))
# Containment suppression, added alongside IoU-NMS (Runzhe, 2026-08-04).
# IoU divides by the UNION, so a small box sitting wholly inside a big one
# scores near zero and survives - measured on the live output, all five
# overlapping pairs escaped IoU-NMS at 0.3, the worst being img_107 at
# IoU 0.022 with the small box 100% inside the large one. This second test
# asks how much of the SMALLER box is covered, which is what "these two mark
# the same thing" actually looks like. 0 = off.
POST_NMS_CONTAIN     = float(os.environ.get("POST_NMS_CONTAIN", "0"))

detections_table = dynamodb.Table(TABLE_DETECTIONS)
cameras_table    = dynamodb.Table(TABLE_CAMERAS)
config_table     = dynamodb.Table(TABLE_SYSTEM_CONFIG)

FALLBACK_CAMERA_ID   = "manual_upload"
FALLBACK_WAYPOINT_ID = "manual_upload"


# -----------------------------------------------------------------------------
# Utility helpers
# -----------------------------------------------------------------------------
def iso_now():
    """ISO 8601 UTC with Z suffix, e.g. '2026-05-21T05:32:18Z'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_s3_key(object_key):
    """
    Standard key: frames/{camera_id}/{waypoint_id}/{filename}
    Non-standard -> falls back to 'manual_upload' camera.

    Strips '__suffix' from waypoint segment so that:
        frames/cam/manual_test__conf50/foo.jpg
    parses with waypoint_id='manual_test' (clean) while still letting downstream
    code read the override from the full object_key.
    """
    parts = object_key.split("/")
    if len(parts) >= 4 and parts[0] == "frames":
        camera_id = parts[1]
        waypoint_id = parts[2].split("__", 1)[0]
        filename = "/".join(parts[3:])
        return camera_id, waypoint_id, filename
    filename = parts[-1] if parts else object_key
    print(f"[ParseKey] Non-standard key '{object_key}' - using fallback")
    return FALLBACK_CAMERA_ID, FALLBACK_WAYPOINT_ID, filename


# -----------------------------------------------------------------------------
# DDB readers
# -----------------------------------------------------------------------------
def get_camera_config(camera_id):
    """
    Read a single camera's config row from pest-monitoring-cameras.
    Falls back to the 'manual_upload' camera if the requested id is missing,
    so non-standard S3 keys still get a usable config.
    """
    try:
        r = cameras_table.get_item(Key={"camera_id": camera_id})
        if "Item" in r:
            return r["Item"]
    except Exception as e:
        print(f"[Cameras] read failed for {camera_id}: {e}")

    print(f"[Cameras] No config for '{camera_id}', falling back to {FALLBACK_CAMERA_ID}")
    try:
        r = cameras_table.get_item(Key={"camera_id": FALLBACK_CAMERA_ID})
        if "Item" in r:
            return r["Item"]
    except Exception:
        pass

    # Last-resort hardcoded defaults if both lookups fail
    return {
        "target_label":  "Person",
        "model_type":    "general",
        "min_confidence": 80,
    }


def get_system_config():
    """Single-row global config (email recipients, capture flags, etc)."""
    try:
        r = config_table.get_item(Key={"config_key": "detection_settings"})
        return r.get("Item", {}) or {}
    except Exception as e:
        print(f"[SystemConfig] read failed: {e}")
        return {}


def collect_recipients(global_config):
    """Merge primary recipient_email + additional_recipients into a deduped list."""
    primary    = global_config.get("recipient_email", SENDER_EMAIL)
    additional = global_config.get("additional_recipients", [])
    if isinstance(additional, str):
        additional = [e.strip() for e in additional.split(",") if e.strip()]
    all_emails = list(dict.fromkeys(([primary] if primary else []) + list(additional)))
    return [e for e in all_emails if e]


# -----------------------------------------------------------------------------
# Rekognition response processing
# -----------------------------------------------------------------------------
def extract_bounding_boxes(labels, target_label, min_confidence, model_type):
    """
    Filter Rekognition labels by (target_label match AND confidence >= threshold)
    and extract bounding box coordinates (normalized 0-1).

    Returns a list of dicts:
      { Left, Top, Width, Height, Confidence, Name, no_box?: bool, _llm?: dict }
    'no_box=True' means a label hit threshold but had no geometry to draw
    (general detect_labels without an Instances entry).
    '_llm' carries the v4.5 hybrid-gate verdict through to the DB formatter.
    """
    boxes = []
    target_lower = (target_label or "").strip().lower()
    for label in labels:
        name = label.get("Name", "")
        conf = label.get("Confidence", 0)
        if name.strip().lower() != target_lower or conf < min_confidence:
            continue
        if model_type == "custom":
            geometry = label.get("Geometry")
            if geometry and geometry.get("BoundingBox"):
                bb = geometry["BoundingBox"]
                box = {
                    "Left":   bb.get("Left", 0), "Top":   bb.get("Top", 0),
                    "Width":  bb.get("Width", 0), "Height": bb.get("Height", 0),
                    "Confidence": conf, "Name": name.strip(),
                }
                if label.get("_llm"):
                    box["_llm"] = label["_llm"]
                boxes.append(box)
        else:
            instances = label.get("Instances", [])
            if instances:
                for inst in instances:
                    bb = inst.get("BoundingBox", {})
                    boxes.append({
                        "Left":  bb.get("Left", 0), "Top":   bb.get("Top", 0),
                        "Width": bb.get("Width", 0), "Height": bb.get("Height", 0),
                        "Confidence": conf, "Name": name.strip(),
                    })
            else:
                boxes.append({
                    "Left": 0, "Top": 0, "Width": 0, "Height": 0,
                    "Confidence": conf, "Name": name.strip(), "no_box": True,
                })
    return boxes


def boxes_to_db_format(boxes):
    """
    Convert internal bbox dicts (extract_bounding_boxes output) -> DDB schema
    'bboxes' list. We stringify all coordinates because DDB's Number type
    rejects high-precision floats Python emits from Rekognition responses, and
    we already use string format in the Wilbur migration data for consistency.

    Coordinates stay normalized (0-1). The dashboard multiplies them by the
    rendered image's natural width/height when drawing on the canvas.

    v4.4 adds two OPTIONAL fields when the LLM verifier ran on that box:
    'verified_by_llm' ("true"/"false") and 'llm_reason'. The dashboard reads
    boxes by explicit field name (web/dashboard_v4/js/bbox.js getDrawableBoxes),
    so unknown keys are ignored and no frontend change is required.
    """
    out = []
    for b in boxes:
        if b.get("no_box"):
            continue
        item = {
            "label":      b.get("Name", ""),
            "confidence": f"{b.get('Confidence', 0):.1f}",
            "top":        str(b.get("Top", 0)),
            "left":       str(b.get("Left", 0)),
            "width":      str(b.get("Width", 0)),
            "height":     str(b.get("Height", 0)),
        }
        verdict = b.get("_llm")
        if verdict:
            item["verified_by_llm"] = "true" if verdict.get("is_larva") else "false"
            item["llm_reason"]      = str(verdict.get("reason", ""))[:120]
        out.append(item)
    return out


# -----------------------------------------------------------------------------
# Tiling / crop pipeline (v4.2)
# -----------------------------------------------------------------------------
def get_tiling_config(cam_config):
    """Per-camera opt-in (cam_config.tiling_enabled) gated by the global switch.
    Tuning comes from env vars so it can change without a code deploy."""
    enabled = TILING_ENABLED_GLOBAL and bool(cam_config.get("tiling_enabled", False))
    return {
        "enabled":            enabled,
        "cols":               TILE_COLS,
        "rows":               TILE_ROWS,
        "overlap":            TILE_OVERLAP,
        "upscale_long_edge":  TILE_UPSCALE_LONG_EDGE,
        "include_full_frame": TILE_INCLUDE_FULL_FRAME,
        "min_confidence":     TILE_MIN_CONFIDENCE,
        "iou":                TILE_NMS_IOU,
        "max_workers":        TILE_MAX_WORKERS,
    }


def compute_tile_regions(w, h, cols, rows, overlap):
    """Return a list of (x0, y0, x1, y1) PIXEL regions covering the frame as a
    cols x rows grid, each tile expanded by `overlap` (fraction of base tile) on
    every side and clamped to the frame. Overlap ensures a target straddling a
    tile boundary still lands wholly inside at least one tile."""
    regions = []
    base_w = w / cols
    base_h = h / rows
    ov_w = base_w * overlap
    ov_h = base_h * overlap
    for r in range(rows):
        for c in range(cols):
            x0 = max(0, int(round(c * base_w - ov_w)))
            y0 = max(0, int(round(r * base_h - ov_h)))
            x1 = min(w, int(round((c + 1) * base_w + ov_w)))
            y1 = min(h, int(round((r + 1) * base_h + ov_h)))
            if x1 > x0 and y1 > y0:
                regions.append((x0, y0, x1, y1))
    return regions


def _encode_jpeg(pil_img):
    buf = BytesIO()
    pil_img.convert("RGB").save(buf, format="JPEG", quality=90)
    return buf.getvalue()


# Rekognition rejects an oversized image outright with ImageTooLargeException,
# and the full-frame pass is the only job that sends the frame uncropped. On
# the Jewel captures (5712x3213) that pass failed on EVERY run, silently
# costing one of the 17 detection passes on exactly the two images the demo
# depends on. Box coordinates are normalised, so shrinking the full frame
# moves nothing.
FULL_FRAME_MAX_EDGE  = int(os.environ.get("TILE_FULL_FRAME_MAX_EDGE", "4000"))
FULL_FRAME_MAX_BYTES = int(os.environ.get("TILE_FULL_FRAME_MAX_BYTES",
                                          str(4 * 1024 * 1024)))


def _full_frame_bytes(pil_img):
    """JPEG bytes of the whole frame, shrunk until Rekognition will accept it."""
    from PIL import Image
    img = pil_img
    w, h = img.size
    if max(w, h) > FULL_FRAME_MAX_EDGE:
        scale = FULL_FRAME_MAX_EDGE / max(w, h)
        img = img.resize((max(1, int(round(w * scale))),
                          max(1, int(round(h * scale)))), Image.LANCZOS)
        print(f"[Tiling] full-frame pass downscaled {w}x{h} -> "
              f"{img.size[0]}x{img.size[1]} (Rekognition size limit)")
    data = _encode_jpeg(img)
    while len(data) > FULL_FRAME_MAX_BYTES and max(img.size) > 640:
        img = img.resize((max(1, img.size[0] // 2), max(1, img.size[1] // 2)),
                         Image.LANCZOS)
        data = _encode_jpeg(img)
        print(f"[Tiling] full-frame pass re-shrunk to {img.size[0]}x"
              f"{img.size[1]} ({len(data) / 1e6:.1f} MB)")
    return data


def detect_whole_frame(src_bucket, object_key, arn, frame_bytes=None):
    """Whole-frame detect_custom_labels that survives an oversized image.

    The S3Object form is preferred (no download), but Rekognition rejects a
    large frame with ImageTooLargeException, and on the non-tiled path that
    exception used to reach the handler's outer except and abort the whole
    invocation - the record was never written and the frame vanished from the
    dashboard silently. Measured on CAG_Jewel_1 (5712x3213). On that error the
    frame is fetched, shrunk by _full_frame_bytes, and re-sent as bytes.
    """
    try:
        return rekognition.detect_custom_labels(
            Image={"S3Object": {"Bucket": src_bucket, "Name": object_key}},
            ProjectVersionArn=arn, MinConfidence=30,
        ).get("CustomLabels", [])
    except rekognition.exceptions.ImageTooLargeException:
        print("[Detect] frame too large for a direct call, downscaling")
        from PIL import Image
        if frame_bytes is None:
            frame_bytes = s3_client.get_object(
                Bucket=src_bucket, Key=object_key)["Body"].read()
        pil_img = Image.open(BytesIO(frame_bytes))
        pil_img.load()
        return rekognition.detect_custom_labels(
            Image={"Bytes": _full_frame_bytes(pil_img)},
            ProjectVersionArn=arn, MinConfidence=30,
        ).get("CustomLabels", [])


def _crop_upscale_bytes(pil_img, region, upscale_long_edge):
    """Crop `region` from the frame and upscale so its long edge reaches
    `upscale_long_edge` (the zoom that gives a small target more pixels)."""
    from PIL import Image
    x0, y0, x1, y1 = region
    tile = pil_img.crop((x0, y0, x1, y1))
    tw, th = tile.size
    if tw <= 0 or th <= 0:
        return None
    long_edge = max(tw, th)
    if upscale_long_edge and long_edge < upscale_long_edge:
        scale = upscale_long_edge / long_edge
        tile = tile.resize((max(1, int(round(tw * scale))),
                            max(1, int(round(th * scale)))), Image.LANCZOS)
    return _encode_jpeg(tile)


def _detect_custom_on_bytes(image_bytes, arn, min_conf):
    """detect_custom_labels on raw bytes, with simple throttle/backoff retry."""
    last = None
    for attempt in range(3):
        try:
            resp = rekognition.detect_custom_labels(
                Image={"Bytes": image_bytes},
                ProjectVersionArn=arn,
                MinConfidence=min_conf,
            )
            return resp.get("CustomLabels", [])
        except Exception as e:
            last = e
            msg = str(e)
            if "ProvisionedThroughput" in msg or "Throttling" in msg or "Rate exceeded" in msg:
                time.sleep(0.5 * (attempt + 1))
                continue
            break
    print(f"[Tiling] detect failed: {last}")
    return []


def _tile_label_to_global(label, region, frame_w, frame_h):
    """Convert one CustomLabel (tile-normalized geometry) to a synthetic
    CustomLabel with GLOBAL-normalized geometry. region=None means the full
    frame (geometry already global). Returns None if no usable geometry."""
    geo = (label.get("Geometry") or {}).get("BoundingBox")
    if not geo:
        return None
    if region is None:
        g = {
            "Left":   geo.get("Left", 0),  "Top":    geo.get("Top", 0),
            "Width":  geo.get("Width", 0), "Height": geo.get("Height", 0),
        }
    else:
        x0, y0, x1, y1 = region
        tw_px, th_px = (x1 - x0), (y1 - y0)
        g = {
            "Left":   (x0 + geo.get("Left", 0) * tw_px) / frame_w,
            "Top":    (y0 + geo.get("Top", 0)  * th_px) / frame_h,
            "Width":  (geo.get("Width", 0)  * tw_px) / frame_w,
            "Height": (geo.get("Height", 0) * th_px) / frame_h,
        }
    return {
        "Name":       label.get("Name", ""),
        "Confidence": label.get("Confidence", 0),
        "Geometry":   {"BoundingBox": g},
    }


def _iou(a, b):
    ax0, ay0 = a["Left"], a["Top"]
    ax1, ay1 = ax0 + a["Width"], ay0 + a["Height"]
    bx0, by0 = b["Left"], b["Top"]
    bx1, by1 = bx0 + b["Width"], by0 + b["Height"]
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    union = a["Width"] * a["Height"] + b["Width"] * b["Height"] - inter
    return inter / union if union > 0 else 0.0


def nms(labels, iou_thr):
    """Greedy per-class non-max suppression over synthetic CustomLabels with
    GLOBAL-normalized geometry. Keeps the highest-confidence box in each cluster
    of overlapping same-class boxes (overlapping tiles produce duplicates)."""
    items = sorted(labels, key=lambda l: l.get("Confidence", 0), reverse=True)
    kept = []
    for cand in items:
        cb = cand["Geometry"]["BoundingBox"]
        cname = cand.get("Name", "").lower()
        duplicate = False
        for k in kept:
            if k.get("Name", "").lower() != cname:
                continue
            if _iou(cb, k["Geometry"]["BoundingBox"]) > iou_thr:
                duplicate = True
                break
        if not duplicate:
            kept.append(cand)
    return kept


def run_tiled_detection(image_bytes, arn, cfg):
    """Tile the frame, detect per tile (+ optional full frame), convert all hits
    to global coords, and NMS-dedup. Returns a list shaped like Rekognition
    CustomLabels so the rest of the handler is unchanged."""
    from PIL import Image
    pil_img = Image.open(BytesIO(image_bytes))
    pil_img.load()
    frame_w, frame_h = pil_img.size

    # Build the work items: (region_or_None, jpeg_bytes)
    jobs = []
    if cfg["include_full_frame"]:
        jobs.append((None, _full_frame_bytes(pil_img)))
    for reg in compute_tile_regions(frame_w, frame_h, cfg["cols"], cfg["rows"], cfg["overlap"]):
        b = _crop_upscale_bytes(pil_img, reg, cfg["upscale_long_edge"])
        if b:
            jobs.append((reg, b))

    def work(job):
        region, jpeg_bytes = job
        raw = _detect_custom_on_bytes(jpeg_bytes, arn, cfg["min_confidence"])
        out = []
        for lbl in raw:
            g = _tile_label_to_global(lbl, region, frame_w, frame_h)
            if g:
                out.append(g)
        return out

    results = []
    workers = max(1, cfg["max_workers"])
    if workers > 1 and len(jobs) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for chunk in ex.map(work, jobs):
                results.extend(chunk)
    else:
        for job in jobs:
            results.extend(work(job))

    deduped = nms(results, cfg["iou"])
    print(f"[Tiling] {len(jobs)} passes ({frame_w}x{frame_h}) -> "
          f"{len(results)} raw -> {len(deduped)} after NMS")
    return deduped


# -----------------------------------------------------------------------------
# Non-vegetation FP suppression (v4.3)
# -----------------------------------------------------------------------------
def get_suppression_regions(src_bucket, object_key):
    """Run DetectLabels on the frame and return the bounding boxes of hard,
    non-vegetation objects (people/vehicles/furniture/machinery). These are the
    regions a custom-model box must NOT sit on. Returns [] on any error so
    detection is never blocked."""
    try:
        resp = rekognition.detect_labels(
            Image={"S3Object": {"Bucket": src_bucket, "Name": object_key}},
            MaxLabels=50, MinConfidence=SUPPRESS_MIN_CONF,
        )
    except Exception as e:
        print(f"[Suppress] DetectLabels failed, no suppression applied: {e}")
        return []

    regions = []
    for lbl in resp.get("Labels", []):
        name = lbl.get("Name", "").strip().lower()
        if name in SUPPRESS_PROTECT or name not in SUPPRESS_LABELS:
            continue
        for inst in lbl.get("Instances", []):
            bb = inst.get("BoundingBox")
            if bb:
                regions.append({
                    "Left": bb.get("Left", 0), "Top": bb.get("Top", 0),
                    "Width": bb.get("Width", 0), "Height": bb.get("Height", 0),
                    "Name": name, "Confidence": inst.get("Confidence", 0),
                })
    if regions:
        print(f"[Suppress] {len(regions)} hard-object region(s): "
              + ", ".join(sorted({r['Name'] for r in regions})))
    return regions


def _covered_fraction(box, region):
    """Fraction of `box` (normalized bbox dict) that lies inside `region`."""
    ax0, ay0 = box["Left"], box["Top"]
    ax1, ay1 = ax0 + box["Width"], ay0 + box["Height"]
    bx0, by0 = region["Left"], region["Top"]
    bx1, by1 = bx0 + region["Width"], by0 + region["Height"]
    iw = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    ih = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = iw * ih
    area = box["Width"] * box["Height"]
    return inter / area if area > 0 else 0.0


def suppress_nonveg(labels, regions):
    """Drop custom labels whose box is >= SUPPRESS_COVERAGE covered by any
    hard-object region. Returns (kept_labels, dropped_count). Labels without a
    geometry are kept untouched."""
    if not regions:
        return labels, 0
    kept, dropped = [], 0
    for lbl in labels:
        geo = (lbl.get("Geometry") or {}).get("BoundingBox")
        if not geo:
            kept.append(lbl)
            continue
        box = {"Left": geo.get("Left", 0), "Top": geo.get("Top", 0),
               "Width": geo.get("Width", 0), "Height": geo.get("Height", 0)}
        hit = next((r for r in regions
                    if _covered_fraction(box, r) >= SUPPRESS_COVERAGE), None)
        if hit:
            dropped += 1
            print(f"[Suppress] dropped {lbl.get('Name')} "
                  f"{lbl.get('Confidence', 0):.0f}% sitting on '{hit['Name']}'")
        else:
            kept.append(lbl)
    return kept, dropped


# -----------------------------------------------------------------------------
# LLM verification: crop verdicts (v4.4/4.5, denoiser mode v4.7)
# -----------------------------------------------------------------------------
_bedrock_client_lock = threading.Lock()


def get_bedrock_client():
    """Lazily build the bedrock-runtime client. Locked because verify_one_crop
    runs inside a ThreadPoolExecutor: without the lock, the first verified
    frame after a cold start can construct the client concurrently from
    multiple worker threads, which is not safe on boto3's default session and
    can raise inside a worker - and since verify_one_crop catches everything,
    that race would silently score the box as unverified (kept)."""
    global _bedrock_runtime
    if _bedrock_runtime is None:
        with _bedrock_client_lock:
            if _bedrock_runtime is None:
                from botocore.config import Config
                _bedrock_runtime = boto3.client(
                    "bedrock-runtime",
                    region_name=AWS_REGION,
                    config=Config(read_timeout=LLM_VERIFY_TIMEOUT,
                                  connect_timeout=5,
                                  # A 12x12 sweep fires 144 calls per frame and
                                  # several frames can land at once, so account
                                  # TPM throttling is a real failure mode.
                                  # Adaptive mode adds client-side rate
                                  # limiting instead of just retrying harder.
                                  retries={"max_attempts": 5,
                                           "mode": "adaptive"}),
                )
    return _bedrock_runtime


def crop_box_bytes(pil_img, bbox, pad, long_edge):
    """Crop a NORMALIZED bbox out of the frame with `pad` padding (as a fraction
    of the box's own size, floored to LLM_VERIFY_MIN_CONTEXT_PX so a tiny box
    still gets real surrounding context) and upscale the result so its long
    edge reaches `long_edge` (capped at LLM_VERIFY_MAX_UPSCALE so a tiny box
    isn't blown up into pure interpolation noise). Padding matters: a crop cut
    exactly to the box gives the model no surrounding context to tell a larva
    from a leaf vein or a stem."""
    from PIL import Image
    w, h = pil_img.size
    bx0 = bbox.get("Left", 0) * w
    by0 = bbox.get("Top", 0) * h
    bw  = bbox.get("Width", 0) * w
    bh  = bbox.get("Height", 0) * h
    if bw <= 0 or bh <= 0:
        return None

    pad_x = max(bw * pad, LLM_VERIFY_MIN_CONTEXT_PX)
    pad_y = max(bh * pad, LLM_VERIFY_MIN_CONTEXT_PX)
    x0 = max(0, int(round(bx0 - pad_x)))
    y0 = max(0, int(round(by0 - pad_y)))
    x1 = min(w, int(round(bx0 + bw + pad_x)))
    y1 = min(h, int(round(by0 + bh + pad_y)))
    if x1 <= x0 or y1 <= y0:
        return None

    crop = pil_img.crop((x0, y0, x1, y1))
    cw, ch = crop.size
    long_side = max(cw, ch)
    if long_edge and long_side < long_edge:
        scale = min(long_edge / long_side, LLM_VERIFY_MAX_UPSCALE)
        if scale > 1.0:
            crop = crop.resize((max(1, int(round(cw * scale))),
                                max(1, int(round(ch * scale)))), Image.LANCZOS)
    return _encode_jpeg(crop)


def _extract_json_object(text):
    """Return the first balanced {...} substring in text, honoring string
    quoting so a brace inside a quoted value doesn't end the scan early. None
    if no balanced object is found. Replaces a prior greedy regex
    (`re.search(r"\\{.*\\}")`) that broke on any brace appearing after the
    object - e.g. trailing prose from the model, or a markdown fence."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find("{", start + 1)
    return None


def parse_llm_verdict(text):
    """Pull {"is_larva": bool, "reason": str} out of a model reply. Returns None
    when the reply cannot be trusted, which the caller treats as 'unverified'
    and therefore keeps the box. Handles replies wrapped in prose or fences."""
    if not text:
        return None
    obj_text = _extract_json_object(text)
    if obj_text is None:
        return None
    try:
        data = json.loads(obj_text)
    except Exception:
        return None
    if not isinstance(data, dict) or "is_larva" not in data:
        return None

    raw = data.get("is_larva")
    if isinstance(raw, bool):
        is_larva = raw
    elif isinstance(raw, (int, float)) and raw in (0, 1):
        is_larva = bool(raw)
    elif isinstance(raw, str):
        if raw.strip().lower() in ("true", "yes"):
            is_larva = True
        elif raw.strip().lower() in ("false", "no"):
            is_larva = False
        else:
            return None
    else:
        return None

    return {"is_larva": is_larva, "reason": str(data.get("reason", "")).strip()}


def verify_one_crop(crop_bytes):
    """Ask the Bedrock model whether one crop shows a larva. Returns a verdict
    dict, or None on any failure (which means 'keep the box, unverified').

    Model-agnostic on purpose: `temperature` is only sent when explicitly
    configured (it is a 400 on Fable 5 / Mythos 5 / Opus 4.7+ / Sonnet 5), and
    the reply is read by scanning for the first text block, so the leading
    `reasoningContent` blocks that thinking-always-on models emit are skipped
    rather than mistaken for the answer."""
    inference_config = {"maxTokens": LLM_VERIFY_MAX_TOKENS}
    if LLM_VERIFY_TEMPERATURE:
        try:
            inference_config["temperature"] = float(LLM_VERIFY_TEMPERATURE)
        except ValueError:
            print(f"[LLMGate] ignoring non-numeric LLM_VERIFY_TEMPERATURE="
                  f"{LLM_VERIFY_TEMPERATURE!r}")
    try:
        client = get_bedrock_client()
        resp = client.converse(
            modelId=active_llm_model(),
            messages=[{
                "role": "user",
                "content": [
                    {"image": {"format": "jpeg", "source": {"bytes": crop_bytes}}},
                    {"text": LLM_VERIFY_PROMPT},
                ],
            }],
            inferenceConfig=inference_config,
        )
        stop = resp.get("stopReason")
        if stop in ("refusal", "guardrail_intervened"):
            # A safety decline is not a verdict about the crop. Fail open.
            print(f"[LLMGate] model declined (stopReason={stop}), box stays unverified")
            return None
        blocks = resp.get("output", {}).get("message", {}).get("content", [])
        text = next((b.get("text") for b in blocks if b.get("text")), "")
        verdict = parse_llm_verdict(text)
        if verdict is None and stop == "max_tokens":
            # The whole budget went to reasoning/preamble before the JSON. This
            # is the classic symptom of pointing at a thinking-always-on model
            # without raising LLM_VERIFY_MAX_TOKENS.
            print(f"[LLMGate] reply hit the {LLM_VERIFY_MAX_TOKENS}-token cap with no "
                  f"parsable verdict - raise LLM_VERIFY_MAX_TOKENS for this model")
        return verdict
    except Exception as e:
        print(f"[LLMGate] call failed, box stays unverified: {e}")
        return None


def _crop_verdicts(pil_img, candidates):
    """Crop-and-judge each candidate box: the PROVEN per-box verdict path (the
    2026-07-21 A/B: crop 13/14 vs whole-image 6/14). Returns a list of
    (label, verdict-or-None). All the v4.5 machinery lives here: sort by
    confidence, cap at LLM_VERIFY_MAX_BOXES, pad+upscale crops, parallel
    Bedrock calls; every failure yields verdict None (fail-open)."""
    candidates = sorted(candidates, key=lambda l: l.get("Confidence", 0),
                        reverse=True)
    if len(candidates) > LLM_VERIFY_MAX_BOXES:
        print(f"[LLMGate] {len(candidates)} candidate(s) to judge, taking the "
              f"top {LLM_VERIFY_MAX_BOXES} by confidence; the rest stay "
              f"(fail-open)")
        candidates = candidates[:LLM_VERIFY_MAX_BOXES]

    jobs = []
    for lbl in candidates:
        crop = None
        try:
            crop = crop_box_bytes(pil_img, lbl["Geometry"]["BoundingBox"],
                                  LLM_VERIFY_PAD, LLM_VERIFY_LONG_EDGE)
        except Exception as e:
            print(f"[LLMGate] crop failed, box stays (fail-open): {e}")
        if crop:
            jobs.append((lbl, crop))
    if not jobs:
        return []

    def work(job):
        lbl, crop = job
        return lbl, verify_one_crop(crop)

    try:
        workers = max(1, min(LLM_VERIFY_WORKERS, len(jobs)))
        if workers > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=workers) as ex:
                return list(ex.map(work, jobs))
        return [work(j) for j in jobs]
    except Exception as e:
        print(f"[LLMGate] batch failed, boxes stay (fail-open): {e}")
        return []


def apply_llm_verify_gate(labels, frame_bytes, target_label, authority_threshold):
    """The LLM verification gate (v4.5 hybrid rule / v4.7 denoiser mode).

    LLM_VERIFY_ALL_BOXES=false: a target box at/above the camera's
    min_confidence is trusted outright (Runzhe's 2026-07-22 authority rule);
    a box below it goes to the crop verdict - an explicit "not a larva"
    verdict DROPS it, a positive or missing verdict keeps it (fail-open).
    LLM_VERIFY_ALL_BOXES=true (production since v4.7): every target box is
    crop-judged and only a positive verdict lets it survive (fail-closed for
    survival; fail-open only on TOTAL gate failure - see below).

    Returns (labels, n_verified). n_verified counts boxes that got a REAL
    verdict; the caller MUST use it - not "did the call raise" - to decide
    whether the gate ran, because every internal failure path returns
    normally having verified nothing (the 2026-07-22 review lesson).
    """
    target_lower = (target_label or "").strip().lower()

    try:
        from PIL import Image
        pil_img = Image.open(BytesIO(frame_bytes))
        pil_img.load()
    except Exception as e:
        print(f"[LLMGate] cannot open frame, gate skipped (fail-open): {e}")
        return labels, 0, 0, None

    target_boxes = [
        l for l in labels
        if (l.get("Geometry") or {}).get("BoundingBox")
        and l.get("Name", "").strip().lower() == target_lower
    ]
    # v4.7 denoiser: every box is judged (no authority exemption). Otherwise the
    # v4.5/4.6 rule stands - only boxes below the camera's min_confidence.
    if LLM_VERIFY_ALL_BOXES:
        sub_threshold = list(target_boxes)
    else:
        sub_threshold = [l for l in target_boxes
                         if l.get("Confidence", 0) < authority_threshold]

    n_verified = 0
    disputed = list(sub_threshold)

    n_rejected = 0
    if disputed:
        for lbl, verdict in _crop_verdicts(pil_img, disputed):
            if not verdict:
                continue
            lbl["_llm"] = verdict
            n_verified += 1
            if not verdict["is_larva"]:
                n_rejected += 1
                why = ("denoiser: every box judged" if LLM_VERIFY_ALL_BOXES
                       else f"below {authority_threshold}% authority")
                print(f"[LLMGate] {lbl.get('Name')} "
                      f"{lbl.get('Confidence', 0):.0f}% ({why}) judged NOT a "
                      f"larva, dropping: {verdict['reason']}")
    if LLM_VERIFY_ALL_BOXES and sub_threshold and n_verified == 0:
        # TOTAL GATE FAILURE (v4.7.1, 2026-07-26). There were candidates to judge
        # and NOT ONE came back with a verdict -> this is infrastructure down
        # (no Bedrock permission, model id wrong, region-wide throttle), not
        # evidence about the frame. Fail-CLOSED here would silently delete every
        # box on every frame and report a clean garden - which is exactly what
        # happened when LLM_VERIFY_MODEL_ID was switched to Sonnet 4.6 before the
        # Lambda role had permission for it: 492 AccessDenied calls, 44/44 CAG
        # images reported 0 detections. A monitoring system going blind must be
        # LOUD, and must fall back to plain Rekognition thresholding rather than
        # report "no pests". n_verified stays 0, so the caller leaves
        # detection_floor at min_confidence (pre-LLM behaviour).
        print(f"[LLMGate] !! TOTAL GATE FAILURE: {len(sub_threshold)} candidate(s), "
              f"0 verdicts - the model is unreachable. NOT dropping anything; "
              f"falling back to plain min_confidence={authority_threshold}%. "
              f"Check bedrock perms / LLM_VERIFY_MODEL_ID={active_llm_model()}")
    elif LLM_VERIFY_ALL_BOXES:
        # v4.7 denoiser is fail-CLOSED for the survival decision: with the
        # authority exemption gone, EVERY target box was meant to be judged, so
        # a target box survives ONLY with a positive verdict. Boxes that were
        # rejected (verdict says not-larva) AND boxes that never got a verdict
        # at all - over the LLM_VERIFY_MAX_BOXES cap, crop failure, or Bedrock
        # throttle/None - are both dropped. Keeping an un-judged box would let
        # exactly the high-confidence tiling noise this mode exists to remove
        # ride the collapsed detection_floor (0 when the gate ran) straight into
        # target_detected / bboxes / the dashboard. (3 HIGH findings, adversarial
        # review 2026-07-23.) Non-target labels always pass through untouched.
        kept, n_unjudged = [], 0
        for l in labels:
            is_target = (l.get("Name", "").strip().lower() == target_lower
                         and (l.get("Geometry") or {}).get("BoundingBox"))
            if not is_target:
                kept.append(l)
                continue
            v = l.get("_llm")
            if v and v.get("is_larva"):
                kept.append(l)                       # confirmed larva
            elif v is None:
                n_unjudged += 1                      # un-judged -> dropped
            # v and not is_larva -> rejected -> dropped
        if n_unjudged:
            print(f"[LLMGate] denoiser: dropped {n_unjudged} un-judged target "
                  f"box(es) (over LLM_VERIFY_MAX_BOXES cap / crop fail / "
                  f"throttle) - fail-CLOSED, not trusted as detections")
        labels = kept
    elif n_rejected:
        labels = [l for l in labels
                  if not (l.get("_llm") and not l["_llm"]["is_larva"])]

    print(f"[LLMGate] gate done: {n_verified} verdict(s), {n_rejected} "
          f"dropped, model={active_llm_model()}")
    return labels, n_verified


# -----------------------------------------------------------------------------
# Post-gate cleanup (v5.0, 2026-07-29)
# -----------------------------------------------------------------------------
# Turning the cluster-merge gate off (removed in v6.3) took batch_2 from 4/11
# to 9/11 worms but left ~6.5 boxes
# per frame sitting on nothing, because every candidate is now judged on its own
# and near-duplicate crops of the same clutter all pass. Two cheap steps recover
# the precision WITHOUT costing a single worm (measured: recall stayed at 9/11
# through every variant):
#
#   NMS               collapse overlapping survivors, keep the most confident
#   post-verify floor drop survivors under POST_VERIFY_FLOOR
#
# The floor is applied AFTER the LLM verdict on purpose. Measured on batch_2:
# surviving boxes that sit on a worm have median 27% Rekognition confidence,
# survivors that sit on nothing have median 15.5%. That separation only exists
# among boxes the LLM already accepted - the same number used as an INPUT floor
# does nothing useful, which is what the earlier floor sweeps kept showing.
def apply_post_gate_cleanup(labels, target_label, floor_override=None):
    """Deduplicate and floor the target boxes the LLM gate let through.

    Only touches labels matching target_label; anything else passes untouched.
    `floor_override` (v6.2): the per-camera `post_verify_floor` DDB field, so
    the dashboard's threshold knob edits THIS floor - the one that decides
    what is shown - instead of `min_confidence`, which is the candidate floor
    in front of the LLM and was what the dashboard used to edit (that mismatch
    is how 35 ended up silently strangling the candidate stream).
    Returns (labels, n_dropped)."""
    floor = POST_VERIFY_FLOOR if floor_override is None else float(floor_override)
    if POST_NMS_IOU <= 0 and floor <= 0 and POST_MAX_BOX_AREA <= 0:
        return labels, 0

    target_lower = (target_label or "").strip().lower()
    targets, others = [], []
    for lb in labels:
        if (lb.get("Name", "").strip().lower() == target_lower
                and (lb.get("Geometry") or {}).get("BoundingBox")):
            targets.append(lb)
        else:
            others.append(lb)
    if not targets:
        return labels, 0

    before = len(targets)

    if POST_MAX_BOX_AREA > 0:
        kept = []
        for lb in targets:
            g = lb["Geometry"]["BoundingBox"]
            area = float(g.get("Width", 0)) * float(g.get("Height", 0))
            if area > POST_MAX_BOX_AREA:
                print(f"[PostGate] box covers {area*100:.1f}% of the frame "
                      f"(cap {POST_MAX_BOX_AREA*100:.0f}%) - too big to be a "
                      f"larva, dropped despite the verifier accepting it")
                continue
            kept.append(lb)
        targets = kept

    if floor > 0:
        targets = [t for t in targets
                   if t.get("Confidence", 0) >= floor]

    if POST_NMS_IOU > 0 or POST_NMS_CONTAIN > 0:
        kept = []
        for lb in sorted(targets, key=lambda x: -x.get("Confidence", 0)):
            box = lb["Geometry"]["BoundingBox"]
            clash = False
            for k in kept:
                kb = k["Geometry"]["BoundingBox"]
                if POST_NMS_IOU > 0 and _bbox_iou(box, kb) >= POST_NMS_IOU:
                    clash = True
                    break
                if (POST_NMS_CONTAIN > 0
                        and _bbox_contain(box, kb) >= POST_NMS_CONTAIN):
                    clash = True
                    break
            if not clash:
                kept.append(lb)
        targets = kept

    dropped = before - len(targets)
    if dropped:
        print(f"[PostGate] {before} -> {len(targets)} box(es) "
              f"(nms_iou={POST_NMS_IOU}, floor={floor}%), "
              f"{dropped} dropped")
    return others + targets, dropped


def _bbox_contain(a, b):
    """Fraction of the SMALLER box that lies inside the other.

    The companion to IoU: two boxes marking one worm from different tiles can
    share almost no union while one sits entirely within the other. IoU calls
    that 0.02; this calls it 1.0."""
    ax1, ay1 = a["Left"] + a["Width"], a["Top"] + a["Height"]
    bx1, by1 = b["Left"] + b["Width"], b["Top"] + b["Height"]
    ix0, iy0 = max(a["Left"], b["Left"]), max(a["Top"], b["Top"])
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    sa = a["Width"] * a["Height"]
    sb = b["Width"] * b["Height"]
    return inter / max(min(sa, sb), 1e-12)


def _bbox_iou(a, b):
    """IoU of two Rekognition-style {Left,Top,Width,Height} boxes."""
    ax0, ay0 = a["Left"], a["Top"]
    ax1, ay1 = ax0 + a["Width"], ay0 + a["Height"]
    bx0, by0 = b["Left"], b["Top"]
    bx1, by1 = bx0 + b["Width"], by0 + b["Height"]
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    union = (a["Width"] * a["Height"]) + (b["Width"] * b["Height"]) - inter
    return inter / union if union > 0 else 0.0


# -----------------------------------------------------------------------------
# Main handler
# -----------------------------------------------------------------------------
def lambda_handler(event, context):
    try:
        record = event["Records"][0]
        src_bucket = record["s3"]["bucket"]["name"]
        object_key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        print(f"[Trigger] s3://{src_bucket}/{object_key}")

        if not object_key.lower().endswith((".jpg", ".jpeg", ".png")):
            print(f"[Skip] Non-image file: {object_key}")
            return {"statusCode": 200, "body": "Skipped (non-image)"}

        camera_id, waypoint_id, filename = parse_s3_key(object_key)
        print(f"[ParseKey] camera={camera_id} waypoint={waypoint_id} file={filename}")

        # --- 1. Load per-camera config + global system config ---
        cam_config    = get_camera_config(camera_id)
        global_config = get_system_config()

        target_label    = cam_config.get("target_label", "Person")
        target_lower    = target_label.strip().lower()
        min_confidence  = int(cam_config.get("min_confidence", 80))
        model_type      = cam_config.get("model_type", "general")
        custom_model_arn = cam_config.get("custom_model_arn", "") or ""
        email_enabled   = bool(global_config.get("email_enabled", True))
        recipients      = collect_recipients(global_config)

        # --- 2. Per-request override via S3 key path (Test upload feature) ---
        # When the key contains 'manual_test__confN' (N=10-100), override the
        # camera-level min_confidence for THIS run only. Stateless, no DDB swap.
        # Any waypoint segment may carry it, not just 'manual_test', so a model
        # A/B run can name its own waypoint (e.g. 'novapro__conf75__llm-novapro').
        override_match = re.search(r"__conf(\d+)", object_key)
        if override_match:
            override_val = int(override_match.group(1))
            if 10 <= override_val <= 100:
                print(f"[Override] min_confidence: {min_confidence}% -> {override_val}%")
                min_confidence = override_val

        # Pick the verification model for this record. Always called, so a warm
        # container never carries the previous record's model over.
        llm_model_id = set_active_llm_model(object_key, cam_config)

        print(f"[Config] camera={camera_id} target={target_label} "
              f"min_conf={min_confidence} model={model_type} llm={llm_model_id}")

        # --- 2a. PASSTHROUGH CAMERAS (v6.4, 2026-08-21) ----------------------
        # A camera row with detect_enabled=false records the frame and stops.
        # No Rekognition call, no tiling, no Bedrock, no SES, not even the EXIF
        # download - the function returns from here.
        #
        # Why the flag lives on the camera and not on the S3 prefix: this bucket
        # can carry exactly ONE notification filter per event type, and it is
        # already 'frames/'. S3 rejects a second, overlapping prefix rule, so a
        # separate no-detection Lambda is not possible. The routing has to
        # happen inside this function.
        #
        # The record is still written, because the dashboard gallery lists
        # DynamoDB records rather than S3 objects - a frame with no record is
        # invisible. Fields match the normal record exactly, with the detection
        # summary zeroed and source='navigation-capture' so passthrough frames
        # can be filtered apart from real detections.
        #
        # Default is TRUE. Every existing camera row (worm_cam, moth_cam,
        # manual_upload) lacks the field and is completely unaffected.
        if not bool(cam_config.get("detect_enabled", True)):
            passthrough_time = iso_now()
            detections_table.put_item(Item={
                "image_id":            object_key,
                "detection_time":      passthrough_time,
                "pest_type":           target_label,
                "source":              "navigation-capture",
                "created_at":          passthrough_time,
                "bucket":              src_bucket,
                "original_image_key":  object_key,
                "camera_id":           camera_id,
                "waypoint_id":         waypoint_id,
                "target_label":        target_label,
                "target_detected":     False,
                "target_confidence":   "0",
                "min_confidence_used": min_confidence,
                "label_count":         0,
                "labels":              "[]",
                "bboxes":              [],
                "verifications":       {},
                "model_type":          "none",
                "model_arn":           "",
                "llm_verify_model":    "",
            })
            print(f"[Passthrough] detect_enabled=false for {camera_id}: record "
                  f"written, no detection run.")
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "image":           object_key,
                    "camera_id":       camera_id,
                    "waypoint_id":     waypoint_id,
                    "target_detected": False,
                    "passthrough":     True,
                }),
            }

        # --- 2b. EXIF ORIENTATION NORMALISATION (v4.8.3, 2026-07-28) ----------
        # Phone photos are almost always stored in the sensor's landscape pixel
        # order plus an EXIF Orientation tag telling viewers to rotate. Browsers
        # DO apply that tag; PIL and the coordinate maths here do NOT. So a
        # phone upload was measured in one orientation and drawn in another and
        # every box landed in the wrong place (found 2026-07-28 on two iPhone
        # uploads: raw 4032x3024, EXIF Orientation 6, displayed 3024x4032).
        # Fix at ingest: bake the rotation into the pixels ONCE, write the
        # upright frame back to the same S3 key, and let Rekognition, the crops
        # and the dashboard all agree from then on. Untagged / already-upright
        # images are untouched, so nothing that worked before changes.
        try:
            from PIL import Image as _PILImage, ImageOps as _PILImageOps
            _obj = s3_client.get_object(Bucket=src_bucket, Key=object_key)
            _raw = _obj["Body"].read()
            _im = _PILImage.open(BytesIO(_raw))
            _upright = _PILImageOps.exif_transpose(_im)
            if _upright is not None and _upright.size != _im.size:
                _buf = BytesIO()
                _upright.convert("RGB").save(_buf, "JPEG", quality=95)
                _fixed = _buf.getvalue()
                s3_client.put_object(Bucket=src_bucket, Key=object_key,
                                     Body=_fixed, ContentType="image/jpeg")
                frame_bytes_pre = _fixed
                print(f"[EXIF] rotated frame baked in: {_im.size} -> "
                      f"{_upright.size}; S3 object rewritten upright so boxes "
                      f"match what the dashboard draws")
            else:
                frame_bytes_pre = _raw
        except Exception as e:
            print(f"[EXIF] normalisation skipped (non-fatal): {e}")
            frame_bytes_pre = None

        # --- 3. Call Rekognition (tiled for opt-in custom cameras, else single) ---
        # frame_bytes is kept for reuse by the v4.5 hybrid gate so a tiled run never
        # downloads the same frame twice.
        labels = []
        frame_bytes = frame_bytes_pre
        is_custom = (model_type == "custom" and custom_model_arn
                     and not custom_model_arn.startswith("REPLACE_"))
        if is_custom:
            tiling = get_tiling_config(cam_config)
            if tiling["enabled"]:
                try:
                    if frame_bytes is None:
                        obj = s3_client.get_object(Bucket=src_bucket, Key=object_key)
                        frame_bytes = obj["Body"].read()
                    labels = run_tiled_detection(frame_bytes, custom_model_arn, tiling)
                except Exception as e:
                    # Any tiling failure must not break detection: fall back to one call.
                    print(f"[Tiling] FAILED, falling back to single call: {e}")
                    labels = detect_whole_frame(src_bucket, object_key,
                                                custom_model_arn, frame_bytes)
            else:
                labels = detect_whole_frame(src_bucket, object_key,
                                            custom_model_arn, frame_bytes)
        else:
            response = rekognition.detect_labels(
                Image={"S3Object": {"Bucket": src_bucket, "Name": object_key}},
                MaxLabels=50, MinConfidence=50,
            )
            labels = response["Labels"]
        print(f"[Rekognition] {len(labels)} labels returned")

        # --- 3b. Non-vegetation FP suppression (custom models only) ---
        # Rekognition CL OD cannot be trained on negatives, so we filter here:
        # drop custom boxes that sit on hard objects (people/vehicles/furniture/
        # machinery). Only runs when the custom model actually produced a box, so
        # the extra DetectLabels call is skipped for clean frames.
        if is_custom and SUPPRESS_ENABLED and labels:
            has_box = any((l.get("Geometry") or {}).get("BoundingBox") for l in labels)
            if has_box:
                try:
                    regions = get_suppression_regions(src_bucket, object_key)
                    labels, n_dropped = suppress_nonveg(labels, regions)
                    if n_dropped:
                        print(f"[Suppress] removed {n_dropped} non-vegetation FP "
                              f"box(es); {len(labels)} label(s) remain")
                except Exception as e:
                    print(f"[Suppress] Non-fatal, keeping all labels: {e}")

        # --- 3c. LLM verification gate ---
        # Rekognition stays the box generator; the LLM only ever judges
        # cropped, zoomed regions (2026-07-21 A/B: crop 13/14 vs whole-image
        # 6/14 - never ask a model to FIND targets in a wide frame).
        # PER-CAMERA opt-in via cam_config.llm_verify_enabled, same pattern as
        # tiling_enabled: this Lambda is shared by every custom-model camera,
        # and moth_cam's domain (adult moths) does not match the larva prompts,
        # so the gate must never run for a camera that hasn't opted in.
        # hybrid_gate_ran must reflect whether a box was ACTUALLY verified, not
        # merely whether apply_llm_verify_gate() returned without raising - every
        # internal failure path returns normally having verified nothing. Using
        # "didn't raise" would let an unverified sub-threshold box collapse
        # detection_floor to 0 below and count as a detection nothing ever
        # stood behind (a real bug found and fixed 2026-07-22).
        hybrid_gate_ran = False
        if (is_custom and LLM_VERIFY_ENABLED
                and bool(cam_config.get("llm_verify_enabled", False))):
            try:
                if frame_bytes is None:
                    obj = s3_client.get_object(Bucket=src_bucket, Key=object_key)
                    frame_bytes = obj["Body"].read()
                labels, n_verified = apply_llm_verify_gate(
                    labels, frame_bytes, target_label, min_confidence)
                hybrid_gate_ran = n_verified > 0
            except Exception as e:
                print(f"[LLMGate] Non-fatal, keeping all labels: {e}")

        # --- 3b. Post-gate cleanup (v5.0) ---
        # Only meaningful on boxes the LLM already judged, so it is skipped
        # entirely when the gate did not run - otherwise the floor would just
        # be a second, weaker copy of min_confidence.
        if hybrid_gate_ran:
            try:
                labels, _ = apply_post_gate_cleanup(
                    labels, target_label,
                    floor_override=cam_config.get("post_verify_floor"))
            except Exception as e:
                print(f"[PostGate] Non-fatal, keeping all labels: {e}")

        # --- 4. Decide target_detected + max confidence ---
        # If the hybrid gate actually verified something, a sub-threshold box
        # that survived the LLM's check is a legitimate detection in its own
        # right - do not re-apply min_confidence here or it would be filtered
        # right back out. If nothing was verified (gate didn't run, or ran but
        # verified zero boxes), fall back to the plain min_confidence floor.
        target_detected   = False
        target_confidence = 0
        detection_floor   = 0 if hybrid_gate_ran else min_confidence
        for label in labels:
            name = label.get("Name", "")
            conf = label.get("Confidence", 0)
            print(f"  - {name}: {conf:.1f}%")
            if name.strip().lower() == target_lower and conf >= detection_floor:
                target_detected = True
                target_confidence = max(target_confidence, conf)

        # --- 5. Extract bounding boxes for the detection record ---
        # No annotated image is produced here. The dashboard fetches the original
        # frame + these normalized coords and draws the boxes on a client canvas.
        bboxes_for_db = []
        if target_detected:
            try:
                raw_boxes = extract_bounding_boxes(labels, target_label, detection_floor, model_type)
                bboxes_for_db = boxes_to_db_format(raw_boxes)
            except Exception as e:
                print(f"[BoundingBox] Non-fatal: {e}")

        # --- 6. Write detection record to DDB (new schema) ---
        # NOTE: this put_item is UNCONDITIONAL (outside the target_detected check)
        # on purpose. Clean frames also write a record, so the patrol completion
        # gate (poll get_item by S3 key) never dead-locks on a clean waypoint.
        detection_time = iso_now()
        # v4.5 fix: a target-label entry must clear the SAME detection_floor
        # bboxes_for_db used, not be dumped verbatim. Whenever the hybrid gate
        # doesn't run at all (llm_verify_enabled off - moth_cam, manual_upload -
        # or simply no sub-threshold candidate that frame), `labels` is still the
        # raw Rekognition response down to its ~30-50% internal gather floor, far
        # below the camera's min_confidence. The dashboard's getVerifiableBoxes()
        # trusts every target-label entry in this field as a confirmed detection
        # (the whole point of the hybrid gate is that a survivor no longer needs
        # a second confidence check) - so an unfiltered low-confidence entry here
        # would read as a real detection on the dashboard for a frame the record's
        # own target_detected already correctly says is clean. Non-target-label
        # entries (informational only) are left untouched.
        legacy_labels  = json.dumps([
            {"name": l.get("Name", ""), "confidence": round(l.get("Confidence", 0), 1)}
            for l in labels
            if l.get("Name", "").strip().lower() != target_lower
            or l.get("Confidence", 0) >= detection_floor
        ])

        db_item = {
            # PK + SK
            "image_id":            object_key,
            "detection_time":      detection_time,

            # GSI partition key + provenance
            "pest_type":           target_label,
            "source":              "live-detection",
            "created_at":          detection_time,

            # Image locations
            "bucket":              src_bucket,
            "original_image_key":  object_key,

            # Camera context
            "camera_id":           camera_id,
            "waypoint_id":         waypoint_id,

            # Detection summary
            "target_label":        target_label,
            "target_detected":     target_detected,
            "target_confidence":   f"{round(target_confidence, 1)}",
            "min_confidence_used": min_confidence,
            "label_count":         len(labels),
            "labels":              legacy_labels,       # legacy JSON-string for dashboard backward compat
            "bboxes":              bboxes_for_db,       # structured; dashboard draws these on a canvas
            "verifications":       {},                  # filled by user clicks in dashboard

            # Model used
            "model_type":          model_type,
            "model_arn":           custom_model_arn if model_type == "custom" else "",
            # Which multimodal model judged the boxes on this frame. Written on
            # every row so a side-by-side A/B stays readable in the dashboard
            # long after the run, without reading CloudWatch.
            "llm_verify_model":    llm_model_id,
        }
        detections_table.put_item(Item=db_item)
        print(f"[DynamoDB] Wrote: image_id={object_key}  detection_time={detection_time}")

        # --- 7. Send SES alert (if target detected and email enabled) ---
        if target_detected and email_enabled and recipients:
            conf_disp = f"{target_confidence:.1f}%"
            subject = f"[FYP Alert] {target_label} detected at {waypoint_id} ({conf_disp})"
            body_lines = [
                "Detection Alert", "",
                f"Pest:       {target_label}",
                f"Confidence: {conf_disp}",
                f"Camera:     {camera_id}",
                f"Zone:       {waypoint_id}",
                f"Model:      {model_type}",
                f"Image:      s3://{src_bucket}/{object_key}",
                f"Time:       {detection_time}", "",
            ]
            body_lines.append("All labels:")
            for label in labels:
                body_lines.append(f"  - {label.get('Name','')}: {label.get('Confidence',0):.1f}%")
            try:
                ses.send_email(
                    Source=SENDER_EMAIL,
                    Destination={"ToAddresses": recipients},
                    Message={
                        "Subject": {"Data": subject, "Charset": "UTF-8"},
                        "Body":    {"Text": {"Data": "\n".join(body_lines), "Charset": "UTF-8"}},
                    },
                )
                print(f"[SES] Sent alert to {len(recipients)} recipient(s)")
            except Exception as e:
                print(f"[SES] Send failed (recipient may be unverified): {e}")
        elif not email_enabled:
            print("[SES] Email globally disabled")
        elif not target_detected:
            print(f"[Result] {target_label} not detected above {min_confidence}%")

        return {
            "statusCode": 200,
            "body": json.dumps({
                "image":            object_key,
                "camera_id":        camera_id,
                "waypoint_id":      waypoint_id,
                "target_detected":  target_detected,
                "label_count":      len(labels),
                "recipients_count": len(recipients),
            }),
        }

    except Exception as e:
        print(f"[FATAL] {e}")
        import traceback
        traceback.print_exc()
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
