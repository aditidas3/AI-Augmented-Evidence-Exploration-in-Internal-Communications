# AI-Augmented Evidence Exploration for Discovering Latent Corporate Strategies in Internal Communications

## Overview

Investigating corporate behavior across large document collections traditionally requires researchers to manually review thousands of files. This project automates that process by extracting and connecting people, organizations, topics, products, and legal frameworks from the corpus into a structured knowledge graph, surfaced through an interactive research dashboard.

## Team:
This project is being built in collaboration with Advanced Database and Intelligence Lab (ADIL). 

---

## Project Design

Documents are collected and made into JSON items to process them into baseline Knowledge Graph. User questions are entered through Dashboard and the questions are processed into JSON object as well, backend operators are run to extract evidence based on the questions and produced back to the dashboard for the user.  

---

## Tech Stack

- **Neo4j** — graph database
- **Python** — pipeline scripts
- **LLM API** — LLM-based schema design, entity resolution