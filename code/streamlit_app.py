"""Code Ocean Orchestrator — Streamlit demo UI.

Demonstrates chaining two Code Ocean capsules through the public SDK:

    1. Run the "Excel -> CSV" capsule with an Excel data asset mounted.
    2. Capture that computation's /results as a new (result) data asset.
    3. Human in the loop: render a parameter form from this app's own spec
       (PARAM_SPECS below), which mirrors the report capsule's PARAM_SPECS.
    4. Run the "CSV -> HTML report" capsule with the new asset mounted and those
       choices sent as *named run parameters*.
    5. Capture that run's /results too, so the report becomes a lineage node
       carrying the parameters as ``app_parameters``.
    6. Download and display the self-contained HTML report.

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

from co_api import (
    Orchestrator,
    computation_succeeded,
    detect_domain_from_git,
    normalize_domain,
)

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

#: Session-state prefix for the step-2 parameter widgets. Namespaced so a
#: capsule parameter can never collide with a config key.
PARAM_KEY_PREFIX = "param_"

#: Hard cap on a free-text parameter value. Code Ocean keeps parameter values
#: on the computation record and freezes them onto the captured asset, and a
#: multi-MB value has degraded a deployment before — so the form refuses to be
#: the thing that sends one.
PARAM_MAX_CHARS = 2000

#: Code Ocean's parameter escaping backslash-escapes ``"`` and ``$`` and then
#: shell-quotes each value; **single quotes are unsupported**.
#: "Reviewed by the QC team's lead" is exactly the sort of thing a presenter
#: types, so the form quietly swaps in the typographic apostrophe — which is
#: immune to the quoting rule and reads better anyway — and says that it did.
STRAIGHT_APOSTROPHE = "'"
TYPOGRAPHIC_APOSTROPHE = "’"


class ParamSpec(object):
    """One run parameter this app offers for the report capsule.

    Deliberately defined HERE rather than read from the capsule. Code Ocean's
    App Panel cannot be shipped in a repo: a committed
    ``.codeocean/app-panel.json`` never creates a panel, and its mere presence
    makes every run of that capsule fail with
    ``403 corrupted object files``. Named run parameters, on the other hand,
    reach a capsule that has no panel at all — verified on a live deployment,
    where a panel-less capsule received ``--resample_interval=1D`` on argv and
    changed its output accordingly. So the form is app-side and the values
    travel as named parameters.

    Keep this list in step with ``PARAM_SPECS`` in the report capsule's
    ``code/make_report.py``: same ``param_name``s, same defaults.
    """

    def __init__(self, param_name, name, default_value, description,
                 value_options=None):
        self.param_name = param_name
        self.name = name
        self.default_value = default_value
        self.description = description
        self.value_options = value_options or []
        self.type = "list" if value_options else "text"


#: Mirrors capsule-step2-html-report/code/make_report.py PARAM_SPECS.
PARAM_SPECS = [
    ParamSpec("report_title", "Report title", "Sales Performance Report",
              "Heading printed at the top of the report."),
    ParamSpec("regions", "Regions", "All",
              "Comma-separated region names, or All. Matching is case-insensitive."),
    ParamSpec("categories", "Categories", "All",
              "Comma-separated categories, or All. Matching is case-insensitive."),
    ParamSpec("date_from", "Start date", "",
              "YYYY-MM-DD. Blank means start at the earliest row."),
    ParamSpec("date_to", "End date", "",
              "YYYY-MM-DD. Blank means run to the latest row."),
    ParamSpec("min_revenue", "Minimum revenue per transaction", "0",
              "Numeric. 0 keeps every row."),
    ParamSpec("top_n", "Top N products", "10",
              "How many rows in the top-products table. The capsule accepts 1-1000; "
              "this form offers the common choices.",
              value_options=["5", "10", "15", "20", "25"]),
    ParamSpec("analyst_notes", "Analyst notes", "",
              "Free text shown on the report."),
]

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
    # --- mid-pipeline parameters + step-2 result capture ---
    # (none of these may start with PARAM_KEY_PREFIX — that namespace belongs
    # to the parameter widgets and is wiped on reset)
    "form_values": {},        # {param_name: value} currently shown in the form
    "step2_params": {},       # {param_name: value} actually sent to step 2
    "retry_warning": None,    # set when a parameterized run had to be retried
    "step2_retried": False,   # the one-shot unparameterized retry is spent
    "notes_logged": set(),    # notes already logged once (keeps the log clean)
    "report_asset_id": None,  # result data asset captured from step 2
    "report_asset_name": None,
    "capture_warning": None,  # set when the (non-fatal) capture failed
}


def _fresh(default):
    """A private copy of a STATE_DEFAULTS value (mutables must not be shared)."""
    if isinstance(default, list):
        return list(default)
    if isinstance(default, dict):
        return dict(default)
    if isinstance(default, set):
        return set(default)
    return default


for _key, _default in STATE_DEFAULTS.items():
    if _key not in st.session_state:
        st.session_state[_key] = _fresh(_default)


def stage_idx(stage=None):
    return STAGE_ORDER.index(stage if stage is not None else st.session_state.stage)


def now_utc():
    return datetime.now(timezone.utc)


def log_event(message):
    """Append a timestamped line to the demo's event log."""
    st.session_state.event_log.append("%s UTC — %s" % (now_utc().strftime("%H:%M:%S"), message))


def log_once(message):
    """log_event(), but only the first time this exact message shows up.

    Streamlit reruns the whole script on every keystroke, so anything logged
    from a render path (rather than from a click) would otherwise repeat
    forever. Cleared by "Reset demo" along with the rest of the state.
    """
    if message in st.session_state.notes_logged:
        return
    st.session_state.notes_logged.add(message)
    log_event(message)


def set_snippet(title, code):
    """Remember the SDK snippet equivalent to the last action ('Under the hood')."""
    st.session_state.snippet_title = title
    st.session_state.snippet = code


def _brief(exc, limit=220):
    """A one-line, truncated exception message (SDK errors embed a JSON dump)."""
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[:limit] + "…"


# ---------------------------------------------------------------------------
# Configuration (priority: env var -> sidebar input; persisted in session state)
# ---------------------------------------------------------------------------

DOMAIN_LABEL = "Code Ocean domain"
DOMAIN_PLACEHOLDER = "https://your-deployment.codeocean.com"
DOMAIN_HELP = (
    "Base URL of your Code Ocean deployment (the host you sign in to), e.g. "
    "https://your-deployment.codeocean.com. Set $CODEOCEAN_DOMAIN to skip this."
)


def _sidebar_text(label, state_key, default="", secret=False, help_text=None,
                  placeholder=None):
    if state_key not in st.session_state:
        st.session_state[state_key] = default
    return st.sidebar.text_input(
        label,
        key=state_key,
        type="password" if secret else "default",
        help=help_text,
        placeholder=placeholder,
    ).strip()


def resolve_config(label, env_vars, state_key, default="", secret=False, help_text=None,
                   placeholder=None):
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
    return _sidebar_text(label, state_key, default=default, secret=secret,
                         help_text=help_text, placeholder=placeholder)


@st.cache_data(show_spinner=False)
def autodetected_domain():
    """Probe the capsule's git remote once per session (cached; never raises)."""
    return detect_domain_from_git()


def resolve_domain():
    """Deployment-agnostic domain resolution, in priority order:

    1. ``$CODEOCEAN_DOMAIN`` — explicit, always wins.
    2. Auto-detection from the capsule's git remote (works out of the box
       inside any Code Ocean cloud workstation, on any deployment).
    3. A sidebar text input, empty by default — no customer-specific value is
       ever pre-filled.
    """
    env_value = normalize_domain(os.environ.get("CODEOCEAN_DOMAIN", ""))
    if env_value:
        st.sidebar.text_input(DOMAIN_LABEL, value=env_value, disabled=True,
                              help="Set from $CODEOCEAN_DOMAIN", key="env_cfg_domain")
        return env_value

    detected = normalize_domain(autodetected_domain())
    value = normalize_domain(_sidebar_text(
        DOMAIN_LABEL, "cfg_domain",
        default=detected,
        help_text=DOMAIN_HELP,
        placeholder=DOMAIN_PLACEHOLDER,
    ))
    if detected and value == detected:
        st.sidebar.caption("🔎 Domain auto-detected from this capsule")
    return value


st.sidebar.markdown("## 🌊 Configuration")
st.sidebar.caption(
    "Environment variables take priority; anything missing can be entered "
    "here and persists for this browser session."
)

domain = resolve_domain()
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
    if st.button("Search capsules", width="stretch", key="btn_lookup",
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
if st.sidebar.button("♻️ Reset demo", width="stretch"):
    # Clear pipeline state but keep the configuration inputs. Note: resetting
    # while a computation is mid-flight abandons *tracking* only — the
    # computation itself keeps running (and finishes) in Code Ocean.
    for key, default in STATE_DEFAULTS.items():
        st.session_state[key] = _fresh(default)
    # Also drop the App Panel widget values, so the parameter form comes back
    # showing the capsule's own defaults.
    for key in [k for k in st.session_state if str(k).startswith(PARAM_KEY_PREFIX)]:
        del st.session_state[key]
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

# ---------------------------------------------------------------------------
# Mid-pipeline parameters — the human-in-the-loop step between "data ready"
# and "generate report".
#
# The form is NOT hardcoded here. The app asks Code Ocean what the step-2
# capsule accepts (`client.capsules.get_capsule_app_panel`) and renders one
# widget per parameter the capsule declares in its committed
# `.codeocean/app-panel.json`. The values are then sent as *named run
# parameters*, which is what makes a GUI choice part of the provenance record:
# Code Ocean stores them on the computation and freezes them onto any data
# asset captured from that run as `app_parameters`.
# ---------------------------------------------------------------------------

# NOTE: this app deliberately does NOT read a capsule's App Panel. Panels
# cannot be shipped in a repo (a committed .codeocean/app-panel.json creates
# no panel and makes every run of that capsule fail with 403 corrupted object
# files), and named run parameters reach a panel-less capsule anyway. The
# parameter list therefore lives in PARAM_SPECS at the top of this file.


def sanitize_param_value(value):
    """Make a free-text value safe for Code Ocean's parameter escaping.

    The backend backslash-escapes `"` and `$` and then shell-quotes each value,
    and single quotes are unsupported — so a typed `'` becomes `’`. Everything
    else (spaces, punctuation, unicode) travels fine.
    """
    return str(value).replace(STRAIGHT_APOSTROPHE, TYPOGRAPHIC_APOSTROPHE)


def render_parameter_form():
    """Render the step-2 capsule's parameters; return what to actually send.

    The returned dict holds only the parameters whose value **differs from the
    capsule's own default**. Sending an untouched default would be a lie in the
    provenance record: the capsule (and its report) distinguishes "the user
    chose this" from "nobody said, so I used my default", and `app_parameters`
    should show the same thing.

    Returns {} when the step is not active or the config is incomplete.
    """
    if st.session_state.stage != "asset_ready" or not config_ok:
        return {}

    st.subheader("⚙️ Report parameters")
    st.caption(
        "These are the report capsule's run parameters, declared in this app "
        "(`PARAM_SPECS`) to mirror the capsule's own list. Your choices are sent as "
        "*named run parameters* — each one reaching `code/run` as a single "
        "`--param_name=value` argument — which is how Code Ocean records them on the "
        "run and freezes them onto the captured report asset. The capsule needs no "
        "App Panel for this: named parameters reach a panel-less capsule fine."
    )

    renderable = list(PARAM_SPECS)
    hidden_names, file_names = [], []

    values = {}       # everything the form currently shows
    defaults = {}     # the capsule's own default, in the same shape as `values`
    substituted = []  # params whose apostrophes we had to swap
    # Widget state normally persists on its own, but Streamlit drops the state
    # of widgets that were not rendered on the previous run — so the last
    # choices are also mirrored here and used as the widget defaults.
    remembered = st.session_state.form_values
    with st.container(border=True):
        if renderable:
            cols = st.columns(2)
        for i, param in enumerate(renderable):
            # Widget keys are namespaced so the values survive every rerun in
            # st.session_state without colliding with the config inputs.
            key = PARAM_KEY_PREFIX + param.param_name
            label = getattr(param, "name", None) or param.param_name
            help_text = (getattr(param, "description", None)
                         or getattr(param, "help_text", None))
            capsule_default = param.default_value if param.default_value is not None else ""
            default = remembered.get(param.param_name, capsule_default)
            options = list(getattr(param, "value_options", None) or [])
            with cols[i % 2]:
                if getattr(param, "type", "text") == "list" and options:
                    # A list value comes from the capsule's own value_options,
                    # so it is left exactly as the capsule spelled it.
                    #
                    # A hand-edited panel can declare a default_value that is
                    # not one of its own value_options (or none at all). A
                    # dropdown cannot show a value it does not have, so it falls
                    # back to the first option — and THAT is the baseline, not
                    # the unreachable declared default. Comparing against the
                    # declared default instead would make an untouched form
                    # report options[0] as a human choice and freeze it onto the
                    # captured asset as an `app_parameter` nobody picked.
                    shown_default = (capsule_default if capsule_default in options
                                     else options[0])
                    if shown_default != capsule_default:
                        log_once(
                            "app panel parameter `%s` declares default_value %r, which is "
                            "not one of its value_options — the dropdown shows %r instead, "
                            "and leaving it alone counts as untouched"
                            % (param.param_name, capsule_default, shown_default))
                    index = options.index(default if default in options
                                          else shown_default)
                    defaults[param.param_name] = shown_default
                    values[param.param_name] = st.selectbox(
                        label, options, index=index, key=key, help=help_text)
                else:
                    # Compare like with like: the default goes through the same
                    # apostrophe swap the typed value does, so leaving a field
                    # alone never counts as a change.
                    defaults[param.param_name] = sanitize_param_value(capsule_default)
                    raw = st.text_input(
                        label,
                        value=str(default)[:PARAM_MAX_CHARS],
                        key=key,
                        help=help_text,
                        max_chars=PARAM_MAX_CHARS,
                    )
                    values[param.param_name] = sanitize_param_value(raw)
                    if STRAIGHT_APOSTROPHE in raw:
                        substituted.append(param.param_name)

        if not renderable:
            st.info(
                "ℹ️ Every parameter this capsule declares is hidden or of an "
                "unsupported type, so there is nothing to fill in — the report "
                "will run with the capsule's own defaults."
            )
        if hidden_names:
            # `"hidden": true` in app-panel.json. Code Ocean's own App Panel
            # does not show these, so neither does this form.
            st.caption(
                "🙈 Hidden in the capsule's App Panel, so not shown here: %s. "
                "The capsule applies its own default for each."
                % ", ".join("`%s`" % name for name in hidden_names))
            log_once("app panel for capsule %s marks %d parameter(s) hidden (%s) — "
                     "not rendered" % (step2_capsule_id, len(hidden_names),
                                       ", ".join(hidden_names)))
        if file_names:
            # type "file" means "pick a file from a mounted data asset". That
            # needs a file browser, not a text box — a free-text path would
            # just be a nice way to send a value the capsule cannot open.
            st.caption(
                "📎 File parameters are not supported by this demo form, so %s "
                "%s skipped. The capsule applies its own default."
                % (", ".join("`%s`" % name for name in file_names),
                   "is" if len(file_names) == 1 else "are"))
            log_once("app panel for capsule %s declares %d file parameter(s) (%s) — "
                     "not supported by this demo form, skipped"
                     % (step2_capsule_id, len(file_names), ", ".join(file_names)))

        if renderable:
            st.caption(
                "Each value reaches the capsule's `code/run` as one `--param_name=value` "
                "argument — no environment variables, no files. Free text is capped at "
                "%s characters (a multi-MB parameter value has degraded a deployment "
                "before), and a typed apostrophe `'` is sent as `’` because Code Ocean's "
                "parameter escaping does not support single quotes."
                % format(PARAM_MAX_CHARS, ",")
            )
        if substituted:
            st.caption("✏️ Apostrophe replaced with `’` in: %s."
                       % ", ".join("`%s`" % name for name in substituted))

    # Remember every field (so the form redraws as the user left it) but send
    # only what the user actually changed — see this function's docstring.
    st.session_state.form_values = values
    changed = {
        name: value for name, value in values.items()
        if value != defaults.get(name, "")
    }
    if not renderable:
        # Nothing editable was drawn, so there is no "differs from the default"
        # story to tell — the notes above already explain why.
        return changed
    if changed:
        st.caption(
            "▶ %d of %d parameter(s) differ from the capsule's defaults and will be "
            "sent as named run parameters: %s. Untouched fields are left out entirely, "
            "so the capsule uses — and reports — its own defaults."
            % (len(changed), len(values), ", ".join("`%s`" % name for name in changed))
        )
    else:
        st.caption(
            "▶ Everything is still at the capsule's defaults, so **no** parameters will "
            "be sent — the capsule falls back to its own values. Change a field to see "
            "it travel into the run record as an `app_parameter`."
        )
    return changed


# Rendered above the buttons so the human-in-the-loop step reads top-to-bottom:
# data ready → choose parameters → generate report.
report_params = render_parameter_form()

btn_col1, btn_col2, _ = st.columns([1, 1, 2])
start_clicked = btn_col1.button(
    "▶ Start processing",
    type="primary",
    width="stretch",
    key="btn_start",
    disabled=(not config_ok) or st.session_state.stage != "idle",
)
report_clicked = btn_col2.button(
    "📊 Generate report",
    width="stretch",
    key="btn_report",
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


def _asset_progress_writer():
    def on_update(da):
        st.write("`%s` — state: **%s**" % (da.id, da.state))
        log_event("data asset %s → %s" % (da.id, da.state))
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

            asset = orch.wait_for_data_asset(asset_id, poll_s=5, timeout_s=1800,
                                             on_update=_asset_progress_writer())
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


def set_step2_snippet(params, comp_id, asset_name=None, asset_id=None):
    """'Under the hood' for step 2: the real run + capture calls, real values.

    Shows the two calls that carry the demo's whole point — the named
    parameters that make GUI choices part of the run record, and the capture
    that turns the report into a lineage node holding them.
    """
    if params:
        named_block = (
            "    named_parameters=[\n"
            + "".join("        NamedRunParam(param_name=%r, value=%r),\n"
                      % (name, str(value)) for name, value in params.items())
            + "    ],\n"
        )
    else:
        named_block = (
            "    # no named_parameters — this capsule exposes no App Panel,\n"
            "    # so it runs with its own defaults\n"
        )
    capture_name = asset_name or "Report — <YYYY-MM-DD HH:MM UTC>"
    set_snippet(
        "Step 2 — run the report capsule with parameters, then capture the result",
        (
            "from codeocean.computation import (\n"
            "    RunParams, DataAssetsRunParam, NamedRunParam,\n"
            ")\n"
            "from codeocean.data_asset import (\n"
            "    DataAssetParams, Source, ComputationSource,\n"
            ")\n\n"
            "comp = client.computations.run_capsule(RunParams(\n"
            "    capsule_id=%r,\n"
            "    data_assets=[DataAssetsRunParam(\n"
            "        id=%r,   # the asset captured from step 1\n"
            '        mount="processed-csv",\n'
            "    )],\n"
            "%s"
            "))\n"
            "# comp.id == %r ... poll until completed, then:\n"
            'urls = client.computations.get_result_file_urls(comp.id, path="report.html")\n'
            "html = requests.get(urls.download_url).content\n\n"
            "# Capturing the results is what puts the report ON the lineage graph —\n"
            "# the parameters above are frozen onto this asset as app_parameters.\n"
            "asset = client.data_assets.create_data_asset(DataAssetParams(\n"
            "    name=%r,\n"
            '    mount="report",\n'
            '    tags=["orchestrator-demo", "step2-output"],\n'
            "    source=Source(computation=ComputationSource(id=comp.id)),\n"
            "))\n"
            "%s"
        ) % (
            step2_capsule_id,
            st.session_state.asset_id,
            named_block,
            comp_id,
            capture_name,
            "# asset.id == %r\n" % asset_id if asset_id else "",
        ),
    )


def capture_report_asset(orch, comp_id):
    """Capture step 2's /results as a result data asset (NON-FATAL).

    Downloading report.html only gives the user a file; *capturing* the results
    is what makes the report a node in Code Ocean's lineage graph — with the
    run's named parameters frozen onto it as `app_parameters`. If it fails the
    demo carries on and still shows the report.

    Resumable: the new asset id is stored in session_state the moment it
    exists, so a re-entry after an interrupted poll waits on that asset instead
    of creating a duplicate.
    """
    try:
        with st.status("Capturing the report as a data asset…", expanded=True) as status:
            existing_id = st.session_state.report_asset_id
            if existing_id:
                asset_id = existing_id
                st.write("Resuming wait on report data asset `%s`" % asset_id)
            else:
                asset_name = "Report — %s" % now_utc().strftime("%Y-%m-%d %H:%M UTC")
                asset = orch.capture_result_asset(
                    comp_id,
                    name=asset_name,
                    mount="report",
                    tags=["orchestrator-demo", "step2-output"],
                )
                asset_id = asset.id
                st.session_state.report_asset_id = asset_id
                st.session_state.report_asset_name = asset_name
                st.write("Data asset `%s` created (state: **%s**) — waiting until ready"
                         % (asset_id, asset.state))
                log_event("create_data_asset from computation %s → report asset %s"
                          % (comp_id, asset_id))
                set_step2_snippet(st.session_state.step2_params, comp_id,
                                  asset_name=asset_name, asset_id=asset_id)

            asset = orch.wait_for_data_asset(asset_id, poll_s=5, timeout_s=1800,
                                             on_update=_asset_progress_writer())
            if asset.state == "ready":
                status.update(label="Report captured as a data asset ✅", state="complete")
                log_event("report data asset %s ready — parameters frozen as app_parameters"
                          % asset_id)
            else:
                status.update(label="Report capture failed", state="error")
                st.session_state.capture_warning = (
                    "The report ran fine, but capturing it as a data asset ended in "
                    "state `%s`%s — the report below is still complete, it just is "
                    "not a lineage node." % (
                        asset.state,
                        " (%s)" % asset.failure_reason if asset.failure_reason else "")
                )
                log_event("report capture ended in state %s (non-fatal)" % asset.state)
    except Exception as exc:  # noqa: BLE001 — capture is a bonus, never fatal
        st.session_state.capture_warning = (
            "The report ran fine, but capturing it as a data asset failed: %s. The "
            "report below is still complete, it just is not a lineage node."
            % _brief(exc)
        )
        log_event("report capture failed (non-fatal): %s" % _brief(exc))


def step2_retry_available():
    """True when the one-shot unparameterized fallback has not been used yet.

    Two conditions, both required: the run that failed actually carried
    parameters (otherwise dropping them changes nothing), and the fallback is
    still unspent. `step2_retried` is what makes this a one-shot and never a
    loop — it is set *before* the retry starts and only cleared by a fresh
    "Generate report" click or a reset.
    """
    return bool(st.session_state.step2_params) and not st.session_state.step2_retried


def retry_step2_without_parameters(orch, failed_comp_id):
    """Re-run step 2 once with no parameters after a parameterized run failed.

    Contract: "if a parameterized run is rejected, retry once without
    parameters and say so in the log". A run that is *accepted* and then comes
    back `failed` lands the demo in exactly the same hole, so it gets the same
    single, loudly-logged fallback — and the retry itself runs unparameterized,
    so it can never trigger another one.
    """
    spent_params = dict(st.session_state.step2_params)
    st.session_state.step2_retried = True   # set FIRST: no second attempt, ever
    st.session_state.step2_params = {}
    log_event("step 2: computation %s FAILED carrying %d named parameter(s) (%s) — "
              "retrying ONCE without parameters"
              % (failed_comp_id, len(spent_params), ", ".join(spent_params)))
    st.session_state.retry_warning = (
        "The parameterized step-2 run (`%s`) came back **failed**, so it was retried "
        "exactly once *without* parameters (%s). If this second run succeeds, the "
        "capsule most likely choked on one of those values — its run log in Code "
        "Ocean will say which. Note the report below therefore carries no "
        "`app_parameters`." % (
            failed_comp_id,
            ", ".join("`%s=%s`" % (name, value) for name, value in spent_params.items()),
        )
    )
    comp = None
    try:
        with st.status("Retrying step 2 without parameters…", expanded=True):
            st.write("Re-running capsule `%s` with no named parameters" % step2_capsule_id)
            comp = orch.run_capsule(step2_capsule_id,
                                    data_asset_id=st.session_state.asset_id,
                                    mount="processed-csv")
            st.session_state.step2_comp_id = comp.id
            log_event("step 2 retry: run_capsule(%s, no parameters) → computation %s"
                      % (step2_capsule_id, comp.id))
            set_step2_snippet({}, comp.id)
    except Exception as exc:  # noqa: BLE001
        _fail("Step 2 failed with parameters and the unparameterized retry could not "
              "be started: %s" % _brief(exc), revert_stage="asset_ready")
    continue_step2(orch, comp.id)


def continue_step2(orch, comp_id, resuming=False):
    """Poll the step-2 computation, download report.html, capture, and finish."""
    st.session_state.stage = "step2_running"
    render_strip()
    label = ("Resuming step 2: generating HTML report…" if resuming
             else "Step 2: generating HTML report…")
    step2_error = None
    retry_unparameterized = False
    try:
        with st.status(label, expanded=True) as status:
            comp2 = orch.wait_for_computation(
                comp_id, poll_s=5, timeout_s=1800,
                on_update=_comp_progress_writer("step 2"),
            )
            if not computation_succeeded(comp2):
                status.update(label="Step 2 failed", state="error")
                failure = (
                    "Step 2 computation `%s` ended with state `%s` (exit code: %s)."
                    % (comp2.id, comp2.state, comp2.exit_code)
                )
                if step2_retry_available():
                    # The run was accepted but died. One of the parameter values
                    # is the likeliest culprit, so give the demo one more shot
                    # without them rather than dead-ending on a red box.
                    retry_unparameterized = True
                    st.write("⚠️ The parameterized run failed — retrying once "
                             "without parameters…")
                else:
                    step2_error = failure
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
    if retry_unparameterized:
        # Outside the st.status above, so the retry gets its own block. It ends
        # in continue_step2() (with step2_params now empty, so no second retry).
        retry_step2_without_parameters(orch, failed_comp_id=comp_id)
        return
    if step2_error:
        _fail(step2_error, revert_stage="asset_ready")

    # Lineage: capture the report's /results so the run's parameters live on as
    # app_parameters on a real data asset. Non-fatal by design.
    capture_report_asset(orch, comp_id)

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
    st.session_state.retry_warning = None
    st.session_state.capture_warning = None
    st.session_state.report_asset_id = None
    st.session_state.report_asset_name = None
    # A fresh click is a fresh attempt, so the one-shot fallback is re-armed.
    st.session_state.step2_retried = False
    orch = get_client()

    st.session_state.stage = "step2_running"
    render_strip()
    launch_error = None
    comp2 = None
    # What we actually managed to send — the retry path below may empty it.
    used_params = dict(report_params)
    try:
        with st.status("Launching step 2 capsule…", expanded=True):
            st.write("Launching capsule `%s` with data asset `%s` mounted at "
                     "`/data/processed-csv`" % (step2_capsule_id, st.session_state.asset_id))
            if used_params:
                st.write("Named parameters: " + ", ".join(
                    "`%s=%s`" % (name, value) for name, value in used_params.items()))
            else:
                st.write("No named parameters — every field was left at the capsule's "
                         "own default, so there is nothing to override.")
            try:
                comp2 = orch.run_capsule(step2_capsule_id,
                                         data_asset_id=st.session_state.asset_id,
                                         mount="processed-csv",
                                         named_parameters=used_params)
            except Exception as exc:  # noqa: BLE001
                # Named parameters DO reach a capsule with no App Panel — that is
                # verified — so a rejection here means something else (an unknown
                # param_name, or a deployment-side problem). Fall back to the
                # unparameterized run once rather than dead-ending the demo.
                if not used_params:
                    raise
                log_event("step 2: parameterized run REJECTED (%s) — retrying once "
                          "without parameters" % _brief(exc))
                st.write("⚠️ Parameterized run rejected — retrying without parameters…")
                # The one-shot fallback is now spent: the retry below carries no
                # parameters, so a later failure must not trigger a second one.
                st.session_state.step2_retried = True
                st.session_state.retry_warning = (
                    "The parameterized run was rejected (`%s`), so step 2 was retried "
                    "without parameters and used the capsule's own defaults. Named "
                    "parameters do reach capsules that have no App Panel, so this is "
                    "not a missing-panel problem — check that every `param_name` "
                    "matches the capsule's `PARAM_SPECS`, and check the run log."
                    % _brief(exc)
                )
                used_params = {}
                comp2 = orch.run_capsule(step2_capsule_id,
                                         data_asset_id=st.session_state.asset_id,
                                         mount="processed-csv")
            st.session_state.step2_comp_id = comp2.id
            st.session_state.step2_params = used_params
            log_event("step 2: run_capsule(%s, %d named parameter(s)) → computation %s"
                      % (step2_capsule_id, len(used_params), comp2.id))
            set_step2_snippet(used_params, comp2.id)
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

if st.session_state.retry_warning:
    st.warning("⚠️ " + st.session_state.retry_warning)

if stage_idx() >= stage_idx("asset_ready") and st.session_state.asset_id:
    st.success("✅ Data ready — data asset `%s`  (*%s*)"
               % (st.session_state.asset_id, st.session_state.asset_name or ""))

if stage_idx() >= stage_idx("asset_ready") and st.session_state.csv_df is not None:
    st.subheader("Processed CSV preview")
    df = st.session_state.csv_df
    st.dataframe(df.head(50), width="stretch")
    st.caption("`%s` — %s rows × %s columns (showing first 50). "
               "This file now lives in data asset `%s`."
               % (st.session_state.csv_name, format(len(df), ","),
                  len(df.columns), st.session_state.asset_id))

if st.session_state.stage == "asset_ready":
    st.info("👆 The processed data is captured as a versioned data asset. Set the "
            "**⚙️ Report parameters** above, then click **📊 Generate report** to "
            "feed the asset and your choices into the reporting capsule.")

if st.session_state.stage == "report_ready" and st.session_state.report_html:
    st.subheader("📈 Generated report")

    if st.session_state.report_asset_id and not st.session_state.capture_warning:
        st.success("📦 Report captured as data asset `%s`%s"
                   % (st.session_state.report_asset_id,
                      "  (*%s*)" % st.session_state.report_asset_name
                      if st.session_state.report_asset_name else ""))
        if st.session_state.step2_params:
            st.caption(
                "The %d parameter(s) you chose are now **frozen onto that asset** as "
                "`app_parameters` (%s) — open the asset in Code Ocean and its "
                "Provenance box shows the exact run, the mounted CSV asset, and these "
                "values. That is the payoff of routing GUI choices through Code Ocean "
                "instead of keeping them in Streamlit."
                % (len(st.session_state.step2_params),
                   ", ".join("`%s=%s`" % (name, value)
                             for name, value in st.session_state.step2_params.items()))
            )
        else:
            st.caption(
                "The run carried no parameters — either the capsule exposes none or "
                "every field was left at its default, and a default is the capsule's "
                "to apply, not ours to send. The asset still records its own "
                "provenance: the capsule, the commit, and the CSV asset that was "
                "mounted into the run."
            )
    if st.session_state.capture_warning:
        st.warning("⚠️ " + st.session_state.capture_warning)

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
