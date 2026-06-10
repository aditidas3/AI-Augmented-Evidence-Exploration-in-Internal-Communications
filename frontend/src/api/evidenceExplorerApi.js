import { requestJson } from './httpClient.js';

export async function listCollections() {
  return requestJson('/api/collections');
}

export async function resolveFilterConfig(applicationId, selectedCollectionIds) {
  return requestJson('/api/filter-config/resolve', {
    method: 'POST',
    body: JSON.stringify({ application_id: applicationId, selected_collection_ids: selectedCollectionIds })
  });
}

export async function listScopes() {
  return requestJson('/api/scopes');
}

export async function deleteScope(scopeId) {
  return requestJson(`/api/scopes/${encodeURIComponent(scopeId)}`, {
    method: 'DELETE'
  });
}

export async function listRuns() {
  return requestJson('/api/runs');
}

export async function deleteRun(runId) {
  return requestJson(`/api/runs/${encodeURIComponent(runId)}`, {
    method: 'DELETE'
  });
}

export async function listWorkspaces() {
  return requestJson('/api/workspaces');
}

export async function getWorkspaceContext(workspaceId) {
  return requestJson(`/api/workspaces/${workspaceId}`);
}

export async function listQuestions() {
  return requestJson('/api/questions');
}

export async function getRun(runId) {
  return requestJson(`/api/runs/${runId}`);
}

export async function getRunExploration(runId) {
  return requestJson(`/api/runs/${runId}/exploration`);
}

export async function getRunResultIndex(runId) {
  return requestJson(`/api/runs/${runId}/results`);
}

export async function getRunArtifacts(runId) {
  return requestJson(`/api/runs/${runId}/bundles`);
}

export async function listConflictReviews(runId) {
  return requestJson(`/api/runs/${runId}/conflict-reviews`);
}

export async function listAnalystReviews(runId) {
  return requestJson(`/api/runs/${runId}/analyst-reviews`);
}

export async function saveConflictReview(runId, edgeId, review) {
  return requestJson(`/api/runs/${runId}/conflict-reviews/${edgeId}`, {
    method: 'POST',
    body: JSON.stringify(review)
  });
}

export async function saveAnalystReview(runId, objectType, objectId, review) {
  return requestJson(`/api/runs/${runId}/analyst-reviews/${encodeURIComponent(objectType)}/${encodeURIComponent(objectId)}`, {
    method: 'POST',
    body: JSON.stringify(review)
  });
}

export async function previewEvidenceSubset(request) {
  return requestJson('/api/evidence/preview', {
    method: 'POST',
    body: JSON.stringify(request)
  });
}

export async function launchRun(request) {
  return requestJson('/api/runs', {
    method: 'POST',
    body: JSON.stringify(request)
  });
}

export async function createComparison(request) {
  return requestJson('/api/comparisons', {
    method: 'POST',
    body: JSON.stringify(request)
  });
}

export async function createComposite(request) {
  return requestJson('/api/composites', {
    method: 'POST',
    body: JSON.stringify(request)
  });
}
