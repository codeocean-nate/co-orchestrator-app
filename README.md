# Code Ocean Orchestrator — Streamlit app

The UI of the Code Ocean orchestration demo: a Streamlit app, hosted in a Code Ocean
capsule and served by the **Streamlit Cloud Workstation**, that drives other capsules
through the Code Ocean API:

1. **▶ Start processing** — runs the [Excel→CSV capsule](https://github.com/codeocean-nate/co-orchestrator-step1-excel-to-csv)
   with the Excel data asset mounted and polls the computation live.
2. Captures its `/results` as a **new data asset** ("✅ Data ready") and previews the CSV.
3. **📊 Generate report** — runs the [reporting capsule](https://github.com/codeocean-nate/co-orchestrator-step2-html-report)
   with that asset mounted, downloads `report.html`, renders it inline.

An event log records every API interaction and an "Under the hood" expander shows the
exact `codeocean` SDK call behind each step with the session's real IDs.

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
