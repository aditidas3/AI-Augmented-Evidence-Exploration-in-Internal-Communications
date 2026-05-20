"""
OpenAI API client for LLM calls.
Intent analysis test: runs the Intent Analysis prompt against example questions.
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

if not os.environ.get("OPENAI_API_KEY"):
    raise ValueError("OPENAI_API_KEY not found in environment. Set it in .env.")

client = OpenAI(
    api_key=os.environ.get("OPENAI_API_KEY"),
    base_url="https://openrouter.ai/api/v1",
)

# Default model for completions (override via run_intent_analysis(model=...) or call_llm(model=...))
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-oss")
SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompt.txt"
QUESTIONS_PATH = Path(__file__).parent / "questions.txt"


def load_questions(questions_path: Path = None) -> list[str]:
    """Load questions from a txt file (one question per line, blank lines skipped)."""
    path = questions_path or QUESTIONS_PATH
    if not path.exists():
        raise FileNotFoundError(f"Questions file not found: {path}")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    return [q.strip() for q in lines if q.strip()]


def call_llm(
    user_content: str,
    system_content: str = "",
    model: str = None,
    temperature: float = 0.0,
    retry_on_empty: bool = True,
    **kwargs,
) -> str:
    """Single LLM call. Returns assistant text. On empty content, retries once then returns a placeholder string."""
    model = model or OPENAI_MODEL
    messages = []
    if system_content:
        messages.append({"role": "system", "content": system_content})
    messages.append({"role": "user", "content": user_content})

    def _request():
        return client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            **kwargs,
        )

    response = _request()
    if not response.choices:
        if retry_on_empty:
            response = _request()
        if not response.choices:
            return "[LLM returned no choices]"

    choice = response.choices[0]
    content = choice.message.content if choice.message else None
    if content is None or (isinstance(content, str) and not content.strip()):
        if retry_on_empty:
            response = _request()
            if response.choices and response.choices[0].message.content:
                return response.choices[0].message.content.strip()
        finish = getattr(choice, "finish_reason", None)
        return f"[LLM returned no content (finish_reason={finish!r})]"
    return content.strip()


def load_system_prompt(prompt_path: Path = None) -> str:
    """Load the system prompt text from prompt.txt (or provided path)."""
    path = prompt_path or SYSTEM_PROMPT_PATH
    prompt = path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError(f"System prompt file is empty: {path}")
    return prompt


RESULTS_PATH = Path(__file__).parent / "intent_analysis_results.json"


def run_intent_analysis(
    questions: list[str] = None,
    questions_path: Path = None,
    results_path: Path = None,
    system_prompt_path: Path = None,
    model: str = None,
    temperature: float = 0.0,
    max_questions: int = None,
) -> list[dict]:
    """Run intent analysis using prompt.txt as the system prompt and each question as user input."""
    questions = questions if questions is not None else load_questions(questions_path)
    if max_questions is None:
        max_questions = len(questions)
    results_path = results_path or RESULTS_PATH
    system_prompt = load_system_prompt(system_prompt_path)
    if max_questions is not None:
        questions = questions[:max_questions]

    if not questions:
        print("No questions to run. Add questions to questions.txt or pass questions=[...].")
        return []

    results = []
    for i, question in enumerate(questions, start=1):
        try:
            response = call_llm(
                user_content=question,
                system_content=system_prompt,
                model=model,
                temperature=temperature,
            )
        except Exception as e:
            response = f"[ERROR] {type(e).__name__}: {e}"
        # Parse the response into a JSON object if possible, so it's
        # stored as structured data rather than an escaped string.
        try:
            parsed_response = json.loads(response)
        except (json.JSONDecodeError, TypeError):
            parsed_response = response

        results.append({
            "index": i,
            "question": question,
            "response": parsed_response,
        })
        if "[ERROR]" in response:
            print(f"  [{i}/{len(questions)}] error")
        elif response.startswith("[LLM returned no"):
            print(f"  [{i}/{len(questions)}] no content")
        else:
            print(f"  [{i}/{len(questions)}] ok")

    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(results)} results to {results_path}")
    return results


if __name__ == "__main__":
    run_intent_analysis()
