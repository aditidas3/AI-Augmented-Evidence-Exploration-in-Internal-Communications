export function associatedValue(slot, witness) {
  const mentionSurface = witness?.mention?.surface;
  const detailVar = witness?.intent_element?.element_detail?.var;
  return (slot?.value || []).find((value) =>
    (mentionSurface && value.surface === mentionSurface)
    || (detailVar && value.var === detailVar && value.surface === mentionSurface)
  ) || (slot?.value || []).find((value) => value.surface === mentionSurface) || null;
}

export function witnessLabel(witness) {
  return witness?.mention?.surface
    || witness?.intent_element?.element_detail?.surface
    || witness?.witness_id
    || 'Unnamed witness';
}

export function witnessLocation(witness) {
  const artifact = witness?.anchor?.metadata?.artifact_name || witness?.anchor?.artifact_id || 'unknown artifact';
  const path = (witness?.anchor?.path || [])
    .map((part) => `${part.component_type}${part.index}`)
    .join('.');
  return path ? `${artifact} · ${path}` : artifact;
}

export function witnessProvenancePath(witness) {
  return [
    witness?.anchor?.artifact_id,
    witness?.anchor?.anchor_id,
    witness?.mention?.mention_id,
    witness?.witness_id
  ].filter(Boolean);
}

export function buildWitnessContextIndex(slotBindings) {
  const witnessContexts = {};
  for (const slot of slotBindings || []) {
    for (const witness of slot.witnesses || []) {
      witnessContexts[witness.witness_id] = {
        witness,
        slot,
        associatedValue: associatedValue(slot, witness)
      };
    }
  }
  return witnessContexts;
}

