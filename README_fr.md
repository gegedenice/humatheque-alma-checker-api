# Humatheque Alma Check API

Service FastAPI permettant de vérifier si des métadonnées de thèse ou de
mémoire extraites par VLM correspondent déjà à une notice bibliographique de
l'instance Alma de l'Humathèque, et si cette notice décrit bien une thèse ou un
mémoire.

L'API est conçue pour une chaîne de catalogage de thèses et de mémoires. Elle
exécute plusieurs requêtes SRU orientées rappel sur le point d'accès Alma de
l'établissement et, comme Alma SRU n'offre aucun filtre de type de document,
elle **déduit** le caractère universitaire de chaque notice à partir des zones
UNIMARC `328` et `608`. Les exemplaires trouvés en `930`, `995` et `AVA` sont
restitués avec chaque candidat, et le `PPN` Sudoc présent en `035$a` est traité
comme une information de premier plan : il n'entre pas dans le score, mais c'est
lui qui détermine ce que le catalogueur fera de la notice trouvée.

Les spécificités par type de document (quelles déductions comptent comme
correspondance) sont regroupées dans des stratégies `DocumentProfile`
réutilisables.

## Stratégie SRU Alma

Le service utilise le point d'accès SRU institutionnel :

```text
https://eu.alma.exlibrisgroup.com/view/sru/33CCP_INST
```

avec `version=1.2`, `operation=searchRetrieve` et `recordSchema=unimarcxml`. Les
notices sont renvoyées dans l'espace de noms
`info:srw/schema/8/unimarcxml-v0.1` ; l'analyse UNIMARC en tient compte.

Seuls les trois index publiés par l'explain du point d'accès sont utilisés :

| Index | Usage | Relation |
|---|---|---|
| `alma.title` | mots du titre | `all` |
| `alma.creator` | mots de l'auteur | `all` |
| `alma.all_for_ui` | mots-clés génériques : contexte de soutenance et rappel de secours | `all` |

La relation `all` combine en ET les mots du terme entre guillemets, ce qui
préserve le rappel sans dépendre de l'ordre des mots.

Il n'existe **pas** d'équivalent du `tdo=y` du Sudoc : rien dans la requête ne
restreint les résultats aux écrits académiques. La précision est obtenue après
analyse, à partir de la notice elle-même.

## Déduction du caractère universitaire

Chaque candidat est classé à partir de son propre contenu UNIMARC :

| `academic.kind` | Règle | `confidence` |
|---|---|---|
| `thesis` | la zone `328` mentionne `thèse`, `thesis`, `doctorat`, `dissertation`, `habilitation` | `1.0` |
| `dissertation` | la zone `328` mentionne `mémoire`, `master`, `maîtrise`, `DEA`, `DESS` | `1.0` |
| `academic_subject_only` | pas de `328` exploitable, mais `608$a` contient `Thèses et écrits académiques` | `0.6` |
| `not_academic` | aucun des deux | `0.0` |

Si la note de thèse mélange les deux formulations, c'est le type de diplôme en
`328$b` qui tranche. Le cas `academic_subject_only` ne permet pas de distinguer
thèse et mémoire : il est accepté par tous les profils, avec une confiance
moindre.

Les formulations reconnues sont restituées dans `academic.evidence`, par
exemple :

```json
["328$b: Thèse de doctorat", "608$a: Thèses et écrits académiques"]
```

## Profils de document

| Profil | `accepted_kinds` | Cas d'usage |
|---|---|---|
| `thesis` | `thesis`, `academic_subject_only` | thèses de doctorat |
| `dissertation` | `dissertation`, `academic_subject_only` | mémoires de master et autres mémoires |
| `academic` | `thesis`, `dissertation`, `academic_subject_only` | tout écrit académique (défaut) |

Les candidats hors profil sont tout de même renvoyés et scorés, à titre
d'indice. Le profil actif est choisi par la route (`/check/thesis`,
`/check/dissertation`, `/check/academic`) ou par le champ `profile` du corps de
requête, la route ayant priorité.

`GET /profiles` retourne le registre des profils.

## Exemplaires

Alma expose trois formes d'exemplaires, toutes ramenées à une structure
normalisée (`field`, `library`, `institution`, `location`, `call_number`,
`barcode`, `status`, `availability`, `document_type`, `note`, plus les
sous-zones brutes) :

| Zone | Origine | Sous-zones notables |
|---|---|---|
| `930` | exemplaire de type Sudoc | `$b` RCR, `$a` cote, `$e` fonds, `$j` statut |
| `995` | exemplaire local | `$B`/`$C` établissement et bibliothèque, `$K` cote, `$F` code-barres, `$R` type de document (ex. `THESE`), `$O` note de circulation |
| `AVA` | disponibilité Alma | `$q` bibliothèque, `$c` localisation, `$d` cote, `$e` disponibilité, `$f`/`$g` exemplaires |

Chaque candidat expose `items`, `items_count`, `has_items` et
`item_fields_present`.

## Calcul du score

`POST /check/*` exécute l'échelle de requêtes, fusionne les notices par MMS ID
Alma, analyse l'UNIMARC XML et score les candidats :

```text
final =
  0.40 * title
+ 0.24 * author
+ 0.22 * academic
+ 0.06 * year
+ 0.04 * language
+ 0.04 * context_or_advisor
```

`academic` est la confiance de la déduction ci-dessus : une correspondance
parfaite de titre et d'auteur sur une notice sans indice universitaire ne peut
donc pas atteindre le seuil de correspondance.

Chaque candidat porte aussi `score.bibliographic` : le même score sans la
composante universitaire, renormalisé, c'est-à-dire la seule proximité à la
requête. C'est sur cette valeur que sont jugés les candidats hors profil, dont
la composante universitaire vaut `0` par construction.

Les pondérations sont visibles dans la réponse sous `score_weights`.

Statuts :

| Statut | Signification |
|---|---|
| `academic_record_found` | un candidat du profil atteint le seuil de correspondance |
| `ambiguous_academic_candidate` | un candidat du profil existe, mais sous le seuil |
| `off_profile_match_only` | une correspondance bibliographique forte existe, mais pas du type universitaire attendu |
| `no_academic_record_found` | aucun candidat du profil suffisamment proche |

## Le PPN comme information de premier plan

Le `PPN` Sudoc est lu en `035$a` (`(PPN)269767851`), à défaut dans l'URI Sudoc de
la zone `003`, et restitué avec son origine dans `ppn_source`. Il est
volontairement **exclu du score** et récapitulé à la racine de la réponse :

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

### `GET /sru/search`

Endpoint de debug pour une requête CQL Alma brute :

```bash
curl --get "http://localhost:8000/sru/search" \
  --data-urlencode 'query=alma.title all "septembre europeen" and alma.creator all "truc"'
```

### `POST /check/thesis`

Exemple :

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

Forme de réponse :

```jsonc
{
  "source": "alma_sru_academic_check",
  "status": "academic_record_found",
  "match_score": 0.9018,
  "decision_support": {"best_candidate_ppn": "186020198", "best_candidate_has_ppn": true},
  "best_academic_candidate": {
    "mms_id": "991000917199705786",
    "ppn": "186020198",
    "academic": {"kind": "thesis", "confidence": 1.0},
    "counts_for_profile": true,
    "items_count": 6,
    "item_fields_present": ["930", "995", "AVA"],
    "score": {"final": 0.9018, "bibliographic": 0.8741}
  },
  "best_off_profile_candidate": null,
  "candidates": []
}
```

### `POST /check/dissertation`

Force le profil `dissertation`, pour les mémoires de master.

### `POST /check/academic`

Force le profil `academic` : thèse ou mémoire, indifféremment.

## Lancer le service

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload
```

Avec `uv` depuis cet espace de travail :

```bash
uv run uvicorn app:app --reload
```

## Tests

```bash
uv run --with-requirements requirements.txt python test_app.py
```

## Authentification

L'authentification est optionnelle. Définissez `ALMA_API_KEY` ou `API_KEY` ;
les clients devront alors envoyer :

```text
X-API-Key: <clé>
```
