# FINDER-LLM

FINDER-LLM studies next-day downside risk with market data, charts and news. Start with the local app to build a workflow, or use the notebooks to review the paper results.

## Start the app

Use Python 3.11 or newer. From this folder:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

On Windows, activate with `.venv\Scripts\activate`. Open [FINDER Studio](http://127.0.0.1:8765) after starting the app.

1. Choose **Demo** to try the app without a key or a model call.
2. Choose a template, then edit the roles, layers, prompts and models.
3. Review the call count and run. Read each reply or download the result.

For a real run, choose **Live API** and enter your own key. The custom builder accepts OpenAI-compatible Chat Completions endpoints. A model must support that API; JSON output also needs JSON mode. Provider charges apply when you start a live run. The key stays in memory and is not added to downloads or files. The app runs only on your computer; it is not a hosted service.

## Choose a workflow

| Mode | What it does |
| --- | --- |
| Paper workflow | Uses the six fixed roles, prompts, models and output contracts of the paper. It needs your prepared local inputs and the original OpenAI endpoint. |
| Custom workflow | Lets you add layers and roles, choose models, write prompts, set text or JSON output, and use parallel work, a sequence, or debate rounds. This is a new experiment. |
| Demo | Shows how the custom workflow runs using sample replies. It does not analyse evidence or reproduce paper results. |

In a parallel layer, roles receive the same prior context and cannot see each other's replies. In a sequence, each role sees the replies before it. Debate repeats this sequence for the number of rounds you choose. Each new layer receives the earlier layers' replies. The last layer supplies the final output. Stop prevents further calls after calls already in progress finish.

## Read or run the research

Start Jupyter with `jupyter lab`. Notebooks find the project folder from their own location.

| Entry | Purpose |
| --- | --- |
| [01 Run FINDER](notebooks/01%20Run%20FINDER.ipynb) | Run the paper workflow after preparing inputs. Model calls are off by default. |
| [02 Review Results](notebooks/02%20Review%20Results.ipynb) | Read and verify the saved paper results. An optional cell evaluates a new run. |
| [prepare](notebooks/prepare/) | Four steps: market data, news, indicators and historical reference cases. Collection is off by default. |
| [benchmarks](notebooks/benchmarks/) | LSTM IND and LSTM NEWS. They read saved results by default; training is optional. |
| [figures](notebooks/figures/) | Rebuild the paper plots in `Figures/Generated`, leaving the saved figures unchanged. |

Edit [config/settings.json](config/settings.json) for the market, dates and run name. Use a new run name each time. The start and end dates select **information cutoffs**; each target is the next observed trading day. Set `execute_models` to `true` only when you intend to call the API from the notebook. Notebook credentials come from the environment or a local `.env` based on [.env.example](.env.example).

Raw prices, full news articles, reference banks, API keys and private run traces are not shipped. Use data you have permission to use. [config/paper.json](config/paper.json) lists the required input columns. The preparation notebooks write to `Data/`; the paper app checks which input files are missing before a run. Merely changing a market name does not create its inputs.

## What stays fixed in paper mode

Quantitative and Chart Experts work independently. The News Reader selects articles, the News Assistant summarizes their full text, and the News Expert assesses the summaries. The Decision Supervisor then combines the evidence, prior cases and available outcomes. There is no debate in this mode.

The model is `gpt-5-mini` for the five scored roles and `gpt-5-nano` for the News Assistant, with low reasoning effort. Each market window has 126 observations and 63 indicators; three charts use 21 fields. The reference cases precede the current window. Recent outcomes can enter memory only after their target date is available.

New paper runs write five scores to `scores.csv` and six role reports to `Reports/`. The Assistant's score is hidden. Scores measure risk intensity, not calibrated probabilities. Evaluation joins labels on the information cutoff, without another shift. Each agent's alert threshold starts at 0.5, then maximizes historical F1 using only outcomes available before the current forecast. Ties prefer the previous threshold, then the higher value. Evaluation also reports ranking metrics, classification metrics and confusion counts.

The paper's historical news availability remains exploratory. Successful code execution does not establish point-in-time news availability. A fresh model call can give a different reply; the supplied results remain the record of the paper experiment.

## Find the code

| Folder | Contents |
| --- | --- |
| [finder_core](finder_core/README.md) | Paper workflow. Start with `pipeline.py`, `workflow.py`, then `supervisor.py`. |
| [studio](studio/) | Local interface, custom runner and the paper adapter. No front-end build tool is needed. |
| [config](config/) | User settings, shared market settings and the paper input/output contract. |
| [notebooks](notebooks/) | Research steps, optional benchmarks and figures. |
| [tests](tests/) | Offline checks with temporary outputs and synthetic or already published inputs. |

Saved results stay in `FINDER/Official` and `Benchmark/Official`. `Examples` holds selected model reports. `FigureSources` holds the small public inputs used for figures; `Figures/Manuscript` holds saved figures. New research runs go to the ignored `Runs/` folder. The app never publishes a run.

## Checks and optional packages

```sh
python -m unittest discover -s tests -v
```

The tests do not call a paid API or overwrite paper results. They check context sharing, output validation, local request handling, the paper score fields and historical threshold calculations.

LSTM training needs [notebooks/benchmarks/requirements.txt](notebooks/benchmarks/requirements.txt). The figure environment is recorded in [notebooks/figures/requirements.txt](notebooks/figures/requirements.txt). Use separate environments for these optional tasks because their original package versions differ. Diagram rendering also needs `npm install` and `npx playwright install chromium`; news figure typesetting needs LaTeX with Palatino and T1 fonts.

The layout follows [Python's readability principles](https://peps.python.org/pep-0020/). The app uses [progressive disclosure](https://www.nngroup.com/articles/progressive-disclosure/), [visible status and error recovery](https://www.nngroup.com/articles/ten-usability-heuristics/), and [labelled form controls](https://www.w3.org/WAI/tutorials/forms/labels/). These guide the design; they are not a claim of a completed usability study.

## Attribution

Quantlet: FINDER-LLM. Existing project authors: Daniel Traian Pele, David Siang-Li Jheng, Rahul Tak and Stefan Găman.
