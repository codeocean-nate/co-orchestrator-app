"""Code Ocean Orchestrator — Streamlit demo UI.

Demonstrates chaining two Code Ocean capsules through the public SDK:

    1. Run the "Excel -> CSV" capsule with an Excel data asset mounted.
    2. Capture that computation's /results as a new (result) data asset.
    3. Run the "CSV -> HTML report" capsule with the new asset mounted.
    4. Download and display the self-contained HTML report.

This file is named ``streamlit_app.py`` because Code Ocean's Streamlit Cloud
Workstation looks for exactly ``/code/streamlit_app.py``.

Streamlit rerun model notes (important for maintainers):
- Every widget interaction reruns this script top-to-bottom.
- All long-running polling happens INSIDE the button-click branches, within
  st.status blocks, and finishes with st.rerun().
- Everything a completed stage produced (computation ids, the data asset id,
  the CSV preview dataframe, the report bytes) lives in st.session_state so
  the page can be fully re-rendered from state after any rerun.
- If the user interacts with the page mid-poll, Streamlit stops that script
  run (the Code Ocean computation keeps running server-side). The stored
  computation/asset ids let the "Resume polling" branch pick up where the
  interrupted run left off.
"""

import io
import os
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from co_api import Orchestrator, computation_succeeded

# ---------------------------------------------------------------------------
# Page setup + session state
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Code Ocean Orchestrator", layout="wide", page_icon="🌊")

STAGE_ORDER = [
    "idle",
    "step1_running",
    "step1_done",
    "asset_creating",
    "asset_ready",
    "step2_running",
    "report_ready",
]

IN_FLIGHT_STAGES = ("step1_running", "asset_creating", "step2_running")

STATE_DEFAULTS = {
    "stage": "idle",
    "event_log": [],
    "snippet": None,
    "snippet_title": None,
    "step1_comp_id": None,
    "step2_comp_id": None,
    "asset_id": None,
    "asset_name": None,
    "csv_df": None,
    "csv_name": None,
    "report_html": None,
    "last_error": None,
}

for _key, _default in STATE_DEFAULTS.items():
    if _key not in st.session_state:
        st.session_state[_key] = list(_default) if isinstance(_default, list) else _default


def stage_idx(stage=None):
    return STAGE_ORDER.index(stage if stage is not None else st.session_state.stage)


def now_utc():
    return datetime.now(timezone.utc)


def log_event(message):
    """Append a timestamped line to the demo's event log."""
    st.session_state.event_log.append("%s UTC — %s" % (now_utc().strftime("%H:%M:%S"), message))


def set_snippet(title, code):
    """Remember the SDK snippet equivalent to the last action ('Under the hood')."""
    st.session_state.snippet_title = title
    st.session_state.snippet = code


# ---------------------------------------------------------------------------
# Configuration (priority: env var -> sidebar input; persisted in session state)
# ---------------------------------------------------------------------------

def _sidebar_text(label, state_key, default="", secret=False, help_text=None):
    if state_key not in st.session_state:
        st.session_state[state_key] = default
    return st.sidebar.text_input(
        label,
        key=state_key,
        type="password" if secret else "default",
        help=help_text,
    ).strip()


def resolve_config(label, env_vars, state_key, default="", secret=False, help_text=None):
    """Env var wins; otherwise fall back to a sidebar input kept in session state."""
    for env_var in env_vars:
        value = os.environ.get(env_var, "").strip()
        if value:
            if secret:
                st.sidebar.caption("🔑 %s — set from `$%s` ✓" % (label, env_var))
            else:
                st.sidebar.text_input(label, value=value, disabled=True,
                                      help="Set from $%s" % env_var, key="env_" + state_key)
            return value
    return _sidebar_text(label, state_key, default=default, secret=secret, help_text=help_text)


st.sidebar.markdown("## 🌊 Configuration")
st.sidebar.caption(
    "Environment variables take priority; anything missing can be entered "
    "here and persists for this browser session."
)

domain = resolve_config(
    "Code Ocean domain", ["CODEOCEAN_DOMAIN"], "cfg_domain",
    default="https://acmecorp-demo.codeocean.com",
)
token = resolve_config(
    "API token", ["CODEOCEAN_TOKEN", "API_SECRET", "CUSTOM_KEY"], "cfg_token",
    secret=True, help_text="Personal access token. Never displayed or logged.",
)
step1_capsule_id = resolve_config(
    "Step 1 capsule ID (Excel → CSV)", ["STEP1_CAPSULE_ID"], "cfg_step1",
    help_text="UUID of the Excel-to-CSV capsule",
)
step2_capsule_id = resolve_config(
    "Step 2 capsule ID (CSV → HTML)", ["STEP2_CAPSULE_ID"], "cfg_step2",
    help_text="UUID of the HTML-report capsule",
)
input_asset_id = resolve_config(
    "Input data asset ID (Excel)", ["INPUT_DATA_ASSET_ID"], "cfg_asset",
    help_text="UUID of the Excel dataset to process",
)

config_ok = all([domain, token, step1_capsule_id, step2_capsule_id, input_asset_id])


def get_client():
    return Orchestrator(domain=domain, token=token)


# Convenience: look up capsule UUIDs by name via the search API.
with st.sidebar.expander("🔎 Find a capsule ID by name"):
    st.caption("Uses `client.capsules.search_capsules` — handy for grabbing UUIDs.")
    lookup_query = st.text_input("Capsule name contains…", key="cfg_lookup")
    if st.button("Search capsules", use_container_width=True, key="btn_lookup",
                 disabled=not (domain and token and lookup_query.strip())):
        try:
            matches = get_client().search_capsules(lookup_query.strip(), limit=8)
            if not matches:
                st.info("No capsules matched.")
            for cap in matches:
                st.code("%s\n%s" % (cap.name, cap.id), language="text")
        except Exception as exc:  # noqa: BLE001
            st.error("Search failed: %s" % exc)

st.sidebar.divider()
if st.sidebar.button("♻️ Reset demo", use_container_width=True):
    # Clear pipeline state but keep the configuration inputs. Note: resetting
    # while a computation is mid-flight abandons *tracking* only — the
    # computation itself keeps running (and finishes) in Code Ocean.
    for key, default in STATE_DEFAULTS.items():
        st.session_state[key] = list(default) if isinstance(default, list) else default
    st.rerun()
st.sidebar.caption("Resets pipeline progress; configuration is kept.")


# ---------------------------------------------------------------------------
# Header + pipeline progress strip (re-renderable placeholder)
# ---------------------------------------------------------------------------

st.title("🌊 Code Ocean Orchestrator")
st.caption(
    "Chaining capsules through the Code Ocean SDK: run a transform capsule, "
    "capture its results as a versioned data asset, feed that asset into a "
    "reporting capsule, and view the result — full provenance at every step."
)

_strip = st.empty()


def render_strip():
    """Pipeline progress strip: ✅ done · 🔄 in progress · ⬜ pending."""
    idx = stage_idx()
    steps = [
        ("Transform", "Excel → CSV capsule run",
         idx >= stage_idx("step1_done"), st.session_state.stage == "step1_running"),
        ("Capture asset", "/results → data asset",
         idx >= stage_idx("asset_ready"), st.session_state.stage == "asset_creating"),
        ("Report", "CSV → HTML capsule run",
         idx >= stage_idx("report_ready"), st.session_state.stage == "step2_running"),
        ("View", "Interactive HTML report",
         st.session_state.stage == "report_ready", False),
    ]
    with _strip.container():
        cols = st.columns(4)
        for col, (name, subtitle, done, active) in zip(cols, steps):
            icon = "✅" if done else ("🔄" if active else "⬜")
            with col.container(border=True):
                st.markdown("#### %s %s" % (icon, name))
                st.caption(subtitle)


render_strip()

# ---------------------------------------------------------------------------
# Action buttons (rendered every run; work happens inside the click branches)
# ---------------------------------------------------------------------------

if not config_ok:
    st.warning(
        "⚙️ Configuration incomplete — fill in the domain, API token, both "
        "capsule IDs, and the input data asset ID in the sidebar to start the demo."
    )

btn_col1, btn_col2, _ = st.columns([1, 1, 2])
start_clicked = btn_col1.button(
    "▶ Start processing",
    type="primary",
    use_container_width=True,
    disabled=(not config_ok) or st.session_state.stage != "idle",
)
report_clicked = btn_col2.button(
    "📊 Generate report",
    use_container_width=True,
    disabled=(not config_ok) or st.session_state.stage != "asset_ready",
)


def _fail(message, revert_stage):
    """Record an error, rewind the state machine, and rerun the page."""
    st.session_state.last_error = message
    st.session_state.stage = revert_stage
    log_event("ERROR: " + message)
    st.rerun()


def _comp_progress_writer(label):
    def on_update(comp):
        st.write("`%s` — state: **%s**" % (comp.id, comp.state))
        log_event("%s: computation %s → %s" % (label, comp.id, comp.state))
    return on_update


# ---------------------------------------------------------------------------
# Pipeline stages as resumable functions. Each is called from a click branch
# (fresh start) or from the Resume branch (picking up stored ids after an
# interrupted poll). Each ends by advancing the state machine + st.rerun(),
# or by _fail() which rewinds it — so control never falls through.
# ---------------------------------------------------------------------------

def continue_step1(orch, comp_id, resuming=False):
    """Poll the step-1 computation to completion, then capture + preview."""
    st.session_state.stage = "step1_running"
    render_strip()
    label = ("Resuming step 1: converting Excel → CSV…" if resuming
             else "Step 1: converting Excel → CSV…")
    step1_error = None
    comp = None
    try:
        with st.status(label, expanded=True) as status:
            comp = orch.wait_for_computation(
                comp_id, poll_s=5, timeout_s=1800,
                on_update=_comp_progress_writer("step 1"),
            )
            if computation_succeeded(comp):
                status.update(label="Step 1 complete — Excel converted to CSV ✅",
                              state="complete")
            else:
                status.update(label="Step 1 failed", state="error")
                step1_error = (
                    "Step 1 computation `%s` ended with state `%s` (exit code: %s). "
                    "Check the capsule's run log in Code Ocean."
                    % (comp.id, comp.state, comp.exit_code)
                )
    except TimeoutError as exc:
        step1_error = "Step 1 timed out: %s" % exc
    except Exception as exc:  # noqa: BLE001 — never surface raw tracebacks
        step1_error = "Step 1 failed (computation: %s): %s" % (comp_id, exc)
    if step1_error:
        _fail(step1_error, revert_stage="idle")

    st.session_state.stage = "step1_done"
    render_strip()
    capture_and_preview(orch, comp_id)


def capture_and_preview(orch, comp_id, existing_asset_id=None):
    """Turn step-1 /results into a data asset (or resume waiting on one),
    wait until ready, then fetch the CSV preview and advance to asset_ready."""
    st.session_state.stage = "asset_creating"
    render_strip()
    capture_error = None
    asset = None
    try:
        with st.status("Capturing results as a data asset…", expanded=True) as status:
            if existing_asset_id:
                asset_id = existing_asset_id
                asset_name = st.session_state.asset_name or "Processed CSV"
                st.write("Resuming wait on data asset `%s`" % asset_id)
            else:
                asset_name = "Processed CSV — %s" % now_utc().strftime("%Y-%m-%d %H:%M UTC")
                asset = orch.capture_result_asset(
                    comp_id,
                    name=asset_name,
                    mount="processed-csv",
                    tags=["orchestrator-demo", "step1-output"],
                )
                asset_id = asset.id
                # Store immediately so an interrupted poll can resume on this asset.
                st.session_state.asset_id = asset_id
                st.session_state.asset_name = asset_name
                st.write("Data asset `%s` created (state: **%s**) — waiting until ready"
                         % (asset_id, asset.state))
                log_event("create_data_asset from computation %s → asset %s"
                          % (comp_id, asset_id))
                set_snippet(
                    "Capture — computation results → data asset",
                    (
                        "from codeocean.data_asset import (\n"
                        "    DataAssetParams, Source, ComputationSource,\n"
                        ")\n\n"
                        "asset = client.data_assets.create_data_asset(DataAssetParams(\n"
                        '    name="%s",\n'
                        '    description="Created by the orchestrator demo",\n'
                        '    mount="processed-csv",\n'
                        '    tags=["orchestrator-demo", "step1-output"],\n'
                        '    source=Source(computation=ComputationSource(id="%s")),\n'
                        "))\n"
                        "# asset.id == \"%s\"\n"
                        "while asset.state not in (\"ready\", \"failed\"):\n"
                        "    time.sleep(5)\n"
                        "    asset = client.data_assets.get_data_asset(asset.id)\n"
                    ) % (asset_name, comp_id, asset_id),
                )

            def _asset_update(da):
                st.write("`%s` — state: **%s**" % (da.id, da.state))
                log_event("data asset %s → %s" % (da.id, da.state))

            asset = orch.wait_for_data_asset(asset_id, poll_s=5, timeout_s=1800,
                                             on_update=_asset_update)
            if asset.state == "ready":
                status.update(label="Data asset ready ✅", state="complete")
            else:
                status.update(label="Data asset creation failed", state="error")
                capture_error = "Data asset `%s` ended in state `%s`%s" % (
                    asset.id, asset.state,
                    " — %s" % asset.failure_reason if asset.failure_reason else "")
    except TimeoutError as exc:
        capture_error = "Data asset creation timed out: %s" % exc
    except Exception as exc:  # noqa: BLE001
        capture_error = "Data asset creation failed (computation: %s): %s" % (comp_id, exc)
    if capture_error:
        _fail(capture_error, revert_stage="idle")

    st.session_state.asset_id = asset.id
    st.session_state.asset_name = st.session_state.asset_name or asset.name

    # ---- CSV preview: download the primary CSV via a presigned URL (non-fatal).
    try:
        with st.status("Downloading CSV preview…", expanded=False) as status:
            result_files = orch.list_results(comp_id)
            csv_files = [f for f in result_files if f.path.lower().endswith(".csv")]
            if csv_files:
                primary = max(csv_files, key=lambda f: f.size or 0)
                st.write("Fetching `%s` (%s bytes) via presigned URL" % (primary.path, primary.size))
                csv_bytes = orch.download_result(comp_id, primary.path)
                st.session_state.csv_df = pd.read_csv(io.BytesIO(csv_bytes))
                st.session_state.csv_name = primary.path
                log_event("downloaded CSV preview %s (%d rows)"
                          % (primary.path, len(st.session_state.csv_df)))
                status.update(label="CSV preview ready", state="complete")
            else:
                log_event("no CSV files found in step 1 results — skipping preview")
                status.update(label="No CSV found to preview", state="error")
    except Exception as exc:  # noqa: BLE001 — preview is nice-to-have, not fatal
        log_event("CSV preview failed (non-fatal): %s" % exc)

    st.session_state.stage = "asset_ready"
    log_event("stage → asset_ready (data asset %s)" % st.session_state.asset_id)
    st.rerun()


def continue_step2(orch, comp_id, resuming=False):
    """Poll the step-2 computation, then download report.html and finish."""
    st.session_state.stage = "step2_running"
    render_strip()
    label = ("Resuming step 2: generating HTML report…" if resuming
             else "Step 2: generating HTML report…")
    step2_error = None
    try:
        with st.status(label, expanded=True) as status:
            comp2 = orch.wait_for_computation(
                comp_id, poll_s=5, timeout_s=1800,
                on_update=_comp_progress_writer("step 2"),
            )
            if not computation_succeeded(comp2):
                status.update(label="Step 2 failed", state="error")
                step2_error = (
                    "Step 2 computation `%s` ended with state `%s` (exit code: %s)."
                    % (comp2.id, comp2.state, comp2.exit_code)
                )
            else:
                st.write("Locating `report.html` in the computation results…")
                result_files = orch.list_results(comp2.id)
                report = next((f for f in result_files if f.name == "report.html"), None)
                if report is None:
                    report = next((f for f in result_files
                                   if f.path.lower().endswith(".html")), None)
                if report is None:
                    status.update(label="No report found", state="error")
                    step2_error = ("Step 2 computation `%s` completed but produced no "
                                   ".html file in /results." % comp2.id)
                else:
                    st.write("Downloading `%s` (%s bytes)…" % (report.path, report.size))
                    report_bytes = orch.download_result(comp2.id, report.path)
                    st.session_state.report_html = report_bytes
                    log_event("downloaded %s (%d bytes)" % (report.path, len(report_bytes)))
                    status.update(label="Report ready ✅", state="complete")
    except TimeoutError as exc:
        step2_error = "Step 2 timed out: %s" % exc
    except Exception as exc:  # noqa: BLE001
        step2_error = "Step 2 failed (computation: %s): %s" % (comp_id, exc)
    if step2_error:
        _fail(step2_error, revert_stage="asset_ready")

    st.session_state.stage = "report_ready"
    log_event("stage → report_ready")
    st.rerun()


# ------------------------------------------------ Step 1 + capture (one click)

if start_clicked:
    st.session_state.last_error = None
    orch = get_client()

    st.session_state.stage = "step1_running"
    render_strip()
    launch_error = None
    comp = None
    try:
        with st.status("Launching step 1 capsule…", expanded=True):
            st.write("Launching capsule `%s` with data asset `%s` mounted at "
                     "`/data/excel-input`" % (step1_capsule_id, input_asset_id))
            comp = orch.run_capsule(step1_capsule_id,
                                    data_asset_id=input_asset_id,
                                    mount="excel-input")
            st.session_state.step1_comp_id = comp.id
            log_event("step 1: run_capsule(%s) → computation %s" % (step1_capsule_id, comp.id))
            set_snippet(
                "Step 1 — run a capsule with a data asset mounted",
                (
                    "from codeocean import CodeOcean\n"
                    "from codeocean.computation import RunParams, DataAssetsRunParam\n\n"
                    'client = CodeOcean(domain="%s", token=API_TOKEN)\n\n'
                    "comp = client.computations.run_capsule(RunParams(\n"
                    '    capsule_id="%s",\n'
                    "    data_assets=[DataAssetsRunParam(\n"
                    '        id="%s",\n'
                    '        mount="excel-input",   # appears at /data/excel-input/\n'
                    "    )],\n"
                    "))\n"
                    "# comp.id == \"%s\"\n"
                    "while comp.state not in (\"completed\", \"failed\"):\n"
                    "    time.sleep(5)\n"
                    "    comp = client.computations.get_computation(comp.id)\n"
                ) % (domain, step1_capsule_id, input_asset_id, comp.id),
            )
    except Exception as exc:  # noqa: BLE001
        launch_error = "Could not start step 1: %s" % exc
    if launch_error:
        _fail(launch_error, revert_stage="idle")

    continue_step1(orch, comp.id)


# ------------------------------------------------------- Step 2 (one click)

if report_clicked:
    st.session_state.last_error = None
    orch = get_client()

    st.session_state.stage = "step2_running"
    render_strip()
    launch_error = None
    comp2 = None
    try:
        with st.status("Launching step 2 capsule…", expanded=True):
            st.write("Launching capsule `%s` with data asset `%s` mounted at "
                     "`/data/processed-csv`" % (step2_capsule_id, st.session_state.asset_id))
            comp2 = orch.run_capsule(step2_capsule_id,
                                     data_asset_id=st.session_state.asset_id,
                                     mount="processed-csv")
            st.session_state.step2_comp_id = comp2.id
            log_event("step 2: run_capsule(%s) → computation %s" % (step2_capsule_id, comp2.id))
            set_snippet(
                "Step 2 — run the report capsule and download its output",
                (
                    "comp = client.computations.run_capsule(RunParams(\n"
                    '    capsule_id="%s",\n'
                    "    data_assets=[DataAssetsRunParam(\n"
                    '        id="%s",         # the asset captured from step 1\n'
                    '        mount="processed-csv",\n'
                    "    )],\n"
                    "))\n"
                    "# comp.id == \"%s\" ... poll until completed, then:\n"
                    "items = client.computations.list_computation_results(comp.id).items\n"
                    "urls = client.computations.get_result_file_urls(comp.id, path=\"report.html\")\n"
                    "html = requests.get(urls.download_url).content\n"
                ) % (step2_capsule_id, st.session_state.asset_id, comp2.id),
            )
    except Exception as exc:  # noqa: BLE001
        launch_error = "Could not start step 2: %s" % exc
    if launch_error:
        _fail(launch_error, revert_stage="asset_ready")

    continue_step2(orch, comp2.id)


# ------------------------------------------- Resume after an interrupted poll
# Reached only when no button was clicked this run (each click branch ends in
# st.rerun or _fail). If the stage says work is in flight, a previous script
# run was interrupted mid-poll; offer to resume from the stored ids.

if st.session_state.stage in IN_FLIGHT_STAGES:
    st.warning(
        "⏸️ Polling was interrupted mid-run (interacting with the page stops the "
        "script; the Code Ocean computation itself keeps running server-side). "
        "Resume to pick up where it left off."
    )
    if st.button("🔁 Resume polling", type="primary", disabled=not config_ok):
        orch = get_client()
        stage = st.session_state.stage
        if stage == "step1_running":
            if st.session_state.step1_comp_id:
                continue_step1(orch, st.session_state.step1_comp_id, resuming=True)
            else:  # interrupted before the run was even created
                st.session_state.stage = "idle"
                st.rerun()
        elif stage == "asset_creating":
            capture_and_preview(orch, st.session_state.step1_comp_id,
                                existing_asset_id=st.session_state.asset_id)
        elif stage == "step2_running":
            if st.session_state.step2_comp_id:
                continue_step2(orch, st.session_state.step2_comp_id, resuming=True)
            else:
                st.session_state.stage = "asset_ready"
                st.rerun()


# ---------------------------------------------------------------------------
# Stage rendering — everything below draws purely from st.session_state,
# so the page stays complete after any rerun.
# ---------------------------------------------------------------------------

if st.session_state.last_error:
    st.error("❌ " + st.session_state.last_error)

if stage_idx() >= stage_idx("asset_ready") and st.session_state.asset_id:
    st.success("✅ Data ready — data asset `%s`  (*%s*)"
               % (st.session_state.asset_id, st.session_state.asset_name or ""))

if stage_idx() >= stage_idx("asset_ready") and st.session_state.csv_df is not None:
    st.subheader("Processed CSV preview")
    df = st.session_state.csv_df
    st.dataframe(df.head(50), use_container_width=True)
    st.caption("`%s` — %s rows × %s columns (showing first 50). "
               "This file now lives in data asset `%s`."
               % (st.session_state.csv_name, format(len(df), ","),
                  len(df.columns), st.session_state.asset_id))

if st.session_state.stage == "asset_ready":
    st.info("👆 The processed data is captured as a versioned data asset. "
            "Click **📊 Generate report** to feed it into the reporting capsule.")

if st.session_state.stage == "report_ready" and st.session_state.report_html:
    st.subheader("📈 Generated report")
    html_str = st.session_state.report_html.decode("utf-8", errors="replace")
    components.html(html_str, height=900, scrolling=True)
    st.download_button(
        "Download report.html",
        data=st.session_state.report_html,
        file_name="report.html",
        mime="text/html",
    )

# ---------------------------------------------------------------------------
# Event log + under-the-hood snippet
# ---------------------------------------------------------------------------

st.divider()

with st.expander("📜 Event log (%d events)" % len(st.session_state.event_log)):
    if st.session_state.event_log:
        st.code("\n".join(st.session_state.event_log), language="text")
    else:
        st.caption("No events yet — start the pipeline to see every API interaction here.")

with st.expander("🔍 Under the hood — the SDK call behind the last action"):
    if st.session_state.snippet:
        st.markdown("**%s**" % st.session_state.snippet_title)
        st.code(st.session_state.snippet, language="python")
        st.caption("Real IDs from this session; `API_TOKEN` stands in for your token.")
    else:
        st.caption("Run a step to see the equivalent `codeocean` SDK code with real IDs.")
