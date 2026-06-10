import { getRunArtifacts, getRunResultIndex } from '../api/evidenceExplorerApi.js';
import { requestText } from '../api/httpClient.js';
import { parsePermissiveJson } from './permissiveJson.js';

async function fetchBundle(path) {
  return parsePermissiveJson(await requestText(path));
}

async function loadRunBundlesFromResultIndex(runId) {
  const resultIndex = await getRunResultIndex(runId);
  const refs = {
    correctedIntent: resultIndex.corrected_intent_ref,
    align: resultIndex.align_bundle_ref,
    trace: resultIndex.trace_bundle_ref,
    conflict: resultIndex.conflict_bundle_ref,
    construct: resultIndex.construct_bundle_ref,
    explain: resultIndex.explain_bundle_ref
  };
  const entries = await Promise.all(
    Object.entries(refs).map(async ([key, path]) => [key, await fetchBundle(path)])
  );
  return Object.fromEntries(entries);
}

export async function loadRunBundles(runId) {
  try {
    const artifacts = await getRunArtifacts(runId);
    return {
      correctedIntent: artifacts.correctedIntent || artifacts.corrected_intent,
      align: artifacts.align,
      trace: artifacts.trace,
      conflict: artifacts.conflict,
      construct: artifacts.construct,
      explain: artifacts.explain
    };
  } catch (error) {
    // During migration, older/local servers may only expose a result-index
    // document plus file refs. Keep that fallback so the UI is not coupled to
    // one backend implementation.
    return loadRunBundlesFromResultIndex(runId);
  }
}
