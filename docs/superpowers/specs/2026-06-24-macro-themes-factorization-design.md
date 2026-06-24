# Factorisation des thèmes en grands thèmes (onboarding) — Design

**Goal:** Réduire le nombre de dossiers de 2ᵉ niveau produits par l'onboarding en
regroupant les thèmes fins détectés par la Vision en un petit nombre de **grands
thèmes** par section, piloté par l'option `granularity` existante.

**Architecture:** `propose_taxonomy` passe de **1 à 2 passes LLM**. La passe 1
(existante) assigne un DOMAINE (section de 1ᵉʳ niveau) à chaque cluster. Une passe 2
(nouvelle) regroupe, *à l'intérieur de chaque section*, les thèmes en grands thèmes
— en réutilisant le primitive d'assignation par lots avec matching **par numéro**.
Le grand thème devient le dossier de 2ᵉ niveau ; les thèmes fins y sont **absorbés**
(mappés via `theme_mapping`, sans créer de dossier).

**Tech Stack:** Python 3.13, SiliconFlow LLM (`Qwen/Qwen2.5-72B-Instruct` via
`get_agent_llm`), `with_structured_output(..., method="function_calling")`, Pydantic.

---

## 1. Problème

L'onboarding produit aujourd'hui une taxonomie `SECTION / Thème` où **chaque cluster
de thème devient son propre dossier de 2ᵉ niveau**. Sur le corpus de test (300
fichiers → ~355 clusters canonicaux répartis sur ~11 sections), cela donne 30+
sous-dossiers par section.

L'option `granularity: compact` n'aide pas : elle n'agit **que sur le nombre de
sections** (1ᵉʳ niveau, via le prompt `_assign_system`), pas sur le nombre de thèmes
par section. D'où le constat utilisateur : « en mode compacte, beaucoup trop de
folders ».

## 2. Décisions validées (brainstorming)

1. **Structure** : arbre à **2 niveaux**. Le grand thème devient le dossier de 2ᵉ
   niveau ; les thèmes fins sont **absorbés** (mappés dedans, pas de dossier). On
   réduit le nombre TOTAL de dossiers — c'est la « factorisation » au sens propre.
2. **Compacité** : pilotée par l'option `granularity` **existante** (compact / auto /
   detailed). Cible souple + plafond dur de sécurité. Aucun nouveau réglage UI.
3. **Calcul** : **2ᵉ passe LLM par section**, généralisant `_assign_sections`
   (assignation par lots, matching par numéro). Pas de dépendance embeddings.

## 3. Flux

```
clusters (canonicals, triés par volume décroissant)
  │
  ├─ PASSE 1  _assign_sections(llm, clusters, opts)   → {canonical: SECTION}   [inchangé]
  │
  ├─ grouper les clusters PAR section assignée
  │
  ├─ PASSE 2  pour CHAQUE section engorgée :
  │     _assign_macro_themes(llm, section_clusters, opts, low, high)
  │                                                   → {canonical: GRAND-THÈME} [nouveau]
  │     puis _enforce_cap(...) (collapse déterministe des plus petits → « Divers »)
  │
  └─ construire  SECTION / GrandThème  +  theme_mapping (raw → SECTION/GrandThème)
```

`macro_of` est calculé pour **tous** les clusters : grand thème pour les sections
regroupées, **propre canonical** pour les sections sautées / `detailed` (→
comportement actuel à l'identique).

## 4. Pilotage par granularité

Table déterministe (constante de module) :

```python
# granularity → règles de la passe 2 (None = passe 2 désactivée)
_GRANULARITY: dict[str, dict | None] = {
    "compact":  {"skip": 6,  "low": 3, "high": 6,  "cap": 8},
    "auto":     {"skip": 12, "low": 6, "high": 12, "cap": 16},
    "detailed": None,
}
_DIVERS = {"fr": "Divers", "en": "Misc", "auto": "Divers"}
```

| granularité | passe 2 déclenchée si la section a… | cible grands thèmes | plafond dur |
|---|---|---|---|
| **compact** | > 6 thèmes | 3–6 / section | 8 |
| **auto** | > 12 thèmes | 6–12 / section | 16 |
| **detailed** | jamais | — (1 dossier = 1 thème) | — |

Deux garde-fous **déterministes** (sans LLM) :

- **Skip** : une section avec ≤ `skip` thèmes garde ses thèmes individuels (on ne
  factorise que les sections engorgées). Résultat hétérogène et naturel.
- **Plafond (`_enforce_cap`)** — algorithme unique et sans ambiguïté (la même
  sémantique est reprise mot pour mot au §5.1) :
  1. Calculer le volume cumulé (`sum(count)`) de chaque grand thème distinct.
  2. **Si** le nombre de grands thèmes distincts est **≤ `cap`** → ne rien changer.
  3. **Sinon** : trier par `(-volume, nom)` (**tie-break déterministe et stable** sur
     égalités de volume), garder les **`cap-1` plus gros**, fusionner **tout le
     reste** dans la clé `_DIVERS[lang]`. Fusion **idempotente** : si le LLM a déjà
     produit un grand thème nommé « Divers », on réutilise cette clé (pas de
     doublon). Le résultat compte alors ≤ `cap` grands thèmes.
  4. **Garde-fou « Divers » dominant** : si, après fusion, le volume de « Divers »
     dépasse celui du plus gros grand thème conservé, c'est le signe que le LLM n'a
     pas regroupé. On **dégrade alors la section au grain fin** (comme un skip :
     chaque thème garde son propre canonical) plutôt que de présenter un fourre-tout
     dominant. Déterministe, conservateur, jamais pire que le comportement actuel.

## 5. Composants

### 5.1 `agents/onboarding/taxonomy_llm.py` (cœur)

**Réutilisation des modèles Pydantic existants.** La passe 2 réutilise
`_Assignments` / `_Assign` : le champ `section` porte le **nom du grand thème** (label
large générique). Pas de nouveau modèle (DRY + cohérence avec le pattern de test
existant qui construit `_Assign(index=…, section=…)`).

**Nouveau `_macro_system(opts, existing, low, high) -> str`** — prompt système de la
passe 2 (miroir de `_assign_system`) :

```python
def _macro_system(opts: dict[str, Any], existing: str, low: int, high: int) -> str:
    lang = {
        "fr": "Donne les noms de grands thèmes en FRANÇAIS.",
        "en": "Give broad-theme names in ENGLISH.",
        "auto": "Donne les noms dans la langue dominante du corpus.",
    }[opts["folder_language"]]
    return (
        "Tu regroupes des thèmes spécialisés en GRANDS THÈMES (sous-domaines larges). "
        f"Vise {low} à {high} grands thèmes au total. Chaque thème porte un NUMÉRO ; "
        "pour CHAQUE thème, renvoie son NUMÉRO (`index`) et le grand thème (`section`) "
        "auquel il appartient. RÉUTILISE en priorité un grand thème déjà créé : "
        f"{existing}. Crée un nouveau grand thème seulement si nécessaire. "
        "NE nomme PAS un grand thème comme la section elle-même ni « Général/Divers » "
        "(noms réservés). " + lang
        + " Assigne TOUS les thèmes, du premier au dernier."
    )
```

> Le nudge anti-collision du prompt est un confort ; le garde-fou **déterministe**
> (boucle de construction, ci-dessous) reste la vraie protection.

**Nouveau `_assign_macro_themes(llm, section_clusters, opts, low, high) -> dict[str, str]`**
— miroir exact de `_assign_sections`, scope = une section, vocabulaire de sortie =
grands thèmes locaux à la section (seedés entre lots via une **nouvelle locale**
`seen`, l'équivalent du `sections_seen` de `_assign_sections`). Lots de `_CHUNK`
(40). Matching par numéro. Retourne `{canonical: grand_thème}`.

**Nouveau `_enforce_cap(macro_map, section_clusters, cap, lang) -> dict[str, str]`** —
collapse déterministe, **strictement l'algorithme du §4** :

```python
def _enforce_cap(macro_map, section_clusters, cap, lang):
    vol: dict[str, int] = {}
    for c in section_clusters:
        m = macro_map.get(c["canonical"])
        if m:
            vol[m] = vol.get(m, 0) + int(c["count"])
    if len(vol) <= cap:
        return macro_map                                   # ≤ cap → rien à faire
    ordered = sorted(vol, key=lambda m: (-vol[m], m))      # tie-break (-volume, nom)
    keep = set(ordered[:cap - 1])
    divers = _DIVERS[lang]
    out = {c["canonical"]: (m if (m := macro_map.get(c["canonical"])) in keep
                            else divers)
           for c in section_clusters if macro_map.get(c["canonical"])}
    # garde-fou « Divers » dominant → dégrader la section au grain fin
    divers_vol = sum(int(c["count"]) for c in section_clusters
                     if out.get(c["canonical"]) == divers)
    top_vol = vol[ordered[0]]
    if divers_vol > top_vol:
        return {c["canonical"]: c["canonical"] for c in section_clusters}
    return out
```

**Modif `propose_taxonomy`** — orchestration des 2 passes :

```python
opts = _normalize_options(options)
if not clusters:
    return [_RESIDUAL], {}
assigned = _assign_sections(llm, clusters, opts)          # passe 1
if not assigned:
    return [_RESIDUAL], {}

# regrouper par section + passe 2
gran = _GRANULARITY.get(opts["granularity"])              # None si detailed
by_section: dict[str, list[dict]] = {}
for c in clusters:
    sec = assigned.get(c["canonical"])
    if sec:
        by_section.setdefault(sec, []).append(c)

macro_of: dict[str, str] = {}
for sec, sec_clusters in by_section.items():
    if gran is None or len(sec_clusters) <= gran["skip"]:
        for c in sec_clusters:                            # garde le grain fin
            macro_of[c["canonical"]] = c["canonical"]
        continue
    try:
        m = _assign_macro_themes(llm, sec_clusters, opts, gran["low"], gran["high"])
    except Exception as exc:                              # noqa: BLE001 — frontière LLM
        log.warning("propose_taxonomy: passe 2 échouée section %r: %s", sec, exc)
        m = {}
    m = _enforce_cap(m, sec_clusters, gran["cap"], opts["folder_language"])
    for c in sec_clusters:                                # trou d'index → grain fin
        macro_of[c["canonical"]] = m.get(c["canonical"]) or c["canonical"]
```

Le **reste de la boucle de construction est inchangé**, sauf la source du
sous-dossier et son garde-fou de collision. **Changement de régime important** : le
sous-dossier provient désormais d'un nom de **grand thème** (générique, donc bien plus
collisionnel avec le nom de section qu'un thème fin). Le fallback existant `sub →
« Général »` agrégerait alors **plusieurs grands thèmes distincts dans un même bucket
« Général » non plafonné** (refusion silencieuse). On modifie donc l'ordre de
fallback pour **préférer le grain fin** avant « Général » :

```python
# avant : sub = _fmt(c["canonical"], case, sep) ; if collision: sub = _fmt(_GENERAL[...])
sub = _fmt(macro_of.get(c["canonical"], c["canonical"]), case, sep)
def _collides(s: str) -> bool:
    return (not s) or s.upper() == sec_fmt.upper() or s.upper() in ("INBOX", "_INBOX")
if _collides(sub):
    sub = _fmt(c["canonical"], case, sep)          # 1) repli sur le thème fin (distinct)
    if _collides(sub):
        sub = _fmt(_GENERAL[opts["folder_language"]], case, sep)   # 2) dernier recours
```

Ainsi deux grands thèmes distincts qui se `_fmt`-eraient comme la section ne
fusionnent plus : chacun retombe sur **son** thème fin (distinct), pas sur un
« Général » partagé. Les autres garde-fous existants restent en place : `_fmt`
(préfixe numérique, casse, acronymes), rejet des noms de section réservés
(`INBOX`/`_INBOX`), `_A-TRIER` résiduel, fallback global si la passe 1 échoue partout.

> **Note importante** : `_assign_macro_themes` n'est appelé que pour les sections
> ayant > `skip` clusters. Comme `_assign_sections` reste appelé **une fois** par
> `propose_taxonomy` (au début), un même `llm` mocké renvoyant une seule
> `_Assignments` ne casse les tests existants que si la passe 2 se déclenche — ce
> qui n'arrive pas (leurs sections ont 1-5 clusters ≤ 12 en `auto`). Compatibilité
> ascendante garantie.

### 5.2 `agents/onboarding/proposition.py` (enrichissement catégories)

Les thèmes fins disparaissant en tant que dossiers, le dossier macro
« Machine-Learning » recevrait des mots-clés pauvres. On injecte les noms absorbés
dans le `rationale` envoyé à `categories_llm`.

**Modif `propose_categories`** — paramètre optionnel additif :

```python
def propose_categories(tree_folders: list[str],
                       folder_hints: dict[str, list[str]] | None = None) -> dict:
    ...
    # remplace la list-comprehension actuelle de `creations` par une boucle :
    creations: list[dict] = []
    for f in leaves:
        rationale = "dossier de la taxonomie d'onboarding"
        hints = (folder_hints or {}).get(f)
        if hints:
            rationale += " — regroupe : " + ", ".join(hints[:12])
        creations.append({"path": f, "rationale": rationale})
    ...
```

**Modif `build_proposal`** — calcule `folder_hints` depuis `mapping` + `clusters`
(pas de changement de signature de `propose_taxonomy`, qui reste `(folders, mapping)`).
⚠️ **Conserver le passage de `options`** (régression testée par
`test_build_proposal_passes_onboarding_options`) : `folder_hints` s'**ajoute** après,
on ne retire pas l'argument existant.

```python
options = _load_profile_cfg(profile).get("onboarding_options")   # LIGNE EXISTANTE — à conserver
tree, mapping = propose_taxonomy(get_agent_llm(), clusters, options)
folder_hints: dict[str, list[str]] = {}
for c in clusters:
    raws = c["raw_members"]
    folder = mapping.get(raws[0]) if raws else None   # None attendu si non mappé (orphelin/_INBOX) → hint ignoré
    if folder:
        folder_hints.setdefault(folder, []).append(c["canonical"])
cats = propose_categories(tree, folder_hints)
```

> Tous les `raw_members` d'un cluster mappent vers le **même** dossier (boucle de
> construction de `propose_taxonomy`), donc `mapping.get(raws[0])` est représentatif.
> Pour un bucket agrégé (« Divers » / « Général »), les hints sont volontairement
> hétérogènes — c'est un fourre-tout assumé, peu structurant pour les mots-clés, sans
> conséquence sur la couverture.

### 5.3 UI & docs

- `dashboard/templates/onboarding.html` — **pas de nouveau réglage**. Mettre à jour le
  texte d'aide de `granularity` : « compact regroupe les thèmes en grands thèmes ».
- `agents/CLAUDE.md` (section Agent Onboarding) + `dashboard/CLAUDE.md` — documenter le
  pipeline à 2 passes.

## 6. Gestion d'erreurs — dégradation gracieuse

| Cas | Comportement |
|---|---|
| Échec LLM passe 2 sur une section | Cette section retombe sur ses thèmes individuels (`m={}` → fallback grain fin). Isolation par section ; les autres ne sont pas affectées. Warning loggé. |
| Thème non assigné par la passe 2 (trou d'index) | Garde son propre canonical comme sous-dossier. Jamais droppé. |
| Plafond dépassé (> `cap` grands thèmes) | Collapse déterministe vers « Divers » (tri `(-volume, nom)`). Si « Divers » deviendrait dominant → la section est **dégradée au grain fin** (cf. §4). |
| Nom de grand thème == section / réservé / vide | **Repli sur le thème fin distinct** d'abord (évite la refusion de plusieurs macros dans un « Général » partagé non plafonné), « Général » seulement en dernier recours (cf. §5.1). |
| Passe 1 échoue partout | Fallback `[_A-TRIER]` existant (inchangé). |

**Invariant** : aucun thème Vision n'est jamais perdu — il route toujours un fichier,
au pire vers son propre dossier fin, au mieux vers son grand thème.

## 7. Tests (`tests/auto/test_agent_onboarding.py` — LLM **mocké**, jamais réel)

Nouveaux tests (classe `TestMacroThemeFactorization` ou extensions de
`TestProposeTaxonomy`) :

> **Patron de mock obligatoire** : surtout PAS de `side_effect` positionnel fixe. Les
> sections **skippées (≤ skip) ne consomment AUCUN `invoke`**, et l'ordre des `invoke`
> de passe 2 = ordre de première apparition des sections dans `clusters`. Un mock
> positionnel se désynchronise. Suivre le patron existant
> (`test_build_proposal_end_to_end`) : un `side_effect` callable qui **inspecte
> `messages[-1]["content"]`** (le payload) pour décider passe 1 vs passe 2 et
> identifier la section, puis renvoie l'`_Assignments` adéquate.

- **`_assign_macro_themes`** : matching par numéro ; réutilise les grands thèmes entre
  lots (locale `seen`) ; ignore index hors-borne / `section` vide.
- **propose_taxonomy compact, section engorgée** : une section de > 6 clusters →
  dossiers de 2ᵉ niveau ≤ `cap` ; `theme_mapping` route `raw → SECTION/GrandThème` ;
  profondeur 2. Mock inspectant le payload (cf. encadré).
- **propose_taxonomy compact, petite section (≤ 6)** : thèmes laissés individuels
  (skip), identiques à aujourd'hui ; assert **aucun** `invoke` de passe 2.
- **detailed** : passe 2 désactivée → sortie **strictement identique** au comportement
  actuel (1 dossier/thème). Assert qu'aucun appel macro n'est fait.
- **Anti-refusion « Général »** : une section où le LLM nomme **deux** grands thèmes
  distincts comme la section (→ collision) → les deux clusters retombent sur **leurs
  thèmes fins distincts**, PAS sur un « Général » partagé (asserter 2 dossiers
  distincts, pas 1 « Général » fusionné).
- **`_enforce_cap` — conservation** : > cap grands thèmes → collapse à ≤ cap avec
  « Divers » ; conserve les `cap-1` plus gros par volume.
- **`_enforce_cap` — tie-break déterministe** : sur **égalités de `count`** à la
  frontière du cap, l'appartenance est stable et reproductible (tri `(-volume, nom)`)
  — lancer 2× donne le même résultat.
- **`_enforce_cap` — « Divers » dominant** : si la queue collapsée dépasse le plus gros
  grand thème conservé → la section est **dégradée au grain fin** (assert : pas de
  dossier « Divers », chaque thème garde son canonical).
- **Échec passe 2 d'une section** : `invoke` lève → fallback gracieux, tous les thèmes
  de la section restent mappés (vers leur dossier fin).
- **Régression** : la suite `TestProposeTaxonomy` existante reste **verte** (sections
  petites en `auto` → passe 2 sautée).

Validation finale :
- `uv run python -m unittest tests.auto.test_agent_onboarding -v` → vert.
- `uv run ruff check agents/onboarding/ tests/auto/test_agent_onboarding.py` → clean.
- **Smoke live** sur `/private/tmp/klodo-onb-big` en `granularity=compact` : moyenne
  dossiers/section en forte baisse (objectif ~3-6), profondeur 2, couverture
  de classification ≥ avant la factorisation.

## 8. Hors périmètre (YAGNI)

- Réutilisation du primitive de regroupement pour la **refonte** d'une taxonomie
  existante ou la **déduplication des ~6 818 thèmes orphelins** (backlog séparé). Le
  primitive `_assign_macro_themes` est conçu pour être réutilisable, mais on ne câble
  rien hors onboarding ici.
- Pas d'arbre à 3 niveaux (SECTION/GrandThème/ThèmeFin) — explicitement écarté au
  profit de l'absorption.
- Pas de contrôle UI dédié au nombre de dossiers (réutilise `granularity`).

## 9. Risques / points de vigilance

- **Sur-fusion** : en `compact`, des thèmes distincts peuvent atterrir ensemble. Mitigé
  par : skip des petites sections, cible souple, et le grain fin conservé dans
  `theme_mapping` (le classement reste précis ; seul le rangement est plus large).
- **Coût LLM** : +1 appel texte par section engorgée (~5-15 appels, pas de Vision).
  Acceptable au bootstrap.
- **Qualité des noms de grands thèmes** : dépend du LLM. Cas dégénérés bornés
  **déterministe­ment** : collision macro==section → repli sur le thème fin distinct
  (jamais un « Général » partagé non plafonné) ; explosion du nombre de macros →
  `_enforce_cap` avec tie-break stable et garde-fou « Divers » dominant (dégradation
  au grain fin). Aucun de ces cas ne perd de thème ni ne crée de fourre-tout dominant.
