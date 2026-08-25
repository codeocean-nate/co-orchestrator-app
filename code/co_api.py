"""Thin Code Ocean SDK wrapper used by the orchestrator demo.

This module is deliberately Streamlit-free, so the same orchestration logic can
drive the Streamlit UI (streamlit_app.py) and any headless script or notebook.

Code Ocean concepts, in one paragraph
-------------------------------------
A *capsule* is a versioned unit of code + environment. Running a capsule
creates a *computation*; input *data assets* are mounted read-only under
``/data/<mount>/`` and everything the capsule writes to ``/results/`` is
captured. A computation's results can themselves be turned into a new
(result) data asset, which can then be mounted into the next capsule —
that chaining is exactly what this demo orchestrates.

Built against the installed ``codeocean`` SDK (0.16.x). Method and field
names below match that SDK exactly:

* ``client.computations.run_capsule(RunParams(...)) -> Computation``
* ``client.computations.get_computation(id) -> Computation``
  (``state`` in initializing|running|finalizing|completed|failed)
* ``client.data_assets.create_data_asset(DataAssetParams(...)) -> DataAsset``
* ``client.data_assets.get_data_asset(id) -> DataAsset``
  (``state`` in draft|ready|failed)
* ``client.computations.list_computation_results(id, path) -> Folder``
  (``.items`` of FolderItem with ``.name .path .type .size``)
* ``client.computations.get_result_file_urls(id, path) -> FileURLs``
  (``.download_url`` is a presigned URL; fetch it with requests)
* ``client.capsules.get_capsule_app_panel(id) -> AppPanel`` (404 = no panel;
  any other failure raises AppPanelLookupError — see get_app_panel)
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Callable, Dict, Iterable, List, Optional
from urllib.parse import urlsplit

import requests
from urllib3.util import Retry

from codeocean import CodeOcean
from codeocean.capsule import AppPanel, Capsule, CapsuleSearchParams
from codeocean.computation import (
    Computation,
    DataAssetsRunParam,
    NamedRunParam,
    RunParams,
)
from codeocean.data_asset import (
    DataAsset,
    DataAssetParams,
    Source,
    ComputationSource,
)
from codeocean.error import Error as CodeOceanError
from codeocean.models.folder import FolderItem


# ---------------------------------------------------------------------------
# Deployment domain: normalization + best-effort auto-detection
#
# Nothing here is specific to any one Code Ocean deployment. The domain comes
# from (a) $CODEOCEAN_DOMAIN, (b) auto-detection from the capsule's git remote
# when running inside a Code Ocean cloud workstation, or (c) the user.
# ---------------------------------------------------------------------------

#: Hosts that are public code-hosting services, never a Code Ocean deployment.
PUBLIC_GIT_HOSTS = frozenset({
    "github.com",
    "gitlab.com",
    "bitbucket.org",
    "codeberg.org",
    "sourceforge.net",
    "git.sr.ht",
    "dev.azure.com",
    "visualstudio.com",
})

#: Directories that may hold the capsule's git checkout inside a workstation.
#: $CO_CAPSULE_PATH wins, then the standard mount point. The current working
#: directory is deliberately NOT probed: outside Code Ocean it is some unrelated
#: clone, and mistaking its remote for the deployment would send the API token
#: to the wrong host.
CAPSULE_PATH_ENV_VAR = "CO_CAPSULE_PATH"
DEFAULT_CAPSULE_PATH = "/root/capsule"

#: A capsule's internal remote is ``<base>/capsule-<slug>.git``. Requiring that
#: exact path shape is the positive signal that a remote really is a Code Ocean
#: deployment — "host is not github.com" would happily accept a corporate
#: GitLab mirror and then point the client (and the token) at it.
CAPSULE_REMOTE_PATH_RE = re.compile(r"^/capsule-[A-Za-z0-9._-]+(?:\.git)?$")

#: Hard ceiling on how long a `git remote -v` probe may take (seconds).
GIT_PROBE_TIMEOUT_S = 2.0


def normalize_domain(value: object) -> str:
    """Clean up a user- or machine-supplied deployment URL.

    Strips whitespace and trailing slashes, prepends ``https://`` when a bare
    host was given, keeps a non-default port and any path prefix, and drops
    ``user:password@`` credentials (this value is displayed in the UI, so it
    must never carry a secret). Returns "" for anything empty or host-less, so
    callers can treat the result as a plain truthiness check.

    >>> normalize_domain("  https://foo.codeocean.com/  ")
    'https://foo.codeocean.com'
    >>> normalize_domain("foo.codeocean.com/")
    'https://foo.codeocean.com'
    >>> normalize_domain("https://user:secret@foo.codeocean.com")
    'https://foo.codeocean.com'
    >>> normalize_domain("")
    ''
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    try:
        parsed = urlsplit(text.rstrip("/"))
    except ValueError:
        return ""
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return ""
    base = "%s://%s" % (scheme, host)
    try:
        if parsed.port:
            base += ":%d" % parsed.port
    except ValueError:  # malformed port in the URL
        return ""
    return base + (parsed.path or "").rstrip("/")


def _is_public_git_host(host: str) -> bool:
    """True for github.com and friends (including their subdomains)."""
    host = host.lower()
    return any(host == known or host.endswith("." + known) for known in PUBLIC_GIT_HOSTS)


def deployment_base_from_remote(url: str) -> Optional[str]:
    """Extract a Code Ocean deployment base URL from a git remote, else None.

    A capsule's internal git remote is ``<base>/capsule-<slug>.git``, and that
    path shape is what identifies the remote as a deployment — see
    CAPSULE_REMOTE_PATH_RE. The remote's scheme and port are preserved
    (deployments exist on http and on non-443 ports) and credentials dropped.

    Returns None — never a guess — for anything else: public git hosts,
    non-HTTP(S) remotes (``ssh://``, scp-style ``git@host:path``), and remotes
    whose path is not exactly one ``capsule-<slug>.git`` segment. Falling back
    to manual entry is always safer than pointing the client at the wrong host.
    """
    if not url:
        return None
    text = str(url).strip()
    if "://" not in text:
        return None  # scp-style ssh remote (git@host:path) or junk
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https"):
        return None  # ssh://, git://, file://
    host = (parsed.hostname or "").lower()
    if not host or _is_public_git_host(host):
        return None
    if not CAPSULE_REMOTE_PATH_RE.match(parsed.path or ""):
        return None  # not a capsule remote — do not assume it is a deployment
    base = "%s://%s" % (parsed.scheme.lower(), host)
    try:
        if parsed.port:
            base += ":%d" % parsed.port
    except ValueError:
        return None
    return base


def _git_remote_urls(path: str, timeout_s: float = GIT_PROBE_TIMEOUT_S) -> List[str]:
    """Return the remote URLs of the git repo at ``path`` ([] on any problem).

    Deliberately total: a missing git binary, a missing directory, a non-repo,
    or a hung git all yield [] rather than raising.
    """
    try:
        if not path or not os.path.isdir(path):
            return []
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
        proc = subprocess.run(
            ["git", "-C", path, "remote", "-v"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=env,
        )
        if proc.returncode != 0:
            return []
        urls = []
        for line in (proc.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2:  # "origin  <url> (fetch)"
                urls.append(parts[1])
        return urls
    except Exception:  # noqa: BLE001 — detection must never break the caller
        return []


def _candidate_capsule_paths() -> List[str]:
    """Where a capsule checkout lives inside a Code Ocean workstation.

    Only Code Ocean locations — never the current working directory, which
    outside Code Ocean is an unrelated clone (see CAPSULE_PATH_ENV_VAR).
    """
    paths = []
    env_path = os.environ.get(CAPSULE_PATH_ENV_VAR, "").strip()
    if env_path:
        paths.append(env_path)
    paths.append(DEFAULT_CAPSULE_PATH)
    seen = set()
    return [p for p in paths if p and not (p in seen or seen.add(p))]


def detect_domain_from_git(
    candidate_paths: Optional[Iterable[str]] = None,
    timeout_s: float = GIT_PROBE_TIMEOUT_S,
) -> Optional[str]:
    """Best-effort: infer the deployment domain from the capsule's git remote.

    Returns the deployment base URL when a candidate directory is a git repo
    with a ``<base>/capsule-<slug>.git`` remote, otherwise None. This is a
    convenience for the common case, not a guarantee — set $CODEOCEAN_DOMAIN
    when you want certainty. Never raises and never blocks for more than
    ``timeout_s`` per candidate.
    """
    try:
        paths = list(candidate_paths) if candidate_paths is not None else _candidate_capsule_paths()
        for path in paths:
            for url in _git_remote_urls(path, timeout_s=timeout_s):
                base = deployment_base_from_remote(url)
                if base:
                    return normalize_domain(base) or None
    except Exception:  # noqa: BLE001 — detection is strictly opportunistic
        return None
    return None


# ---------------------------------------------------------------------------
# App Panel lookup: "this capsule has no panel" vs "the lookup broke"
# ---------------------------------------------------------------------------

class AppPanelLookupError(RuntimeError):
    """The App Panel *lookup* failed — we do not know what the capsule declares.

    Deliberately distinct from "this capsule has no App Panel", which is a
    genuine 404 and is reported by returning None. The difference matters a
    lot to a caller that caches the answer: "no panel" is a permanent fact
    about the capsule, while a 500 or a dropped connection is a transient
    hiccup that must NOT be remembered as "this capsule has no parameters".
    """


#: Only used when an HTTP error carries no structured status code at all.
#: Kept narrow on purpose — a connection error whose message happens to say
#: "not known" must never be mistaken for a 404.
_NOT_FOUND_TEXT_RE = re.compile(r"\b404\b|not found", re.IGNORECASE)


def http_status_code(exc: BaseException) -> Optional[int]:
    """Best-effort HTTP status code for an SDK/requests exception, else None.

    ``codeocean.error.Error`` exposes ``.status_code`` directly (it copies it
    off the wrapped ``requests.HTTPError``), but it also keeps the original as
    ``.http_err``; a plain ``requests.HTTPError`` only has ``.response``. Try
    all three shapes rather than assuming one.
    """
    for attr_path in (
        ("status_code",),                 # codeocean.error.Error
        ("response", "status_code"),      # requests.HTTPError
        ("http_err", "response", "status_code"),  # Error -> wrapped HTTPError
    ):
        value: object = exc
        for attr in attr_path:
            value = getattr(value, attr, None)
            if value is None:
                break
        if isinstance(value, int):
            return value
    return None


def _looks_like_not_found(exc: BaseException) -> bool:
    """Last-resort 404 detection from an HTTP error's message text."""
    return bool(_NOT_FOUND_TEXT_RE.search(str(exc)))


def computation_succeeded(comp: Computation) -> bool:
    """Contract completion check: state == "completed" AND exit_code in (0, None).

    A "completed" computation with a nonzero exit_code means the capsule's
    ``run`` script failed; treat it the same as state == "failed".
    """
    return comp.state == "completed" and comp.exit_code in (0, None)


def computation_failed(comp: Computation) -> bool:
    """True when the computation ended unsuccessfully (failed state or bad exit)."""
    if comp.state == "failed":
        return True
    return comp.state == "completed" and comp.exit_code not in (0, None)


def build_named_run_params(
    named_parameters: Optional[Dict[str, object]],
) -> Optional[List[NamedRunParam]]:
    """Turn ``{param_name: value}`` into the SDK's named-parameter list.

    Code Ocean's *named parameters* are how a GUI choice becomes part of the
    provenance record: the backend appends each one to the capsule's ``code/run``
    as a single ``--param_name=value`` argv token, stores them on the
    Computation, and freezes them onto any data asset captured from that run as
    ``app_parameters``. Values are always sent as strings.

    Returns None (not an empty list) when there is nothing to send, so the
    caller can leave the field off RunParams entirely and keep unparameterized
    runs byte-identical to a call that never knew about parameters.
    """
    if not named_parameters:
        return None
    return [
        NamedRunParam(param_name=str(name), value=str(value))
        for name, value in named_parameters.items()
    ]


class Orchestrator:
    """Small facade over the Code Ocean SDK for run -> capture -> run chains."""

    def __init__(self, domain: str, token: str):
        # Normalized here too so any caller (UI, smoke test, notebook) can pass
        # a bare host or a trailing-slash URL without breaking request paths.
        self.domain = normalize_domain(domain)
        # Retry transient HTTP errors against the CO API. The SDK mounts this
        # Retry policy via an HTTPAdapter (TCPKeepAliveAdapter) on the domain.
        # Default allowed_methods excludes POST, so a run is never duplicated.
        retry = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        self.client = CodeOcean(domain=self.domain, token=token, retries=retry)

    # -------------------------------------------------------------- capsules

    def search_capsules(self, query: str, limit: int = 10) -> List[Capsule]:
        """Find capsules by text query (supports field:value syntax, e.g. name:foo).

        Each returned Capsule has .id (the UUID used to run it), .name and
        .slug (the number in the capsule's URL, used for its git remote).
        """
        results = self.client.capsules.search_capsules(
            CapsuleSearchParams(query=query, limit=limit)
        )
        return list(results.results or [])

    def get_app_panel(self, capsule_id: str) -> Optional[AppPanel]:
        """Return the capsule's App Panel definition, or None if it has none.

        A capsule's *App Panel* (committed as ``.codeocean/app-panel.json``) is
        the capsule's own declaration of the parameters it accepts: each entry
        has a ``name`` (label), ``param_name`` (argument key), ``type``
        ("text"/"list"), ``default_value`` and, for lists, ``value_options``.
        Reading it lets a caller build a parameter form *from the capsule*
        instead of hardcoding one — add a parameter to the capsule and it
        appears in the UI with no code change.

        Two very different things can go wrong here, and the caller has to be
        able to tell them apart:

        * **404 — the capsule has no App Panel.** A permanent fact about the
          capsule (panels are optional). Returns ``None``, meaning "no
          parameters to offer, run the capsule with its own defaults".
        * **Anything else** — a 500, an expired token, a DNS blip, a timeout,
          an unexpected payload shape. Raises :class:`AppPanelLookupError`.

        Collapsing the second case into ``None`` (as this used to) lets one
        transient hiccup be recorded as "this capsule has no parameters", which
        silently deletes the parameter form for the rest of the session.
        """
        try:
            return self.client.capsules.get_capsule_app_panel(capsule_id)
        except (CodeOceanError, requests.HTTPError) as exc:
            # An HTTP-level failure: the server answered, so there is a status.
            status = http_status_code(exc)
            if status == 404:
                return None
            if status is None and _looks_like_not_found(exc):
                # No structured status (odd SDK/proxy error shape) — only then
                # is it worth reading the message text.
                return None
            raise AppPanelLookupError(
                "App Panel lookup for capsule %s failed%s: %s"
                % (capsule_id, " (HTTP %s)" % status if status else "", exc)
            ) from exc
        except requests.RequestException as exc:
            # Connection reset, DNS failure, timeout: transient by nature and
            # never evidence that the capsule lacks a panel.
            raise AppPanelLookupError(
                "App Panel lookup for capsule %s failed (no response): %s"
                % (capsule_id, exc)
            ) from exc
        except Exception as exc:  # noqa: BLE001 — e.g. an unparseable payload.
            raise AppPanelLookupError(
                "App Panel lookup for capsule %s failed unexpectedly: %s"
                % (capsule_id, exc)
            ) from exc

    # ------------------------------------------------------------------ runs

    def run_capsule(
        self,
        capsule_id: str,
        data_asset_id: Optional[str] = None,
        mount: Optional[str] = None,
        named_parameters: Optional[Dict[str, object]] = None,
    ) -> Computation:
        """Start a capsule run, optionally mounting one data asset.

        The asset appears inside the capsule at ``/data/<mount>/``.
        ``named_parameters`` is a plain ``{param_name: value}`` dict; it only
        applies to capsules that expose an App Panel, and it is what makes GUI
        choices show up in the run's provenance (see build_named_run_params).
        When it is empty or None the field is left off RunParams entirely, so
        an unparameterized run is exactly the call it always was.

        Returns immediately with the new Computation (state "initializing").
        """
        data_assets = None
        if data_asset_id:
            data_assets = [DataAssetsRunParam(id=data_asset_id, mount=mount)]
        run_kwargs = {"capsule_id": capsule_id, "data_assets": data_assets}
        run_params = build_named_run_params(named_parameters)
        if run_params:
            run_kwargs["named_parameters"] = run_params
        params = RunParams(**run_kwargs)
        return self.client.computations.run_capsule(params)

    def wait_for_computation(
        self,
        comp_id: str,
        poll_s: float = 5,
        timeout_s: float = 1800,
        on_update: Optional[Callable[[Computation], None]] = None,
    ) -> Computation:
        """Poll a computation until it reaches a terminal state.

        Calls ``on_update(comp)`` whenever the observed state changes
        (initializing -> running -> completed/failed). Returns the final
        Computation; raises TimeoutError if it does not finish in time.
        The caller decides success via computation_succeeded().
        """
        deadline = time.monotonic() + timeout_s
        last_state = None
        while True:
            comp = self.client.computations.get_computation(comp_id)
            if comp.state != last_state:
                last_state = comp.state
                if on_update:
                    on_update(comp)
            if comp.state in ("completed", "failed"):
                return comp
            if time.monotonic() > deadline:
                raise TimeoutError(
                    "Computation %s did not finish within %ss (last state: %s)"
                    % (comp_id, timeout_s, comp.state)
                )
            time.sleep(poll_s)

    # ---------------------------------------------------------- data assets

    def capture_result_asset(
        self,
        comp_id: str,
        name: str,
        mount: str,
        tags: List[str],
    ) -> DataAsset:
        """Turn a completed computation's /results into a new result data asset.

        The asset starts in state "draft" while Code Ocean copies files;
        follow with wait_for_data_asset() before mounting it anywhere.
        """
        params = DataAssetParams(
            name=name,
            description="Created by the orchestrator demo from computation %s" % comp_id,
            mount=mount,
            tags=tags,
            source=Source(computation=ComputationSource(id=comp_id)),
        )
        return self.client.data_assets.create_data_asset(params)

    def wait_for_data_asset(
        self,
        da_id: str,
        poll_s: float = 5,
        timeout_s: float = 1800,
        on_update: Optional[Callable[[DataAsset], None]] = None,
    ) -> DataAsset:
        """Poll a data asset until it is "ready" or "failed" (draft = in progress)."""
        deadline = time.monotonic() + timeout_s
        last_state = None
        while True:
            da = self.client.data_assets.get_data_asset(da_id)
            if da.state != last_state:
                last_state = da.state
                if on_update:
                    on_update(da)
            if da.state in ("ready", "failed"):
                return da
            if time.monotonic() > deadline:
                raise TimeoutError(
                    "Data asset %s was not ready within %ss (last state: %s)"
                    % (da_id, timeout_s, da.state)
                )
            time.sleep(poll_s)

    # -------------------------------------------------------------- results

    def list_results(self, comp_id: str) -> List[FolderItem]:
        """Recursively list a computation's result FILES as a flat list.

        The API lists one folder level at a time
        (list_computation_results(comp_id, path)), so walk folders
        breadth-first. Each returned FolderItem has .name/.path/.type/.size.
        """
        files: List[FolderItem] = []
        pending = [""]  # empty path = /results root
        while pending:
            path = pending.pop(0)
            folder = self.client.computations.list_computation_results(comp_id, path=path)
            for item in folder.items:
                if item.type == "folder":
                    pending.append(item.path)
                else:
                    files.append(item)
        return files

    def download_result(self, comp_id: str, path: str) -> bytes:
        """Download one result file's bytes via its presigned URL."""
        file_urls = self.client.computations.get_result_file_urls(comp_id, path=path)
        resp = requests.get(file_urls.download_url, timeout=120)
        resp.raise_for_status()
        return resp.content
