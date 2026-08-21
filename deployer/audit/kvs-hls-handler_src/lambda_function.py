"""
=============================================================================
Lambda: kvs-hls-handler   (KVS HLS playback-URL generator)
=============================================================================
Standalone KVS service Lambda. Restores the originally-planned `kvs-hls-handler`
function that was deferred during the W7 6->3 Lambda consolidation.

Kept SEPARATE from `pest-monitoring-api` on purpose:
  - distinct IAM surface: only kinesisvideo read actions, no DynamoDB / S3 / SES
  - KVS is a separable phase; a dedicated function keeps the boundary clean

Route (API Gateway HTTP API `zwpcbivmsj`, integrated to THIS function):
  GET /video-playback?stream=<kvs_stream_name>

The dashboard Live-stream tab (loadStream -> api.getVideoPlayback) calls this
to obtain a playable HLS URL for a KVS stream. The /stream/start|stop|status
control routes stay in `pest-monitoring-api` (they only toggle a DynamoDB
flag and never touch KVS).

Runtime: Python 3.12, 128 MB, 30 s timeout
Role:    kvs-hls-handler-role
IAM:     kinesisvideo:GetDataEndpoint, kinesisvideo:GetHLSStreamingSessionURL
=============================================================================
"""
import json
import os
import boto3

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Content-Type": "application/json",
}


def cors_response(status, body):
    return {"statusCode": status, "headers": _CORS, "body": json.dumps(body)}


def get_hls_url(stream):
    """Resolve a live HLS streaming-session URL for a KVS stream name."""
    kv = boto3.client("kinesisvideo", region_name=AWS_REGION)
    endpoint = kv.get_data_endpoint(
        StreamName=stream,
        APIName="GET_HLS_STREAMING_SESSION_URL",
    )["DataEndpoint"]
    archived = boto3.client(
        "kinesis-video-archived-media",
        region_name=AWS_REGION,
        endpoint_url=endpoint,
    )
    return archived.get_hls_streaming_session_url(
        StreamName=stream,
        PlaybackMode="LIVE",
        Expires=3600,
    )["HLSStreamingSessionURL"]


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path   = event.get("requestContext", {}).get("http", {}).get("path", "/")
    params = event.get("queryStringParameters", {}) or {}

    if method == "OPTIONS":
        return cors_response(200, {"message": "OK"})

    if path == "/video-playback" and method == "GET":
        stream = (params.get("stream") or "").strip()
        if not stream:
            return cors_response(400, {"error": "Missing 'stream' query parameter"})
        try:
            hls_url = get_hls_url(stream)
            return cors_response(200, {"stream": stream, "hls_url": hls_url})
        except Exception as e:
            # Common causes: the stream does not exist in this account, or it
            # exists but has no live fragments yet (producer is not pushing).
            return cors_response(503, {
                "error": str(e),
                "stream": stream,
                "hint": "Stream missing or no live fragments - check the stream "
                        "exists in this account and the producer is pushing.",
            })

    return cors_response(404, {"error": f"Unknown route: {method} {path}"})