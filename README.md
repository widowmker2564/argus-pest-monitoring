# ARGUS — Smart Pest Monitoring System

Automated moth and fall-armyworm detection for the indoor greenery at Jewel
Changi Airport (Shiseido Forest Valley, Singapore).

A camera uploads a frame to S3. An S3 event triggers one Lambda, which runs
Amazon Rekognition Custom Labels as a high-recall detector and then asks Claude
Sonnet on Bedrock to judge every candidate box. Surviving detections land in
DynamoDB, an operator sees them on a web dashboard, and a hit sends an email
alert.

Built as a final-year ECE diploma project at Ngee Ann Polytechnic, April–August
2026.

---

## Start here

| If you want to | Read |
|---|---|
| Understand the system | [`docs/architecture.md`](docs/architecture.md) |
| Know what is true right now | [`docs/state.md`](docs/state.md) |
| Know why it is built this way | [`docs/DECISIONS.md`](docs/DECISIONS.md) |
| Avoid problems already solved | [`docs/PITFALLS.md`](docs/PITFALLS.md) |
| Get a guided tour of the code | [`docs/code_walkthrough.md`](docs/code_walkthrough.md) |
| Rebuild the whole thing | [`reports/manual/08_reproduction_runbook.md`](reports/manual/08_reproduction_runbook.md) |
| Use an AI coding agent here | [`AGENTS.md`](AGENTS.md) |

**Working with a coding assistant?** [`AGENTS.md`](AGENTS.md) is the orientation
file. It follows the [agents.md](https://agents.md) convention and is picked up
automatically by most agent tools. It is deliberately vendor-neutral — nothing
in this repository assumes a particular assistant.

---

## What is in here

```
lambda/       AWS Lambda functions. pest-detection-processor.py is the entire
              detection pipeline and the most important file in the repo.
web/          Operator dashboard. Vanilla JS ES modules, no build step.
robot/        Unitree Go2 + Jetson Orin: patrol, capture, gimbal, navigation.
minipc/       Fixed-camera capture and the Kinesis Video Streams controller.
deployer/     ARGUS: a one-click installer that stands the whole AWS stack up
              on a fresh account in 15 stages.
datasets/     The evaluation holdout: the fixed set of real site photographs
              every model version is scored against.
docs/         Knowledge base. See the table above.
reports/      Final report and technical manual (docx + pdf), the defence
              deck, and the manual's markdown source.
tools/        Repository tooling. publication_gate.py scans for secrets before
              a push.
```

---

## Running it

No build step, no package manager for the frontend. Lambdas are plain Python
3.12.

```bash
python -m http.server 8000 --directory web/dashboard_v4
```

Deploy the full AWS stack to a fresh account:

```bash
python deployer/deploy.py --profile <your-aws-profile> --prefix argus --target-label armyworm-larva
```

Check for secrets before pushing:

```bash
python tools/publication_gate.py
```

---

## The technical manual

[`reports/manual/`](reports/manual/) is the deepest documentation here: ten
chapters covering the architecture, cloud backend, models and training, the
dashboard, both edge platforms, the deployer, and a full reproduction
runbook.

---

## What this system can and cannot demonstrate

Stated plainly, because it shapes every number in the reports.

**It can show** that the model detects, by offline evaluation against a fixed
holdout; that the robot patrols autonomously with the full cloud chain live;
and that the stack deploys reproducibly onto a clean AWS account.

**It cannot show** that a real worm will be caught on a given patrol — there are
no worms at the deployment site. Live detection is demonstrated through the
dashboard's Test upload panel.

Evaluation numbers are quoted from a fixed scored holdout and nothing else. The
holdout contains no true negatives, so an image-level false-positive rate is not
measurable on it, and no such rate is claimed.

---

## Credentials

No credentials are in this repository. Placeholders such as `<pass>`,
`<REDACTED>`, `<SET_ON_VM>` and `AKIA-REDACTED-*` are deliberate — do not fill
them in. AWS credentials belong in `~/.aws/credentials` or the environment;
device credentials live on the device; the deployer stores its IAM key in the
OS credential store.

`tools/publication_gate.py` enforces this. It scans every text file for AWS key
IDs, secret-key literals, GitHub tokens, private-key blocks and inline
credentials, and exits non-zero if it finds one. Run it before every push.

---

## Acknowledgements

The moth detection model is inherited from the project's predecessor, Wilbur
Teo. Supervised by Dr. Yan Li at Ngee Ann Polytechnic. Built for Changi Airport
Group.
