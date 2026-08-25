# Aqua prompt — build the Code Ocean orchestration demo

Paste the messages below into Aqua's chat (nav bar → Aqua) **one at a time, in order,
waiting for each to finish** before sending the next. This mirrors the staged-prompt
pattern that works best with Aqua and keeps each task inside its request timeout.
Everything runs in one chat session — Aqua keeps the context (capsule IDs, asset IDs)
between messages.

Prefer a click-by-click guide, or don't have Aqua? See [SETUP.md](SETUP.md), which
covers the same build as manual UI steps.

## Before you start

Two things in this file are placeholders you supply:

| Placeholder | Replace with |
|---|---|
| `https://YOUR-DEPLOYMENT.codeocean.com` | the URL you use to reach Code Ocean, no trailing slash (appears once, in Task 4) |
| `<capsule ID from Task N>` / `<… asset ID from Task 1>` | the UUIDs Aqua reports back to you as it goes |

Tasks 1–3 and 5 are copy-paste-as-is. Only Task 4 needs editing before you send it.

Check these prerequisites first:

| Requirement | Why |
|---|---|
| Aqua available on the deployment, **v4.3+** | Tasks 1–4 create capsules from a git URL |
| **v4.7+** for Task 5 | launching a cloud workstation from chat; on older versions launch Streamlit yourself from the Reproducibility Panel |
| Your account can create capsules and data assets | every task |
| The deployment can reach **github.com** | Aqua clones the four public repos |
| A Code Ocean API token you can copy | the one manual step at the end |

If the deployment has no outbound access to github.com, the clone step will fail — use
the [manual / import fallback](#fallbacks) instead of fighting it.

**Why this works:** Aqua can create capsules from a git URL, edit code, set the
environment (starter env + pip packages + env vars), start reproducible runs, create
data assets from results, attach assets to capsules, and launch cloud workstations. It
cannot (and must not be asked to) handle secrets, create releases, or build pipeline
DAGs — the one manual step for you is attaching your API token at the end.

**If Aqua asks a clarifying question**, answer it and let it continue — that's normal.
If a step fails twice, tell it what the error says; it's good at run→diagnose→fix loops
when you let it retry.

---

## Message 1 of 5 — context + input data asset

```
We're building a 4-capsule orchestration demo in this deployment. I'll send you one
task per message. General rules for the whole session: work autonomously and fix any
problems along the way (adjust pip packages or compute resources if a run fails);
after each task, report the IDs of everything you created (capsule IDs and data asset
IDs); do not create releases, do not build pipelines, and never touch secrets or
tokens.

Task 1 — create the demo's input data asset:
1. Create a new capsule named "Orchestrator Demo — Data Seed" by cloning this public
   git repository: https://github.com/codeocean-nate/co-orchestrator-data-seed
   Use a plain Python starter environment; it needs no extra packages (the run script
   only copies a bundled Excel file to /results).
2. Start a reproducible run and wait for it to complete. If it fails, fix it and rerun.
3. Create a data asset from that run's results, named exactly "Demo Sales Data (Excel)",
   with description "Seeded retail sales workbook (500 transactions + regional targets)
   — input for the orchestration demo" and tags: orchestrator-demo, input. Wait until
   the data asset is ready.
4. Report the data seed capsule ID and the "Demo Sales Data (Excel)" data asset ID.
```

## Message 2 of 5 — step 1 capsule (Excel → CSV)

```
Task 2 — create and verify the first processing capsule:
1. Create a new capsule named "Orchestrator Demo — Step 1: Excel to CSV" by cloning
   this public git repository:
   https://github.com/codeocean-nate/co-orchestrator-step1-excel-to-csv
2. Set up its environment: Python starter environment with pip packages pandas and
   openpyxl.
3. Attach the "Demo Sales Data (Excel)" data asset from Task 1 to this capsule.
4. Start a reproducible run and wait for it to complete. Verify from the run log that
   it wrote csv/sales_data__transactions.csv, csv/sales_data__regional_targets.csv,
   and manifest.json (the script prints a "wrote ..." line for each output). If the
   run fails, fix it and rerun.
5. After the run succeeds, detach the "Demo Sales Data (Excel)" data asset from this
   capsule again (keep the asset itself — do not delete it). The orchestrator app
   mounts it explicitly at run time, so it must not stay attached.
6. Report this capsule's ID and the computation ID of the successful run.
```

## Message 3 of 5 — step 2 capsule (CSV → HTML report)

```
Task 3 — create and verify the reporting capsule, rehearsing the full hand-off:
1. Create a new capsule named "Orchestrator Demo — Step 2: HTML Report" by cloning
   this public git repository:
   https://github.com/codeocean-nate/co-orchestrator-step2-html-report
2. Set up its environment: Python starter environment with pip packages pandas and
   plotly.
3. Create a data asset from the successful Task 2 run (the computation ID you
   reported in Task 2), named exactly "Processed CSV — build verification", with
   tags: orchestrator-demo, step1-output. Wait until it is ready. (This rehearses
   the capture step the orchestrator app performs at demo time.)
4. Attach that new data asset to this capsule, start a reproducible run, and wait for
   it to complete. Verify from the run log the "wrote /results/report.html" line and
   that the size it reports is over 1 MB (the report embeds plotly.js so it is fully
   self-contained). If the run fails, fix it and rerun.
5. After the run succeeds, detach the "Processed CSV — build verification" data asset
   from this capsule (keep the asset itself — do not delete it). At demo time the
   orchestrator app mounts a freshly captured asset instead, and a leftover attachment
   could collide with it.
6. Report this capsule's ID and the "Processed CSV — build verification" data asset ID.
```

## Message 4 of 5 — orchestrator app capsule (Streamlit)

*Before sending: replace `https://YOUR-DEPLOYMENT.codeocean.com` with your deployment's
real URL, and replace the three `<...>` placeholders with the actual IDs Aqua reported
in Tasks 1–3. You can leave the `<...>` IDs as-is — Aqua will substitute from its own
earlier reports — but pasting the real IDs is the more reliable option. Do set
`CODEOCEAN_DOMAIN`: it is simply the URL you sign in to, and setting it explicitly
avoids relying on auto-detection, which is only a best-effort convenience.*

```
Task 4 — create the Streamlit orchestrator app capsule:
1. Create a new capsule named "Orchestrator Demo — App" by cloning this public git
   repository: https://github.com/codeocean-nate/co-orchestrator-app
   Important: the Streamlit entry point must remain at code/streamlit_app.py — do not
   rename or move it.
2. Set up its environment: Python starter environment with pip packages streamlit,
   codeocean, pandas, and requests.
3. Add these environment variables to the capsule environment, using the real IDs
   from the earlier tasks:
   - CODEOCEAN_DOMAIN = https://YOUR-DEPLOYMENT.codeocean.com
   - STEP1_CAPSULE_ID = <capsule ID from Task 2>
   - STEP2_CAPSULE_ID = <capsule ID from Task 3>
   - INPUT_DATA_ASSET_ID = <"Demo Sales Data (Excel)" asset ID from Task 1>
   Do NOT add any API token or secret — I will attach that myself.
4. Start a reproducible run and wait for it to complete — for this capsule the run
   only sanity-checks that all four packages import and the app code compiles, then
   exits. If it fails, fix the environment and rerun.
5. Report this capsule's ID and the environment variables you set with their values.
```

## Message 5 of 5 — launch

```
Task 5 — launch the app: start the Streamlit cloud workstation on the
"Orchestrator Demo — App" capsule from Task 4 so I can open the app in my browser.
Confirm it is serving. Then summarize the whole build: every capsule and data asset
you created across Tasks 1-5, with names and IDs, and what remains manual for me
(attaching my API token as a secret).
```

---

## Your manual steps (Aqua refuses secrets — by design)

1. **Attach your API token to the app capsule**: Account → Roles and Secrets → Add
   secret → **Custom Key** with your API token as the value; then app capsule →
   settings gear → **Credentials** tab → add the secret. It surfaces as `CUSTOM_KEY`,
   which the app checks (after `CODEOCEAN_TOKEN` and `API_SECRET`). If your version
   offers Secrets Actions → "Set Environment Variable Names", name it
   `CODEOCEAN_TOKEN`. (Exact menu names differ slightly between versions — the secret
   just needs to end up attached to the capsule as a credential.)
   **Important:** the workstation Aqua launched in Task 5 started *before* the secret
   existed, so it won't have the env var — shut that workstation down and relaunch
   Streamlit from the Reproducibility Panel after attaching the secret.
   - *No-setup alternative for a quick demo:* skip the secret and paste the token into
     the app sidebar's password field in the already-running workstation — it's
     session-only and never displayed.
2. Open the Streamlit cloud workstation and run the two-click flow:
   **▶ Start processing** → "✅ Data ready" + CSV preview → **📊 Generate report** →
   the HTML report renders inline.

## No Aqua on this deployment?

Build exactly the same thing by hand — same repos, same order, same verification:

1. **➕ New Capsule → Clone from Git** for each of the four public repos below, naming
   the capsules as in Tasks 1–4.
2. **Environments** (Environment Editor → pip3 packages): data seed — none; step 1 —
   `pandas`, `openpyxl`; step 2 — `pandas`, `plotly`; app — `streamlit`, `codeocean`,
   `pandas`, `requests`.
3. Run the data seed capsule, capture its results as the **Demo Sales Data (Excel)**
   data asset, then verify step 1 and step 2 with the same attach → run → check the
   log → detach sequence described in Tasks 2 and 3.
4. Set the app capsule's environment variables (Task 4), attach the token secret, and
   launch the Streamlit cloud workstation from the Reproducibility Panel.

[SETUP.md](SETUP.md) walks through all of that click by click.

## Fallbacks

- **Aqua can't clone from the git URL** (tool not offered / no egress to github.com):
  have it create an empty Python capsule with the same name instead. Then get the code
  in yourself: clone the public repo to your machine and push it into the capsule's
  internal git remote at `https://YOUR-DEPLOYMENT.codeocean.com/capsule-<SLUG>.git`
  (username = your Code Ocean account email, password = your API token; `<SLUG>` is the
  number in the capsule's URL). Fresh capsules already have a root commit, so overlay
  your files on top of the capsule's branch rather than force-pushing. If a push is
  rejected with *"uncommitted changes in the Code Ocean IDE"*, commit or discard those
  changes in that capsule's IDE and retry.
- **Streamlit CW won't launch via Aqua** (that tool is newer and lightly tested):
  launch it manually — app capsule → Reproducibility Panel → Streamlit icon.
- **A capsule run fails on environment build**: tell Aqua the error text and ask it to
  fix the environment and rerun; it can also bump compute resources if a run is
  starved.

## Updating the code later

The four GitHub repos are the source of truth:

| Capsule | Repo |
|---|---|
| Data seed | https://github.com/codeocean-nate/co-orchestrator-data-seed |
| Step 1: Excel → CSV | https://github.com/codeocean-nate/co-orchestrator-step1-excel-to-csv |
| Step 2: HTML report | https://github.com/codeocean-nate/co-orchestrator-step2-html-report |
| Orchestrator app | https://github.com/codeocean-nate/co-orchestrator-app |

To customize the demo, fork the repo you want to change and clone your fork instead.
After an upstream update, either ask Aqua to apply the same change in the capsule, or
push the new code straight to the capsule's git remote (commit any pending IDE changes
in the capsule first).
