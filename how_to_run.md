# How to run yaml file

Edit pre_kg_config.yaml (done by humans)
        ↓
Feed to LLM with the prompt at the bottom of the YAML
        ↓
LLM outputs new rule_engine.py (take yaml prompt and generate new rule_engine.py)
        ↓
Drop it in the same folder — pre_kg_rules.py picks it up automatically



# CASE 1: Processing data for first time. Run the below command in terminal
## (for bash use \ for next line, for powershell use ` for cmd prompt use ^)
## 1. Clean new JSONL
python pre_kg_rules.py \
    --doc   <filename_doc>.jsonl  --email <filename_email>.jsonl \
    --ppt   <filename_ppt>.jsonl  --xls   <filename_spreadsheet>.jsonl \
    --txt   <filename_text>.jsonl 
    --out-dir ./clean

## 2. Load into Neo4j to create baseline Knowledge Graph
python kg_loader.py \
    --doc   clean/DOC_clean.jsonl \
    --email clean/EMAIL_clean.jsonl \
    --ppt   clean/PPT_clean.jsonl \
    --xls   clean/XLS_clean.jsonl \
    --txt   clean/TXT_clean.jsonl \
    --uri   <url> \
    --user  <user> \
    --password <password> \
    [--dry-run] [--batch-size 200] [--limit 10]

## 3. Run post-KG rules to perform Entity Resolution, Structural Integrity and enrichment
python post_kg_rules.py \
    --uri      <url> \
    --user     <username> \
    --password <your-password> \
    --out      post_kg_report.json

# CASE 2: For the new JSONL with same structure and updated data

## 1. Clean the new JSONL
python pre_kg_rules.py \
  --doc NEW_DOC.jsonl --email NEW_EMAIL.jsonl ... \
  --out-dir ./clean_new

## 2. Load into the existing graph (MERGE handles overlaps automatically)
python kg_loader.py \
  --doc clean_new/DOC_clean.jsonl ... \
  --uri "neo4j+s://..." --password "..."

## 3. Re-run post-KG to catch any new duplicates and add new derived edges
python post_kg_rules.py \
  --uri "neo4j+s://..." --password "..." \
  --out post_kg_report_v2.json

# CASE 3: Run EXTERNAL LIBRARIES
python external_libs.py \\
    --uri      bolt://localhost:7687 \\
    --user     neo4j \\
    --password <pw> \\
    [--source  rxnorm]      
    [--limit   20]          
    [--dry-run]            

# Validation Steps for CASE 3
## Test with 5 drugs first — no writes
python external_libs.py \
    --uri bolt://localhost:7687 --user neo4j --password <pw> \
    --limit 5 --dry-run

## Run only RxNorm, skip FDA Orange Book
python external_libs.py \
    --uri bolt://localhost:7687 --user neo4j --password <pw> \
    --source rxnorm

## Run only FDA Orange Book
python external_libs.py \
    --uri bolt://localhost:7687 --user neo4j --password <pw> \
    --source fda_orange_book

## Run both (default)
python external_libs.py \
    --uri bolt://localhost:7687 --user neo4j --password <pw> \
    --source rxnorm,fda_orange_book

# NOTE for CASE 3
external_libs.py is safe to re-run at any time. It uses MERGE on kg_id so re-running it won't create duplicate Vocab nodes. It just updates the properties in place. This means if an API call failed the first time, you can run it again and it will fill in the gaps.