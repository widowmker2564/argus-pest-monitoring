# ARGUS deployer

A single Windows desktop app (`.exe`) that stands up the entire ARGUS AI-vision
detection stack into a **customer's own AWS account** — cloud resources, local
config, and the guided AWS-account/credential steps — **without the user ever
leaving the app**.

## What it is

| File | Role |
|---|---|
| `app.py` | Desktop shell: pywebview (Edge WebView2) window hosting the premium UI, the JS↔Python bridge (`Api`), the second embedded window for AWS's own pages, and the event pump that streams deploy progress to the UI. |
| `deploy.py` | The engine: boto3 orchestration of 15 idempotent stages that recreate the whole stack in a fresh account. Runnable standalone (CLI) or driven by `app.py` via `set_emitter` / `run_plan` / `Ctx.from_params`. |
| `web/index.html` | The ARGUS "machine-eye" UI — self-contained (inline CSS/JS, WebGL + Canvas2D hero, no CDNs). Doubles as a browser preview (simulated) and the real app (when `window.pywebview.api` is present). |
| `legal/` | Terms of Use + Privacy Policy shown on the first-run consent screen. |
| `STACK_MANIFEST.md` + `audit/` | The recreate-level BOM `deploy.py` was built from (audited from the live reference account). |
| `build.ps1` | Packages everything into `dist/ARGUS.exe`. |

## The one-stop rule (how, honestly)

The user never opens a browser or another app. But AWS account signup has **no
public API**, and card data must **never** pass through our software (PCI-DSS).
The resolution — the standard enterprise pattern (Stripe/Plaid onboarding):

- A **second embedded window** renders AWS's OWN pages (`portal.aws.amazon.com`,
  the IAM console) inside our app. The user types their card and passwords
  **directly into AWS's page** — we never proxy, read, or store them.
- That window has **no JS bridge and no injected script**. We observe only its
  **URL** (via the `loaded` event) to advance the guidance rail. This is the
  trust boundary and it holds only because the AWS window stays script-free.
- Once the user pastes the IAM **access key** (created in the embedded IAM
  console), it is validated against STS and stored **encrypted** in the Windows
  Credential Manager (via `keyring`) — never plaintext, never logged.
- From there, 100% of the deployment is automated CLI (boto3), streamed live to
  the "deployment theater" screen.

## Run from source (dev)

```
pip install -r requirements.txt
python app.py            # add --debug for devtools
```
Without pywebview the same UI runs as a clickable **preview** (all actions
simulated) by serving `web/` over any static server.

## Package to one .exe

```
powershell -ExecutionPolicy Bypass -File build.ps1
```
Produces `dist\ARGUS.exe`. Ship `MicrosoftEdgeWebView2Setup.exe` (≈2 MB
evergreen bootstrapper) next to it for machines that lack the WebView2 runtime
(preinstalled on Win11 / current Win10).

### Packaging notes (real gotchas, already handled in code)
- **Assets** unpack to `sys._MEIPASS`; `deploy.py` resolves `audit/`, `lambda/`,
  `legal/` from there when frozen (`getattr(sys, "frozen", False)`).
- **State is writable:** when frozen, `OUT` → `%LOCALAPPDATA%\ARGUS\out` (the
  `_MEIPASS` temp dir is wiped on exit).
- **The Pillow layer can't pip-build inside a frozen exe** (no toolchain, and
  `sys.executable` is the exe). `build.ps1` prebuilds `layer/fyp-pillow.zip`
  once and bundles it; `stage_layer` publishes the bundled zip when present.

## Owner TODO before shipping commercially
- Fill the bracketed placeholders in `legal/terms_of_use.md` and
  `legal/privacy_policy.md` (`[Company/Owner Name]`, `[Jurisdiction]`,
  `[Contact Email]`) and have a lawyer review.
- Provide/confirm an app icon and the WebView2 bootstrapper.
