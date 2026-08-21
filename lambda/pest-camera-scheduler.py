"""
=============================================================================
Lambda: pest-camera-scheduler  (v1.0 — new account, new schema)
=============================================================================
Refactored from schedule-executor v1 (136 lines):
  - Removed all WebSocket broadcast code (dashboard polls instead)
  - Camera config: nested-map → per-row read from pest-monitoring-cameras
  - Schedule logs: schedule-logs (old account) → pest-monitoring-schedule-logs
  - Hardcoded values → env vars

Trigger:
  EventBridge Rules named `pest-sched-{camera_id}-{action}` invoke this Lambda.
  Event payload (set by pest-monitoring-api when scheduling):
    { "camera_id": "...", "action": "start"|"stop",
      "schedule_id": "...", "trigger_time": "HH:MM" }

What it does:
  1. Read the target camera from pest-monitoring-cameras
  2. Resolve the camera's custom_model_arn
  3. Call rekognition.start/stop_project_version
  4. Update the camera's model_running flag
  5. Write a row to pest-monitoring-schedule-logs for audit

Runtime: Python 3.12, 128 MB, 60 s timeout
Role:    pest-camera-scheduler-role
=============================================================================
"""
import json
import os
import uuid
import boto3
from datetime import datetime, timezone

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
AWS_REGION          = os.environ.get("AWS_REGION", "us-east-1")
TABLE_CAMERAS       = os.environ.get("TABLE_CAMERAS",       "pest-monitoring-cameras")
TABLE_SCHEDULE_LOGS = os.environ.get("TABLE_SCHEDULE_LOGS", "pest-monitoring-schedule-logs")

dynamodb       = boto3.resource("dynamodb", region_name=AWS_REGION)
cameras_table  = dynamodb.Table(TABLE_CAMERAS)
logs_table     = dynamodb.Table(TABLE_SCHEDULE_LOGS)
rekognition    = boto3.client("rekognition", region_name=AWS_REGION)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def get_camera(camera_id):
    try:
        r = cameras_table.get_item(Key={"camera_id": camera_id})
        return r.get("Item")
    except Exception as e:
        print(f"[Cameras] read failed for {camera_id}: {e}")
        return None


def update_camera(camera_id, fields):
    if not fields:
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


def write_schedule_log(camera_id, action, status, message, schedule_id=None, trigger_time=None):
    """Audit row in pest-monitoring-schedule-logs."""
    log_id = str(uuid.uuid4())
    ts = _utcnow_iso()
    item = {
        "log_id":       log_id,
        "timestamp":    ts,
        "camera_id":    camera_id,
        "action":       action,
        "status":       status,        # success | failure
        "message":      message,
        "schedule_id":  schedule_id or "",
        "trigger_time": trigger_time or "",
    }
    try:
        logs_table.put_item(Item=item)
    except Exception as e:
        print(f"[ScheduleLogs] write failed (non-fatal): {e}")


# -----------------------------------------------------------------------------
# Handler
# -----------------------------------------------------------------------------
def lambda_handler(event, context):
    print(f"[Trigger] event = {json.dumps(event, default=str)[:500]}")

    camera_id    = event.get("camera_id")
    action       = (event.get("action") or "").lower()
    schedule_id  = event.get("schedule_id")
    trigger_time = event.get("trigger_time")

    # Validate
    if not camera_id:
        msg = "Missing camera_id in event"
        print(f"[Error] {msg}")
        return {"statusCode": 400, "body": json.dumps({"error": msg})}
    if action not in ("start", "stop"):
        msg = f"Invalid action '{action}', expected 'start' or 'stop'"
        print(f"[Error] {msg}")
        write_schedule_log(camera_id, action, "failure", msg, schedule_id, trigger_time)
        return {"statusCode": 400, "body": json.dumps({"error": msg})}

    # Load camera
    cam = get_camera(camera_id)
    if not cam:
        msg = f"Unknown camera: {camera_id}"
        print(f"[Error] {msg}")
        write_schedule_log(camera_id, action, "failure", msg, schedule_id, trigger_time)
        return {"statusCode": 404, "body": json.dumps({"error": msg})}

    model_arn = cam.get("custom_model_arn", "")
    if not model_arn or str(model_arn).startswith("REPLACE_"):
        msg = f"Camera {camera_id} has no model ARN configured"
        print(f"[Error] {msg}")
        write_schedule_log(camera_id, action, "failure", msg, schedule_id, trigger_time)
        return {"statusCode": 400, "body": json.dumps({"error": msg})}

    # Execute action against Rekognition
    try:
        if action == "start":
            rekognition.start_project_version(ProjectVersionArn=model_arn, MinInferenceUnits=1)
            update_camera(camera_id, {"model_running": True})
            msg = f"Model start initiated for {camera_id}"
        else:  # stop
            rekognition.stop_project_version(ProjectVersionArn=model_arn)
            update_camera(camera_id, {"model_running": False})
            msg = f"Model stop initiated for {camera_id}"

        print(f"[Success] {msg}")
        write_schedule_log(camera_id, action, "success", msg, schedule_id, trigger_time)
        return {"statusCode": 200,
                "body": json.dumps({"message": msg, "camera_id": camera_id,
                                    "action": action, "arn": model_arn})}
    except rekognition.exceptions.ResourceInUseException as e:
        # Already in target state (e.g. start when already running) — treat as soft success
        msg = f"Rekognition reports already in target state: {str(e)[:200]}"
        print(f"[Warn] {msg}")
        write_schedule_log(camera_id, action, "success", msg, schedule_id, trigger_time)
        return {"statusCode": 200,
                "body": json.dumps({"message": msg, "camera_id": camera_id,
                                    "action": action, "soft_success": True})}
    except Exception as e:
        import traceback; traceback.print_exc()
        msg = f"Failed to {action} model: {str(e)[:300]}"
        print(f"[Error] {msg}")
        write_schedule_log(camera_id, action, "failure", msg, schedule_id, trigger_time)
        return {"statusCode": 500,
                "body": json.dumps({"error": msg, "camera_id": camera_id,
                                    "action": action})}
