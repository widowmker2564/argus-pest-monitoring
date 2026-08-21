# CREDENTIALS — what the system needs that this repository does not contain

Nothing here holds a secret. This is the checklist of every credential ARGUS
depends on, where it is consumed, and how to get a fresh one. Hand the values
over out of band — never in a repository file, an issue, a chat message, or an
email.

Each row was traced to the code or configuration that reads it.

---

## 1. AWS — production account `506868652945`

| # | Credential | Consumed by | Notes |
|---|---|---|---|
| 1.1 | **Console password** for IAM user `Student_QianRunzhe` | A human in the AWS console | Needed to create keys, read the bill, and manage Rekognition models by hand. Never use root. |
| 1.2 | **Access key ID + secret access key** for that user (CLI profile `prod`) | Every `--profile prod` command in `docs/aws.md`; `deployer/deploy.py`; the edge devices | The secret is shown once at creation. Store it in `~/.aws/credentials` or the OS credential store. |

The deployer never asks for the console password. It takes an access key pair
and stores it in Windows Credential Manager via DPAPI
(`deployer/app.py:129`), never in a file.

**Retired development account `366356442579`** (profile `nbk2`, IAM user
`cag_user`) only matters if someone needs the historical detection records that
back the report's threshold study. Nothing operative runs there.

---

## 2. Edge device — mini PC (fixed moth camera)

| # | Credential | Consumed by | Notes |
|---|---|---|---|
| 2.1 | **Windows 11 host login** | A human at the machine | |
| 2.2 | **Ubuntu VM login, user `wilburteo`** | A human over SSH | The account name is inherited from the project's predecessor. Renaming it would break the systemd unit paths and the reverse tunnel's `authorized_keys`. |
| 2.3 | **Hikvision camera password**, user `admin` at `192.168.1.66` | `RTSP_PASS` env var read by `minipc/kvs_controller.py:54`; also the camera's own web UI | Set on the VM only. Never written into a repo file. |
| 2.4 | **AWS access key + secret deployed on the VM** | `run_kvs_controller.sh` exports them so the `kvssink` GStreamer element can authenticate | **Open item:** the key on the device is still a development-account key. It must be swapped for a production key before the live stream works there. Manual §6.10. |

---

## 3. Edge device — Jetson Orin on the Unitree Go2

| # | Credential | Consumed by | Notes |
|---|---|---|---|
| 3.1 | **Orin SSH login, user `unitree`** at `192.168.123.18` | A human over SSH | `sudo` is needed for `nmcli` network changes. |
| 3.2 | **`~/.aws/credentials` on the Orin** | `boto3` inside `go2_patrol_gated.py` and `kvs_controller.py` | **Open item:** still the development-account `cag_user` key, which cannot write the production bucket. The repointed scripts only work end to end after this swap. Manual §5.12.6. |
| 3.3 | **SSH keypair for the Orin ↔ VM reverse tunnel** | The self-healing tunnel; key auth both directions, no passwords | The laptop's `~/.ssh/id_ed25519`, the Orin's key in the VM's `authorized_keys`, and the VM's key in the Orin's. Hand over the private keys or regenerate the pairs. |
| 3.4 | **Go2 robot / Unitree app account and the dog-network Wi-Fi** | A human pairing with the robot | Needed to drive the dog, load maps, and reach `192.168.123.x`. |
| 3.5 | **SIYI A8 Mini gimbal**, if its app or Wi-Fi is password-protected | A human configuring the camera | |

---

## 4. Dashboard

| # | Credential | Consumed by | Notes |
|---|---|---|---|
| 4.1 | **Cognito dashboard user: email + password** | The operator signing in to the CloudFront URL | Accounts are admin-created only; there is no self sign-up. Create one with `aws cognito-idp admin-create-user` (see `docs/dashboard.md`) or the deployer's own screen. |

The browser never holds an AWS key. `api.js` attaches the Cognito ID token and
the HTTP API's JWT authorizer is the enforcement boundary.

---

## 5. Email alerts

| # | Credential | Consumed by | Notes |
|---|---|---|---|
| 5.1 | **Access to the verified SES sender/recipient mailbox** | SES identity verification, and receiving the alerts | The account is still in the SES sandbox, so only verified addresses can receive mail. Leaving the sandbox is a support request. |

---

## 6. Third-party data sources

| # | Credential | Needed for | Notes |
|---|---|---|---|
| 6.1 | **Roboflow account / API key** | Re-downloading the `moth-zldog` larva dataset | A key was once hardcoded in a download script. That script is deleted; treat the old key as compromised and issue a new one. |
| 6.2 | **Purchase record for the corn(DST1105) dataset** | Re-obtaining the fall-armyworm-larva training images | The training imagery is not in this repository. |

---

## 7. What does NOT need handing over

- Anything in this repository. The publication gate
  (`tools/publication_gate.py`) fails the build if a credential appears.
- The Rekognition model ARNs, bucket names, API Gateway ID, Cognito pool and
  client IDs, CloudFront distribution ID, account IDs. These are identifiers,
  not secrets, and they are already in `docs/aws.md`.
- The bulk training imagery. It is re-obtainable from the two sources in §6.

---

## 8. How to hand this over

**Prefer new credentials over shared ones.** For anything AWS, the correct
handover is not to pass along the existing key:

1. Create a new IAM user for the successor with `AdministratorAccess`.
2. Have them create their own access key.
3. Delete the old user's access keys once the new ones are confirmed working.

Same principle for the Cognito dashboard account and the device logins: create
fresh ones, verify, then remove the old.

**For the credentials that genuinely must be passed** — device passwords, the
camera password, SSH private keys — use a password manager's share feature or
hand them over in person. Do not put them in a repository, an issue, a chat
message, or an email.

**Rotate after handover.** Every credential the departing owner ever held
should be rotated, whether or not it was shared, including the camera password
and the Roboflow key.
