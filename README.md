# Humatheque Alma Check API

FastAPI service for checking whether VLM-extracted academic-document metadata
already has a bibliographic record in the Humatheque Alma instance, and
whether that record describes a thesis or a dissertation.

The API targets a thesis and mémoire cataloguing pipeline. It runs several
recall-oriented SRU queries against the institutional Alma endpoint, and because
Alma SRU exposes no document-type predicate, it **deduces** the academic nature
of every hit from UNIMARC `328` and `608`. Holdings found in `930`, `995` and
`AVA` are returned with each candidate, and the Sudoc `PPN` carried in `035$a`
is reported as first-class information: it does not influence the match score,
but it is what decides what a cataloguer does with a hit.

The document-kind specifics (which deduced kinds count as a match) are bundled
into reusable `DocumentProfile` strategies, so the same pipeline can serve
theses, mémoires, and future document profiles without per-document forks.

## Alma SRU Strategy

The service uses the institutional Alma SRU endpoint:

```text
https://eu.alma.exlibrisgroup.com/view/sru/33CCP_INST
```

with `version=1.2`, `operation=searchRetrieve` and `recordSchema=unimarcxml`.
Records come back in the `info:srw/schema/8/unimarcxml-v0.1` namespace, so all
UNIMARC accessors are namespace-tolerant.

Only the three indexes published by the endpoint's explain response are used:

| Index | Purpose | Relation used |
|---|---|---|
| `alma.title` | title words | `all` |
| `alma.creator` | author words | `all` |
| `alma.all_for_ui` | generic keywords, used for defence context and fallback recall | `all` |

The `all` relation ANDs the words of the quoted term, which keeps recall high
without depending on phrase order.

There is **no** equivalent of Sudoc's `tdo=y`: nothing in the query restricts
results to academic writing. Precision is obtained after parsing, from the
record itself.

## Academic deduction

Every candidate is classified from its own UNIMARC content:

| `academic.kind` | Rule | `confidence` |
|---|---|---|
| `thesis` | `328` mentions `thèse`, `thesis`, `doctorat`, `dissertation`, `habilitation` | `1.0` |
| `dissertation` | `328` mentions `mémoire`, `master`, `maîtrise`, `DEA`, `DESS` | `1.0` |
| `academic_subject_only` | no usable `328`, but `608$a` contains `Thèses et écrits académiques` | `0.6` |
| `not_academic` | neither | `0.0` |

When a `328` note mixes both wordings, the degree type in `328$b` decides.
`academic_subject_only` cannot be narrowed to thesis vs mémoire, so it is
accepted by every profile and flagged with a lower confidence.

The matched wordings are echoed in `academic.evidence`, for example:

```json
["328$b: Thèse de doctorat", "608$a: Thèses et écrits académiques"]
```

## Document profiles

A `DocumentProfile` declares which deduced kinds count as a match:

| Profile | `accepted_kinds` | Use case |
|---|---|---|
| `thesis` | `thesis`, `academic_subject_only` | doctoral theses |
| `dissertation` | `dissertation`, `academic_subject_only` | master's mémoires and other dissertations |
| `academic` | `thesis`, `dissertation`, `academic_subject_only` | any academic writing (default) |

Candidates outside the active profile are still returned and scored, as
off-profile evidence. The active profile is selected by either the route
(`/check/thesis`, `/check/dissertation`, `/check/academic`) or the `profile`
field in the request body. Routes override the body so clients cannot
accidentally mix them.

`GET /profiles` returns the registry, so new profiles can be added in code and
discovered by clients.

## Items

Alma serves three holdings flavours side by side, all three are parsed into one
normalized shape (`field`, `library`, `institution`, `location`, `call_number`,
`barcode`, `status`, `availability`, `document_type`, `note`, plus the raw
subfields):

| Field | Origin | Notable subfields |
|---|---|---|
| `930` | Sudoc-style holding | `$b` library RCR, `$a` call number, `$e` collection, `$j` status |
| `995` | local item | `$B`/`$C` institution and library, `$K` call number, `$F` barcode, `$R` document type (e.g. `THESE`), `$O` circulation note |
| `AVA` | Alma availability | `$q` library, `$c` location, `$d` call number, `$e` availability, `$f`/`$g` copies |

Each candidate reports `items`, `items_count`, `has_items` and
`item_fields_present`.

## Scoring

`POST /check/*` runs the query ladder, merges records by Alma MMS ID, parses
UNIMARC XML, and scores candidates with deterministic components:

```text
final =
  0.40 * title
+ 0.24 * author
+ 0.22 * academic
+ 0.06 * year
+ 0.04 * language
+ 0.04 * context_or_advisor
```

`academic` is the deduction confidence above, so a perfect title and author
match on a record with no academic evidence cannot reach the match threshold.

Each candidate also carries `score.bibliographic`: the same score with the
academic component removed and renormalized, i.e. query proximity alone. It is
what off-profile candidates are judged on, since their academic component is `0`
by construction.

Weights are echoed in the response under `score_weights`.

Statuses:

| Status | Meaning |
|---|---|
| `academic_record_found` | an in-profile candidate reaches the match threshold |
| `ambiguous_academic_candidate` | an in-profile candidate exists but stays below the match threshold |
| `off_profile_match_only` | a strong bibliographic match exists, but it is not the academic kind this profile accepts |
| `no_academic_record_found` | no in-profile candidate close enough |

## PPN as first-class information

The Sudoc `PPN` is read from `035$a` (`(PPN)269767851`), falling back to the
`003` Sudoc URI, and reported with its origin in `ppn_source`. It is
deliberately **excluded from the score** and summarized at response top level:

```jsonc
"decision_support": {
  "best_candidate_ppn": "186020198",
  "best_candidate_has_ppn": true,
  "academic_candidates_with_ppn": 2,
  "academic_candidates_without_ppn": 0,
  "ppns": [
    {
      "ppn": "186020198",
      "mms_id": "991000917199705786",
      "sudoc_url": "https://www.sudoc.fr/186020198",
      "ppn_source": "035$a (PPN) prefix",
      "score": 0.9018,
      "academic_kind": "thesis",
      "items_count": 6
    }
  ]
}
```

## Endpoints

### `GET /health`

```json
{"ok": true}
```

### `GET /profiles`

Lists the registered document profiles and their accepted kinds.

### `GET /sru/search`

Debug endpoint for a raw Alma CQL query:

```bash
curl --get "http://localhost:8000/sru/search" \
  --data-urlencode 'query=alma.title all "septembre europeen" and alma.creator all "truc"'
```

### `POST /check/thesis`

Forces the `thesis` profile. Same request body for every `/check/*` route:

```bash
curl -X POST "http://localhost:8000/check/thesis" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Le 11-septembre européen",
    "subtitle": "la sensibilité morale des Européens à l'\''épreuve des attentats",
    "author": "Gérôme Truc",
    "degree_type": "Thèse de doctorat",
    "discipline": "Sociologie",
    "granting_institution": "EHESS",
    "defense_year": 2014,
    "language": "fre"
  }'
```

Response shape:

```jsonc
{
  "source": "alma_sru_academic_check",
  "profile": {
    "name": "thesis",
    "accepted_kinds": ["thesis", "academic_subject_only"]
  },
  "sru": {
    "endpoint": "https://eu.alma.exlibrisgroup.com/view/sru/33CCP_INST",
    "version": "1.2",
    "record_schema": "unimarcxml",
    "indexes": ["alma.title", "alma.creator", "alma.all_for_ui"],
    "queries": [/* per-query trace, with total_found and diagnostics */],
    "off_profile_candidates": 3
  },
  "status": "academic_record_found",
  "match_score": 0.9018,
  "decision_support": {/* see above */},
  "best_academic_candidate": {
    "mms_id": "991000917199705786",
    "ppn": "186020198",
    "has_ppn": true,
    "sudoc_url": "https://www.sudoc.fr/186020198",
    "title": "Le 11-septembre européen : la sensibilité morale des Européens…",
    "academic": {"is_academic": true, "kind": "thesis", "confidence": 1.0},
    "counts_for_profile": true,
    "items_count": 6,
    "item_fields_present": ["930", "995", "AVA"],
    "score": {"final": 0.9018, "bibliographic": 0.8741, "title": 0.8321, "author": 1.0}
  },
  "best_off_profile_candidate": null,
  "candidates": [/* all candidates, sorted by final score */]
}
```

### `POST /check/dissertation`

Forces the `dissertation` profile. Use it for master's mémoires:

```bash
curl -X POST "http://localhost:8000/check/dissertation" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "11-11 : Memories Retold",
    "subtitle": "l'\''art vidéoludique au service de la mémoire de la Grande Guerre",
    "author": "Pauline Morizot",
    "degree_type": "Mémoire de Master 2",
    "discipline": "Histoire",
    "granting_institution": "Paris 1",
    "defense_year": 2019,
    "language": "fre"
  }'
```

### `POST /check/academic`

Forces the `academic` profile: any thesis or dissertation counts.

## Run

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload
```

With `uv` from this workspace:

```bash
uv run uvicorn app:app --reload
```

## Tests

`test_app.py` checks the parsing, the academic deduction, the items mapping and
the query building against an offline UNIMARC fixture — no network:

```bash
uv run --with-requirements requirements.txt python test_app.py
```

## Authentication

Authentication is optional. Set `ALMA_API_KEY` or `API_KEY`; clients must then
send:

```text
X-API-Key: <key>
```
