"""
=============================================================================
Lambda: pest-monitoring-api  (v4.0 — new account, new schema, merged history)
=============================================================================
Merged + refactored from:
  - pest-control-api v3.6        (913 lines, settings/model/schedule/identities/cost/verify/stream)
  - pest-history-query v3        (113 lines, GET /history)

Key changes from old account:
  - Camera config: nested-map in single `system-config` row
    → per-row in `pest-monitoring-cameras` (one row per camera, PK camera_id)
  - Global config (email/auto_capture/etc): single-row `pest-monitoring-system-config`
  - Detection records: `pest-monitoring-detections` (new schema with pest_type/source/bboxes/etc)
  - Schedule logs:    `pest-monitoring-schedule-logs`
  - All bucket names, table names, account IDs, schedule-executor ARN → env vars
  - GET /settings returns SAME shape as old API (cameras as nested map) for dashboard compat
  - /history merged in (no separate pest-history-query Lambda)
  - WebSocket broadcast: REMOVED (dashboard now polls)
  - Date filtering on /history handles BOTH old space-separated and new ISO 8601
    timestamps so historical (migrated) and live data both queryable

Routes:
  GET    /settings
  POST   /settings
  POST   /model/start
  POST   /model/stop
  GET    /model/status
  GET    /presigned-url
  POST   /detection/verify
  DELETE /detection            (v4.3 — permanent delete: DDB record + S3 objects)
  GET    /history
  GET    /cost
  GET    /identities
  POST   /identities
  DELETE /identities
  GET    /schedule
  POST   /schedule
  DELETE /schedule
  GET    /schedule-logs
  POST   /stream/start
  POST   /stream/stop
  GET    /stream/status
  OPTIONS *                     (CORS preflight)

Runtime: Python 3.12, 256 MB, 30 s timeout
Role:    pest-monitoring-api-role
=============================================================================
"""
import json
import os
import uuid
import boto3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from boto3.dynamodb.conditions import Attr, Key as DDBKey
from botocore.config import Config

# -----------------------------------------------------------------------------
# Configuration via env vars
# -----------------------------------------------------------------------------
AWS_REGION             = os.environ.get("AWS_REGION", "us-east-1")
TABLE_SYSTEM_CONFIG    = os.environ.get("TABLE_SYSTEM_CONFIG",    "pest-monitoring-system-config")
TABLE_CAMERAS          = os.environ.get("TABLE_CAMERAS",          "pest-monitoring-cameras")
TABLE_DETECTIONS       = os.environ.get("TABLE_DETECTIONS",       "pest-monitoring-detections")
TABLE_SCHEDULE_LOGS    = os.environ.get("TABLE_SCHEDULE_LOGS",    "pest-monitoring-schedule-logs")
S3_FRAMES_BUCKET       = os.environ["S3_FRAMES_BUCKET"]                    # required (original frames)
S3_PROCESSED_BUCKET    = os.environ["S3_PROCESSED_BUCKET"]                 # required (annotated images)
SCHEDULE_EXECUTOR_ARN  = os.environ.get("SCHEDULE_EXECUTOR_ARN", "")       # filled after pest-camera-scheduler deploy
CONFIG_KEY             = "detection_settings"

# -----------------------------------------------------------------------------
# AWS clients & resources
# -----------------------------------------------------------------------------
dynamodb       = boto3.resource("dynamodb",   region_name=AWS_REGION)
config_table   = dynamodb.Table(TABLE_SYSTEM_CONFIG)
cameras_table  = dynamodb.Table(TABLE_CAMERAS)
detection_tbl  = dynamodb.Table(TABLE_DETECTIONS)
logs_table     = dynamodb.Table(TABLE_SCHEDULE_LOGS)

rekognition    = boto3.client("rekognition",  region_name=AWS_REGION)
s3             = boto3.client("s3",           region_name=AWS_REGION,
                              config=Config(signature_version="s3v4"))
ses            = boto3.client("ses",          region_name=AWS_REGION)
events         = boto3.client("events",       region_name=AWS_REGION)
lambda_client  = boto3.client("lambda",       region_name=AWS_REGION)

# -----------------------------------------------------------------------------
# Allow-listed field names — protect DDB writes from arbitrary key injection
# -----------------------------------------------------------------------------
GLOBAL_ALLOWED = {
    "email_enabled", "recipient_email", "additional_recipients",
    "auto_capture", "capture_interval",
}
CAMERA_ALLOWED = {
    "label", "target_label", "model_type", "custom_model_arn",
    "min_confidence", "model_running", "default_waypoint_id",
    "kvs_stream_name", "stream_enabled", "tiling_enabled",
    "llm_verify_enabled",  # v4.5 hybrid gate opt-in; without this, only
                           # direct DynamoDB access can flip it per camera
    "llm_model_id",        # v6.3: per-camera AI verification model, stored as
                           # an ALIAS ('sonnet46'/'haiku45') that the processor
                           # resolves; a Test upload's __llm- key still wins
                           # for that one run
    "post_verify_floor",   # v6.2: the dashboard threshold edits THIS (the
                           # display/denoise floor after the AI check), never
                           # min_confidence (the candidate floor before it)
    "max_runtime_min",     # v6.2: per-camera watchdog auto-stop window
}

DAY_MAP = {"Mon": "MON", "Tue": "TUE", "Wed": "WED", "Thu": "THU",
           "Fri": "FRI", "Sat": "SAT", "Sun": "SUN"}


# =============================================================================
# COMMON HELPERS
# =============================================================================
def cors_response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type":                  "application/json",
            "Access-Control-Allow-Origin":   "*",
            "Access-Control-Allow-Methods":  "GET,POST,DELETE,OPTIONS",
            "Access-Control-Allow-Headers":  "Content-Type,Authorization",
        },
        "body": json.dumps(body, default=_json_default),
    }


def _json_default(o):
    if isinstance(o, Decimal):
        return int(o) if o == int(o) else float(o)
    if isinstance(o, set):
        return list(o)
    if isinstance(o, datetime):
        return o.isoformat() + "Z"
    raise TypeError(f"Not serializable: {type(o)}")


def _account_id_from_context(context):
    """Extract our own AWS account ID from the Lambda invocation context.
    Used for building EventBridge rule SourceArn dynamically."""
    try:
        return context.invoked_function_arn.split(":")[4]
    except Exception:
        return ""


# =============================================================================
# DDB READERS — split into per-camera vs global config (new schema)
# =============================================================================
def get_system_config():
    """Single-row global config (email recipients, auto_capture, etc)."""
    try:
        r = config_table.get_item(Key={"config_key": CONFIG_KEY})
        return r.get("Item", {}) or {}
    except Exception as e:
        print(f"[SystemConfig] read failed: {e}")
        return {}


def get_camera(camera_id):
    """Read a single camera row from pest-monitoring-cameras."""
    try:
        r = cameras_table.get_item(Key={"camera_id": camera_id})
        return r.get("Item")
    except Exception as e:
        print(f"[Cameras] read failed for {camera_id}: {e}")
        return None


def list_cameras_as_map():
    """Scan pest-monitoring-cameras and return {camera_id: {fields...}}
    matching the OLD nested-map shape so dashboard JS doesn't need changes."""
    cams = {}
    try:
        resp = cameras_table.scan()
        for it in resp.get("Items", []):
            cid = it.get("camera_id")
            if cid:
                # Inner dict mirrors old shape (no camera_id key inside since
                # it's already the outer key)
                cams[cid] = {k: v for k, v in it.items() if k != "camera_id"}
        while "LastEvaluatedKey" in resp:
            resp = cameras_table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            for it in resp.get("Items", []):
                cid = it.get("camera_id")
                if cid:
                    cams[cid] = {k: v for k, v in it.items() if k != "camera_id"}
    except Exception as e:
        print(f"[Cameras] list failed: {e}")
    return cams


# =============================================================================
# DDB WRITERS
# =============================================================================
def update_system_config(updates):
    if not updates:
        return
    expr_parts, expr_vals, expr_names = [], {}, {}
    for k, v in updates.items():
        expr_parts.append(f"#{k} = :{k}")
        expr_vals[f":{k}"]  = v
        expr_names[f"#{k}"] = k
    config_table.update_item(
        Key={"config_key": CONFIG_KEY},
        UpdateExpression="SET " + ", ".join(expr_parts),
        ExpressionAttributeValues=expr_vals,
        ExpressionAttributeNames=expr_names,
    )


def update_camera(camera_id, fields):
    """Update specific fields on a single camera row.
    MUCH simpler than the old nested-map update_camera_fields() because
    new schema is one row per camera."""
    if not fields or not camera_id:
        return
    expr_parts, expr_vals, expr_names = [], {}, {}
    for k, v in fields.items():
        expr_parts.append(f"#{k} = :{k}")
        expr_vals[f":{k}"]  = v
        expr_names[f"#{k}"] = k
    cameras_table.update_item(
        Key={"camera_id": camera_id},
        UpdateExpression="SET " + ", ".join(expr_parts),
        ExpressionAttributeValues=expr_vals,
        ExpressionAttributeNames=expr_names,
    )


# =============================================================================
# /settings — returns merged global + cameras for dashboard compat
# =============================================================================
def handle_get_settings():
    sys_cfg = get_system_config()
    cameras = list_cameras_as_map()
    # Merge to match old single-row response shape: {config_key, ...global..., cameras: {...}}
    return cors_response(200, {**sys_cfg, "cameras": cameras})


def handle_post_settings(body):
    updated = []

    # ---- Per-camera update path ----
    if "camera_id" in body and "fields" in body:
        cam_id = body["camera_id"]
        raw    = body["fields"] or {}
        fields = {}
        for k, v in raw.items():
            if k not in CAMERA_ALLOWED:
                continue
            if k == "min_confidence":
                v = int(v)
            fields[k] = v
        if fields:
            update_camera(cam_id, fields)
            updated.append({"camera_id": cam_id, "fields": list(fields.keys())})

    # ---- Global update path ----
    global_src = body.get("global") if isinstance(body.get("global"), dict) else {}
    if not global_src:
        global_src = {k: v for k, v in body.items() if k in GLOBAL_ALLOWED}
    global_updates = {}
    for k, v in global_src.items():
        if k not in GLOBAL_ALLOWED:
            continue
        if k == "capture_interval":
            v = int(v)
        global_updates[k] = v
    if global_updates:
        update_system_config(global_updates)
        updated.append({"global": list(global_updates.keys())})

    return cors_response(200, {"message": "Settings updated", "updated": updated})


# =============================================================================
# /model/*
# =============================================================================
def _camera_arn(camera_id):
    cam = get_camera(camera_id)
    if not cam:
        raise ValueError(f"Unknown camera: {camera_id}")
    arn = cam.get("custom_model_arn", "")
    if not arn or str(arn).startswith("REPLACE_"):
        raise ValueError(f"Camera {camera_id} has no model ARN configured")
    return arn


def handle_model_start(camera_id):
    try:
        arn = _camera_arn(camera_id)
        rekognition.start_project_version(ProjectVersionArn=arn, MinInferenceUnits=1)
        update_camera(camera_id, {"model_running": True})
        return cors_response(200, {"message": "Model start initiated",
                                   "camera_id": camera_id, "arn": arn})
    except Exception as e:
        return cors_response(500, {"error": str(e), "camera_id": camera_id})


def handle_model_stop(camera_id):
    try:
        arn = _camera_arn(camera_id)
        rekognition.stop_project_version(ProjectVersionArn=arn)
        update_camera(camera_id, {"model_running": False})
        return cors_response(200, {"message": "Model stop initiated",
                                   "camera_id": camera_id, "arn": arn})
    except Exception as e:
        return cors_response(500, {"error": str(e), "camera_id": camera_id})


def handle_model_status(camera_id):
    cameras = {camera_id: get_camera(camera_id)} if camera_id else list_cameras_as_map()
    # Re-key for status output (preserve camera_id key, value is the camera dict)
    if camera_id and cameras[camera_id] is None:
        return cors_response(404, {"error": f"Unknown camera: {camera_id}"})

    try:
        projects_resp = rekognition.describe_projects()
        project_arns = [p["ProjectArn"] for p in projects_resp.get("ProjectDescriptions", [])]
    except Exception as e:
        return cors_response(500, {"error": f"describe_projects failed: {e}"})

    results = {}
    for cid, cam in cameras.items():
        if not cam:
            results[cid] = {"status": "UNKNOWN_CAMERA"}
            continue
        version_arn = cam.get("custom_model_arn", "")
        if cam.get("model_type") != "custom" or not version_arn or str(version_arn).startswith("REPLACE_"):
            results[cid] = {"status": "NOT_CONFIGURED",
                           "model_type": cam.get("model_type", "general")}
            continue
        # Match the project portion of the version ARN against describe_projects
        proj_arn = None
        if "/version/" in version_arn:
            target_project_prefix = version_arn.split("/version/")[0]
            for parn in project_arns:
                if parn.startswith(target_project_prefix + "/"):
                    proj_arn = parn
                    break
        if not proj_arn:
            results[cid] = {"status": "NOT_FOUND", "arn": version_arn}
            continue
        try:
            vresp = rekognition.describe_project_versions(ProjectArn=proj_arn)
            real_status, msg = "NOT_FOUND", ""
            for v in vresp.get("ProjectVersionDescriptions", []):
                if v.get("ProjectVersionArn") == version_arn:
                    real_status = v.get("Status", "UNKNOWN")
                    msg = v.get("StatusMessage", "")
                    break
            results[cid] = {"status": real_status, "arn": version_arn, "message": msg}
        except Exception as e:
            results[cid] = {"status": "ERROR", "error": str(e)}

    if camera_id:
        return cors_response(200, {**results.get(camera_id, {"status": "UNKNOWN"}),
                                   "camera_id": camera_id})
    return cors_response(200, {"cameras": results})


# =============================================================================
# /presigned-url — supports POST upload (v3.4.4) + GET download
# =============================================================================
def handle_presigned_url(params):
    key = params.get("key", "")
    method = params.get("method", "GET").upper()
    if not key:
        return cors_response(400, {"error": "Missing 'key' parameter"})
    if method not in ("GET", "PUT"):
        return cors_response(400, {"error": "method must be GET or PUT"})
    try:
        if method == "PUT":
            response = s3.generate_presigned_post(
                Bucket=S3_FRAMES_BUCKET,
                Key=key,
                ExpiresIn=3600,
                Conditions=[["content-length-range", 1, 25 * 1024 * 1024]],
            )
            return cors_response(200, {"url": response["url"],
                                       "fields": response["fields"],
                                       "method": "POST", "key": key})
        # GET: pick the bucket by key shape.
        #   "frames/{cam}/{wp}/{file}"  → original frame      → frames bucket
        #   "{cam}/{wp}/{file}" (no prefix) → annotated/processed → processed bucket
        bucket = S3_FRAMES_BUCKET if key.startswith("frames/") else S3_PROCESSED_BUCKET
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=3600,
        )
        return cors_response(200, {"url": url, "method": "GET", "key": key, "bucket": bucket})
    except Exception as e:
        return cors_response(500, {"error": str(e)})


# =============================================================================
# /detection/verify  (v3.6 — per-bbox verdicts + legacy v3.4 image-level)
# =============================================================================
def handle_verify_detection(body):
    image_id = body.get("image_id")
    if not image_id:
        return cors_response(400, {"error": "image_id required"})

    # Resolve composite (image_id, detection_time) by querying for the row
    try:
        qresp = detection_tbl.query(
            KeyConditionExpression=DDBKey("image_id").eq(image_id), Limit=1,
        )
        items = qresp.get("Items", [])
        if not items:
            return cors_response(404, {"error": "No detection record found for image_id"})
        full_key = {"image_id": image_id, "detection_time": items[0]["detection_time"]}
    except Exception as e:
        return cors_response(500, {"error": f"Lookup failed: {e}"})

    bbox_index = body.get("bbox_index")
    verdict    = body.get("verdict")
    is_per_bbox = bbox_index is not None
    is_legacy   = (not is_per_bbox) and isinstance(verdict, (bool, type(None)))

    # ---- Per-bbox path (v3.6) ----
    if is_per_bbox:
        try:
            bbox_index = int(bbox_index)
        except (TypeError, ValueError):
            return cors_response(400, {"error": "bbox_index must be an integer"})
        if bbox_index < 0:
            return cors_response(400, {"error": "bbox_index must be non-negative"})
        if verdict not in ("TP", "FP", None):
            return cors_response(400, {"error": "verdict must be 'TP', 'FP', or null"})

        bbox_key = str(bbox_index)
        try:
            if verdict is None:
                try:
                    detection_tbl.update_item(
                        Key=full_key,
                        UpdateExpression="REMOVE verifications.#k",
                        ExpressionAttributeNames={"#k": bbox_key},
                        ConditionExpression="attribute_exists(verifications)",
                    )
                except detection_tbl.meta.client.exceptions.ConditionalCheckFailedException:
                    pass
                return cors_response(200, {"ok": True, "image_id": image_id,
                                           "bbox_index": bbox_index, "verdict": None})

            detection_tbl.update_item(
                Key=full_key,
                UpdateExpression="SET verifications = if_not_exists(verifications, :empty)",
                ExpressionAttributeValues={":empty": {}},
            )
            detection_tbl.update_item(
                Key=full_key,
                UpdateExpression="SET verifications.#k = :v",
                ExpressionAttributeNames={"#k": bbox_key},
                ExpressionAttributeValues={":v": verdict},
            )
            return cors_response(200, {"ok": True, "image_id": image_id,
                                       "bbox_index": bbox_index, "verdict": verdict})
        except Exception as e:
            return cors_response(500, {"error": str(e)})

    # ---- Legacy image-level path (v3.4 callers) ----
    if is_legacy:
        try:
            if verdict is None:
                detection_tbl.update_item(Key=full_key, UpdateExpression="REMOVE verified")
                return cors_response(200, {"ok": True, "image_id": image_id,
                                           "verified": None, "mode": "legacy"})
            detection_tbl.update_item(
                Key=full_key,
                UpdateExpression="SET verified = :v",
                ExpressionAttributeValues={":v": bool(verdict)},
            )
            return cors_response(200, {"ok": True, "image_id": image_id,
                                       "verified": bool(verdict), "mode": "legacy"})
        except Exception as e:
            return cors_response(500, {"error": str(e)})

    return cors_response(400, {
        "error": "Body must include either {bbox_index, verdict} for per-bbox or {verdict: bool|null} for legacy"
    })


# =============================================================================
# DELETE /detection  (v4.3 — gallery permanent delete)
# =============================================================================
def handle_delete_detection(params):
    """
    DELETE /detection?image_id=<key>
    Permanently removes one detection: EVERY DynamoDB row for that image_id
    (at-least-once S3 events can write duplicate rows sharing the PK) plus the
    S3 objects those rows reference. Safety gates (this endpoint must never
    become an arbitrary S3 key deleter — the frames bucket also holds training
    assets under assets/ and datasets/):
      1. image_id MUST have at least one detection record; none = 404 and
         NOTHING is touched.
      2. Frame objects are only deleted under the "frames/" prefix (every
         capture record lives there; training assets are out of reach).
      3. Any S3 delete failure aborts with 500 BEFORE rows are removed — the
         record is the only index to the object, so it must outlive it. The
         call is safely retryable (delete_object on a gone key is a no-op).
    """
    image_id = ((params or {}).get("image_id") or "").strip()
    if not image_id:
        return cors_response(400, {"error": "image_id required"})

    # Collect ALL rows for this image_id (composite PK+SK permits duplicates)
    try:
        rows = []
        resp = detection_tbl.query(KeyConditionExpression=DDBKey("image_id").eq(image_id))
        rows.extend(resp.get("Items", []))
        while "LastEvaluatedKey" in resp:
            resp = detection_tbl.query(
                KeyConditionExpression=DDBKey("image_id").eq(image_id),
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            rows.extend(resp.get("Items", []))
        if not rows:
            return cors_response(404, {"error": "No detection record found for image_id"})
    except Exception as e:
        return cors_response(500, {"error": f"Lookup failed: {e}"})

    frame_keys     = {r.get("original_image_key") or image_id for r in rows}
    processed_keys = {r["processed_image_key"] for r in rows if r.get("processed_image_key")}
    deleted = {"frames": 0, "processed": 0, "records": 0, "skipped_non_frames_keys": 0}

    # S3 objects first, rows last: a failure here returns 500 with the rows
    # intact, so the user can simply retry.
    try:
        for k in frame_keys:
            if k.startswith("frames/"):
                s3.delete_object(Bucket=S3_FRAMES_BUCKET, Key=k)
                deleted["frames"] += 1
            else:
                deleted["skipped_non_frames_keys"] += 1
        for k in processed_keys:
            s3.delete_object(Bucket=S3_PROCESSED_BUCKET, Key=k)
            deleted["processed"] += 1
    except Exception as e:
        return cors_response(500, {"error": f"S3 delete failed (records kept — retry): {e}",
                                   "deleted": deleted})

    try:
        for r in rows:
            detection_tbl.delete_item(
                Key={"image_id": image_id, "detection_time": r["detection_time"]})
            deleted["records"] += 1
    except Exception as e:
        return cors_response(500, {"error": f"Record delete failed: {e}", "deleted": deleted})

    return cors_response(200, {"ok": True, "image_id": image_id, "deleted": deleted})


# =============================================================================
# /history  (merged from pest-history-query v3)
# Handles BOTH old space-separated and new ISO 8601 detection_time formats.
# =============================================================================
def handle_get_history(params):
    """
    Query parameters (all optional):
      camera, zone, model (substring on target_label),
      detected (true|false),
      date_from / date_to (YYYY-MM-DD inclusive),
      pest_type, source,
      limit (default 100, max 500)
    """
    try:
        camera = (params.get("camera") or "").strip()
        zone   = (params.get("zone") or "").strip()
        model_substring = (params.get("model") or "").strip().lower()
        detected  = (params.get("detected") or "").strip().lower()
        date_from = (params.get("date_from") or "").strip()
        date_to   = (params.get("date_to") or "").strip()
        pest_type = (params.get("pest_type") or "").strip()
        source    = (params.get("source") or "").strip()
        try:
            limit = max(1, min(int(params.get("limit", 100)), 500))
        except ValueError:
            limit = 100

        filters = []
        if camera:    filters.append(Attr("camera_id").eq(camera))
        if zone:      filters.append(Attr("waypoint_id").eq(zone))
        if detected == "true":  filters.append(Attr("target_detected").eq(True))
        elif detected == "false": filters.append(Attr("target_detected").eq(False))
        if pest_type: filters.append(Attr("pest_type").eq(pest_type))
        if source:    filters.append(Attr("source").eq(source))

        # Date filter — string range works for BOTH formats because:
        #   "2026-05-04"          < "2026-05-04 07:00:27" (old)
        #   "2026-05-04"          < "2026-05-04T07:00:27Z" (new)
        #   "2026-05-05"          > both upper bounds within 2026-05-04
        if date_from:
            filters.append(Attr("detection_time").gte(date_from))
        if date_to:
            try:
                next_day = (datetime.strptime(date_to, "%Y-%m-%d")
                            + timedelta(days=1)).strftime("%Y-%m-%d")
                filters.append(Attr("detection_time").lt(next_day))
            except ValueError:
                filters.append(Attr("detection_time").lte(date_to + "T23:59:59Z"))

        scan_kwargs = {}
        if filters:
            expr = filters[0]
            for f in filters[1:]:
                expr = expr & f
            scan_kwargs["FilterExpression"] = expr

        items, scanned = [], 0
        while True:
            resp = detection_tbl.scan(**scan_kwargs)
            scanned += resp.get("ScannedCount", 0)
            for item in resp.get("Items", []):
                if model_substring:
                    if model_substring not in str(item.get("target_label", "")).lower():
                        continue
                items.append(item)
            if "LastEvaluatedKey" not in resp or scanned > 10000:
                break
            scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

        items.sort(key=lambda x: str(x.get("detection_time", "")), reverse=True)
        items = items[:limit]
        return cors_response(200, {"items": items, "count": len(items), "scanned": scanned})

    except Exception as e:
        import traceback; traceback.print_exc()
        return cors_response(500, {"error": str(e)})


# =============================================================================
# /cost (Cost Explorer)
# =============================================================================
def handle_cost(params=None):
    params = params or {}
    try:
        days = int(params.get("days", 30))
    except (ValueError, TypeError):
        days = 30
    days = max(1, min(days, 365))

    granularity = (params.get("granularity") or "").upper()
    if granularity not in ("DAILY", "MONTHLY"):
        granularity = "DAILY" if days <= 60 else "MONTHLY"

    try:
        ce = boto3.client("ce", region_name=AWS_REGION)
        end   = datetime.utcnow().strftime("%Y-%m-%d")
        start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")

        response = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity=granularity,
            Metrics=["UnblendedCost", "UsageQuantity"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )

        buckets = []
        for period in response.get("ResultsByTime", []):
            entry = {"date": period["TimePeriod"]["Start"],
                     "end":  period["TimePeriod"]["End"],
                     "services": {}, "total": 0.0}
            for group in period.get("Groups", []):
                service = group["Keys"][0]
                amount  = float(group["Metrics"]["UnblendedCost"]["Amount"])
                usage   = float(group["Metrics"]["UsageQuantity"]["Amount"])
                if amount > 0.0001:
                    entry["services"][service] = {"cost": round(amount, 4),
                                                  "usage": round(usage, 4)}
                    entry["total"] += amount
            entry["total"] = round(entry["total"], 4)
            buckets.append(entry)

        service_totals, service_usage = {}, {}
        grand_total = 0.0
        for b in buckets:
            for svc, info in b["services"].items():
                service_totals[svc] = round(service_totals.get(svc, 0) + info["cost"], 4)
                service_usage[svc]  = round(service_usage.get(svc, 0)  + info["usage"], 4)
            grand_total += b["total"]

        sorted_services = dict(sorted(service_totals.items(), key=lambda x: -x[1]))

        try:
            mtd_start = datetime.utcnow().replace(day=1).strftime("%Y-%m-%d")
            mtd_resp = ce.get_cost_and_usage(
                TimePeriod={"Start": mtd_start, "End": end},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
            )
            mtd_total = sum(
                float(p["Total"]["UnblendedCost"]["Amount"])
                for p in mtd_resp.get("ResultsByTime", [])
            )
        except Exception:
            mtd_total = None

        return cors_response(200, {
            "period":        {"start": start, "end": end, "days": days},
            "granularity":   granularity,
            "buckets":       buckets,
            "daily":         buckets,         # backwards compat
            "service_totals": sorted_services,
            "service_usage": service_usage,
            "grand_total":   round(grand_total, 4),
            "month_to_date": round(mtd_total, 4) if mtd_total is not None else None,
            "currency":      "USD",
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return cors_response(500, {"error": str(e)})


# =============================================================================
# /identities (SES subscribers managed via system-config table)
# =============================================================================
def handle_get_identities():
    try:
        sys_cfg         = get_system_config()
        recipient_email = sys_cfg.get("recipient_email", "")
        additional      = sys_cfg.get("additional_recipients", [])
        if isinstance(additional, str):
            additional = [e.strip() for e in additional.split(",") if e.strip()]

        all_emails = list(dict.fromkeys(
            ([recipient_email] if recipient_email else []) + list(additional)
        ))
        if not all_emails:
            return cors_response(200, {"emails": [], "primary": recipient_email})

        ses_resp = ses.get_identity_verification_attributes(Identities=all_emails)
        attrs = ses_resp.get("VerificationAttributes", {})

        result = []
        for email in all_emails:
            attr = attrs.get(email, {})
            result.append({"email": email,
                          "is_primary": email == recipient_email,
                          "verification_status": attr.get("VerificationStatus", "NotFound")})
        return cors_response(200, {"emails": result, "primary": recipient_email})
    except Exception as e:
        return cors_response(500, {"error": str(e)})


def handle_post_identity(body):
    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return cors_response(400, {"error": "Valid 'email' required"})
    try:
        ses.verify_email_identity(EmailAddress=email)
        sys_cfg = get_system_config()
        additional = sys_cfg.get("additional_recipients", [])
        if isinstance(additional, str):
            additional = [e.strip() for e in additional.split(",") if e.strip()]
        if email not in additional and email != sys_cfg.get("recipient_email", ""):
            additional.append(email)
            update_system_config({"additional_recipients": additional})
        return cors_response(200, {"message": "Verification email sent", "email": email,
                                   "note": "User must click verification link to receive alerts"})
    except Exception as e:
        return cors_response(500, {"error": str(e)})


def handle_delete_identity(params):
    email = (params.get("email") or "").strip().lower()
    if not email:
        return cors_response(400, {"error": "Missing 'email' query parameter"})
    sys_cfg = get_system_config()
    if email == sys_cfg.get("recipient_email", ""):
        return cors_response(400, {"error": "Cannot delete primary recipient. "
                                            "Change 'recipient_email' via /settings first."})
    try:
        try:
            ses.delete_identity(Identity=email)
        except Exception as e:
            print(f"[SES] delete_identity non-fatal: {e}")
        additional = sys_cfg.get("additional_recipients", [])
        if isinstance(additional, str):
            additional = [e.strip() for e in additional.split(",") if e.strip()]
        if email in additional:
            additional.remove(email)
            update_system_config({"additional_recipients": additional})
        return cors_response(200, {"message": "Removed", "email": email})
    except Exception as e:
        return cors_response(500, {"error": str(e)})


# =============================================================================
# /schedule (EventBridge Rules — pest-sched-* prefix)
# =============================================================================
def _rule_name(camera_id, action):
    return f"pest-sched-{camera_id}-{action}"


def _cron_expression(time_str, days):
    """Dashboard times are Singapore local; EventBridge RULE crons are UTC.
    The original version passed the SGT time straight through, so every
    schedule fired 8 hours late. SGT = UTC+8 with no DST, so the conversion is
    a fixed -8h; when that crosses midnight the DAY list must shift back one
    (Mon 05:40 SGT is Sun 21:40 UTC)."""
    try:
        hh, mm = time_str.split(":")
        hh, mm = int(hh), int(mm)
    except Exception:
        raise ValueError(f"Invalid time '{time_str}', expected HH:MM")
    if not 0 <= hh <= 23 or not 0 <= mm <= 59:
        raise ValueError(f"Time out of range: {time_str}")
    hh -= 8
    shift = 0
    if hh < 0:
        hh += 24
        shift = -1
    order = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    day_codes = [DAY_MAP.get(d, d.upper()) for d in days] if days else order[:]
    if shift:
        day_codes = [order[(order.index(d) + shift) % 7] for d in day_codes
                     if d in order]
    day_list = ",".join(day_codes) if day_codes else "MON-SUN"
    return f"cron({mm} {hh} ? * {day_list} *)"


def _put_scheduled_rule(camera_id, action, time_str, days, account_id):
    if not SCHEDULE_EXECUTOR_ARN:
        raise RuntimeError("SCHEDULE_EXECUTOR_ARN env var not set — deploy pest-camera-scheduler first")
    rule_name   = _rule_name(camera_id, action)
    schedule_id = str(uuid.uuid4())
    events.put_rule(Name=rule_name,
                    ScheduleExpression=_cron_expression(time_str, days),
                    State="ENABLED",
                    Description=f"Scheduled {action} for {camera_id}")
    try:
        lambda_client.add_permission(
            FunctionName=SCHEDULE_EXECUTOR_ARN,
            StatementId=f"sched-invoke-{rule_name}",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=f"arn:aws:events:{AWS_REGION}:{account_id}:rule/{rule_name}",
        )
    except lambda_client.exceptions.ResourceConflictException:
        pass  # permission already exists
    events.put_targets(
        Rule=rule_name,
        Targets=[{
            "Id":  "1",
            "Arn": SCHEDULE_EXECUTOR_ARN,
            "Input": json.dumps({"camera_id": camera_id, "action": action,
                                 "schedule_id": schedule_id, "trigger_time": time_str}),
        }],
    )
    return rule_name, schedule_id


def _delete_scheduled_rule(camera_id, action):
    rule_name = _rule_name(camera_id, action)
    try:    events.remove_targets(Rule=rule_name, Ids=["1"])
    except Exception: pass
    try:    events.delete_rule(Name=rule_name)
    except Exception: pass
    try:
        lambda_client.remove_permission(
            FunctionName=SCHEDULE_EXECUTOR_ARN,
            StatementId=f"sched-invoke-{rule_name}",
        )
    except Exception: pass


def handle_get_schedule(params):
    camera_id = params.get("camera")
    if camera_id:
        cam = get_camera(camera_id)
        if not cam:
            return cors_response(404, {"error": f"Unknown camera: {camera_id}"})
        return cors_response(200, {"camera_id": camera_id,
                                   "schedule": cam.get("schedule") or {}})
    cameras = list_cameras_as_map()
    all_schedules = {cid: (cam.get("schedule") or {}) for cid, cam in cameras.items()}
    return cors_response(200, {"schedules": all_schedules})


def handle_post_schedule(body, context):
    camera_id = body.get("camera_id")
    enabled = bool(body.get("enabled", False))
    start_time = body.get("start_time")
    days = body.get("days", [])
    if not camera_id:
        return cors_response(400, {"error": "camera_id required"})
    if enabled and not start_time:
        return cors_response(400, {"error": "start_time required when enabled=true"})
    if not get_camera(camera_id):
        return cors_response(404, {"error": f"Unknown camera: {camera_id}"})

    account_id = _account_id_from_context(context)
    try:
        # v6.2 (Runzhe): start-only scheduling. The model starts at the chosen
        # time and the watchdog closes it after the camera's max_runtime_min
        # (per-camera DDB field, default 75). No stop time exists any more;
        # a legacy stop rule is deleted on every save so old pairs die off.
        _delete_scheduled_rule(camera_id, "stop")
        if enabled:
            _put_scheduled_rule(camera_id, "start", start_time, days, account_id)
            schedule = {"enabled": True,
                        "start_time": start_time, "days": days,
                        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
        else:
            _delete_scheduled_rule(camera_id, "start")
            schedule = {"enabled": False,
                        "start_time": start_time, "days": days,
                        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
        update_camera(camera_id, {"schedule": schedule})
        return cors_response(200, {"message": "Schedule updated",
                                   "camera_id": camera_id, "schedule": schedule})
    except Exception as e:
        import traceback; traceback.print_exc()
        return cors_response(500, {"error": str(e)})


def handle_delete_schedule(params):
    camera_id = params.get("camera")
    if not camera_id:
        return cors_response(400, {"error": "camera required"})
    try:
        _delete_scheduled_rule(camera_id, "start")
        _delete_scheduled_rule(camera_id, "stop")
        update_camera(camera_id, {"schedule": {"enabled": False}})
        return cors_response(200, {"message": "Schedule deleted", "camera_id": camera_id})
    except Exception as e:
        return cors_response(500, {"error": str(e)})


# =============================================================================
# /schedule-logs
# =============================================================================
def handle_get_schedule_logs(params):
    camera_id = params.get("camera")
    limit = min(int(params.get("limit", 50)), 500)
    try:
        scan_kwargs = {}
        if camera_id:
            scan_kwargs["FilterExpression"] = Attr("camera_id").eq(camera_id)
        items = []
        while len(items) < limit:
            resp = logs_table.scan(**scan_kwargs)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            scan_kwargs["ExclusiveStartKey"] = lek
        items.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return cors_response(200, {"logs": items[:limit], "count": min(len(items), limit)})
    except Exception as e:
        return cors_response(500, {"error": str(e)})


# =============================================================================
# /stream/* — KVS placeholder, just toggles cameras.X.stream_enabled
# =============================================================================
def handle_stream_start(camera_id):
    cam = get_camera(camera_id)
    if not cam:
        return cors_response(404, {"error": f"Unknown camera: {camera_id}"})
    if not cam.get("kvs_stream_name"):
        return cors_response(400, {"error": f"Camera {camera_id} has no kvs_stream_name configured"})
    update_camera(camera_id, {"stream_enabled": True})
    return cors_response(200, {"camera_id": camera_id, "stream_enabled": True,
                               "kvs_stream_name": cam.get("kvs_stream_name"),
                               "message": "Stream enabled. Producer will start pushing on next poll."})


def handle_stream_stop(camera_id):
    cam = get_camera(camera_id)
    if not cam:
        return cors_response(404, {"error": f"Unknown camera: {camera_id}"})
    update_camera(camera_id, {"stream_enabled": False})
    return cors_response(200, {"camera_id": camera_id, "stream_enabled": False,
                               "kvs_stream_name": cam.get("kvs_stream_name"),
                               "message": "Stream disabled."})


def handle_stream_status(camera_id):
    cameras = {camera_id: get_camera(camera_id)} if camera_id else list_cameras_as_map()
    if camera_id and cameras[camera_id] is None:
        return cors_response(404, {"error": f"Unknown camera: {camera_id}"})
    result = {}
    for cid, cam in cameras.items():
        if not cam:
            result[cid] = {"status": "UNKNOWN_CAMERA"}; continue
        kvs = cam.get("kvs_stream_name")
        if not kvs:
            result[cid] = {"stream_enabled": False, "kvs_stream_name": None,
                          "status": "NOT_STREAMABLE"}
            continue
        result[cid] = {"stream_enabled": bool(cam.get("stream_enabled", False)),
                      "kvs_stream_name": kvs,
                      "status": "ENABLED" if cam.get("stream_enabled") else "DISABLED"}
    if camera_id:
        return cors_response(200, {**result.get(camera_id, {"status": "UNKNOWN"}),
                                   "camera_id": camera_id})
    return cors_response(200, {"streams": result})


# =============================================================================
# ROUTER
# =============================================================================
def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path   = event.get("requestContext", {}).get("http", {}).get("path", "/")
    params = event.get("queryStringParameters", {}) or {}

    if method == "OPTIONS":
        return cors_response(200, {"message": "OK"})

    try:
        # /settings
        if path == "/settings" and method == "GET":
            return handle_get_settings()
        if path == "/settings" and method == "POST":
            return handle_post_settings(json.loads(event.get("body", "{}")))

        # /model
        if path == "/model/start" and method == "POST":
            body = json.loads(event.get("body", "{}"))
            cid = body.get("camera_id") or params.get("camera")
            if not cid: return cors_response(400, {"error": "camera_id required"})
            return handle_model_start(cid)
        if path == "/model/stop" and method == "POST":
            body = json.loads(event.get("body", "{}"))
            cid = body.get("camera_id") or params.get("camera")
            if not cid: return cors_response(400, {"error": "camera_id required"})
            return handle_model_stop(cid)
        if path == "/model/status" and method == "GET":
            return handle_model_status(params.get("camera"))

        # /presigned-url
        if path == "/presigned-url" and method == "GET":
            return handle_presigned_url(params)

        # /detection  (v4.3: permanent delete)
        if path == "/detection" and method == "DELETE":
            return handle_delete_detection(params)

        # /detection/verify
        if path == "/detection/verify" and method == "POST":
            return handle_verify_detection(json.loads(event.get("body", "{}")))

        # /history (merged from pest-history-query)
        if path == "/history" and method == "GET":
            return handle_get_history(params)

        # /cost
        if path == "/cost" and method == "GET":
            return handle_cost(params)

        # /identities
        if path == "/identities" and method == "GET":
            return handle_get_identities()
        if path == "/identities" and method == "POST":
            return handle_post_identity(json.loads(event.get("body", "{}")))
        if path == "/identities" and method == "DELETE":
            return handle_delete_identity(params)

        # /schedule
        if path == "/schedule" and method == "GET":
            return handle_get_schedule(params)
        if path == "/schedule" and method == "POST":
            return handle_post_schedule(json.loads(event.get("body", "{}")), context)
        if path == "/schedule" and method == "DELETE":
            return handle_delete_schedule(params)

        # /schedule-logs
        if path == "/schedule-logs" and method == "GET":
            return handle_get_schedule_logs(params)

        # /stream
        if path == "/stream/start" and method == "POST":
            body = json.loads(event.get("body", "{}"))
            cid = body.get("camera_id") or params.get("camera")
            if not cid: return cors_response(400, {"error": "camera_id required"})
            return handle_stream_start(cid)
        if path == "/stream/stop" and method == "POST":
            body = json.loads(event.get("body", "{}"))
            cid = body.get("camera_id") or params.get("camera")
            if not cid: return cors_response(400, {"error": "camera_id required"})
            return handle_stream_stop(cid)
        if path == "/stream/status" and method == "GET":
            return handle_stream_status(params.get("camera"))

        return cors_response(404, {"error": f"Unknown route: {method} {path}"})
    except Exception as e:
        import traceback; traceback.print_exc()
        return cors_response(500, {"error": str(e)})