# Read the core

Start with these five files. The main flow uses six roles.

1. `pipeline.py` runs one date at a time: prepare inputs, collect evidence, ask the supervisor, and save the result.
2. `workflow.py` runs three separate evidence branches. The news branch uses Reader → Assistant → Expert, with a title screen and a calendar check.
3. `supervisor.py` joins the three reports, charts, current values, past examples, and known past outcomes.
4. `prompts.py` holds the exact task and role text. `contracts.py` defines the JSON each role must return.
5. `public_outputs.py` saves reports and five score columns. It computes the paper's metrics and updates each threshold using only earlier resolved outcomes.

The Quantitative Expert and Chart Expert run alongside the news branch. Their results meet at one independent Decision Supervisor. The News Assistant produces factual summaries and event records. The Supervisor reports the evidence balance and an assessment summary.

## Supporting files

| Purpose | Files |
| --- | --- |
| Settings and dated inputs | `config.py`, `settings.py`, `markets.py`, `data.py`, `tools.py`, `overseas.py` |
| News and charts | `news_collection.py`, `news_sources.py`, `future_events.py`, `charts.py`, `verification.py` |
| Historical examples and memory | `few_shot.py`, `few_shot_builder.py`, `label_artifact.py`, `memory.py`, `model_context.py` |
| API calls and saved records | `client.py`, `provider_retry.py`, `storage.py`, `events.py`, `reporting.py` |

`tools.py` builds fixed input packets in Python. Models do not select or call tools. `future_events.py` keeps source dates and event identities consistent across dates; these checks are part of the paper's news flow.

## What stays fixed

The paper uses `gpt-5-mini`, with `gpt-5-nano` for the News Assistant, and low reasoning effort. Inputs use 126 market observations, 63 indicators, and three charts over the latest 21 observations. The label uses a separate 252-return history. The first alert threshold is 0.5; later thresholds use only resolved past results.

Date checks, source hashes, full selected news text, JSON checks, bounded retries, and score masking are kept. A successful software test does not establish historical news availability or reproduce a paid model run.

New runs go to `Runs/<market>/<run name>/`. Existing run folders and saved research outputs are protected. API keys are passed to clients at runtime and are not saved in run settings.
