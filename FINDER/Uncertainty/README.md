# Ranking confidence intervals

`metrics_ci.csv` and `paired_differences_ci.csv` contain the saved results from `02 Review Results.ipynb`. The inputs are the daily scores and event labels in `FINDER/Official` and the aligned LSTM probabilities in `Benchmark/Official`.

The notebook uses 5,000 stationary-bootstrap draws with mean block length 10 observations on the union of market target dates and seed 20261027. Each draw shares date indices across all markets, labels and models. Market holidays remain missing. Pointwise 95% percentile intervals use the 2.5th and 97.5th percentiles. Paired differences are Decision Supervisor minus comparator. Equal-weight summaries average the six market metrics within each draw.

Draws lacking either label class are omitted for that market and for the equal-weight average; valid and invalid counts are included in both files. Aggregate `event_rate` is the mean of the six market prevalences; aggregate `n` and `events` are left blank. Intervals condition on the saved predictions and exclude training and LLM-generation variability. They are not adjusted for multiple comparisons.

Rerunning the notebook writes fresh tables under `Results/Confidence Intervals/`. Figure 8 in `notebooks/figures/Comparison.ipynb` uses those local tables when available and otherwise uses the saved table here. Its interval bars have opacity 0.4; score markers retain full opacity.
