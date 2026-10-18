# Retrieval Evaluation

Retrieval quality is treated as an engineering metric. The evaluator is repository tooling, not part of the MemBus runtime API.

## Dataset

The baseline dataset is:

```text
evals/golden_v1.json
```

It covers:

- exact issue IDs
- code symbols
- file paths
- tool preferences
- deterministic aliases
- workspace/project/repo isolation
- type filtering
- incidents/procedures

It should grow from real retrieval failures.

## Isolation

`tools/evaluate.py` creates a temporary MemBus database, seeds fixture memories and aliases, runs queries, then removes the database. It never writes to the user's store.

## Metrics

- **Recall@5 / Recall@10** — expected memories retrieved in Top-K.
- **MRR** — rank of the first expected memory.
- **Zero-result rate** — queries returning nothing.
- **Context-preference error rate** — a lower-priority context result ranking ahead of an expected preferred-context result.
- **Latency P50 / P95** — in-process `MemBus.search` regression indicators.

## Run

```bash
python -m pip install -e '.[dev]'

python tools/evaluate.py evals/golden_v1.json
```

Apply CI-style gates:

```bash
python tools/evaluate.py evals/golden_v1.json \
  --min-recall-5 0.95 \
  --min-recall-10 0.95 \
  --max-context-preference-error-rate 0.0
```

A failed threshold exits with status 2.

## Regression policy

When a real query fails:

1. reduce it to the smallest representative fixture;
2. add stable expected/lower-priority keys;
3. prove the case fails before changing retrieval;
4. fix retrieval;
5. keep the regression permanently.

The purpose of the golden set is to make new complexity falsifiable. Query rewrite, reranking, embeddings, graph retrieval, or other machinery should not enter the runtime unless the dataset demonstrates a meaningful gain.
