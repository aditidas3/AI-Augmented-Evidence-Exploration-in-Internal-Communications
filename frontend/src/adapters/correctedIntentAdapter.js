export function adaptCorrectedIntent(raw) {
  const first = Array.isArray(raw) ? raw[0] : raw;
  const response = first?.response || {};
  const header = response.Header || {};
  return {
    question_id: header.question_id || `question-${first?.index || 'unknown'}`,
    text: first?.question || header.question_text || '',
    corrected_intent: {
      intent_id: header.intent_id || null,
      schema_version: header.schema_version || null,
      question_text: header.question_text || first?.question || '',
      entity_hints: response.EntityHints || [],
      raw: response
    }
  };
}
