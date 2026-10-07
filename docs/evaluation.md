# Retrieval Evaluation

MemBus treats retrieval quality as an engineering metric, not a subjective impression. Any proposal to add query rewriting, a reranker, embeddings, a graph index, or another retrieval subsystem should be evaluated against a versioned golden dataset.

## Current baseline

The initial dataset is `evals/golden_v1.json`.

It covers representative coding-memory behavior:

- exact issue IDs
- code symbols
- file paths
- package/tool preferences
- aliases such as `pg -> postgresql`
- project and workspace isolation
- repository procedures
- type filtering
- infrastructure incidents

The dataset is intentionally small at first. It should grow from real retrieval failures rather than synthetic feature checklists.

## Isolation

Evaluation always creates a temporary SQLite database, seeds the fixture memories and aliases, runs the queries, then removes the database. It never evaluates by writing into the user's canonical MemBus store.

## Metrics

### Recall@5 / Recall@10

For each query, recall is the fraction of explicitly expected memories retrieved in Top-K. This is the primary signal for deciding whether recall needs a more complex retriever.

### MRR

Mean Reciprocal Rank measures how early the first expected memory appears: rank #1 = 1.0, rank #2 = 0.5, rank #5 = 0.2, missing = 0.

### Precision@5

Measures the fraction of Top-5 results that are explicitly expected relevant items in the golden annotation. Because many current cases have only one expected result, this metric is less important than Recall and MRR in the initial dataset.

### Zero-result rate

The fraction of golden queries returning no result.

### Wrong-scope hit rate

Golden cases can name `forbidden` memories. This detects scope leakage—for example, a query in `workspace=work / project=payment` must not return the same-named project from `workspace=personal`.

### Latency P50 / P95

Measured around the in-process service search call in the isolated evaluator. These are regression indicators rather than cross-machine benchmark claims.

## Run locally

Install development dependencies:

```bash
python -m pip install -e '.[dev]'
```

Run the report:

```bash
membus eval evals/golden_v1.json
```

Apply quality gates:

```bash
membus eval evals/golden_v1.json \
  --min-recall-5 0.95 \
  --min-recall-10 0.95 \
  --max-wrong-scope-rate 0.0
```

A threshold failure exits with status code 2.

## CI policy

CI runs both `pytest` and a separate golden retrieval gate. The golden command is intentionally visible as its own CI step so metric changes can be inspected independently from unit tests.

## How to add a regression case

When a real query fails:

1. Reduce it to the smallest representative memory/query pair.
2. Add the memory to `memories` with a stable fixture `key`.
3. Add the query to `queries`.
4. Reference the expected key(s).
5. If the failure involved scope leakage, add `forbidden` keys.
6. Run the existing retriever before changing code and confirm the new test actually reproduces the failure.
7. Fix retrieval.
8. Keep the case permanently.

A golden set that only contains queries the current system already handles is not useful.

## Complexity gate example

Suppose the dataset eventually contains 200 real queries:

```text
lexical-only Recall@10:          0.80
+ deterministic aliases:        0.84
+ multi-query rewrite:           0.91
+ embedding side index:          0.975
```

Then the vector index has a measurable reason to exist.

If lexical/query rewrite already reaches 0.96 Recall@10 and hybrid vector retrieval reaches only 0.967, the extra model/index lifecycle may not justify the improvement.

## Dataset versioning

Do not silently rewrite historical expectations merely to make a new algorithm look better. If the semantic definition of relevance changes materially, document the reason in the commit and, when appropriate, create a new dataset version.

The purpose of the golden set is to make architecture changes falsifiable.
