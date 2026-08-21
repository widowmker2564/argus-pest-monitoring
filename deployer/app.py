#!/usr/bin/env python3
"""
app.py - ARGUS desktop deployer shell (pywebview / Edge WebView2 backend).

One window, one .exe: hosts the premium ARGUS UI (web/) and drives the whole
one-stop flow — credential capture, the embedded-AWS assist pane, and the live
cloud deployment (deploy.py) — WITHOUT the user ever leaving the app.

Trust boundary (enforced here, not just claimed):
  - The customer's AWS card + passwords are typed into AWS's OWN pages, shown in a
    SEPARATE embedded WebView2 window (open_aws_pane). We never read that window's
    DOM; we only observe navigation URLs to advance the guidance rail.
  - The IAM access key the customer pastes is stored via the OS credential store
    (keyring -> Windows Credential Manager / DPAPI), never in plaintext, never logged.

Architecture:
  UI (web/index.html)  --window.pywebview.api.*-->  Api (this file)
  Api  --background thread--> deploy.run_plan(ctx, plan)
  deploy._EMIT --> Api._emit --> window.evaluate_js('ARGUS.onDeployEvent(...)')

Run from source:  python app.py
Package:          see build_exe.md
"""

import json
import os
import queue
import sys
import threading
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"

try:
    import webview  # pywebview
except ImportError:
    print("pywebview is required: pip install pywebview", file=sys.stderr)
    raise

# deploy.py / training.py live next to this file
sys.path.insert(0, str(HERE))
import deploy  # noqa: E402
import training  # noqa: E402

# keyring is optional at dev time; the app degrades to an in-memory store with a warning
try:
    import keyring
except ImportError:
    keyring = None

KEYRING_SERVICE = "ARGUS-deployer"


class Api:
    """The JS<->Python bridge. Every method here is callable from the UI as
    window.pywebview.api.<name>(...) and returns a JSON-serializable value."""

    def __init__(self):
        self._window = None
        self._aws_window = None
        self._events = queue.Queue()
        self._deploying = False
        self._training = False
        # start_training's check-then-set spans a slow STS call; without a
        # lock a double-click launches two whole pipelines.
        self._flag_lock = threading.Lock()
        self._close_anyway = False

    def bind(self, window):
        self._window = window

    # ---- event streaming (Python -> JS) ------------------------------------
    def _push(self, kind, payload):
        """Called from the deploy thread; forwards an event to the UI."""
        try:
            data = json.dumps({"kind": kind, **payload})
            if self._window:
                self._window.evaluate_js(f"window.ARGUS && ARGUS.onDeployEvent({data})")
        except Exception:
            pass

    # ---- credential handling (never plaintext) -----------------------------
    def verify_credentials(self, access_key_id, secret_access_key):
        """Validate the pasted IAM key against STS, then store it encrypted.
        Returns {ok, account, arn} or {ok:false, error}. The secret is NEVER
        returned to the UI or logged."""
        try:
            import boto3
            sess = boto3.Session(
                aws_access_key_id=access_key_id.strip(),
                aws_secret_access_key=secret_access_key.strip(),
            )
            ident = sess.client("sts").get_caller_identity()
        except Exception as e:
            return {"ok": False, "error": _friendly_aws_error(e)}
        # ROOT keys are refused OUTRIGHT, before anything is stored: the product
        # forbids them (they cannot be permission-scoped or safely rotated) and
        # the whole keys screen exists to teach the IAM-user alternative.
        if ident["Arn"].endswith(":root"):
            return {"ok": False, "error":
                    "These are ROOT account keys and cannot be used. In the AWS "
                    "window, create the 'deployer' IAM user (steps on the left) "
                    "and paste that user's access key instead."}
        # Permission preflight (2026-07-15): a bare IAM user PASSES the STS check
        # yet fails every deploy stage with AccessDenied. Detect that here and
        # name the fix, instead of letting the deployment die 8 stages in.
        # admin: True = AdministratorAccess found; False = definitely missing;
        # None = could not determine (key lacks IAM read — treated as a warning).
        admin = None
        try:
            if ":user/" in ident["Arn"]:
                iam = sess.client("iam")
                uname = ident["Arn"].split("/")[-1]
                pols = iam.list_attached_user_policies(UserName=uname)["AttachedPolicies"]
                admin = any(p["PolicyName"] == "AdministratorAccess" for p in pols)
                if not admin:
                    for g in iam.list_groups_for_user(UserName=uname)["Groups"]:
                        gp = iam.list_attached_group_policies(
                            GroupName=g["GroupName"])["AttachedPolicies"]
                        if any(p["PolicyName"] == "AdministratorAccess" for p in gp):
                            admin = True
                            break
        except Exception:
            admin = None
        # store encrypted, keyed by account; UI only ever sees the account id
        if keyring:
            keyring.set_password(KEYRING_SERVICE, "aws_access_key_id", access_key_id.strip())
            keyring.set_password(KEYRING_SERVICE, "aws_secret_access_key", secret_access_key.strip())
        else:
            self._mem_creds = (access_key_id.strip(), secret_access_key.strip())
        return {"ok": True, "account": ident["Account"], "arn": ident["Arn"],
                "admin": admin}

    def _load_creds(self):
        if keyring:
            return (keyring.get_password(KEYRING_SERVICE, "aws_access_key_id"),
                    keyring.get_password(KEYRING_SERVICE, "aws_secret_access_key"))
        return getattr(self, "_mem_creds", (None, None))

    def forget_credentials(self):
        if keyring:
            for k in ("aws_access_key_id", "aws_secret_access_key"):
                try:
                    keyring.delete_password(KEYRING_SERVICE, k)
                except Exception:
                    pass
        self._mem_creds = (None, None)
        return {"ok": True}

    # ---- the embedded AWS pane (the one-stop trick) ------------------------
    def open_aws_pane(self, which="signup"):
        """Open AWS's OWN page in a SEPARATE embedded window. The user types card /
        password directly into AWS here; we never touch its DOM. We only watch the
        URL to advance the guidance rail in the main window."""
        urls = {
            "signup": "https://portal.aws.amazon.com/billing/signup",
            "console": "https://console.aws.amazon.com/",
            # Create-user page, NOT #/security_credentials: that page is the
            # signed-in (root) user's own credentials and nudges the customer
            # toward creating ROOT access keys — the exact thing the product
            # forbids. The wizard walks them through creating the 'deployer'
            # IAM user here instead. (Fixed 2026-07-15.)
            "iam-keys": "https://console.aws.amazon.com/iam/home#/users/create",
            # done-screen shortcuts: confirm the SES identity, label + train
            "ses": "https://console.aws.amazon.com/ses/home#/verified-identities",
            "rekognition": "https://console.aws.amazon.com/rekognition/custom-labels#/projects",
            # account closure is ROOT + console-only (AWS provides no API for
            # standalone accounts) — we guide, never automate, never see root.
            "close-account": "https://console.aws.amazon.com/billing/home#/account",
        }
        url = urls.get(which, urls["signup"])
        if self._aws_window is not None:
            try:
                self._aws_window.load_url(url)
                return {"ok": True, "reopened": True}
            except Exception:
                self._aws_window = None
        self._aws_window = webview.create_window(
            "Amazon Web Services  —  secure, hosted by AWS",
            url=url, width=1120, height=820,
        )

        def _on_loaded():
            try:
                cur = self._aws_window.get_current_url()
            except Exception:
                cur = ""
            # Only the URL is read — never the page content (trust boundary).
            self._push("aws_nav", {"which": which, "url": cur or ""})

        self._aws_window.events.loaded += _on_loaded
        return {"ok": True, "reopened": False}

    def close_aws_pane(self):
        if self._aws_window is not None:
            try:
                self._aws_window.destroy()
            except Exception:
                pass
            self._aws_window = None
        return {"ok": True}

    # ---- deployment --------------------------------------------------------
    def preflight(self, cfg):
        """Cheap validation before deploying: creds present, prefix legal, STS ok."""
        import re
        ak, sk = self._load_creds()
        if not ak or not sk:
            return {"ok": False, "error": "No AWS credentials on file. Add them first."}
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,20}", cfg.get("prefix", "")):
            return {"ok": False, "error": "Deployment id must be lowercase letters, "
                                          "numbers or hyphens (2-21 chars)."}
        if not cfg.get("sender_email"):
            return {"ok": False, "error": "An alert sender email is required."}
        return {"ok": True}

    def start_deploy(self, cfg):
        """Kick off the full deployment in a background thread. Progress streams
        back via ARGUS.onDeployEvent. Returns immediately."""
        if self._deploying:
            return {"ok": False, "error": "A deployment is already running."}
        if self._training:
            return {"ok": False, "error": "A training run is active — wait for "
                                          "it to reach the cloud stage first."}
        pre = self.preflight(cfg)
        if not pre["ok"]:
            return pre
        self._deploying = True
        threading.Thread(target=self._deploy_worker, args=(cfg,), daemon=True).start()
        return {"ok": True}

    def _deploy_worker(self, cfg):
        try:
            ak, sk = self._load_creds()
            deploy.set_emitter(self._push)
            ctx = deploy.Ctx.from_params(
                access_key_id=ak, secret_access_key=sk,
                region=cfg.get("region", "us-east-1"),
                prefix=cfg.get("prefix", "argus"),
                target_label=cfg.get("target_label", "target"),
                sender_email=cfg.get("sender_email", ""),
                recipient_email=cfg.get("recipient_email") or cfg.get("sender_email", ""),
                deployment_name=cfg.get("deployment_name", "My deployment"),
                live_view=bool(cfg.get("live_view", False)),
            )
            ok, err = deploy.run_plan(ctx, list(deploy.STAGES))
            # run_plan already emits the 'error' event itself — pushing again
            # here printed every failure twice in the feed.
        except Exception as e:
            self._push("error", {"message": str(e)})
            traceback.print_exc()
        finally:
            deploy.set_emitter(None)
            self._deploying = False

    # ---- resume / integrity (added 2026-07-15) ------------------------------
    def has_credentials(self):
        """True if a verified IAM key is already in the OS credential store —
        lets the UI mark the keys step done when the user relaunches."""
        ak, sk = self._load_creds()
        return {"ok": bool(ak and sk)}

    def deployment_status(self):
        """What happened before this launch? Reads deploy_state.json:
        none     — never deployed
        partial  — started but no completion stamp (crashed / user quit mid-run)
        complete — full run finished (stamp present)
        The UI uses this to offer 'Resume deployment' / jump to the done screen."""
        p = deploy.OUT / "deploy_state.json"
        if not p.exists():
            return {"status": "none"}
        try:
            state = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {"status": "none"}
        out = {
            "last_stage": state.get("deploy:last_stage", ""),
            "dashboard": ("https://" + state["cloudfront:domain"])
                          if state.get("cloudfront:domain") else "",
            "api": state.get("apigw:url", ""),
        }
        if state.get("deploy:completed_at"):
            return {"status": "complete",
                    "completed_at": state["deploy:completed_at"], **out}
        return {"status": "partial", **out}

    def verify_deployment(self, cfg=None):
        """Live read-only audit: is every deployed resource really there?
        Runs deploy.verify() with the stored key; returns per-resource results.
        Safe on partial deployments (missing things come back ok=False)."""
        try:
            ak, sk = self._load_creds()
            if not ak or not sk:
                return {"ok": False, "error": "No stored credentials."}
            cfg = cfg or {}
            # The state file is the source of truth for WHERE the deployment
            # lives — the UI's saved cfg can drift (or be defaults). Otherwise a
            # healthy ap-southeast-1 stack gets audited in us-east-1 and reads
            # as broken.
            region = cfg.get("region", "us-east-1")
            prefix = cfg.get("prefix", "argus")
            try:
                state = json.loads((deploy.OUT / "deploy_state.json")
                                   .read_text(encoding="utf-8"))
                region = state.get("deploy:region") or region
                prefix = state.get("deploy:prefix") or prefix
                if not state.get("deploy:region") and state.get("apigw:url"):
                    # older state files: recover the region from the API URL
                    m = state["apigw:url"].split(".execute-api.")
                    if len(m) == 2:
                        region = m[1].split(".amazonaws.com")[0] or region
            except Exception:
                pass
            ctx = deploy.Ctx.from_params(
                access_key_id=ak, secret_access_key=sk,
                region=region,
                prefix=prefix,
                target_label=cfg.get("target_label", "target"),
                sender_email=cfg.get("sender_email", "verify@localhost"),
            )
            checks = deploy.verify(ctx)
            return {"ok": True, "checks": checks,
                    "present": sum(1 for c in checks if c["ok"]),
                    "total": len(checks)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- helpers shared by the post-deploy management features -------------
    def _read_state(self):
        try:
            return json.loads((deploy.OUT / "deploy_state.json")
                              .read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _mgmt_session(self):
        """boto3 session on the stored key, in the DEPLOYED region (from the
        state file, not the UI form — same source-of-truth rule as verify)."""
        import boto3
        ak, sk = self._load_creds()
        if not ak or not sk:
            return None, None
        state = self._read_state()
        region = state.get("deploy:region", "us-east-1")
        return boto3.Session(aws_access_key_id=ak, aws_secret_access_key=sk,
                             region_name=region), state

    # ---- dashboard account management (Cognito CRUD, 2026-07-16) -----------
    def list_dashboard_users(self):
        try:
            sess, state = self._mgmt_session()
            if not sess or not state.get("cognito:pool"):
                return {"ok": False, "error": "No deployment on file."}
            cog = sess.client("cognito-idp")
            out = []
            for u in cog.list_users(UserPoolId=state["cognito:pool"],
                                    Limit=60).get("Users", []):
                email = next((a["Value"] for a in u.get("Attributes", [])
                              if a["Name"] == "email"), u["Username"])
                out.append({"email": email, "username": u["Username"],
                            "status": u.get("UserStatus", ""),
                            "enabled": u.get("Enabled", True)})
            return {"ok": True, "users": out}
        except Exception as e:
            return {"ok": False, "error": _friendly_aws_error(e)}

    def create_dashboard_user(self, email, password):
        """Admin-create with a PERMANENT password (no FORCE_CHANGE_PASSWORD
        limbo — the dashboard login intentionally doesn't handle it)."""
        try:
            sess, state = self._mgmt_session()
            if not sess or not state.get("cognito:pool"):
                return {"ok": False, "error": "No deployment on file."}
            cog = sess.client("cognito-idp")
            pool = state["cognito:pool"]
            cog.admin_create_user(
                UserPoolId=pool, Username=email,
                UserAttributes=[{"Name": "email", "Value": email},
                                {"Name": "email_verified", "Value": "true"}],
                MessageAction="SUPPRESS")
            cog.admin_set_user_password(UserPoolId=pool, Username=email,
                                        Password=password, Permanent=True)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": _friendly_aws_error(e)}

    def delete_dashboard_user(self, email):
        try:
            sess, state = self._mgmt_session()
            if not sess or not state.get("cognito:pool"):
                return {"ok": False, "error": "No deployment on file."}
            sess.client("cognito-idp").admin_delete_user(
                UserPoolId=state["cognito:pool"], Username=email)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": _friendly_aws_error(e)}

    # ---- one-click teardown (2026-07-16) ------------------------------------
    def destroy_deployment(self, confirm_text):
        """Delete EVERYTHING the deployment created. The UI must send the
        deployment prefix as typed confirmation; runs in a thread and streams
        ('destroy', ...) events. Irreversible by design."""
        if self._deploying:
            return {"ok": False, "error": "A deployment/teardown is already running."}
        # NEVER tear down under a live training pipeline: destroy would delete
        # the Rekognition project out from under it, and the training thread's
        # next whole-dict ctx.save() would resurrect the archived state file.
        if self._training:
            return {"ok": False, "error": "A training run is active. Wait for "
                                          "it to finish (or fail) before "
                                          "deleting the deployment."}
        state = self._read_state()
        prefix = state.get("deploy:prefix", "")
        if not state:
            return {"ok": False, "error": "No deployment on file."}
        if (confirm_text or "").strip() != prefix:
            return {"ok": False,
                    "error": f"Type the deployment id '{prefix}' exactly to confirm."}
        ak, sk = self._load_creds()
        if not ak or not sk:
            return {"ok": False, "error": "No stored credentials."}
        self._deploying = True

        def worker():
            try:
                deploy.set_emitter(self._push)
                ctx = deploy.Ctx.from_params(
                    access_key_id=ak, secret_access_key=sk,
                    region=state.get("deploy:region", "us-east-1"),
                    prefix=prefix or "argus",
                    target_label="teardown",
                    sender_email=state.get("deploy:sender", "teardown@localhost"),
                )
                deploy.destroy(ctx)
                # PROOF OF ABSENCE: the destroy engine's own step results are
                # best-effort claims. Re-run the SAME live audit used to prove
                # the deployment complete — now success means 0 resources left.
                # (ctx.state is still populated in memory; destroy only archived
                # the on-disk file.)
                checks = deploy.verify(ctx)
                leftovers = [c["name"] for c in checks if c["ok"]]
                self._push("destroy_verified", {
                    "live": len(leftovers), "total": len(checks),
                    "leftovers": leftovers[:12]})
            except Exception as e:
                self._push("destroy", {"line": f"TEARDOWN FAILED: {e}", "ok": False})
            finally:
                deploy.set_emitter(None)
                self._deploying = False
        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    def account_alive(self):
        """STS liveness probe for the stored key. Distinguishes AUTH-dead
        (account closed / key revoked -> the UI offers a fresh start) from
        NETWORK errors (must never be mistaken for a closed account)."""
        ak, sk = self._load_creds()
        if not ak or not sk:
            return {"ok": False, "reason": "no-creds"}
        try:
            import boto3
            ident = boto3.Session(
                aws_access_key_id=ak, aws_secret_access_key=sk
            ).client("sts").get_caller_identity()
            return {"ok": True, "account": ident["Account"]}
        except Exception as e:
            code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            dead = code in ("InvalidClientTokenId", "UnrecognizedClientException",
                            "SignatureDoesNotMatch", "ExpiredToken")
            return {"ok": False, "reason": "auth" if dead else "network",
                    "error": str(e)[:140]}

    # ---- in-app training pipeline (2026-07-17) ------------------------------
    # Deterministic automation only (the 2026-07-16 architecture ruling):
    # every bridge below drives training.py's fixed API pipeline.
    def _train_ctx(self):
        """Ctx on the stored key in the DEPLOYED region/prefix (state file is
        the source of truth — same rule as verify/_mgmt_session)."""
        ak, sk = self._load_creds()
        if not ak or not sk:
            return None
        state = self._read_state()
        return deploy.Ctx.from_params(
            access_key_id=ak, secret_access_key=sk,
            region=state.get("deploy:region", "us-east-1"),
            prefix=state.get("deploy:prefix", "argus"),
            target_label="training",
            sender_email=state.get("deploy:sender", "train@localhost"))

    def pick_training_folder(self):
        """Native folder picker — the customer never types a path."""
        if not self._window:
            return {"ok": False, "error": "No window."}
        try:
            res = self._window.create_file_dialog(webview.FOLDER_DIALOG)
            if not res:
                return {"ok": False, "cancelled": True}
            path = res[0] if isinstance(res, (list, tuple)) else res
            return {"ok": True, "path": str(path)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def analyze_training_folder(self, path):
        """Local fail-fast dataset report (no AWS, no upload)."""
        try:
            return training.analyze(path)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def plan_training(self, report, class_id):
        """Train/test division + cost estimate for the cost gate."""
        try:
            return training.plan_for_class(report, class_id)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def start_training(self, params):
        """Kick off the full pipeline in a background thread; progress streams
        back as train_* events. The UI must have cost-gated already."""
        # Claim the flag ATOMICALLY before any slow work (the STS call in
        # Ctx.from_params takes long enough for a double-click to race).
        with self._flag_lock:
            if self._deploying:
                return {"ok": False, "error": "A deployment is running — wait "
                                              "for it to finish first."}
            if self._training:
                return {"ok": False, "error": "A training run is already active."}
            self._training = True
        try:
            folder = (params or {}).get("path", "")
            class_id = (params or {}).get("class_id")
            class_name = (params or {}).get("class_name")
            if class_id is None:
                self._training = False
                return {"ok": False, "error": "Pick the class to train on."}
            try:
                ctx = self._train_ctx()
            except Exception as e:
                self._training = False
                return {"ok": False, "error": _friendly_aws_error(e)}
            if ctx is None:
                self._training = False
                return {"ok": False, "error": "No stored credentials."}
        except Exception:
            self._training = False
            raise
        self._close_anyway = False

        def worker():
            try:
                training.set_emitter(self._push)
                training.run_training(ctx, folder, class_id, class_name)
            except Exception as e:
                self._push("train_error", {"message": str(e)})
                traceback.print_exc()
            finally:
                training.set_emitter(None)
                self._training = False
        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    def watch_training(self):
        """Re-attach a watcher to an in-flight run (relaunch / crash case).
        Also completes the wire-in instantly if training finished while the
        app was closed."""
        with self._flag_lock:
            if self._training:
                return {"ok": True, "already": True}
            self._training = True
        try:
            ctx = self._train_ctx()
        except Exception as e:
            self._training = False
            return {"ok": False, "error": _friendly_aws_error(e)}
        if ctx is None:
            self._training = False
            return {"ok": False, "error": "No stored credentials."}
        if not ctx.state.get("train:pending"):
            self._training = False
            return {"ok": False, "error": "No training run is in progress."}

        def worker():
            try:
                training.set_emitter(self._push)
                training.watch_and_wire(ctx)
            except Exception as e:
                self._push("train_error", {"message": str(e)})
                traceback.print_exc()
            finally:
                training.set_emitter(None)
                self._training = False
        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    def training_status(self):
        """none / trained / in_progress / needs_wire (+ active model info).
        Cheap: answers from local state unless a run is pending."""
        try:
            state = self._read_state()
            if not state.get("rekognition:project"):
                return {"ok": True, "status": "no-deployment"}
            if not state.get("train:pending"):
                # 'preparing' = the local pipeline (convert/upload/datasets)
                # is running in-process but nothing is submitted to AWS yet —
                # the UI must show the live run view, not the folder picker.
                if self._training:
                    status = "preparing"
                elif state.get("train:active_arn"):
                    status = "trained"
                else:
                    status = "none"
                return {"ok": True, "status": status,
                        "active_f1": state.get("train:active_f1"),
                        "rollback": bool(state.get("train:rollback_arn")),
                        "last_failure": state.get("train:last_failure"),
                        "aborted_attempt": (bool(state.get("train:attempt"))
                                            and not self._training),
                        "watching": self._training}
            ctx = self._train_ctx()
            if ctx is None:
                return {"ok": False, "error": "No stored credentials."}
            out = training.status(ctx)
            out["watching"] = self._training
            return out
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def rollback_model(self):
        """Swap every custom camera back to the previous model ARN."""
        try:
            ctx = self._train_ctx()
            if ctx is None:
                return {"ok": False, "error": "No stored credentials."}
            return training.rollback(ctx)
        except Exception as e:
            return {"ok": False, "error": _friendly_aws_error(e)}

    def on_closing(self):
        """Window-close guard. Once training is SUBMITTED (train:pending on
        disk) closing is safe — the cloud continues without us. But during
        the local convert/upload/datasets phases a close hard-kills the run,
        so block the first attempt and explain; a second close within the
        same session is honored (the user has been told)."""
        try:
            if self._training and not self._read_state().get("train:pending"):
                if not self._close_anyway:
                    self._close_anyway = True
                    try:
                        self._window.evaluate_js(
                            "window.showNote && showNote('Training is still "
                            "uploading — closing now cancels it before it "
                            "reaches AWS. Close again to exit anyway.', 7000)")
                    except Exception:
                        pass
                    return False
        except Exception:
            pass
        return True

    # ---- misc --------------------------------------------------------------
    def legal_text(self, which):
        """Return the Terms of Use / Privacy text bundled with the app."""
        fname = {"tos": "terms_of_use.md", "privacy": "privacy_policy.md"}.get(which)
        p = HERE / "legal" / (fname or "")
        if p.exists():
            return {"ok": True, "text": p.read_text(encoding="utf-8")}
        return {"ok": False, "text": ""}

    def open_external(self, url):
        """Only used for the final dashboard URL — opens the user's real browser."""
        import webbrowser
        webbrowser.open(url)
        return {"ok": True}

    def quit(self):
        """Decline-and-exit / Exit setup: close everything. Honors the same
        guard as the window X — destroy() bypasses the closing event, so the
        un-submitted-training check must be repeated here."""
        if not self.on_closing():
            return {"ok": False, "blocked": "training"}
        self.close_aws_pane()
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass
        return {"ok": True}


def _friendly_aws_error(e):
    s = str(e)
    if "InvalidClientTokenId" in s or "SignatureDoesNotMatch" in s:
        return "Those keys were not accepted by AWS. Check for a copy/paste slip."
    if "AccessDenied" in s:
        return "The keys are valid but lack permission. Use an administrator key."
    return "Could not reach AWS with those keys. Check your connection and try again."


def main():
    api = Api()
    window = webview.create_window(
        "ARGUS",
        url=str(WEB / "index.html"),
        # js_api is what makes window.pywebview.api exist in the page. It was
        # MISSING until 2026-07-15, so the shipped exe always fell back to the
        # simulated preview (REAL=false) — every button dead. Root cause of the
        # "it's still a demo" complaint. Do not remove.
        js_api=api,
        width=1180, height=820, min_size=(940, 680),
        background_color="#050507",
    )
    api.bind(window)
    # closing-guard: protect an un-submitted training run from a stray X
    window.events.closing += api.on_closing
    # gui='edgechromium' -> Edge WebView2 on Windows (bundled runtime on Win11).
    # storage_path pins the WebView2 profile to a STABLE on-disk location, so
    # cookies/localStorage survive relaunches: the embedded AWS window keeps the
    # customer's console session (no re-login within AWS's ~12h session window)
    # and the wizard's own resume state persists. private_mode=False is required
    # for the profile to be written at all. (Added 2026-07-15.)
    # Always per-user (%LOCALAPPDATA%), NEVER inside the project tree: the
    # profile holds console session cookies, autofill and history — in dev runs
    # deploy.OUT.parent would be deployer/ itself, and a later zip/handoff of
    # the folder would ship the customer's browser profile with it.
    profile_dir = (Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
                   / "ARGUS" / "webview_profile")
    profile_dir.mkdir(parents=True, exist_ok=True)
    webview.start(gui="edgechromium", debug=("--debug" in sys.argv),
                  private_mode=False, storage_path=str(profile_dir))


if __name__ == "__main__":
    main()
