# AGENTS.md — orientation for any coding agent

This file is the entry point for AI coding assistants. It follows the
[agents.md](https://agents.md) convention and is read automatically by most
agent tools (Cursor, GitHub Copilot, OpenAI Codex, Windsurf, Zed, Gemini CLI,
Claude Code via `CLAUDE.md`, and others). Nothing here is vendor-specific.

**If you are a human:** read [`README.md`](README.md) first.

---

## 1. What this project is

ARGUS is a smart pest monitoring system built for the indoor greenery at Jewel
Changi Airport (Changi Airport Group's Shiseido Forest Valley, Singapore). It
detects moths and fall-armyworm larvae from camera frames, verifies each
detection with a vision LLM, and shows the results on an operator dashboard.

It was built as a final-year ECE diploma project at Ngee Ann Polytechnic
(**NP — not NYP**), April–August 2026, extending a predecessor's moth-detection
system.

**One sentence for the architecture:** a camera uploads a frame to S3 → an S3
event triggers one Lambda → that Lambda runs Amazon Rekognition Custom Labels
as a high-recall detector, then asks Claude Sonnet on Bedrock to judge every
candidate box → surviving boxes are written to DynamoDB → a vanilla-JS
dashboard reads them through an HTTP API behind Cognito auth.

Two capture paths exist: a fixed mini-PC camera (the production shape) and a
Unitree Go2 quadruped carrying a Jetson Orin and a SIYI A8 Mini gimbal (the
demo/testbed shape).

---

## 2. Read these before you touch anything

Read the file that covers the area you are working in. Do not infer the
system's behaviour from code alone — several important facts are decisions,
not code, and only exist in these files.

| File | What it holds |
|---|---|
| [`docs/state.md`](docs/state.md) | **Start here.** The living state + roadmap. Newest entries at the top. Everything that is true right now. |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Why the system is shaped the way it is. Every major fork, what was chosen, and what was rejected. |
| [`docs/PITFALLS.md`](docs/PITFALLS.md) | Traps that already cost this project time. Read before debugging anything. |
| [`docs/architecture.md`](docs/architecture.md) | The request path end to end, with the file that owns each hop. |
| [`docs/aws.md`](docs/aws.md) | Lambdas, S3, DynamoDB, Rekognition, API Gateway, Cognito, IAM, CLI quirks. |
| [`docs/detection.md`](docs/detection.md) | Model facts, thresholds, and hard-won detection learnings. |
| [`docs/hardware.md`](docs/hardware.md) | Go2, Jetson Orin, SIYI A8, mini PC, IPs, navigation, gimbal control. |
| [`docs/dashboard.md`](docs/dashboard.md) | Frontend module layout, auth, deploy. |
| [`docs/model_ladder.md`](docs/model_ladder.md) | Every model version v3 → v9r and what each one proved. |
| [`reports/manual/`](reports/manual/) | The full technical manual, 10 chapters. The deepest reference in the repo. |
| [`docs/code_walkthrough.md`](docs/code_walkthrough.md) | A guided tour of the live code, subsystem by subsystem, with verified `file:line` anchors and the live configuration values. |

**Precedence when two files disagree:** `docs/state.md` wins, then
`reports/manual/`, then everything else. The code is ground truth over all of
them.

---

## 3. Repository map

```
lambda/       AWS Lambda functions. pest-detection-processor.py is the whole
              detection pipeline and is the single most important file here.
web/          The operator dashboard. Vanilla JS ES modules, no build step,
              no framework. dashboard_v4/ is live.
robot/        Go2 + Jetson Orin: patrol, capture, gimbal, navigation probes.
minipc/       Fixed-camera capture and the Kinesis Video Streams controller.
deployer/     ARGUS, a one-click installer that stands the whole AWS stack up
              on a fresh account. deploy.py is the 15-stage deployment.
datasets/     The evaluation holdout: the fixed set of real site photographs
              every model version is scored against. Nothing is trained on it.
docs/         The knowledge base. See the table above.
reports/      final/ = the final report (docx + pdf), the technical manual
              (docx + pdf), and the defence deck. manual/ = the manual's
              markdown source, 10 chapters.
tools/        Repository tooling. publication_gate.py scans for secrets and
              content-rule violations before a push.
```

---

## 4. How to work in this repo

- **English only** in code: comments, docstrings, identifiers, string literals.
- **Prose style:** plainest possible English, short sentences. Avoid academic
  connectors ("thereby", "which means", "establishing that").
- **Keep real proper nouns.** SIYI A8, Go2, Jetson Orin, Rekognition, USLAM,
  S3, DynamoDB, Cognito, Jewel Changi. Do not abstract them into generic
  phrases like "the vendor stack" or "the cloud processor".
- **For AWS steps, give both** the console click-path and the CLI command.
- **Verify before claiming.** Before saying a file is missing or stale, open
  it. Before deleting anything, check what references it.
- **Update `docs/state.md` in the same change** whenever a durable fact
  changes: something deployed, validated, decided, broken, or retired. Mark
  roadmap items DONE with the date; do not delete them. A result that exists
  only in a chat log is a result that is lost.

### Security rules (hard)

- Never commit secrets: AWS secret access keys, GitHub tokens, device or RTSP
  passwords, API tokens, private keys. Credentials belong in the environment,
  in `~/.aws/credentials`, or in the OS credential store — never in a file
  here.
- Placeholders in this repo (`<pass>`, `<REDACTED>`, `<SET_ON_VM>`,
  `AKIA-REDACTED-*`) are deliberate. Do not "helpfully" fill them in.
- Before any push, run the publication gate: `python tools/publication_gate.py`.
  A non-zero exit means do not push.

---

## 5. Project framing that is easy to get wrong

These are decisions, not opinions. Getting them wrong produces confidently
incorrect work.

- **The Go2 quadruped is a demo and testbed only.** The production vision is
  fixed cameras at each waypoint. There is no robot deployment.
- **There is no automatic retraining loop.** Wrong detections are labelled by
  hand. Do not describe a "flywheel", and do not claim any number of flagged
  photos triggers a retrain.
- **There are no worms at Jewel.** This shapes every claim the project can
  make. The system can be shown to detect (offline evaluation against a fixed
  holdout) and to patrol with the full cloud chain live, but nobody can promise
  a real worm will be caught on a given patrol. Live detection is demonstrated
  through the dashboard's Test upload panel. Printed targets were tried and do
  not work.
- **Training data comes only from sources the project controls** — purchased
  and public datasets, plus augmentation. The client is not a data source.
- **The scored holdout is fixed:** `batch_2` 102–109, `CAG_Jewel_1/2`, and the
  4 field-realistic photos. Recall and detection-rate numbers are quoted from
  that set and nothing else. Other images may be shown as qualitative
  demonstration but never counted.
- **v9/v9r is described as "added data augmentation"** — flips, rotations and
  exposure jitter, a 13x build over the source images.
- **The vanilla-JS dashboard is the final deliverable.** It is not a
  placeholder for a framework rewrite.
- **Model F1 is not pipeline accuracy.** Since processor v6.0 the detector is
  deliberately a high-recall front end and the LLM gate supplies precision. A
  detector F1 read on its own test split does not describe what the system
  does. See `docs/detection.md`.

---

## 6. Running things

There is no build step and no package manager for the frontend. The Lambda
functions are plain Python 3.12.

```bash
# dashboard: serve the folder, open index.html
python -m http.server 8000 --directory web/dashboard_v4
```

```bash
# stand the whole AWS stack up on a fresh account (15 stages)
python deployer/deploy.py --profile <your-aws-profile> --prefix argus --target-label armyworm-larva
```

Full reproduction instructions, including every AWS resource and the order to
create it in, are in
[`reports/manual/08_reproduction_runbook.md`](reports/manual/08_reproduction_runbook.md).

---

## 7. If you are picking this project up

Read in this order:

1. `README.md` — what exists and where.
2. `docs/state.md` — the top ~200 lines. That is the current state.
3. `docs/DECISIONS.md` — why it looks like this.
4. `docs/PITFALLS.md` — so you do not re-pay for lessons already paid for.
5. `reports/manual/01_system_architecture.md` — then the chapter for your area.
6. `lambda/pest-detection-processor.py` — the pipeline itself.
