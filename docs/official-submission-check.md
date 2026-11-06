# Official submission review — 2026-11-06

Authoritative rules: https://builderr.ai/challenges/signalpost and https://builderr.ai/docs/signalpost-evaluation-harness.md . Official scoring is recall/coverage 50, precision/evidence 30, synthesis 12, UX 8. The local `score_competition_v3.py` is an optimization proxy and does not implement the official rubric. For each evidence family the official recall combines 70% company coverage and 30% fact coverage. A local smoke report is an execution check, not an official score.

No numeric HTTP or wall-time cap is published in that contract. Builderr supplies the official time/resource/network policy equally to entrants. Internal defaults are 20 HTTP attempts per company (2,000 for 100; 20,000 for 1,000) and half the batch size in provider credits (50 for 100; 500 for 1,000). They are not official limits. `--request-budget` and `--paid-search-budget` override them; use the official supplied values when provided.

Python 3.12+, install: `python -m pip install -r requirements-lock.txt` then `python -m pip install --no-deps -e .` . One evaluator run command after installation:

```sh
python run.py --manifest <official-batch.jsonl> --bulk <official-registry-snapshot.csv.gz> --out runs/official --workers 8
```

The optional `--bulk` parameter uses Builderr's frozen identity snapshot rather than silently selecting our own companies. Append `--request-budget <supplied-attempt-limit>` when the evaluator specifies it. The batch size is read from the manifest. Each company must emit one terminal envelope, including failures/missing evidence. Output is `runs/official/submission.jsonl`.

Secrets: the official contract specifies server-side secrets through documented environment variables only. The challenge page says credentials tied to a builder's own service account are not reproducible for evaluation. This public checkout contains no Serper payload or key. Organizer-managed `SERPER_API_KEY` is optional; without it public-source fallback runs with zero provider requests. User-local credential files are not submitted. Personal encoded keys must not be included in the official artifact.

Caches: public local-cache seed maps are included as discovery leads; capture, SQLite, provider response and NAV caches remain local/ignored. The smoke test reuses available local caches and bounded settings; its report declares that. The Jbox preview has a separate public 1,000-company feed and is a product showcase, not an official batch result.

Jbox entry: https://signalpost-builderr-sankeerth-preview.sites.jboxai.com/ . Its 1,000-company JSON feed returned HTTP 200 with cross-origin access during review. Full interactive/mobile checking of this deployed version was not performed in this review. The separate Jbox bonus accepts the site link by 2026-11-07 23:59 IST according to the challenge page.

Runtime models, dependency licences, provider cost assumptions and source rights are disclosed in [submission-dependencies.md](submission-dependencies.md). No separate repository source-code licence is declared; dependency licences are not a licence grant for our own code.
