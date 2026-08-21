# Deployment rehearsal protocol

Two rounds. Round 1 shakes out bugs cheaply; round 2 is the full customer-realism
dress rehearsal. Do not skip round 1 — burning the "fresh account" experience on a
packaging bug wastes it.

## Round 1 — cheap shakeout (dev machine, old account)

Target: the dormant old account `396278862184` (queued for closure; its Gen-1
residue usefully exercises the adopt-existing idempotency paths).

1. `pip install -r requirements.txt`, then `python app.py --debug`.
2. Paste an IAM key for the OLD account (create one in its console if none).
3. Run the full wizard: consent -> keys -> config (any target label) -> review ->
   hold-to-deploy. Watch all 15 stages stream into the theater.
4. PASS = theater reaches f/1.4, done screen shows a real CloudFront URL, and
   `%USERPROFILE%`-side `deployer/out/deploy_state.json` has every id.
5. **Training pipeline (added 2026-07-17):** done screen -> "Train your model"
   -> pick a small YOLO folder (e.g. ~30 images cut from `C:\Dataset`) ->
   analysis report shows counts/warnings -> cost gate shows the estimate ->
   type TRAIN. Cheapest real proof: let it run to TRAINING_COMPLETED (~1 h,
   ~$4), confirm F1 appears and `custom_model_arn` landed on both camera
   rows, then close/reopen the app mid-watch once to prove re-attach.
6. Known acceptable noise: stage adoption logs ("exists, adopting") from residue.
7. Bugs found here get fixed BEFORE round 2.

## Round 2 — full dress rehearsal (the real thing)

Realism on all four axes: account, machine, credentials, usage.

1. **Build the real artifact:** `powershell -File build.ps1` -> `dist\ARGUS.exe`
   (build.ps1 also prebuilds `layer/fyp-pillow.zip` on first run).
2. **Clean machine:** run ARGUS.exe on the mini PC's Win11 host (no Python, no
   AWS CLI, no cached credentials) — that is the customer's computer.
3. **Fresh AWS account, created THROUGH the app:** walk the embedded signup flow
   for real — email verification, payment card typed into AWS's embedded page,
   phone code, Basic plan. This is the only true test of the one-stop UX.
4. **IAM key via the embedded console flow**, pasted into the app; confirm the
   STS check passes and the key lands in Windows Credential Manager (inspect:
   Control Panel -> Credential Manager -> "ARGUS-deployer").
5. **Real deploy**, all 15 stages.
6. **Day-1 customer smoke test:**
   - open the dashboard URL from the done screen (HTTPS, ARGUS login page)
   - admin-create a Cognito user (console is fine), sign in
   - Settings -> Test upload an image -> a record appears in Gallery (clean
     record path; no model exists yet — expected)
   - "Train your model" -> pick a labeled YOLO folder -> analysis + cost gate
     render; running the actual training here is optional if round 1 proved it
   - confirm the SES verification email arrived and, after confirming, that the
     sender identity shows Verified
   - check EventBridge Scheduler has `pest-model-watchdog-15min` ENABLED
7. **Cost check:** leave the account idle 24 h; billing should read ~$0.
8. Keep the account afterwards as the DEMO/handoff account (for Dr. Li / final
   presentation), or close it — closing a fresh account is one console action.

## Success criteria (what "it works" means)
- Zero terminal windows, zero visits to an external browser during the whole flow.
- Every user-typed secret went either into AWS's own embedded page (card,
  passwords) or into the OS credential store (IAM key). Nothing in plaintext.
- The done screen's URL serves the ARGUS dashboard over HTTPS with login.
- deploy_state.json contains: 6 role ARNs, 4 tables, 3 buckets, layer ARN,
  5 lambdas, pool+client ids, api id/url, cloudfront id/domain, project ARN.
