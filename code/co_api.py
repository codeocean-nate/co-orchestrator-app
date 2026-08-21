"""Thin Code Ocean SDK wrapper used by the orchestrator demo.

This module is deliberately Streamlit-free so the same orchestration logic
drives both the Streamlit UI (streamlit_app.py) and the headless smoke test
(scripts/smoke_test.py).

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
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional

import requests
from urllib3.util import Retry

from codeocean import CodeOcean
from codeocean.capsule import Capsule, CapsuleSearchParams
from codeocean.computation import Computation, RunParams, DataAssetsRunParam
from codeocean.data_asset import (
    DataAsset,
    DataAssetParams,
    Source,
    ComputationSource,
)
from codeocean.models.folder import FolderItem


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


class Orchestrator:
    """Small facade over the Code Ocean SDK for run -> capture -> run chains."""

    def __init__(self, domain: str, token: str):
        self.domain = domain.rstrip("/")
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

    # ------------------------------------------------------------------ runs

    def run_capsule(
        self,
        capsule_id: str,
        data_asset_id: Optional[str] = None,
        mount: Optional[str] = None,
    ) -> Computation:
        """Start a capsule run, optionally mounting one data asset.

        The asset appears inside the capsule at ``/data/<mount>/``.
        Returns immediately with the new Computation (state "initializing").
        """
        data_assets = None
        if data_asset_id:
            data_assets = [DataAssetsRunParam(id=data_asset_id, mount=mount)]
        params = RunParams(capsule_id=capsule_id, data_assets=data_assets)
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
