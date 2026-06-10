from __future__ import annotations

from abc import ABC, abstractmethod

from runs import get_run
from storage import load_json, resolve_storage_path


class ArtifactProvider(ABC):
    """Source of corrected intent and operator bundle artifacts for a run.

    The local provider reads from result-index file refs. A production provider
    can call the real execution backend, object store, or job service while
    preserving the same API shape for the frontend.

    Keep this boundary separate from exploration state. Workspaces/questions/
    comparisons describe analyst activity; artifacts describe operator outputs
    for one run.
    """

    @abstractmethod
    def result_index(self, run_id: str) -> dict | None:
        raise NotImplementedError

    @abstractmethod
    def artifacts(self, run_id: str) -> dict | None:
        raise NotImplementedError


class LocalFileArtifactProvider(ArtifactProvider):
    def result_index(self, run_id: str) -> dict | None:
        run = get_run(run_id)
        if not run or not run.get('result_index_ref'):
            return None
        return load_json(resolve_storage_path(run['result_index_ref']))

    def artifacts(self, run_id: str) -> dict | None:
        result_index = self.result_index(run_id)
        if not result_index:
            return None

        refs = {
            'correctedIntent': result_index.get('corrected_intent_ref'),
            'align': result_index.get('align_bundle_ref'),
            'trace': result_index.get('trace_bundle_ref'),
            'conflict': result_index.get('conflict_bundle_ref'),
            'construct': result_index.get('construct_bundle_ref'),
            'explain': result_index.get('explain_bundle_ref')
        }
        return {
            key: load_json(resolve_storage_path(ref))
            for key, ref in refs.items()
            if ref
        }


artifact_provider: ArtifactProvider = LocalFileArtifactProvider()
