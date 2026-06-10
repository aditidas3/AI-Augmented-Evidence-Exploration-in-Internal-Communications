from __future__ import annotations

import time

from storage import QUESTIONS_PATH, load_json, now_iso, save_json


def list_questions():
    payload = load_json(QUESTIONS_PATH)
    return payload if isinstance(payload, list) else []


def save_questions(items):
    save_json(QUESTIONS_PATH, items)


def get_question(question_id: str):
    return next((item for item in list_questions() if item['question_id'] == question_id), None)


def find_or_create_question(question_payload: dict, workspace_id: str = 'workspace-default') -> dict:
    text = (question_payload.get('text') or '').strip()
    if not text:
        raise ValueError('question.text is required')

    items = list_questions()
    for item in items:
        if item.get('workspace_id') == workspace_id and item.get('text') == text:
            return item

    question = {
        'question_id': question_payload.get('question_id') or f"question-{int(time.time() * 1000)}",
        'workspace_id': workspace_id,
        'text': text,
        'created_at': now_iso(),
        'updated_at': now_iso()
    }
    items.append(question)
    save_questions(items)
    return question
