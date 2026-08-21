# Map profiles — full parameter set per recorded map

> **Note on script paths.** Sections below describe work done earlier in
> the project. Some of the scripts they name were removed in the
> 2026-08-21 repository cleanup, which kept only production code and the
> current pipeline. The reasoning and the measurements stand; the paths
> are a record of how the work was done, not files you will find here.


Switching maps = (1) select the map in the app once (localize on it),
(2) paste that map's block into `go2_patrol_gated.py` (INITIAL_POSE + WAYPOINTS),
(3) push to the Orin. The patrol script reads the live map_id, so nothing else
changes. Recorded maps stay on the dog — recording a new one does NOT delete
the old ones.

This hand-paste step applies to `go2_patrol_gated.py` only. The handover script
`go2_console.py` (navigate + photo, no detection) keeps its own routes in
`robot/patrol_maps.json` — one entry per map, holding the map id, the start pose
and the waypoints. Survey a new map there and nothing needs pasting, and the old
routes stay in the file until they are deleted from its menu.

---

## JEWEL SITE v2 — `1BEC7FFDF97C47AC8BD751143D3FE187` (CURRENT, 5/5 VALIDATED)
Re-mapped by Runzhe 2026-07-29 after the first Jewel map proved unusable. **Validated
by a clean 5/5 autonomous patrol at 14:55** (2 minutes, every waypoint first attempt,
no retries) — see `robot/_archive/2026-07-29/patrol_jewel_5of5_clean.log`.

Surveyed by walking the route twice with a continuous pose logger and extracting the
dwell points. Round-to-round repeatability on the first three points was 1.8 / 0.9 /
2.4 cm, and a 14 m loop closed to 0.17 m.

```python
# INITIAL_POSE is the in-place NUDGE TARGET, not the first waypoint. With
# SKIP_LOCALIZATION_BRINGUP=True it must be set to wherever the dog is physically
# standing at launch, or the "nudge" becomes a walk across the site.
INITIAL_POSE = {"x": -5.001, "y": -0.817, "yaw": 1.249}   # = the wp_return spot

WAYPOINTS = [
    {"name": "wp1",   "x": -4.179, "y":  0.836, "yaw":  0.669, "capture": False},
    {"name": "zone1", "x": -2.367, "y":  1.608, "yaw":  1.256},
    {"name": "zone2", "x": -1.775, "y":  0.928, "yaw": -2.519},
    {"name": "zone3", "x": -3.189, "y":  1.263, "yaw": -3.030},
    {"name": "wp_return", "x": -5.013, "y": -0.825, "yaw": 1.374, "capture": False},
]
```

Two settings this route depends on, both in `go2_patrol_gated.py`:
- **`send_goal` default is `repeat=1`.** USLAM treats every `set_goal_pose` as a new
  goal, so a repeat arriving after TRACKING starts raises `GOAL_CHANGED` and kills
  the goal the script is waiting on. With `repeat=3` waypoints failed at random.
- **`SKIP_LOCALIZATION_BRINGUP = True`.** Localization is established in the app and
  the patrol must not re-seed it.

**Do not launch while driving the dog by remote.** A nav goal fired into manual
control stopped localization mid-run at 14:50:24 and the patrol aborted at bringup
(`navigation/start` refused because the dog was not tracking). Let the nudge do the
moving.

Clearance analysis is NOT possible on this stack: `/uslam/cloud_map` never publishes,
and `/uslam/localization/cloud_world` carries LIVE lidar returns (only 254 of ~2100
points persist between samples 12 s apart), so anyone standing near the dog is
measured as an obstacle. Judge waypoints by whether a patrol reaches them, not by
computed clearance.

---

## RETIRED — first Jewel map `F0E056FC045649B7BE3BDFF92FC54363` (2026-07-29, unusable)
Recorded by Runzhe on site at Jewel, 2026-07-29. **Confirmed loaded** — `get_map_id.py`
answers with this id from a cold start, and all 10 `/uslam` topics are up.
**This is the zone of record from now on.** The lab PRIMARY and DEMO maps below are
history: the W15 roadmap item "switch back to the PRIMARY map" is obsolete, since the
demo runs here, not in the lab.

Waypoints: **survey in progress.** Coordinates get filled in below as each point is
walked and grabbed; capture flags are Runzhe's call per point.

```python
# Start point surveyed 2026-07-29 12:32. Median of 30 settled frames,
# spread x 0.012 / y 0.022 / yaw 0.016.
INITIAL_POSE = {"x": 0.141, "y": 0.220, "yaw": -0.404}

WAYPOINTS = [
    # Capture points are named by ZONE, because the waypoint name becomes the S3
    # key segment (frames/worm_cam/<name>/<ts>.jpg) - so detections stay traceable
    # to a zone in the dashboard. Navigate-only points keep the wpN naming.
    #
    # wp1 = the start point, same pose as INITIAL_POSE (project convention).
    {"name": "wp1", "x": 0.141, "y": 0.220, "yaw": -0.404, "capture": False},
    # wp2 surveyed 12:38, settled (spread x 0.016 / y 0.006 / yaw 0.016).
    {"name": "wp2", "x": 0.912, "y": 0.090, "yaw": 0.123, "capture": False},
    # ZONE 1 - CAPTURES. Surveyed 12:44, settled (spread x 0.005 / y 0.027 / yaw 0.008).
    {"name": "zone1", "x": 2.145, "y": 0.009, "yaw": 2.843},
    # ZONE 2 - CAPTURES. Surveyed 12:49, settled (spread x 0.017 / y 0.013 / yaw 0.005).
    {"name": "zone2", "x": 2.957, "y": -0.818, "yaw": 0.229},
    # ZONE 3 - CAPTURES, and the LAST point. Route ends here, no return leg
    # (same shape as the old DEMO map). Surveyed 12:54, settled
    # (spread x 0.010 / y 0.018 / yaw 0.012).
    # NOTE: only 0.175 m from zone2 but rotated ~157 deg - same standing spot,
    # opposite heading. See the warning under the route summary below.
    {"name": "zone3", "x": 2.840, "y": -0.948, "yaw": -2.505},
]
```

Route as surveyed: `wp1 → wp2 → zone1 → zone2 → zone3`, ~3 m of travel, capture at
the three zones only. Ends at zone3.

**Deployed to `~/go2/go2_patrol_gated.py` 2026-07-29** (backup
`.bak_prejewel_20260729`), along with `START_COUNTDOWN_S 3 → 25` so there is time to
pull the cable when the run is launched remotely, and `DDB_GATE_TIMEOUT_S 40 → 150`.
**`INITIAL_POSE` in the deployed file is NOT wp1** — it is the pose the dog was
standing at when the run was set up (2.860, -0.981, -2.505, i.e. zone3), because
that value is the localization seed, not the first waypoint. Re-seeding a live,
correctly-tracking localization with a pose the dog is not at will break it. Set
`INITIAL_POSE` to wherever the dog actually stands at launch; the canonical
start-of-route pose is the `INITIAL_POSE` recorded at the top of this section.

**ALL COORDINATES ABOVE ARE VOID. The survey frame was never the map frame.**

First run 2026-07-29 13:09 reached 1 of 5, and that one "REACHED" was false — the
dog was already inside goal tolerance and never departed. It shuffled 5.6 m of
cumulative path for 0.071 m of net displacement and 358° of yaw.

Root cause, established from `/uslam/server_log` at 13:23-13:24 (full capture in
`robot/_archive/2026-07-29/uslam_relocalization_failures.log`): **localization
never matched the map.** Seeding `(0,0,0)` and calling `localization/start` was
accepted and odom began publishing, but USLAM was integrating odometry from that
seed rather than locating the dog in the map. Every surveyed coordinate is
therefore an odometry frame anchored at an arbitrary origin. By the time the dog
had walked ~10 m the offset was ~5 m: Runzhe returned it to the physical start
point and the survey frame read 2.47 m away, while the app's own relocalization
seed for that same spot was `(5.051, 1.134, -1.660)` — nowhere near the surveyed
`(0.141, 0.220)`.

Goals sent in that frame land at meaningless map positions, which is the NO_PATH /
FAILURE storm. It is not a clearance problem and not a map problem: the map is
sound, spanning 11.4 x 13.3 m.

**An earlier entry here blamed obstacle clearance at the waypoints. That was
wrong** — the clearance was measured in the same void frame, so it proved nothing.
Retracted rather than deleted, so the reasoning error is not repeated.

Also void for the same reason: the "zone2 → zone3 is a 17 cm turn in place"
observation, and the computed back-off positions.

Action: get a genuinely successful relocalization first (see below), confirm it by
watching for the success token on `/uslam/server_log` rather than trusting that
`localization/start` returned success, then re-survey **all five points** from
scratch in the frame that relocalization establishes.

Localization on this map does NOT auto-start after mapping — `get_map_id` answers
and the map is loaded, but `/uslam/localization/odom` has **0 publishers** until
`localization/set_initial_pose` + `localization/start` are sent. Seeding `(0,0,0)`
worked here (the dog was near the mapping origin) and localization converged to the
start pose above. Symptom if this is missed: the pose survey reads nothing and it
looks like an avoidance-off or dog-not-moving problem, which it is not.

Survey method for this map: a continuous logger runs on the Orin at
`/tmp/pose_logger.py` writing `/tmp/pose_log.txt`, so points can be taken by walking
the dog and reading the last stable line — no nudge-and-rerun per point. Prerequisites
that silently break the survey if missed: **obstacle avoidance must be ON** (avoidance
OFF silences `/uslam/localization/odom` entirely) and localization must be initialized
on this map in the app.

---

## PRIMARY — lab room map `04114624684C4194B7008EDB3A5642D2`
Recorded W13 (2026-06-30). Waypoints re-surveyed 2026-07-03 morning after app
re-localization; validated 4/4 REACHED (~50 s loop) by `tests/wp_test_2.py`.
**This is the map to return to after the client test.**

```python
INITIAL_POSE = {"x": 0.143, "y": -0.009, "yaw": -1.569}

WAYPOINTS = [
    {"name": "wp1", "x":  0.143, "y": -0.009, "yaw": -1.569, "capture": False},
    {"name": "wp2", "x":  0.250, "y": -1.415, "yaw": -1.652},
    {"name": "wp3", "x": -3.090, "y": -1.573, "yaw":  3.112},
    {"name": "wp4", "x": -0.132, "y": -1.505, "yaw": -0.090},
    # Return to start: "capture": False = navigate only, no scan / S3 / gate.
    {"name": "wp_return", "x": 0.143, "y": -0.009, "yaw": -1.569, "capture": False},
]
```

Full pre-swap script snapshot: `robot/_archive/2026-07-03/orin/go2_patrol_gated.py`.

---

## DEMO — showcase route `DACB71661B5A48D48C6631AB2480C611` (CURRENTLY DEPLOYED)
Recorded + surveyed 2026-07-09 morning (pose.py one-shots; app-relocalized first;
avoidance ON — see hardware.md: avoidance OFF silences odom entirely).
Flow: operator drives the dog to the start and initializes localization in the
app, then launches the patrol. **Capture at wp1 (the start, in place); wp2/wp3
navigate only; the route ENDS at wp3 — no return leg.**

```python
INITIAL_POSE = {"x": -0.232, "y": 0.015, "yaw": -0.110}

WAYPOINTS = [
    # wp1 = start, CAPTURES in place (default capture=True).
    {"name": "wp1", "x": -0.232, "y":  0.015, "yaw": -0.110},
    # wp2 re-picked + probe-validated 2026-07-09 pm (original (1.143,-0.176)
    # = dead zone, planner refused it and 4 neighbors; see tests/wp2_probe.py).
    {"name": "wp2", "x": -0.491, "y":  1.749, "yaw":  3.043, "capture": False},
    # Final point — dog stops and holds here.
    {"name": "wp3", "x":  1.238, "y":  1.679, "yaw": -1.587, "capture": False},
]
```

---

## RETIRED — client-test rescan `7853B2C397A44F8EB317D1C12D5B1F1C`
Recorded 2026-07-03 for the first client demo (wp2-only capture). **Discarded
2026-07-09** (Runzhe: superseded by the DEMO map above). Coords kept for the
record only:
`INITIAL_POSE = {"x": -0.086, "y": 0.142, "yaw": 0.033}`; wp2 (1.281, 0.451,
-1.784) / wp3 (1.133, -1.108, 3.014) / wp4 (-0.051, -1.185, 1.643).

---

Older dead maps (do not use): `7E7AABAE...` (W12, unplannable — free space only
around the start), `DA230BE6...` (old room).
