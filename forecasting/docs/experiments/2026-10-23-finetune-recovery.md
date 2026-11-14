# Fine-tuning failure and checkpoint recovery

The run `train-20261016-221747-ada5ecbd` (Kronos Base, 43 coins,
daily candles) finished 32 predictor epochs, selecting epoch 10, then failed
during validation forecasting with:

```
Kronos produced non-positive or non-finite predicted closes for FIL.
```

The traceback is in `.dashboard/jobs/train-20261016-221747-ada5ecbd.log`.
The failure occurred after 159 validation forecasts, before test evaluation.
Previously, the predictor was first saved only after validation, test and latest
forecasts, metrics and report generation all succeeded. Its selected weights
were held only in memory. No predictor checkpoint or backup was found for this
run, so those weights cannot be recovered. Tokenizer artifacts exist separately;
Windows denied read access during this investigation, preventing validation of
their contents.

## Durable training artifacts

Kronos now saves `predictor.pt` on every validation improvement, before progress
callbacks, and updates it after training finishes, before forecasting starts.
It contains the selected predictor, matched frozen tokenizer, constructors,
configuration, data signature, split/data provenance and training history.
`predictor_summary.json` provides a lightweight inventory view. Weights and
metadata are written and flushed to temporary files, then published by file
replacement. An interrupted write preserves the previous checkpoint; metadata
inside `predictor.pt` is authoritative if its summary publication is interrupted.

`training_complete` distinguishes finished training from interruption.
`evaluation_complete: false` means generated-price evaluation and residual
calibration have not completed. These checkpoints can be used for read-only
chart inference; the dashboard labels them **evaluation incomplete**. They do
not supply calibrated residual intervals or claim held-out scores. Successful
evaluation still publishes the existing `model.pt`, summary, forecasts and
report bundle. Normal training refuses to overwrite recovery weights unless
`--overwrite` is explicitly requested.

Invalid generated prices remain errors: they are not clamped, substituted or
silently excluded from evaluation. A forecasting or report failure now retains
the trained checkpoint and records its location in the job log. A failed job
still reports failure, so incomplete evaluation cannot look like success.

## Removing saved models

The training tab's **Saved models** list provides removal controls for saved
predictors, tokenizers and other run artifacts, including failed/stopped runs.
Removal requires confirmation and preserves job history. Original downloaded
models under `models/` cannot be removed through this control. Active training
and artifacts in use by another active job cannot be removed.

## Verification

Regression coverage trains real tiny Kronos modules and checks interruption,
validation/test/latest forecast errors, report failure, restoration of the best
epoch, matching tokenizer weights, failed atomic publication and read-only chart
inference. Worker tests distinguish saved weights from completed evaluation;
dashboard tests cover removal, original-model protection and path confinement.
No full-size retraining or removal of existing user models was performed.
