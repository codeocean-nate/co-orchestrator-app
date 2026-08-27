# Code Ocean Orchestrator — Streamlit app

The UI of the Code Ocean orchestration demo: a Streamlit app, hosted in a Code Ocean
capsule and served by the **Streamlit Cloud Workstation**, that drives other capsules
through the Code Ocean API:

1. **▶ Start processing** — runs the [Excel→CSV capsule](https://github.com/codeocean-nate/co-orchestrator-step1-excel-to-csv)
   with the Excel data asset mounted and polls the computation live.
2. Captures its `/results` as a **new data asset** ("✅ Data ready") and previews the CSV.
3. **⚙️ Set report parameters** — the human-in-the-loop step. The app renders the form
   for the [reporting capsule](https://github.com/codeocean-nate/co-orchestrator-step2-html-report):
   title, region and category filters, a date window, minimum revenue, top-N products,
   analyst notes.
4. **📊 Generate report** — runs the reporting capsule with that asset mounted **and your
   parameters attached to the run**, downloads `report.html`, renders it inline.
5. Captures the report as a **result data asset** too (`Report — <ts>`), so the values you
   chose are frozen onto it as `app_parameters` and the human decision becomes part of the
   recorded provenance, next to the data it came from.

An event log records every API interaction and an "Under the hood" expander shows the
exact `codeocean` SDK call behind each step with the session's real IDs.

## Report parameters

The app defines the report's parameter list itself and renders one widget per parameter.
Your choices travel to the capsule as *named run parameters* — each one reaching the
capsule's `code/run` as a single `--param_name=value` argv token, which `code/run`
forwards to `make_report.py` — and that is what gets them recorded on the computation and
carried onto the captured report asset as `app_parameters`. **The step-2 capsule needs no
App Panel for any of this**; named parameters reach a capsule that has none, which is
verified behaviour, not a workaround.

To add a parameter, add it here and to `PARAM_SPECS` in the report capsule's
`code/make_report.py`. Do **not** add a `.codeocean/app-panel.json` to the report capsule:
that file makes the capsule unrunnable (`403 corrupted object files`) — see the [report
capsule's README](https://github.com/codeocean-nate/co-orchestrator-step2-html-report#why-there-is-no-codeoceanapp-paneljson-here).

Two documented limits apply to free-text values: **no single quotes** (Code Ocean's
parameter escaping does not support them, so the app substitutes a typographic apostrophe
`’`), and **keep values short** (the app caps free text at 2,000 characters). Only values
that differ from the default are sent, so an untouched form runs the capsule exactly as it
runs with no parameters at all. See [SETUP.md](SETUP.md#troubleshooting).

## Setting it up on your deployment

**[SETUP.md](SETUP.md)** is the complete guide: the four capsules and their
environments, how to create the input data asset, configuration, the API token, and
launching the workstation. It works on any Code Ocean deployment.

If your deployment has Aqua, **[AQUA_PROMPT.md](AQUA_PROMPT.md)** builds the whole demo
from five pasted prompts.

## Layout

- `code/streamlit_app.py` — the app. Named exactly this because Code Ocean's Streamlit
  Cloud Workstation serves `/code/streamlit_app.py`.
- `code/co_api.py` — Streamlit-free SDK wrapper (run → poll → capture → download).
- `code/run` — Reproducible Run entrypoint: environment sanity check only (the CW serves
  the app; a completed run is needed to release the capsule as a No-Code App).

## Environment

Python 3.9+ with pip packages `streamlit`, `codeocean`, `pandas`, `requests`.

## Configuration

Env vars take priority; anything missing can be typed into the sidebar:

| Env var | Meaning |
|---|---|
| `STEP1_CAPSULE_ID` | UUID of the Excel→CSV capsule |
| `STEP2_CAPSULE_ID` | UUID of the report capsule |
| `INPUT_DATA_ASSET_ID` | UUID of the Excel data asset |
| `CODEOCEAN_TOKEN` / `API_SECRET` / `CUSTOM_KEY` | API token — attach it as a Code Ocean secret; never hardcode it |
| `CODEOCEAN_DOMAIN` | optional — the deployment URL is auto-detected; set this to override it |

See [SETUP.md](SETUP.md#configuration-reference) for where each value comes from.

## Run locally

```bash
pip install -r requirements.txt
export CODEOCEAN_TOKEN=...                                  # your token
export CODEOCEAN_DOMAIN=https://YOUR-DEPLOYMENT.codeocean.com   # required outside Code Ocean
streamlit run code/streamlit_app.py
```
