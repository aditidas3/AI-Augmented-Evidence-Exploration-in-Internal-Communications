# PIPELINE TO ANALYZE AND IMPROVE INTENT OBJECT

All commands below are run from the `pipeline/intent_analyzer/` directory.

### STEP 1: To validate any intent object
```
python validator/main.py --intent <original_intent_file>.json --question-file <question>.txt --output output/report.json
```
### OR ###
```
python validator/main.py --intent intent.json --question "<type_question_manually>"
```
** Inputs - intent object, user question (keep intent object and user question in same folder)

** Default output - `output/validation_report.json`

### STEP 1B: To validate a batch file like `intent_analysis_results.json`
If the input JSON is a top-level array of records shaped like:
`{"index": 1, "question": "...", "response": {...intent...}}`
the validator now processes every record automatically.

```
python validator/main.py --intent intent_analysis_results.json --output output/batch_report.json
```

Question priority per record:
1. `--question`
2. `--question-file`
3. top-level record `question`
4. `response.Header.question_text`

** Default output - `output/validation_report.json`
Each batch item contains:
- `record_index`
- `question`
- `intent_id`
- full per-record `report`

### STEP 2:  To improve the intent object
```
python validator/corrector.py --intent <original_intent_file>.json --report <obtained_report_from_step1>.json
```
** Inputs - intent object, report obtained in validation step

** Default outputs - `output/corrected_intent.json`, `output/correction_log.json`

### STEP 2B: To improve a batch file
```
python validator/corrector.py --intent intent_analysis_results.json --report batch_report.json
```

This applies corrections to every record in the batch and preserves the outer
`index/question/response` structure in `corrected_intent.json`.

### Full batch pipeline
```
python validator/corrector.py --pipeline --intent intent_analysis_results.json
```

** Default outputs**
- `output/corrected_intent.json`
- `output/correction_log.json`
