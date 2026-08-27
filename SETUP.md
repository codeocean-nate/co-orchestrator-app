# Setting up the orchestration demo on your Code Ocean deployment

A complete, from-scratch guide for standing this demo up on **any** Code Ocean
deployment. Everything it needs lives in four public GitHub repos, so there is nothing
to download and nothing to upload by hand.

Two routes, same result:

- **Route A — [let Aqua build it](#route-a--let-aqua-build-it)** (fastest, ~15 minutes
  of waiting): paste five prompts and Aqua creates the capsules, environments, data
  asset and verification runs for you.
- **Route B — [build it by hand](#route-b--build-it-by-hand)** (~30 minutes): the same
  steps as UI clicks, for deployments without Aqua.

Both end at the same place: a Streamlit app running in a cloud workstation that drives
the other capsules through the Code Ocean API.

## What the demo does

The app — itself hosted in a capsule — uses the Code Ocean API to run *other* capsules
in the background, capture their results as versioned data assets, chain those assets
into the next capsule, and visualize the final output, with full provenance at every
step.

```
                        ┌─────────────────────────────────────────┐
                        │   Orchestrator capsule (Streamlit CW)   │
                        │   code/streamlit_app.py + co_api.py     │
                        └───────┬────────────▲───────────┬────────┘
                        run     │            │ poll /     │ run
                        capsule │            │ download   │ capsule
                                ▼            │            ▼
   ┌──────────────┐    ┌────────────────┐    │    ┌────────────────┐    ┌──────────────┐
   │ Data asset:  │───▶│ Step 1 capsule │    │    │ Step 2 capsule │───▶│ /results/    │
   │ sales_data   │mount│ Excel → CSV   │    │   mount│ CSV → HTML │    │ report.html  │
   │ .xlsx        │    │ /data/excel-   │    │    │ /data/processed│    │ (self-       │
   └──────────────┘    │ input          │    │    │ -csv           │    │  contained)  │
                       └───────┬────────┘    │    └────────▲───────┘    └──────────────┘
                               │ /results    │             │
                               ▼             │             │
                       ┌────────────────────────┐          │
                       │ NEW result data asset  │──────────┘
                       │ "Processed CSV — <ts>" │  mounted into step 2
                       └────────────────────────┘
                                  │
                                  ▼  the human-in-the-loop step
                       ┌──────────────────────────────────────────┐
                       │ ⚙️  Report parameters, chosen in the app  │
                       │    and sent as named parameters on the   │
                       │    step-2 run (no App Panel needed)      │
                       └─────────────────────┬────────────────────┘
                                             │ step 2 runs with them
                                             ▼
                       ┌──────────────────────────────────────────┐
                       │ NEW result data asset "Report — <ts>"    │
                       │ the report + your parameter values,      │
                       │ frozen onto it as app_parameters         │
                       └──────────────────────────────────────────┘
```

**The demo flow, as the audience sees it:**

1. Click **▶ Start processing** → the app runs the Excel→CSV capsule with the Excel
   data asset mounted, and polls the computation live.
2. When it completes, the app captures `/results` as a **new data asset** and polls it
   until **"✅ Data ready"**, then previews the CSV in a table.
3. **⚙️ Set report parameters** — the human-in-the-loop step. The app renders a form for
   the report capsule: title, region and category filters, a date window, minimum
   revenue, top-N products, analyst notes. The capsule needs no App Panel for this; the
   values are sent as named run parameters on the run itself.
4. Click **📊 Generate report** → the app runs the reporting capsule with the new asset
   mounted **and your parameters attached to the run**, polls it, downloads
   `report.html`, and renders it inline.
5. The report is captured as a **result data asset** too (`Report — <ts>`), so the values
   chosen in step 3 are frozen onto it as `app_parameters` — the human decision becomes
   part of the recorded provenance, next to the data it came from.
6. An **event log** shows every API interaction, and an **"Under the hood"** expander
   shows the exact `codeocean` SDK call behind each step with the real IDs.

## Before you start

Replace these placeholders wherever they appear below:

| Placeholder | What to substitute |
|---|---|
| `https://YOUR-DEPLOYMENT.codeocean.com` | the URL you use to reach Code Ocean, with no trailing slash |
| `<SLUG>` | the number in a capsule's URL: `…/capsule/<SLUG>/tree` |
| `<STEP1_CAPSULE_ID>`, `<STEP2_CAPSULE_ID>`, `<INPUT_DATA_ASSET_ID>` | capsule and data asset **UUIDs**, which you collect as you go (not slugs) |

Prerequisites:

| Requirement | Needed for |
|---|---|
| Permission to **create capsules** and **create data assets** | both routes |
| The deployment can reach **github.com** | cloning the four public repos; if it cannot, see [no GitHub access](#no-github-access) |
| **Cloud workstations** enabled, with the Streamlit workstation type available | running the app |
| Compute can reach the **deployment's own API host** and its **object-storage endpoint** (presigned result downloads often live on a different host) | the app orchestrating runs and fetching the report |
| A **Code Ocean API token** (Account → Credentials/API tokens) | the app calls the API as you |
| Aqua available, **v4.3+** | Route A (creating capsules from a git URL) |
| Aqua **v4.7+** | Route A's last step (launching the cloud workstation from chat); on older versions launch Streamlit yourself |

## The four capsules

| Capsule (suggested name) | Repo to clone | Environment | Role |
|---|---|---|---|
| Orchestrator Demo — Data Seed | https://github.com/codeocean-nate/co-orchestrator-data-seed | Python starter, **no packages** | Stages the bundled `sales_data.xlsx` into `/results` so you can create the input data asset by capturing a run |
| Orchestrator Demo — Step 1: Excel to CSV | https://github.com/codeocean-nate/co-orchestrator-step1-excel-to-csv | Python starter + `pandas`, `openpyxl` | Converts every sheet of every mounted workbook to `/results/csv/*.csv` + `manifest.json` |
| Orchestrator Demo — Step 2: HTML Report | https://github.com/codeocean-nate/co-orchestrator-step2-html-report | Python starter + `pandas`, `plotly` | Reads mounted CSVs, writes one fully self-contained `/results/report.html`. Accepts the report's run parameters as `--param_name=value` argv, which is what the app's **⚙️ Report parameters** form sends |
| Orchestrator Demo — App | https://github.com/codeocean-nate/co-orchestrator-app | Python starter + `streamlit`, `codeocean`, `pandas`, `requests` | The Streamlit UI that orchestrates the other two |

Names are only a suggestion; nothing in the code depends on them. What *is* load-bearing:
the app's entry point must stay at **`code/streamlit_app.py`** — that is the path Code
Ocean's Streamlit Cloud Workstation serves.

### Do not add an `app-panel.json`

None of these repos contains a `.codeocean/app-panel.json`, and none of them should.
Measured on a live deployment: a capsule holding a committed `.codeocean/app-panel.json`
**cannot be run at all** — every run attempt fails with

```
403 {"message":"corrupted object files","corrupted_object_files":[".codeocean/app-panel.json"]}
```

Deleting the file makes the capsule runnable again. Committing it does not produce an App
Panel either: App Builder still offers "Create App" and the capsule keeps a plain
**Reproducible Run** button.

The demo does not need one. The app's **⚙️ Report parameters** values are sent as *named
run parameters* on the step-2 run, and named parameters reach a capsule that has no App
Panel — verified, not a workaround. If you separately want an App Panel in the Code Ocean
UI, build it in the **App Builder** (or ask Aqua to) and let Code Ocean write that file
itself; that is optional and has nothing to do with the orchestrator app.

## Route A — let Aqua build it

Open [AQUA_PROMPT.md](AQUA_PROMPT.md) and paste its five messages into Aqua's chat one
at a time, waiting for each to finish. Aqua creates all four capsules from the repos
above, sets the environments, creates the input data asset, verifies each stage with
real runs, wires the app's environment variables, and launches the workstation.

Aqua will not touch secrets, so two things stay manual: [attaching your API
token](#the-api-token) and, if the workstation was launched before the secret existed,
relaunching it.

Nothing extra is needed for the app's **⚙️ Report parameters** form: the app defines
those parameters itself and sends them as named run parameters, which reach the step-2
capsule whether or not it has an App Panel. Aqua must not add a
`.codeocean/app-panel.json` to any capsule — see [Do not add an
app-panel.json](#do-not-add-an-app-paneljson).

Then skip to [Run the demo](#run-the-demo).

## Route B — build it by hand

### 1. Create the four capsules

For each row of [the table above](#the-four-capsules): **➕ New Capsule → Clone from
Git**, paste the repo URL, and name the capsule.

You need the step 1 and step 2 capsules' **UUIDs** in step 6 — and the UUID is not the
number in the capsule URL. If your deployment does not surface it somewhere convenient,
don't hunt for it now: the app's sidebar has a **🔎 Find a capsule ID by name** search
that looks UUIDs up for you once the app is running.

### 2. Set the environments

In each capsule's **Environment Editor**, pick a Python starter environment and add the
pip3 packages from the table. The data seed capsule needs none — its `code/run` only
copies a file.

### 3. Create the input data asset

The API has no direct file-upload path, so the demo ships its input *as code* and turns
it into a data asset by capturing a run. That is all the data seed capsule is for.

1. Open **Orchestrator Demo — Data Seed** and start a **Reproducible Run**.
2. The log should end with:
   ```
   Staged sales_data.xlsx (25379 bytes) into results.
   ```
3. From that computation's results, **capture the results as a data asset**:
   - **Name**: `Demo Sales Data (Excel)`
   - **Description**: `Seeded retail sales workbook (500 transactions + regional targets) — input for the orchestration demo`
   - **Tags**: `orchestrator-demo`, `input`
4. Wait until the asset's state is **ready**, then note its **UUID** — this is
   `<INPUT_DATA_ASSET_ID>`.

### 4. Verify step 1 (Excel → CSV)

1. Attach **Demo Sales Data (Excel)** to the step 1 capsule and start a Reproducible Run.
2. Check the run log — the script prints a line per output:
   ```
   wrote csv/sales_data__transactions.csv (500 rows)
   wrote csv/sales_data__regional_targets.csv (16 rows)
   Wrote /results/manifest.json
   ```
   You do not need to open the result files; the log is the check.
3. **Detach the data asset again** (keep the asset — do not delete it). The app mounts
   it explicitly at run time, so it must not stay attached to the capsule.

### 5. Verify step 2 (CSV → HTML report)

This rehearses the exact hand-off the app performs at demo time.

1. Capture the successful step 1 run's results as a data asset named
   `Processed CSV — build verification`, tags `orchestrator-demo`, `step1-output`.
   Wait until it is ready.
2. Attach that asset to the step 2 capsule and start a Reproducible Run.
3. Check the log for the report line and confirm the size is over 1 MB — the report
   inlines plotly.js so it is fully self-contained:
   ```
   [step2] wrote /results/report.html (4.9 MB)
   [step2] wrote /results/manifest.json
   ```
4. **Confirm the parameters reach the code.** Nothing needs to be configured on the
   capsule for this — the app sends the values as named run parameters and Code Ocean
   appends them to `code/run`. To prove it end to end before demo time, run the capsule
   once over the API with `named_parameters={"report_title": "Panel-free check",
   "top_n": "5"}`: the run log echoes the arguments it received, the heading on
   `report.html` changes, and the computation records the values. That capsule has no App
   Panel, and it does not need one.
5. **Detach the verification asset** from the capsule (again, keep the asset). At demo
   time the app mounts a freshly captured asset, and a leftover attachment can collide
   with it.

### 6. Configure the app capsule

Add the [environment variables](#configuration-reference) to the app capsule's
Environment Editor, then start one Reproducible Run of the app capsule. It does not
serve anything — it just verifies that all four packages import and the app code
compiles:

```
  streamlit  1.50.0
  codeocean  0.16.0
  pandas     2.x
  requests   2.x
Environment OK — all app dependencies import cleanly.
App code compiles.
```

If that run fails, fix the environment before going further — the workstation uses the
same image.

### 7. Attach the token and launch

Continue with [The API token](#the-api-token) and [Launch the
app](#launch-the-app).

## Configuration reference

The app reads its configuration from environment variables first, and falls back to
sidebar inputs for anything missing (values you type persist for that browser session).

| Env var | Required | Meaning |
|---|---|---|
| `STEP1_CAPSULE_ID` | yes | UUID of the Excel→CSV capsule |
| `STEP2_CAPSULE_ID` | yes | UUID of the HTML-report capsule |
| `INPUT_DATA_ASSET_ID` | yes | UUID of the `Demo Sales Data (Excel)` asset |
| `CODEOCEAN_TOKEN`, or `API_SECRET`, or `CUSTOM_KEY` | yes | API token, checked in that order. Attach it as a secret — see below. Never displayed or logged. |
| `CODEOCEAN_DOMAIN` | recommended | The deployment URL — the host you sign in to, e.g. `https://your-deployment.codeocean.com`. The app tries to auto-detect it from the capsule's git remote, but that is a best-effort convenience that only recognises the standard `https://<host>/capsule-<slug>.git` remote shape; it returns nothing (and you type the URL in the sidebar) for anything else. Setting this explicitly is always correct, and it is required when running the app outside Code Ocean. |

Set the three UUIDs as **custom environment variables** in the app capsule's
Environment Editor. They are capsule/asset **UUIDs**, not the numeric slugs from the
URL. If you skip them, the app still works — you paste the IDs into the sidebar at demo
time, and the sidebar's **🔎 Find a capsule ID by name** search will look up capsule
UUIDs for you.

## The API token

The app calls the Code Ocean API as you, so it needs a token at runtime. Do not put a
token in an environment variable or in the code — attach it as a secret:

1. **Account → Roles and Secrets → Add secret → Custom Key**, with your API token as
   the value.
2. App capsule → **settings gear → Credentials** tab → add that secret.
3. It surfaces in the capsule as `CUSTOM_KEY`, which the app checks after
   `CODEOCEAN_TOKEN` and `API_SECRET`. If your version offers **Secrets Actions → "Set
   Environment Variable Names"**, name it `CODEOCEAN_TOKEN` instead.

Menu names differ slightly between versions; what matters is that the secret ends up
attached to the app capsule as a credential.

> **A workstation only sees secrets attached before it started.** If the Streamlit
> workstation was already running when you attached the secret, shut it down and
> relaunch it, or the app will not see the token.

**Alternative for a quick demo — paste it in the sidebar.** Skip the secret entirely
and type the token into the app sidebar's password field. It is held in the Streamlit
session only, never displayed and never logged. It disappears when the session ends,
which makes it a good fit for a one-off demo and a bad fit for anything shared.

## Launch the app

App capsule → **Reproducibility Panel → Streamlit** (the cloud workstation icon). Code
Ocean serves `code/streamlit_app.py` and opens it in a browser tab.

## Run the demo

With the sidebar showing all five settings resolved:

1. **▶ Start processing** — runs step 1 with the Excel asset mounted, polls it live,
   then automatically captures the results as a new data asset and polls that to ready.
2. **✅ Data ready** appears with the new asset's ID, and the CSV preview renders.
3. **⚙️ Report parameters** — the form above the buttons. Set a report title, narrow the
   regions, categories or date window, raise the minimum revenue, change top-N, type an
   analyst note. Leaving a field at its default simply means that value is not sent.
   This is the moment to say what actually travels: each choice becomes a named run
   parameter on the step-2 computation — a `--param_name=value` argument appended to the
   capsule's `code/run` — so the GUI decision is recorded by Code Ocean rather than
   living and dying inside Streamlit.
4. **📊 Generate report** — runs step 2 with the new asset mounted *and those values
   attached as named run parameters*, downloads `report.html`, and renders it inline. A
   download button offers the file itself, and the report's **Report parameters** card
   lists exactly what the run received.
5. The app then captures the report as its own result data asset, `Report — <ts>`, and
   shows its ID. Open that asset in Code Ocean: its provenance names the step-2 run and
   the mounted CSV asset, and your choices are frozen onto it as `app_parameters` — the
   reason the parameters travel through Code Ocean instead of staying in Streamlit.
6. Open **Event log** and **Under the hood** to show the API calls behind what just
   happened.

**♻️ Reset demo** clears the pipeline state but keeps your configuration, so you can
run it again for the next audience.

## Optional — share it as a No-Code App

To give colleagues the running app without the code: complete one Reproducible Run of
the app capsule (step 6 above), commit everything, fill in the capsule metadata, then
**Release → Release Functionality → No-Code App** (Streamlit Cloud Workstation type).
An *Open No-Code App* link then appears on the Internal Releases page. Sessions are
cloud workstations under the hood, so the deployment's idle timeout applies.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Sidebar says **"⚙️ Configuration incomplete"** | One of the five settings is empty. Env vars fill in as disabled fields; anything editable is missing. |
| App starts but every API call 401/403s | The token never reached the app. Confirm the secret is attached to *this* capsule, then relaunch the workstation — a workstation started before the secret was attached does not have it. |
| Token is set but calls fail against the wrong host | Set `CODEOCEAN_DOMAIN` explicitly to your deployment URL, with no trailing slash. |
| Connection errors on ▶ Start processing or when downloading the report | The workstation has no egress to what it needs. The app calls the deployment's own API host, and downloads results from **presigned object-storage URLs**, which on most deployments are a different host. Both must be reachable from compute. |
| **Capsule not found** on ▶ Start processing | A slug was used instead of a UUID. `STEP1_CAPSULE_ID` / `STEP2_CAPSULE_ID` are UUIDs; use the sidebar's **🔎 Find a capsule ID by name** search to get them. |
| Step 1 run "succeeds" but the report is generic, not the sales report | No Excel file was mounted, so step 1 fell back to synthetic data. Check `INPUT_DATA_ASSET_ID` points at the ready `Demo Sales Data (Excel)` asset. |
| Step 2 run fails, or the report is only a few KB | The environment is missing `plotly` (or `pandas`). Fix the Environment Editor packages and rerun. |
| A capsule refuses to run: **403 `corrupted object files`**, naming `.codeocean/app-panel.json` | That file is present in the capsule and makes it unrunnable. Delete `.codeocean/app-panel.json` from the capsule (commit the deletion, or push a commit that removes it) and the capsule runs again. Never add it back: the demo's parameters do not need it — see [Do not add an app-panel.json](#do-not-add-an-app-paneljson). |
| A parameter you set had no effect | Only values that differ from the default are sent — an untouched field is left out on purpose, so the capsule uses (and reports) its own default. The form's caption says how many parameters will travel. Otherwise check the report's **Report parameters** card: it lists what the run actually received and flags anything it had to skip (an unparseable date or number logs a warning and is ignored — it never fails the run). The same values are frozen onto the captured `Report — <ts>` asset as `app_parameters`. |
| Report renders blank in the app | Only happens if the report is not self-contained. Confirm the log's `wrote /results/report.html` line reports over 1 MB — plotly.js is inlined, so a small file means a bad build. |
| Polling seems to stop mid-run | Clicking anything mid-poll ends Streamlit's script run; the Code Ocean computation keeps going. Use the app's **🔁 Resume polling** button. |
| Environment build fails on the app capsule | Retry the build; if it persists, pin versions in the Environment Editor and rerun the sanity-check run before launching the workstation. |
| Git push into a capsule rejected: *uncommitted changes in the Code Ocean IDE* | Open that capsule's IDE, commit or discard the pending changes, then push again. |

### No GitHub access

If the deployment cannot reach github.com, **Clone from Git** will fail. Create empty
Python capsules instead and push the code in yourself:

```bash
git clone https://github.com/codeocean-nate/co-orchestrator-step1-excel-to-csv
cd co-orchestrator-step1-excel-to-csv
git remote add co https://YOUR-DEPLOYMENT.codeocean.com/capsule-<SLUG>.git
git push co HEAD:main      # username = your Code Ocean account email
                           # password = your API token
```

Every capsule exposes an internal git remote at
`https://YOUR-DEPLOYMENT.codeocean.com/capsule-<SLUG>.git`. Fresh capsules are seeded
with a root commit whose history is unrelated to the repo's, so if that push is
rejected, overlay the files on top of the capsule's branch instead of force-pushing:
check out the capsule's branch, copy the repo's files over it, commit, and push. Repeat
for each of the four repos.

## Customizing the demo

**Add a parameter to the report.** This is the headline of the demo, and it is worth
doing live. It touches two places, and neither of them is a Code Ocean config file:

1. **The capsule** — add an entry to `PARAM_SPECS` in the step-2 capsule's
   `code/make_report.py` (argument key, label, default) and use the value. `code/run`
   forwards `"$@"` and the script parses `--param_name=value` with `argparse`, so nothing
   else has to change; unknown arguments are ignored, so the capsule stays runnable
   whatever the app sends.
2. **The app** — add the same parameter to the app's own parameter list so a widget
   appears in the **⚙️ Report parameters** form and the value is sent with the run.

The value you type in the GUI then lands in `app_parameters` on the captured
`Report — <ts>` asset, alongside the run that used it. Do **not** create a
`.codeocean/app-panel.json` for the new parameter — that file does not add a panel, it
makes the capsule unrunnable ([why](#do-not-add-an-app-paneljson)).

Two documented limits apply to free-text values:

- **No single quotes.** Code Ocean's parameter escaping does not support them, so the app
  substitutes a typographic apostrophe (`’`) before sending the value and tells you it
  did. Double quotes, `$`, spaces and other unicode travel fine.
- **Keep values short.** A multi-MB parameter value has degraded a deployment before, so
  the app caps free text at 2,000 characters.

The four GitHub repos are the source of truth. To change the data, the report, or the
app, fork the repo you want to change and clone your fork instead — the setup is
otherwise identical. To swap in your own input data, replace `sales_data.xlsx` in the
data seed repo (or create the input data asset from your own file); step 1 converts any
workbook, and step 2 falls back to a generic data profile for any schema it does not
recognize.
