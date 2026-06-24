# Factorisation des thèmes en grands thèmes (onboarding) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Réduire le nombre de dossiers de 2ᵉ niveau produits par l'onboarding en regroupant les thèmes fins (Vision) en un petit nombre de **grands thèmes** par section, via une 2ᵉ passe LLM pilotée par l'option `granularity` existante.

**Architecture:** `propose_taxonomy` (dans `agents/onboarding/taxonomy_llm.py`) passe de 1 à 2 passes LLM. La passe 1 (`_assign_sections`, inchangée) assigne un DOMAINE à chaque cluster. Une passe 2 (`_assign_macro_themes`, nouvelle) regroupe *par section* les thèmes en grands thèmes, avec matching **par numéro**. Garde-fous déterministes : skip des petites sections, `_enforce_cap` (tie-break stable + anti-« Divers » dominant), repli grain-fin avant « Général ». Le grand thème devient le dossier de 2ᵉ niveau ; les thèmes fins y sont **absorbés** (mappés via `theme_mapping`, sans dossier).

**Tech Stack:** Python 3.13 (`uv`), SiliconFlow LLM (`Qwen/Qwen2.5-72B-Instruct` via `get_agent_llm`), `with_structured_output(..., method="function_calling")`, Pydantic, `unittest` + `mock`, `ruff`.

**Spec:** `docs/superpowers/specs/2026-06-24-macro-themes-factorization-design.md`
**Branche:** `feature/onboarding-macro-themes` (déjà créée, spec committée).

---

## Contexte de code (à connaître avant de commencer)

Fichier cœur `agents/onboarding/taxonomy_llm.py` (état actuel) :
- Constantes : `_RESIDUAL = "_A-TRIER"`, `_CHUNK = 40`, `_GENERAL = {"fr": "Général", "en": "General", "auto": "Général"}`.
- `DEFAULT_OPTIONS` contient `granularity` (`auto | compact | detailed`), `folder_language`, `folder_case`, `word_separator`, `numbered_sections`, `min_depth`, `max_depth`.
- `_normalize_options(options)` borne déjà `granularity` (les valeurs invalides retombent sur `auto`).
- Modèles Pydantic : `class _Assign(BaseModel): index: int; section: str` et `class _Assignments(BaseModel): items: list[_Assign]`.
- `_assign_sections(llm, clusters, opts) -> {canonical: section}` : appelle `llm.with_structured_output(_Assignments, method="function_calling").invoke([{"role":"system","content": _assign_system(...)}, {"role":"user","content": f"Thèmes à classer :\n{payload}"}])`, où `payload = "\n".join(f"{i+1}. {c['canonical']} (volume {c['count']})" ...)`. Matching par `a.index`.
- `_assign_system(opts, existing)` contient le mot **« DOMAINES »** (sert à distinguer la passe 1 dans les mocks).
- `_fmt(name, case, sep)` : formate un nom de dossier (retire préfixe numérique, casse, FS-safe).
- `propose_taxonomy(llm, clusters, options=None) -> (sorted(folders), mapping)` : appelle `_assign_sections`, puis construit `SECTION/Thème` (le sous-dossier vient de `c["canonical"]`).

Un `cluster` est un dict `{"canonical": str, "canonical_forms": list, "raw_members": list[str], "count": int}`. **Tous les `raw_members` d'un cluster mappent vers le même dossier.**

Tests : `tests/auto/test_agent_onboarding.py`. Classe `TestProposeTaxonomy` (helper `_llm(clusters, sec_map)` → `invoke.return_value` fixe). Classe `TestBuildProposal` (setUp crée un profil tmp ; `test_build_proposal_end_to_end` montre le mock `fake_invoke(messages)` parsant `messages[-1]["content"]`).

**Conventions projet** : jamais de Vision/LLM réel en test (mocker). Type hints modernes (`X | None`, `list[str]`). Lancer via `uv`. Commits en anglais, trailer `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## File Structure

| Fichier | Responsabilité | Tâches |
|---|---|---|
| `agents/onboarding/taxonomy_llm.py` | Cœur : constantes `_GRANULARITY`/`_DIVERS`, `_macro_system`, `_collides`, `_assign_macro_themes`, `_enforce_cap`, orchestration `propose_taxonomy` 2 passes | 1, 2, 3, 4 |
| `agents/onboarding/proposition.py` | `propose_categories(tree, folder_hints)` + `build_proposal` calcule `folder_hints` | 5 |
| `tests/auto/test_agent_onboarding.py` | Nouvelle classe `TestMacroThemeFactorization` + extensions `TestProposeCategories`/`TestBuildProposal` | 1-5 |
| `dashboard/templates/onboarding.html` | Texte d'aide `granularity` | 6 |
| `agents/CLAUDE.md`, `dashboard/CLAUDE.md` | Doc pipeline 2 passes | 6 |

---

## Task 1 : constantes + `_macro_system` + `_collides`

**Files:**
- Modify: `agents/onboarding/taxonomy_llm.py` (ajouts près des constantes + nouveaux helpers)
- Test: `tests/auto/test_agent_onboarding.py` (nouvelle classe `TestMacroThemeFactorization`)

- [ ] **Step 1 : Écrire les tests qui échouent**

Ajouter en fin de fichier de test (avant `if __name__`), une nouvelle classe :

```python
class TestMacroThemeFactorization(unittest.TestCase):
    def test_macro_system_reflects_target_and_language(self):
        from agents.onboarding import taxonomy_llm as t
        s = t._macro_system(t._normalize_options({"folder_language": "fr"}),
                            "Machine Learning, Bases de Données", low=3, high=6)
        self.assertIn("3", s)
        self.assertIn("6", s)
        self.assertIn("GRANDS THÈMES", s)               # distingue la passe 2 dans les mocks
        self.assertIn("FRANÇAIS", s.upper())
        self.assertIn("Machine Learning", s)            # grands thèmes déjà créés réinjectés
        self.assertIn("réservés", s.lower())            # règle anti-collision (section/Général/Divers)

    def test_collides_detects_section_and_reserved(self):
        from agents.onboarding import taxonomy_llm as t
        self.assertTrue(t._collides("", "Sciences"))
        self.assertTrue(t._collides("Sciences", "Sciences"))
        self.assertTrue(t._collides("sciences", "SCIENCES"))   # insensible à la casse
        self.assertTrue(t._collides("INBOX", "Sciences"))
        self.assertFalse(t._collides("Astrophysique", "Sciences"))

    def test_granularity_table_values(self):
        from agents.onboarding import taxonomy_llm as t
        self.assertIsNone(t._GRANULARITY["detailed"])
        self.assertEqual(t._GRANULARITY["compact"], {"skip": 6, "low": 3, "high": 6, "cap": 8})
        self.assertEqual(t._GRANULARITY["auto"], {"skip": 12, "low": 6, "high": 12, "cap": 16})
        self.assertEqual(t._DIVERS["fr"], "Divers")
```

- [ ] **Step 2 : Lancer pour vérifier l'échec**

Run : `uv run python -m unittest tests.auto.test_agent_onboarding.TestMacroThemeFactorization -v`
Expected : FAIL — `AttributeError: module ... has no attribute '_macro_system'` (et `_collides`, `_GRANULARITY`, `_DIVERS`).

- [ ] **Step 3 : Implémenter**

Dans `agents/onboarding/taxonomy_llm.py`, sous la ligne `_GENERAL = {...}` (≈ ligne 23), ajouter :

```python
_DIVERS = {"fr": "Divers", "en": "Misc", "auto": "Divers"}
# granularity → règles de la passe 2 (None = passe 2 désactivée)
_GRANULARITY: dict[str, dict | None] = {
    "compact":  {"skip": 6,  "low": 3, "high": 6,  "cap": 8},
    "auto":     {"skip": 12, "low": 6, "high": 12, "cap": 16},
    "detailed": None,
}


def _collides(sub: str, sec_fmt: str) -> bool:
    """True si `sub` est vide, identique à la section, ou un nom réservé."""
    return (not sub) or sub.upper() == sec_fmt.upper() or sub.upper() in ("INBOX", "_INBOX")


def _macro_system(opts: dict[str, Any], existing: str, low: int, high: int) -> str:
    """Prompt système de la passe 2 (regroupement en grands thèmes). Miroir de
    `_assign_system`. Contient « GRANDS THÈMES » pour être distingué de la passe 1.
    """
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

- [ ] **Step 4 : Lancer pour vérifier le succès**

Run : `uv run python -m unittest tests.auto.test_agent_onboarding.TestMacroThemeFactorization -v`
Expected : PASS (3 tests).

- [ ] **Step 5 : Commit**

```bash
git add agents/onboarding/taxonomy_llm.py tests/auto/test_agent_onboarding.py
git commit -m "feat(onboarding): constantes _GRANULARITY/_DIVERS + _macro_system + _collides

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2 : `_assign_macro_themes` (passe 2, par section)

**Files:**
- Modify: `agents/onboarding/taxonomy_llm.py`
- Test: `tests/auto/test_agent_onboarding.py` (`TestMacroThemeFactorization`)

- [ ] **Step 1 : Écrire les tests qui échouent**

Ajouter dans `TestMacroThemeFactorization`. D'abord un helper de mock **conscient des passes** (parse le payload, lève optionnellement en passe 2) :

```python
    def _llm(self, sec_map, macro_map=None, fail_pass2=False):
        """Mock LLM conscient des passes : inspecte le payload (jamais positionnel).
        sec_map  : {canonical: section}  (passe 1)
        macro_map: {canonical: grand_thème} (passe 2)
        """
        import re
        from agents.onboarding import taxonomy_llm as t

        def _invoke(messages):
            system = messages[0]["content"]
            user = messages[-1]["content"]
            is_macro = "GRANDS THÈMES" in system
            if is_macro and fail_pass2:
                raise RuntimeError("403 passe 2")
            items = []
            for line in user.splitlines():
                m = re.match(r"\s*(\d+)\.\s+(.*?)\s+\(volume", line)
                if not m:
                    continue
                idx, name = int(m.group(1)), m.group(2)
                label = (macro_map or {}).get(name) if is_macro else sec_map.get(name)
                if label:
                    items.append(t._Assign(index=idx, section=label))
            return t._Assignments(items=items)

        llm = mock.Mock()
        llm.with_structured_output.return_value.invoke.side_effect = _invoke
        return llm

    def test_assign_macro_themes_index_matching(self):
        from agents.onboarding import taxonomy_llm as t
        sec = [{"canonical": "deep learning", "raw_members": ["DL"], "count": 5},
               {"canonical": "sql", "raw_members": ["SQL"], "count": 3}]
        llm = self._llm({}, macro_map={"deep learning": "Machine Learning", "sql": "Bases de Données"})
        out = t._assign_macro_themes(llm, sec, t._normalize_options(None), low=3, high=6)
        self.assertEqual(out["deep learning"], "Machine Learning")
        self.assertEqual(out["sql"], "Bases de Données")

    def test_assign_macro_themes_ignores_out_of_range_and_empty(self):
        from agents.onboarding import taxonomy_llm as t
        sec = [{"canonical": "a", "raw_members": ["A"], "count": 1}]
        llm = mock.Mock()
        llm.with_structured_output.return_value.invoke.return_value = t._Assignments(items=[
            t._Assign(index=99, section="X"),      # hors-borne → ignoré
            t._Assign(index=1, section="   "),     # vide → ignoré
        ])
        out = t._assign_macro_themes(llm, sec, t._normalize_options(None), low=3, high=6)
        self.assertEqual(out, {})

    def test_assign_macro_themes_reuses_macros_across_batches(self):
        # > _CHUNK clusters → 2 lots ; le 2e lot doit voir les grands thèmes du 1er
        from agents.onboarding import taxonomy_llm as t
        sec = [{"canonical": f"t{i}", "raw_members": [f"T{i}"], "count": 1} for i in range(t._CHUNK + 5)]
        seen_existing = []

        def _invoke(messages):
            seen_existing.append(messages[0]["content"])
            import re
            items = [t._Assign(index=int(n), section="Macro")
                     for n in re.findall(r"^\s*(\d+)\. ", messages[-1]["content"], re.M)]
            return t._Assignments(items=items)

        llm = mock.Mock()
        llm.with_structured_output.return_value.invoke.side_effect = _invoke
        out = t._assign_macro_themes(llm, sec, t._normalize_options(None), low=3, high=6)
        self.assertEqual(len(out), t._CHUNK + 5)               # tous assignés
        self.assertIn("Macro", seen_existing[1])               # 2e lot voit le grand thème du 1er
```

- [ ] **Step 2 : Lancer pour vérifier l'échec**

Run : `uv run python -m unittest tests.auto.test_agent_onboarding.TestMacroThemeFactorization -v`
Expected : FAIL — `AttributeError: ... '_assign_macro_themes'`.

- [ ] **Step 3 : Implémenter**

Dans `agents/onboarding/taxonomy_llm.py`, après `_assign_sections` (≈ ligne 129), ajouter :

```python
def _assign_macro_themes(llm: Any, section_clusters: list[dict], opts: dict[str, Any],
                         low: int, high: int) -> dict[str, str]:
    """Regroupe les clusters d'UNE section en grands thèmes. Par lots, matching par
    NUMÉRO (le LLM reformule la canonical). Réutilise les grands thèmes entre lots.
    Retourne {canonical: grand_thème}.
    """
    structured = llm.with_structured_output(_Assignments, method="function_calling")
    assigned: dict[str, str] = {}
    seen: list[str] = []
    for start in range(0, len(section_clusters), _CHUNK):
        batch = section_clusters[start:start + _CHUNK]
        existing = ", ".join(seen[:60]) or "(aucun encore — crée les premiers)"
        payload = "\n".join(f"{i + 1}. {c['canonical']} (volume {c['count']})"
                            for i, c in enumerate(batch))
        try:
            res = structured.invoke(
                [{"role": "system", "content": _macro_system(opts, existing, low, high)},
                 {"role": "user", "content": f"Thèmes à classer :\n{payload}"}])
        except Exception as exc:  # noqa: BLE001 — frontière LLM
            log.warning("assign_macro_themes: lot %d échoué: %s", start // _CHUNK, exc)
            continue
        for a in res.items:
            idx = a.index - 1
            if not (0 <= idx < len(batch)):
                continue
            macro = (a.section or "").strip()
            if not macro:
                continue
            assigned[batch[idx]["canonical"]] = macro
            if macro not in seen:
                seen.append(macro)
    return assigned
```

- [ ] **Step 4 : Lancer pour vérifier le succès**

Run : `uv run python -m unittest tests.auto.test_agent_onboarding.TestMacroThemeFactorization -v`
Expected : PASS (tous les tests de la classe).

- [ ] **Step 5 : Commit**

```bash
git add agents/onboarding/taxonomy_llm.py tests/auto/test_agent_onboarding.py
git commit -m "feat(onboarding): _assign_macro_themes — passe 2 de regroupement par section

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3 : `_enforce_cap` (plafond déterministe)

**Files:**
- Modify: `agents/onboarding/taxonomy_llm.py`
- Test: `tests/auto/test_agent_onboarding.py` (`TestMacroThemeFactorization`)

- [ ] **Step 1 : Écrire les tests qui échouent**

Helper local pour fabriquer des clusters + ajouter 4 tests dans `TestMacroThemeFactorization` :

```python
    @staticmethod
    def _cl(name, count):
        return {"canonical": name, "raw_members": [name.upper()], "count": count}

    def test_enforce_cap_under_cap_unchanged(self):
        from agents.onboarding import taxonomy_llm as t
        cl = [self._cl("a", 1), self._cl("b", 1), self._cl("c", 1)]
        macro = {"a": "A", "b": "B", "c": "C"}
        self.assertEqual(t._enforce_cap(macro, cl, cap=8, lang="fr"), macro)

    def test_enforce_cap_collapses_keeping_biggest(self):
        from agents.onboarding import taxonomy_llm as t
        cl = [self._cl("big1", 100), self._cl("big2", 90),
              self._cl("small1", 5), self._cl("small2", 4)]
        macro = {"big1": "Alpha", "big2": "Beta", "small1": "Gamma", "small2": "Delta"}
        out = t._enforce_cap(macro, cl, cap=3, lang="fr")   # garde cap-1=2 plus gros
        self.assertEqual(out["big1"], "Alpha")
        self.assertEqual(out["big2"], "Beta")
        self.assertEqual(out["small1"], "Divers")
        self.assertEqual(out["small2"], "Divers")
        self.assertLessEqual(len(set(out.values())), 3)

    def test_enforce_cap_deterministic_tiebreak(self):
        from agents.onboarding import taxonomy_llm as t
        # 1 gros + 3 ex-aequo ; cap=3 → garde gros + 1 ex-aequo (tie-break par nom)
        cl = [self._cl("huge", 100), self._cl("z", 10), self._cl("a", 10), self._cl("b", 10)]
        macro = {"huge": "Huge", "z": "Zeta", "a": "Alpha", "b": "Beta"}
        out1 = t._enforce_cap(macro, cl, cap=3, lang="fr")
        out2 = t._enforce_cap(macro, cl, cap=3, lang="fr")
        self.assertEqual(out1, out2)                         # reproductible
        self.assertEqual(out1["huge"], "Huge")
        self.assertEqual(out1["a"], "Alpha")                # (-10, "Alpha") gagne le dernier slot
        self.assertEqual(out1["b"], "Divers")
        self.assertEqual(out1["z"], "Divers")

    def test_enforce_cap_divers_dominant_degrades_to_fine(self):
        from agents.onboarding import taxonomy_llm as t
        # queue collapsée (8+8+8=24) > plus gros conservé (10) → grain fin
        cl = [self._cl("k1", 10), self._cl("k2", 9),
              self._cl("t1", 8), self._cl("t2", 8), self._cl("t3", 8)]
        macro = {"k1": "A", "k2": "B", "t1": "C", "t2": "D", "t3": "E"}
        out = t._enforce_cap(macro, cl, cap=3, lang="fr")
        self.assertEqual(out, {"k1": "k1", "k2": "k2", "t1": "t1", "t2": "t2", "t3": "t3"})
        self.assertNotIn("Divers", out.values())
```

- [ ] **Step 2 : Lancer pour vérifier l'échec**

Run : `uv run python -m unittest tests.auto.test_agent_onboarding.TestMacroThemeFactorization -v`
Expected : FAIL — `AttributeError: ... '_enforce_cap'`.

- [ ] **Step 3 : Implémenter**

Dans `agents/onboarding/taxonomy_llm.py`, après `_assign_macro_themes`, ajouter :

```python
def _enforce_cap(macro_map: dict[str, str], section_clusters: list[dict],
                 cap: int, lang: str) -> dict[str, str]:
    """Plafonne le nb de grands thèmes d'une section (cf. spec §4). Déterministe.

    ≤ cap → inchangé. Sinon : tri `(-volume, nom)`, garde les `cap-1` plus gros,
    fusionne le reste dans `_DIVERS[lang]` (clé idempotente). Si « Divers » devient
    le plus gros bucket → dégrade la section au grain fin (chaque thème = son canonical).
    """
    vol: dict[str, int] = {}
    for c in section_clusters:
        m = macro_map.get(c["canonical"])
        if m:
            vol[m] = vol.get(m, 0) + int(c["count"])
    if len(vol) <= cap:
        return macro_map
    ordered = sorted(vol, key=lambda m: (-vol[m], m))      # tie-break déterministe
    keep = set(ordered[:cap - 1])
    divers = _DIVERS[lang]
    out: dict[str, str] = {}
    for c in section_clusters:
        m = macro_map.get(c["canonical"])
        if not m:
            continue
        out[c["canonical"]] = m if m in keep else divers
    divers_vol = sum(int(c["count"]) for c in section_clusters
                     if out.get(c["canonical"]) == divers)
    if divers_vol > vol[ordered[0]]:                       # « Divers » dominant → grain fin
        return {c["canonical"]: c["canonical"] for c in section_clusters}
    return out
```

- [ ] **Step 4 : Lancer pour vérifier le succès**

Run : `uv run python -m unittest tests.auto.test_agent_onboarding.TestMacroThemeFactorization -v`
Expected : PASS.

- [ ] **Step 5 : Commit**

```bash
git add agents/onboarding/taxonomy_llm.py tests/auto/test_agent_onboarding.py
git commit -m "feat(onboarding): _enforce_cap — plafond déterministe (tie-break + anti-Divers dominant)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4 : câbler la passe 2 dans `propose_taxonomy`

**Files:**
- Modify: `agents/onboarding/taxonomy_llm.py` (réécriture de `propose_taxonomy`, ≈ lignes 159-205)
- Test: `tests/auto/test_agent_onboarding.py` (`TestMacroThemeFactorization`)

- [ ] **Step 1 : Écrire les tests qui échouent**

Ajouter dans `TestMacroThemeFactorization` (réutilise `self._llm` de Task 2) :

```python
    def test_compact_engorged_section_collapses_to_macros(self):
        from agents.onboarding import taxonomy_llm as t
        # 8 clusters (> skip=6) tous en "Informatique" → 3 grands thèmes
        cl = [self._cl(f"t{i}", 10 - i) for i in range(8)]
        sec_map = {f"t{i}": "Informatique" for i in range(8)}
        macro_map = {**{f"t{i}": "Machine Learning" for i in range(4)},
                     **{f"t{i}": "Bases de Données" for i in range(4, 7)},
                     "t7": "Réseaux"}
        llm = self._llm(sec_map, macro_map=macro_map)
        tree, mapping = t.propose_taxonomy(llm, cl, options={"granularity": "compact"})
        self.assertEqual(mapping["T0"], "01-Informatique/Machine-Learning")
        self.assertEqual(mapping["T7"], "01-Informatique/Réseaux")
        subs = {f for f in tree if f.startswith("01-Informatique/")}
        self.assertEqual(len(subs), 3)                        # factorisé : 3 dossiers, pas 8

    def test_compact_small_section_skips_pass2(self):
        from agents.onboarding import taxonomy_llm as t
        cl = [self._cl(f"t{i}", 1) for i in range(4)]         # 4 ≤ skip=6
        llm = self._llm({f"t{i}": "Sciences" for i in range(4)},
                        macro_map={f"t{i}": "NE_DOIT_PAS_ETRE_UTILISE" for i in range(4)})
        tree, mapping = t.propose_taxonomy(llm, cl, options={"granularity": "compact"})
        self.assertEqual(mapping["T0"], "01-Sciences/T0")     # grain fin conservé
        # passe 2 jamais appelée → un seul invoke (la passe 1)
        self.assertEqual(llm.with_structured_output.return_value.invoke.call_count, 1)

    def test_detailed_disables_pass2(self):
        from agents.onboarding import taxonomy_llm as t
        cl = [self._cl(f"t{i}", 1) for i in range(10)]        # > 6 mais detailed → pas de passe 2
        llm = self._llm({f"t{i}": "Informatique" for i in range(10)},
                        macro_map={f"t{i}": "Macro" for i in range(10)})
        _, mapping = t.propose_taxonomy(llm, cl, options={"granularity": "detailed"})
        self.assertEqual(mapping["T0"], "01-Informatique/T0")     # 1 dossier = 1 thème
        self.assertEqual(llm.with_structured_output.return_value.invoke.call_count, 1)

    def test_anti_refusion_general_keeps_fine_grain(self):
        from agents.onboarding import taxonomy_llm as t
        # 8 clusters en "Sciences" ; le LLM nomme 2 grands thèmes "Sciences" (collision)
        cl = [self._cl(f"t{i}", 10 - i) for i in range(8)]
        sec_map = {f"t{i}": "Sciences" for i in range(8)}
        macro_map = {**{f"t{i}": "Astrophysique" for i in range(6)},
                     "t6": "Sciences", "t7": "Sciences"}      # collisions
        llm = self._llm(sec_map, macro_map=macro_map)
        _, mapping = t.propose_taxonomy(llm, cl, options={"granularity": "compact"})
        # les deux collisions retombent sur leurs thèmes FINS distincts, pas un "Général" partagé
        self.assertEqual(mapping["T6"], "01-Sciences/T6")
        self.assertEqual(mapping["T7"], "01-Sciences/T7")
        self.assertNotEqual(mapping["T6"], mapping["T7"])

    def test_pass2_failure_degrades_to_fine(self):
        from agents.onboarding import taxonomy_llm as t
        cl = [self._cl(f"t{i}", 1) for i in range(8)]         # > skip → passe 2 tentée
        llm = self._llm({f"t{i}": "Informatique" for i in range(8)},
                        macro_map={f"t{i}": "Macro" for i in range(8)}, fail_pass2=True)
        _, mapping = t.propose_taxonomy(llm, cl, options={"granularity": "compact"})
        for i in range(8):                                    # tous mappés au grain fin
            self.assertEqual(mapping[f"T{i}"], f"01-Informatique/T{i}")
```

- [ ] **Step 2 : Lancer pour vérifier l'échec**

Run : `uv run python -m unittest tests.auto.test_agent_onboarding.TestMacroThemeFactorization -v`
Expected : FAIL — les 5 nouveaux tests échouent (la passe 2 n'est pas encore câblée ; `propose_taxonomy` continue de produire 1 dossier par thème).

- [ ] **Step 3 : Implémenter — réécrire `propose_taxonomy`**

Remplacer **intégralement** la fonction `propose_taxonomy` existante par :

```python
def propose_taxonomy(llm: Any, clusters: list[dict],
                     options: dict | None = None) -> tuple[list[str], dict[str, str]]:
    """Retourne (tree_folders, theme_mapping). theme_mapping = {raw_theme: folder}.

    Deux passes : (1) `_assign_sections` assigne un domaine à chaque cluster ;
    (2) `_assign_macro_themes` regroupe, par section engorgée, les thèmes en grands
    thèmes (pilotée par `granularity`). Le grand thème devient le sous-dossier ; les
    thèmes fins y sont absorbés. Garde-fous : skip des petites sections, `_enforce_cap`,
    repli grain-fin avant « Général ». `_A-TRIER` toujours présent.
    """
    opts = _normalize_options(options)
    if not clusters:
        return [_RESIDUAL], {}
    assigned = _assign_sections(llm, clusters, opts)
    if not assigned:
        log.warning("propose_taxonomy: aucune assignation LLM — fallback _A-TRIER seul")
        return [_RESIDUAL], {}

    # Passe 2 : regrouper les thèmes en grands thèmes, par section.
    gran = _GRANULARITY.get(opts["granularity"])   # None si detailed
    by_section: dict[str, list[dict]] = {}
    for c in clusters:
        sec = assigned.get(c["canonical"])
        if sec:
            by_section.setdefault(sec, []).append(c)
    macro_of: dict[str, str] = {}
    for sec, sec_clusters in by_section.items():
        if gran is None or len(sec_clusters) <= gran["skip"]:
            for c in sec_clusters:                 # petite section / detailed → grain fin
                macro_of[c["canonical"]] = c["canonical"]
            continue
        try:
            m = _assign_macro_themes(llm, sec_clusters, opts, gran["low"], gran["high"])
        except Exception as exc:  # noqa: BLE001 — frontière LLM
            log.warning("propose_taxonomy: passe 2 échouée section %r: %s", sec, exc)
            m = {}
        m = _enforce_cap(m, sec_clusters, gran["cap"], opts["folder_language"])
        for c in sec_clusters:                     # trou d'index → grain fin
            macro_of[c["canonical"]] = m.get(c["canonical"]) or c["canonical"]

    folders: set[str] = {_RESIDUAL}
    mapping: dict[str, str] = {}
    section_num: dict[str, str] = {}   # nom de section (MAJ) → numéro "NN"
    case, sep = opts["folder_case"], opts["word_separator"]
    for c in clusters:
        sec_raw = assigned.get(c["canonical"])
        if not sec_raw:
            continue                               # non assigné → reste orphelin
        sec_fmt = _fmt(sec_raw, case, sep)
        if not sec_fmt or sec_fmt.upper() in ("INBOX", "_INBOX"):
            continue
        if opts["numbered_sections"]:
            key = sec_fmt.upper()
            if key not in section_num:
                section_num[key] = f"{len(section_num) + 1:02d}"
            section_seg = f"{section_num[key]}-{sec_fmt}"
        else:
            section_seg = sec_fmt
        parts = [section_seg]
        if opts["max_depth"] >= 2:                 # SECTION/GrandThème (profondeur 2)
            sub = _fmt(macro_of.get(c["canonical"], c["canonical"]), case, sep)
            if _collides(sub, sec_fmt):
                sub = _fmt(c["canonical"], case, sep)             # 1) repli grain fin distinct
                if _collides(sub, sec_fmt):
                    sub = _fmt(_GENERAL[opts["folder_language"]], case, sep)   # 2) dernier recours
            parts.append(sub)
        folder = "/".join(parts)
        for i in range(1, len(parts) + 1):
            folders.add("/".join(parts[:i]))
        for raw in c["raw_members"]:
            mapping[raw] = folder
    return sorted(folders), mapping
```

- [ ] **Step 4 : Lancer pour vérifier le succès (nouveaux + régression)**

Run : `uv run python -m unittest tests.auto.test_agent_onboarding -v`
Expected : PASS — les 5 nouveaux tests de Task 4 ET toute la classe `TestProposeTaxonomy` existante restent verts (leurs sections font 1-5 clusters ≤ 12 en `auto` par défaut → passe 2 sautée).

- [ ] **Step 5 : Commit**

```bash
git add agents/onboarding/taxonomy_llm.py tests/auto/test_agent_onboarding.py
git commit -m "feat(onboarding): propose_taxonomy à 2 passes — factorisation des thèmes par section

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5 : enrichissement `categories.yaml` (folder_hints)

**Files:**
- Modify: `agents/onboarding/proposition.py` (`propose_categories` ≈ ligne 114, `build_proposal` ≈ lignes 199-201)
- Test: `tests/auto/test_agent_onboarding.py` (`TestProposeCategories`, `TestBuildProposal`)

- [ ] **Step 1 : Écrire les tests qui échouent**

Dans `TestProposeCategories`, ajouter :

```python
    def test_folder_hints_enrich_rationale(self):
        from agents.onboarding import proposition
        tree = ["01-INFO", "01-INFO/Machine-Learning", "_A-TRIER"]
        hints = {"01-INFO/Machine-Learning": ["deep learning", "transformers", "cnn"]}
        fake_llm = mock.Mock()
        with mock.patch("agents.onboarding.proposition.get_agent_llm", return_value=fake_llm), \
             mock.patch("agents.onboarding.proposition.propose_keywords_for_new_folders",
                        return_value=[]) as pk:
            proposition.propose_categories(tree, folder_hints=hints)
        creations = pk.call_args.kwargs["creations"]
        ml = next(c for c in creations if c["path"] == "01-INFO/Machine-Learning")
        self.assertIn("deep learning", ml["rationale"])
        self.assertIn("regroupe", ml["rationale"])
```

Dans `TestBuildProposal`, ajouter (réutilise le pattern `fake_invoke` de `test_build_proposal_end_to_end`) :

```python
    def test_build_proposal_derives_folder_hints(self):
        from agents.onboarding import proposition, taxonomy_llm
        vis = {"title": "T", "theme": "Deep Learning",
               "themes": [{"theme": "Deep Learning", "confidence": 0.9}], "confidence": 0.9}

        def fake_invoke(messages):
            import re as _re
            idxs = _re.findall(r"^(\d+)\. ", messages[-1]["content"], _re.M)
            return taxonomy_llm._Assignments(items=[
                taxonomy_llm._Assign(index=int(n), section="Informatique") for n in idxs])

        fake_llm = mock.Mock()
        fake_llm.with_structured_output.return_value.invoke.side_effect = fake_invoke
        with mock.patch("agents.onboarding.proposition.analyze_cover",
                        side_effect=lambda path, **kw: vis), \
             mock.patch("agents.onboarding.proposition.get_agent_llm", return_value=fake_llm), \
             mock.patch("agents.onboarding.proposition.propose_keywords_for_new_folders",
                        return_value=[]) as pk:
            proposition.build_proposal("perso", lambda d, t: None)
        creations = pk.call_args.kwargs["creations"]
        self.assertTrue(creations)                              # au moins 1 dossier feuille
        self.assertTrue(any("deep learning" in c["rationale"].lower() for c in creations))
```

- [ ] **Step 2 : Lancer pour vérifier l'échec**

Run : `uv run python -m unittest tests.auto.test_agent_onboarding.TestProposeCategories tests.auto.test_agent_onboarding.TestBuildProposal -v`
Expected : FAIL — `propose_categories()` n'accepte pas `folder_hints` (TypeError) ; le rationale ne contient pas « regroupe ».

- [ ] **Step 3 : Implémenter**

Dans `agents/onboarding/proposition.py`, modifier `propose_categories`. Remplacer sa signature et le bloc `creations = [...]` (≈ lignes 114-129). Signature actuelle :

```python
def propose_categories(tree_folders: list[str]) -> dict:
```

→ devient :

```python
def propose_categories(tree_folders: list[str],
                       folder_hints: dict[str, list[str]] | None = None) -> dict:
```

Et remplacer la list-comprehension `creations` actuelle :

```python
    creations = [{"path": f, "rationale": "dossier de la taxonomie d'onboarding"}
                 for f in leaves]
```

par une boucle qui enrichit le rationale :

```python
    creations: list[dict] = []
    for f in leaves:
        rationale = "dossier de la taxonomie d'onboarding"
        hints = (folder_hints or {}).get(f)
        if hints:
            rationale += " — regroupe : " + ", ".join(hints[:12])
        creations.append({"path": f, "rationale": rationale})
```

Dans `build_proposal` (≈ lignes 199-201), après l'appel à `propose_taxonomy`, calculer `folder_hints` et le passer (⚠️ **conserver `options`**). Bloc actuel :

```python
    options = _load_profile_cfg(profile).get("onboarding_options")
    tree, mapping = propose_taxonomy(get_agent_llm(), clusters, options)
    cats = propose_categories(tree)
```

→ devient :

```python
    options = _load_profile_cfg(profile).get("onboarding_options")
    tree, mapping = propose_taxonomy(get_agent_llm(), clusters, options)
    folder_hints: dict[str, list[str]] = {}
    for c in clusters:
        raws = c["raw_members"]
        folder = mapping.get(raws[0]) if raws else None
        if folder:
            folder_hints.setdefault(folder, []).append(c["canonical"])
    cats = propose_categories(tree, folder_hints)
```

- [ ] **Step 4 : Lancer pour vérifier le succès**

Run : `uv run python -m unittest tests.auto.test_agent_onboarding -v`
Expected : PASS — nouveaux tests verts ET `test_build_proposal_passes_onboarding_options` toujours vert (options conservées).

- [ ] **Step 5 : Commit**

```bash
git add agents/onboarding/proposition.py tests/auto/test_agent_onboarding.py
git commit -m "feat(onboarding): enrichir categories.yaml avec les thèmes absorbés (folder_hints)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6 : documentation + lint + suite complète

**Files:**
- Modify: `dashboard/templates/onboarding.html`, `agents/CLAUDE.md`, `dashboard/CLAUDE.md`

- [ ] **Step 1 : Texte d'aide `granularity` dans `onboarding.html`**

Localiser le `<select>` / l'aide de l'option `granularity` (chercher `granularity` dans le fichier) et compléter le libellé d'aide pour mentionner le regroupement. Exemple de texte à intégrer près du select :

```html
<small class="hint">compact regroupe les thèmes fins en grands thèmes (moins de dossiers) ;
detailed garde un dossier par thème.</small>
```

(Adapter à la structure HTML existante : conserver les classes/format des autres `hint` du formulaire.)

- [ ] **Step 2 : `agents/CLAUDE.md` — section « Agent Onboarding », entrée `taxonomy_llm.py »**

Remplacer la description mono-passe par la version 2 passes. Texte à intégrer :

```markdown
- **`taxonomy_llm.py`** — `propose_taxonomy(llm, clusters, options)` : **2 passes**.
  Passe 1 (`_assign_sections`) : un DOMAINE (section) par cluster, par lots, matching
  par numéro. Passe 2 (`_assign_macro_themes`) : regroupe, *par section engorgée*, les
  thèmes fins en **grands thèmes** (pilotée par `granularity` — cf. `_GRANULARITY`).
  Garde-fous déterministes : skip des petites sections, `_enforce_cap` (tie-break
  stable + anti-« Divers » dominant), repli grain-fin avant « Général ». Structure
  `SECTION/GrandThème` (profondeur 2) ; les thèmes fins sont absorbés dans
  `theme_mapping`. `_A-TRIER` résiduel ; `_INBOX` réservé.
```

- [ ] **Step 3 : `dashboard/CLAUDE.md` — pipeline onboarding**

Mettre à jour la ligne décrivant `propose_taxonomy` (chercher `propose_taxonomy`) pour signaler les 2 passes (passe 1 sections + passe 2 grands thèmes pilotée par `granularity`). Ajuster aussi le décompte de tests de la ligne `test_agent_onboarding.py` (mesurer la vraie valeur, cf. Step 5).

- [ ] **Step 4 : Lint**

Run : `uv run ruff check agents/onboarding/ tests/auto/test_agent_onboarding.py`
Expected : `All checks passed!` (corriger les éventuels imports/longueurs de ligne).

- [ ] **Step 5 : Suite complète + décompte exact des tests**

Run : `uv run python -m unittest tests.auto.test_agent_onboarding -v 2>&1 | tail -3`
Expected : `OK`. Relever le nombre de tests (`Ran N tests`) et reporter `N` dans la ligne `dashboard/CLAUDE.md` (remplacer l'ancien « 38 tests » par la valeur mesurée).

Lancer aussi la suite globale pour vérifier l'absence de régression transverse :
Run : `uv run python -m unittest discover -s tests/auto 2>&1 | tail -3`
Expected : `OK`.

- [ ] **Step 6 : Commit**

```bash
git add dashboard/templates/onboarding.html agents/CLAUDE.md dashboard/CLAUDE.md
git commit -m "docs(onboarding): documenter le pipeline taxonomie à 2 passes (factorisation)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7 : validation smoke live (manuelle, hors suite auto)

> Nécessite `SILICONFLOW_API_KEY` + le corpus de test `/private/tmp/klodo-onb-big`
> (300 fichiers à plat). **N'est pas** un test unitaire (LLM/Vision réels). Conforme à
> la règle projet « smoke test avant tout run LLM long ». À exécuter par l'opérateur.

- [ ] **Step 1 : Redémarrer le dashboard sur le code à jour**

```bash
cd /Users/walidnamane/Documents/Claude/Projects/Manage-Biblio
pkill -f "python -m dashboard"; sleep 1
./scripts/test_onboarding.sh dash_start
```

- [ ] **Step 2 : Onboarding en `granularity=compact` sur un profil neuf**

Via l'UI `http://localhost:8080/onboarding` : dossier `/private/tmp/klodo-onb-big`,
granularité **compact**, profondeur max **2**, nom de profil ex. `macro-test`, puis
Analyser (Vision ~6 min).

- [ ] **Step 2 (alternative script)** :

```bash
TEST_DIR=/private/tmp/klodo-onb-big PROFILE=macro-test \
  ./scripts/test_onboarding.sh auto
```

- [ ] **Step 3 : Vérifier le résultat**

```bash
uv run python -c "
import yaml, collections
t = yaml.safe_load(open('profiles/macro-test/tree.yaml'))['folders']
subs = collections.Counter(f.split('/')[0] for f in t if '/' in f)
print('sections:', len([f for f in t if '/' not in f and not f.startswith(\"_\")]))
print('dossiers 2e niveau / section:', dict(subs))
print('profondeurs:', sorted({f.count(\"/\")+1 for f in t}))
"
```

Attendu : **~3-6 dossiers de 2ᵉ niveau par section** (au lieu de 30+), profondeur ≤ 2,
et la couverture du dry-run ≥ celle d'avant la factorisation (affichée par
`./scripts/test_onboarding.sh verify`). Si une section affiche un « Divers » dominant
ou un retour au grain fin massif → relire les seuils `_GRANULARITY`.

- [ ] **Step 4 : Nettoyage**

Supprimer le profil de test une fois validé : `rm -rf profiles/macro-test`.

---

## Self-review (rempli par l'auteur du plan)

**Couverture spec :**
- §3-4 pipeline 2 passes + granularité → Tasks 1, 4. ✓
- §5.1 `_macro_system`/`_assign_macro_themes`/`_enforce_cap`/collision → Tasks 1, 2, 3, 4. ✓
- §5.2 folder_hints (propose_categories + build_proposal, conserver options) → Task 5. ✓
- §5.3 UI/docs → Task 6. ✓
- §6 dégradation gracieuse (échec passe 2, trou d'index, plafond, collision) → tests Tasks 3, 4. ✓
- §7 plan de tests (mock payload-aware, anti-refusion, tie-break, Divers dominant, régression, smoke) → Tasks 2-6, 7. ✓

**Placeholders :** aucun — chaque step montre le code complet et la commande exacte.

**Cohérence des types/noms :** `_GRANULARITY`, `_DIVERS`, `_collides(sub, sec_fmt)`,
`_macro_system(opts, existing, low, high)`, `_assign_macro_themes(llm, section_clusters, opts, low, high)`,
`_enforce_cap(macro_map, section_clusters, cap, lang)`, `macro_of`, `folder_hints`,
`propose_categories(tree_folders, folder_hints=None)` — identiques d'une tâche à l'autre. ✓
