"""
=============================================================================
Lambda: pest-model-watchdog
=============================================================================
Safety net so a Rekognition Custom Labels model never runs all night and
burns credits (~$1/hr). Triggered by an EventBridge rule every 10 minutes.

For each camera with a real custom model ARN:
  - Ask Rekognition for the model's ACTUAL status.
  - If RUNNING:
      * no recorded start time  -> stamp `model_started_at = now` (first sighting)
      * been running >= limit    -> StopProjectVersion + clear the flag
  - If not RUNNING -> clear any stale `model_started_at`.

Self-stamping means it bounds the runtime regardless of HOW the model was
started (dashboard, schedule, or manual console/CLI). Worst case a model
runs MAX_RUNTIME_MIN + one poll interval (~10 min) before being stopped.

Env vars:
  TABLE_CAMERAS    (default "pest-monitoring-cameras")
  MAX_RUNTIME_MIN  (default "60")
=============================================================================
"""
import os
from datetime import datetime, timezone

import boto3

ddb = boto3.resource("dynamodb")
rek = boto3.client("rekognition")

TABLE_CAMERAS   = os.environ.get("TABLE_CAMERAS", "pest-monitoring-cameras")
MAX_RUNTIME_MIN = int(os.environ.get("MAX_RUNTIME_MIN", "60"))

ISO = "%Y-%m-%dT%H:%M:%SZ"


def iso_now():
    return datetime.now(timezone.utc).strftime(ISO)


def parse_iso(s):
    try:
        return datetime.strptime(s, ISO).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def project_arn_for_version(version_arn):
    """Resolve the full PROJECT arn for a model VERSION arn.

    version arn:  arn:...:project/<name>/version/<ver>/<id>
    project arn:  arn:...:project/<name>/<projectId>   (note trailing id)

    The projectId is NOT present in the version arn, so we can't derive it by
    string-splitting — list the projects and match on the 'project/<name>' prefix.
    """
    if "/version/" not in version_arn:
        return None
    prefix = version_arn.split("/version/")[0]   # arn:...:project/<name>
    try:
        resp = rek.describe_projects()
        for p in resp.get("ProjectDescriptions", []):
            parn = p.get("ProjectArn", "")
            if parn.startswith(prefix + "/"):
                return parn
    except Exception as e:
        print(f"[Watchdog] describe_projects failed: {e}")
    return None


def model_status(version_arn):
    """Authoritative RUNNING/STOPPED/... status from Rekognition."""
    parn = project_arn_for_version(version_arn)
    if not parn:
        return None
    try:
        resp = rek.describe_project_versions(ProjectArn=parn)
        for v in resp.get("ProjectVersionDescriptions", []):
            if v.get("ProjectVersionArn") == version_arn:
                return v.get("Status")
    except Exception as e:
        print(f"[Watchdog] describe failed for {parn}: {e}")
    return None


def lambda_handler(event, context):
    table = ddb.Table(TABLE_CAMERAS)
    cams = table.scan().get("Items", [])
    now = datetime.now(timezone.utc)
    stopped, stamped = [], []

    for cam in cams:
        cid = cam.get("camera_id")
        arn = cam.get("custom_model_arn", "")
        if not arn or str(arn).startswith("REPLACE_"):
            continue

        status = model_status(arn)

        if status != "RUNNING":
            # Not running -> drop any stale stamp so the next start measures fresh.
            if cam.get("model_started_at"):
                table.update_item(
                    Key={"camera_id": cid},
                    UpdateExpression="REMOVE model_started_at",
                )
            continue

        started = parse_iso(cam.get("model_started_at", ""))
        if not started:
            # First time we observe it running -> stamp now.
            table.update_item(
                Key={"camera_id": cid},
                UpdateExpression="SET model_started_at = :t",
                ExpressionAttributeValues={":t": iso_now()},
            )
            stamped.append(cid)
            print(f"[Watchdog] {cid} RUNNING, no start time -> stamped {iso_now()}")
            continue

        elapsed_min = (now - started).total_seconds() / 60.0
        print(f"[Watchdog] {cid} RUNNING for {elapsed_min:.1f} min (limit {MAX_RUNTIME_MIN})")

        if elapsed_min >= MAX_RUNTIME_MIN:
            try:
                rek.stop_project_version(ProjectVersionArn=arn)
                table.update_item(
                    Key={"camera_id": cid},
                    UpdateExpression="SET model_running = :f REMOVE model_started_at",
                    ExpressionAttributeValues={":f": False},
                )
                stopped.append(cid)
                print(f"[Watchdog] STOPPED {cid} after {elapsed_min:.1f} min")
            except Exception as e:
                print(f"[Watchdog] stop failed for {cid}: {e}")

    result = {"checked": len(cams), "stopped": stopped, "stamped": stamped}
    print(f"[Watchdog] done: {result}")
    return result