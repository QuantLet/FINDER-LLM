[![Quantlet](https://github.com/StefanGam/test-repo/blob/main/quantlet_design.png?raw=true)](https://quantlet.com)

```
Name of Quantlet: 01 FINDER-LLM

Published in: FINDER-LLM: FINancial Anomaly DEtection and Recognition using Large Language Models

Description: Python code and saved results for a multimodal, multi-agent framework that assesses next-day downside anomaly risk from market indicators, charts and financial news. Six roles produce five published risk scores and structured reports across six financial markets. From the repository root, run python -m http.server 8765 --bind 127.0.0.1 and open http://127.0.0.1:8765/Dashboard/ to review the saved results and reports. Use 02 Review Results.ipynb to inspect the saved tables in Python.

Keywords: Anomaly Detection, Large Language Models, Multimodal AI, Risk Assessment, Time-Series Analysis

Author: Siang-Li JHENG, Rahul Tak, Ștefan Găman, Diana Ioana Micu, Daniel Traian PELE, Adrian Costea

Input: Use Python 3.11 or newer and install requirements.txt. Edit config/settings.json and open 01 FINDER-LLM.ipynb to run the model. Prepare local inputs with notebooks/prepare/. Supply an OpenAI API key through the environment or a local .env file. Raw market data, full news text and reference banks are not included.

Output: New runs are saved under Runs/<market>/<run name>/ as scores.csv and daily reports covering six roles. Evaluation produces evaluation.csv and metrics.csv. Saved paper results are in FINDER/Official and Benchmark/Official; saved figures are in Figures/Manuscript. New runs do not overwrite these outputs. Use 02 Review Results.ipynb for ranking intervals, 03 A4 Sensitivity.ipynb for event-month checks, and notebooks/figures/ for the manuscript figures.

```
