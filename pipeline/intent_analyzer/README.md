# Intent Analyzer

Two related sub-tools for producing and validating intent objects from natural-language questions.

## Layout

- [generator/](generator/) — LLM-driven intent generator. Runs a prompt over a list of questions and emits an intent-object JSON per question.
  - Entry point: [generator/intent_analysis.py](generator/intent_analysis.py)
  - Inputs: [generator/prompt.txt](generator/prompt.txt), [generator/questions.txt](generator/questions.txt)
- [validator/](validator/) — rule-based validator and auto-corrector for intent objects.
  - Entry points: [validator/main.py](validator/main.py) (validate), [validator/corrector.py](validator/corrector.py) (correct)
  - Modules: [validator/question_parser.py](validator/question_parser.py), [validator/validation_layers.py](validator/validation_layers.py), [validator/scoring_engine.py](validator/scoring_engine.py)
  - Usage: [validator/HOW_TO_RUN.md](validator/HOW_TO_RUN.md)
- `output/` — generated reports and corrected intents (gitignored).
- `.env` — `OPENAI_API_KEY` for the generator (gitignored).
- [requirements.txt](requirements.txt) — combined dependencies for both sub-tools.

## Typical flow

1. `python generator/intent_analysis.py` — produces `intent_analysis_results.json` from `questions.txt`.
2. `python validator/main.py --intent intent_analysis_results.json --output output/batch_report.json` — validates every record.
3. `python validator/corrector.py --pipeline --intent intent_analysis_results.json` — applies fixes and re-validates.

See [validator/HOW_TO_RUN.md](validator/HOW_TO_RUN.md) for the full validator/corrector CLI reference.
