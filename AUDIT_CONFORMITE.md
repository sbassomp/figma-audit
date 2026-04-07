# Audit de conformite - figma-audit

**Date** : 2026-04-07
**Langage** : Python 3.11+
**Framework** : FastAPI + Click + Playwright + SQLModel
**Fichiers sources** : 35 fichiers (7 300 lignes)
**Fichiers tests** : 4 fichiers (47 tests)

## Resume

| Priorite | Code+Secu | Accessibilite | Total |
|----------|-----------|---------------|-------|
| Critique | 3 | 1 | 4 |
| Important | 5 | 2 | 7 |
| Mineur | 4 | 4 | 8 |

---

## Notes globales

| Categorie | Score | Status |
|-----------|-------|--------|
| **Score Code** | **62/100** | Action requise |
| **Score Securite** | **65/100** | Action requise (< 70 = deploiement bloque) |
| **Score Accessibilite** | **58/100** | Action requise |
| **Moyenne** | **62/100** | Action requise |

### Sous-scores Code

| Categorie Code | Score |
|----------------|-------|
| Taille fichiers | 5/10 |
| SOLID | 5/10 |
| Patterns/Nommage | 8/10 |
| Code mort | 9/10 |
| Duplication | 7/10 |
| Tests | 3/10 |
| Gestion erreurs | 4/10 |
| Lisibilite | 7/10 |

### Sous-scores Securite (OWASP)

| Categorie OWASP | Score |
|------------------|-------|
| A01 - Broken Access Control | 20/100 |
| A02 - Cryptographic Failures | 85/100 |
| A03 - Injection | 95/100 |
| A05 - Security Misconfiguration | 60/100 |

---

## Violations critiques (3)

### C1. Aucune authentification sur l'API REST
**Fichier** : `figma_audit/api/app.py`, `figma_audit/api/deps.py`
**Regle** : OWASP A01 - Broken Access Control
**Probleme** : Tous les endpoints API sont accessibles sans authentification. N'importe qui ayant acces au port 8321 peut supprimer des projets, modifier des ecarts, telecharger des fichiers.

**Action** : Implementer une authentification par Bearer token (variable d'env `API_TOKEN`) avec un middleware FastAPI. Minimum viable : un token statique verifie par un `Depends(verify_token)`.

### C2. 13 exceptions silencieuses (except: pass)
**Fichiers** : `phases/compare.py:220`, `phases/capture_app.py:34,49,206,210`, `__main__.py:243,308,573,605`, `api/routes/web.py:198`
**Regle** : Gestion des erreurs
**Probleme** : 13 blocs `except Exception: pass` avalent les erreurs silencieusement. Les echecs de DB, de Playwright, de subprocess sont invisibles.

```python
# Actuel
except Exception:
    pass

# Attendu
except Exception as e:
    console.print(f"[yellow]Warning: {e}[/yellow]")
```

**Action** : Remplacer chaque `pass` par un log `console.print` ou `logger.warning`. Ne jamais avaler une exception sans trace.

### C3. Couverture de tests a 11% (4 modules sur 35)
**Fichier** : `tests/`
**Regle** : Tests
**Probleme** : Seuls config, color, models et l'API basique sont testes. Les 6 phases (coeur metier), les clients API (Claude, Figma), les routes htmx, la progression -- rien n'est teste.

**Action** : Priorite aux tests des phases avec mocks (Claude/Figma mockés), puis tests des routes htmx (renvoient du HTML).

---

## Violations importantes (5)

### I1. __main__.py fait 728 lignes (seuil: 500)
**Fichier** : `figma_audit/__main__.py`
**Regle** : Taille fichiers, Single Responsibility
**Probleme** : Le fichier contient les commandes CLI, la logique d'import de screens, le setup du daemon, l'installation systemd/launchd. 4 responsabilites distinctes.

**Action** : Extraire en modules :
- `figma_audit/cli/commands.py` (commandes de phases)
- `figma_audit/cli/setup.py` (setup interactif + daemon)
- `figma_audit/cli/import_screens.py` (import de screens)

### I2. web.py fait 596 lignes avec logique metier melangee
**Fichier** : `figma_audit/api/routes/web.py`
**Regle** : Single Responsibility
**Probleme** : Les routes web contiennent la logique de creation de run, d'import de fichiers, et de requetes DB complexes. Le background task `_run_pipeline_bg` (80 lignes) est dans le fichier de routes.

**Action** : Extraire le background task dans `figma_audit/api/tasks.py` et les requetes complexes dans un service layer.

### I3. capture_app.py fait 555 lignes avec fonctions > 30 lignes
**Fichier** : `figma_audit/phases/capture_app.py`
**Regle** : Taille methodes
**Probleme** : `_run_async` fait ~80 lignes, `_setup_test_data` fait ~70 lignes, `_flutter_login` fait ~50 lignes.

**Action** : Decouper en fonctions plus petites (login, navigation, screenshot, cleanup).

### I4. Endpoints de fichiers sans validation de chemin
**Fichier** : `figma_audit/api/app.py:45-58`
**Regle** : OWASP A03 - Path Traversal
**Probleme** : L'endpoint `/files/{slug}/{path:path}` sert des fichiers depuis `output_dir`. Un `path` comme `../../etc/passwd` pourrait potentiellement etre exploite.

```python
file_path = Path(project.output_dir).expanduser().resolve() / path
```

**Action** : Verifier que le chemin resolu reste dans `output_dir` :
```python
resolved = (output_dir / path).resolve()
if not str(resolved).startswith(str(output_dir.resolve())):
    return Response(status_code=403)
```

### I5. Pas de validation des entrees sur les endpoints API
**Fichier** : `figma_audit/api/routes/screens.py`, `discrepancies.py`
**Regle** : Validation des entrees
**Probleme** : Les status envoyes (`open`, `ignored`, `fixed`, etc.) sont valides dans le code mais les endpoints htmx (`/htmx/.../status/{new_status}`) n'ont pas de validation Pydantic. Un status arbitraire pourrait etre injecte.

**Action** : Utiliser un `Literal["open", "ignored", "fixed", "wontfix"]` dans les routes.

---

## Violations mineures (4)

### M1. Imbrication > 3 niveaux dans export_figma.py
**Fichier** : `figma_audit/phases/export_figma.py:93-96`
**Regle** : Profondeur d'imbrication
**Probleme** : Boucles imbriquees pour le parcours des elements Figma.

**Action** : Extraire la logique d'extraction des fills/colors en fonction dediee.

### M2. Pas de constantes nommees pour les magic strings
**Fichiers** : Multiples
**Regle** : Constantes nommees
**Probleme** : Les status `"open"`, `"current"`, `"obsolete"`, `"completed"`, `"running"` sont des strings literals repetees. Pas de duplication severe mais risque de typo.

**Action** : Creer un module `figma_audit/constants.py` avec des Enum ou des constantes.

### M3. Logique dupliquee dans les routes htmx et web
**Fichier** : `figma_audit/api/routes/htmx.py`, `web.py`
**Regle** : DRY
**Probleme** : Le rendu des cartes de discrepancies est duplique entre le template Jinja2 (`run.html`) et la fonction Python (`_disc_card_html`).

**Action** : Utiliser un template partial Jinja2 inclus par `{% include %}` et par le endpoint htmx.

### M4. Global mutable `_engine` dans db/engine.py
**Fichier** : `figma_audit/db/engine.py:9`
**Regle** : Patterns
**Probleme** : Variable globale mutable `_engine = None` pour le singleton DB. Pas thread-safe dans un contexte FastAPI multi-thread.

**Action** : Utiliser un pattern thread-safe ou passer l'engine via le FastAPI state.

---

## Volet Accessibilite (WCAG 2.1 AA) - Score : 58/100

**Scope** : 9 templates HTML
**Referentiels** : WCAG 2.1 niveau AA

### Violations critiques accessibilite (1)

**AC1. Boutons d'action sans aria-label**
**Ref** : WCAG 4.1.2 Name, Role, Value
**Fichiers** : `run.html:104-106`, `comparison.html:63-65`, `htmx.py:28-38`
**Probleme** : Les boutons "Ignorer", "Won't fix", "Corrige" n'ont pas d'aria-label. Leur fonction depend du contexte visuel (la carte parente) qui n'est pas accessible aux lecteurs d'ecran.
**Action** : Ajouter `aria-label="Ignorer l'ecart: {{ d.description[:50] }}"` sur chaque bouton.

### Violations importantes accessibilite (2)

**AI1. Pas de skip navigation**
**Ref** : WCAG 2.4.1 Bypass Blocks
**Fichier** : `base.html`
**Probleme** : Pas de lien "Aller au contenu principal" pour sauter la sidebar.
**Action** : Ajouter `<a href="#main" class="skip-link">Aller au contenu</a>` avec CSS pour masquer visuellement.

**AI2. Formulaires sans aria-required**
**Ref** : WCAG 3.3.2 Labels or Instructions
**Fichier** : `new_project.html:13`
**Probleme** : Le champ `name` a `required` mais pas `aria-required="true"`.
**Action** : Ajouter `aria-required="true"` sur tous les champs obligatoires.

### Violations mineures accessibilite (4)

**AM1.** Alt text dynamique potentiellement vide (`screens.html:27`, `comparison.html:23,33`)
**AM2.** Sidebar `<nav>` sans `aria-label="Navigation principale"` (`base.html`)
**AM3.** Pas de `role="main"` sur `<main>` (implicite mais mieux d'etre explicite)
**AM4.** Couleurs de contraste non verifiees (variables CSS custom)

### Bonnes pratiques accessibilite

- `<html lang="fr">` present sur tous les templates
- `<meta name="viewport">` correctement configure
- Hierarchie de titres h2/h3 respectee
- Labels `<label for="">` associes aux inputs dans les formulaires
- Tables avec `<thead>` et `<th>` pour les en-tetes

---

## Points positifs

### Architecture & Code
- Structure de package claire et modulaire (phases/, utils/, api/, db/, web/)
- Pydantic v2 pour la validation des donnees
- SQLModel pour l'ORM (zero friction avec les models Pydantic existants)
- CLI Click bien structuree avec auto-decouverte du config YAML
- Dark theme coherent entre le rapport HTML et le dashboard web
- Pipeline resumable (`--from phase`)
- Tracking des tokens/couts par run

### Securite
- Aucun secret en dur dans le code source
- Cles API chargees depuis `~/.config/figma-audit/env` avec chmod 600
- ORM SQLModel = pas d'injection SQL possible
- subprocess utilise avec listes d'arguments (pas de shell=True)
- `.gitignore` correct (exclut .db, .env, output/)

### Infrastructure
- CI/CD GitLab fonctionnel (lint + test + build)
- Build number aligne avec l'instance GitLab (CI_PIPELINE_ID)
- Installation daemon systemd/launchd automatisee

---

## Plan de remediation

### Sprint 1 - Securite (bloquant)
1. **Authentification API** : Bearer token sur tous les endpoints (C1)
2. **Path traversal** : Validation du chemin dans l'endpoint `/files/` (I4)
3. **Validation status** : Literal types sur les endpoints htmx (I5)

### Sprint 2 - Robustesse (prioritaire)
1. **Logging des exceptions** : Remplacer les 13 `except: pass` (C2)
2. **Tests phases** : Ajouter des tests avec mocks pour au moins compare.py et match_screens.py (C3)
3. **Tests API htmx** : Verifier que les fragments HTML sont valides (C3)

### Sprint 3 - Refactoring (amelioration)
1. **Decouper __main__.py** : Extraire setup, import_screens, daemon (I1)
2. **Extraire le background task** de web.py (I2)
3. **Decouper capture_app.py** en fonctions < 30 lignes (I3)

### Sprint 4 - Accessibilite (amelioration)
1. **aria-label sur les boutons** d'action (AC1)
2. **Skip navigation** dans base.html (AI1)
3. **aria-required** sur les formulaires (AI2)

---

## Metriques

| Metrique | Valeur | Seuil | Status |
|----------|--------|-------|--------|
| Fichiers > 500 lignes | 3 | 0 | :x: |
| Fonctions > 30 lignes | ~8 | 0 | :x: |
| Tests | 47 | - | OK |
| Tests passants | 100% | 100% | :white_check_mark: |
| Couverture modules | 11% | > 50% | :x: |
| except pass silencieux | 13 | 0 | :x: |
| Secrets en dur | 0 | 0 | :white_check_mark: |
| Endpoints sans auth | 15+ | 0 | :x: |
| Path traversal possible | 1 | 0 | :x: |
| CI/CD | OK | - | :white_check_mark: |
| Lint (ruff) | 0 erreurs | 0 | :white_check_mark: |

---

## Historique des audits

| Date | Code | Securite | Accessibilite | Evolution |
|------|------|----------|---------------|-----------|
| 2026-04-07 | 62/100 | 65/100 | 58/100 | Audit initial - 7300 lignes, 47 tests, CI OK |
