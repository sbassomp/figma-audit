# figma-audit

Outil CLI + dashboard web pour la **comparaison semantique** entre des designs Figma et une application web deployee.

Contrairement aux outils de visual regression (BackstopJS, Percy, Chromatic) qui comparent deux versions de la meme app entre elles, figma-audit compare un **design Figma** contre son **implementation reelle** et produit un rapport detaille des ecarts.

## Fonctionnalites

- **Analyse du code source** : detection automatique du framework (Flutter, React, Vue, Angular, Next.js), extraction des routes, pages et design tokens via Claude AI
- **Export Figma** : telechargement de l'arbre complet du fichier Figma, extraction des ecrans, tokens de design et elements via l'API REST, avec cache local et gestion du rate limiting
- **Matching intelligent** : association automatique des ecrans Figma aux routes de l'application par vision AI (Claude Vision)
- **Capture de l'application** : navigation automatisee avec Playwright, authentification Flutter CanvasKit, creation de donnees de test via API
- **Comparaison hybride** : analyse programmatique (couleurs deltaE CIE2000, typographie, spacing) + analyse semantique par vision AI
- **Rapport HTML autonome** : fichier HTML standalone avec images embarquees, dark theme, side-by-side interactif
- **Dashboard web** : interface htmx avec suivi des projets, historique des runs, galerie d'ecrans, gestion des ecarts (ignorer, corriger, annoter)
- **Service daemon** : installation en tant que service systemd (Linux) ou launchd (macOS) pour un dashboard permanent

## Installation

```bash
pip install -e .
figma-audit setup
```

La commande `setup` guide l'installation pas a pas :
1. Configuration des cles API (Anthropic, Figma)
2. Initialisation de la base de donnees SQLite
3. Installation du navigateur Chromium (Playwright)
4. Installation optionnelle du daemon systeme

### Pre-requis

- Python 3.11+
- Un compte [Anthropic](https://console.anthropic.com/) avec une cle API
- Un token [Figma Personal Access](https://www.figma.com/developers/api#access-tokens)
- `pdftoppm` (paquet `poppler-utils`) pour l'import de screens depuis un export Figma Desktop

## Demarrage rapide

### 1. Configuration du projet

Creer un fichier `figma-audit.yaml` a la racine du projet :

```yaml
project: ~/dev/mon-projet
figma_url: "https://www.figma.com/design/XXXXXX/Mon-Projet"
app_url: "https://mon-app.example.com"
output: ./output

viewport:
  width: 390
  height: 844
  device_scale_factor: 1

# Compte secondaire pour creer des donnees de test (optionnel)
seed_account:
  email: "test@example.com"
  otp: "1234"
```

Les cles API sont chargees automatiquement depuis `~/.config/figma-audit/env` (cree par `figma-audit setup`).

### 2. Lancer un audit complet

```bash
figma-audit run
```

Le pipeline execute les 6 phases et affiche la progression :

```
[1/6] Analyze code
  12.3s  35 pages  ~$0.167
[2/6] Export Figma
  0.2s  77 ecrans
[3/6] Match screens
  45.2s  70 matches  ~$0.187
[4/6] Capture app
  92.1s  19 pages
[5/6] Compare (estimation: ~279,100 tokens, ~$1.38)
  8m12s  673 ecarts  ~$1.426
[6/6] Report
  3.1s  32.4 MB

Recap du run
  Total: 10m44s | 282,700 tokens | ~$1.78
```

Le rapport est genere dans `output/report.html`.

### 3. Ouvrir le dashboard

```bash
figma-audit serve
```

Ouvrir http://localhost:8321 dans un navigateur.

## Commandes CLI

### Pipeline

| Commande | Description |
|----------|-------------|
| `figma-audit run` | Execute le pipeline complet (6 phases) |
| `figma-audit run --from compare` | Reprend depuis une phase (analyze, figma, match, capture, compare, report) |
| `figma-audit setup` | Configuration interactive (cles, DB, navigateur, daemon) |
| `figma-audit serve` | Demarre le dashboard web sur le port 8321 |

### Phases individuelles

Chaque phase peut etre executee independamment. Les phases lisent et ecrivent dans le repertoire `--output` (defaut: `./audit-results` ou la valeur du YAML).

| Commande | Phase | Entree | Sortie |
|----------|-------|--------|--------|
| `figma-audit analyze -p ~/dev/projet` | 1 | Code source | `pages_manifest.json` |
| `figma-audit figma` | 2 | URL Figma + token | `figma_manifest.json` + PNGs |
| `figma-audit match` | 3 | Manifests phases 1+2 | `screen_mapping.yaml` |
| `figma-audit capture --app-url https://...` | 4 | Mapping + manifest | Screenshots app |
| `figma-audit compare` | 5 | Screenshots + manifests | `discrepancies.json` |
| `figma-audit report` | 6 | Discrepancies | `report.html` |

### Utilitaires

| Commande | Description |
|----------|-------------|
| `figma-audit import-screens export.zip` | Importe des ecrans depuis un export Figma Desktop (ZIP avec PDFs) |

### Options de la phase Figma

```bash
figma-audit figma --offline          # Travaille uniquement depuis le cache local
figma-audit figma --force-refresh    # Force le re-telechargement depuis l'API
figma-audit figma --target-page "45:927"  # Limite a une page Figma specifique
```

## Dashboard web

Le dashboard est accessible via `figma-audit serve` ou en installant le daemon avec `figma-audit setup`.

### Pages

- **Dashboard** (`/`) : vue d'ensemble des projets avec statistiques globales
- **Projet** (`/projects/{slug}`) : timeline des runs, statistiques, bouton pour lancer un nouveau run
- **Galerie d'ecrans** (`/projects/{slug}/screens`) : tous les ecrans Figma avec vignettes, filtrage par statut (current/obsolete)
- **Detail d'un run** (`/projects/{slug}/runs/{id}`) : statistiques, tableau des comparaisons par ecran, liste des ecarts avec filtres
- **Comparaison** (`/projects/{slug}/runs/{id}/compare/{page_id}`) : side-by-side Figma vs Application avec liste des ecarts

### Actions interactives

Toutes les actions sont executees en place via htmx (pas de rechargement de page) :

- **Marquer un ecart** : Ignorer / Won't fix / Corrige
- **Marquer un ecran** : Current / Obsolete
- **Lancer un run** : depuis la page projet
- **Filtrer les ecarts** : par severite (Critical / Important / Tous)

### API REST

La documentation complete de l'API est disponible a `http://localhost:8321/docs` (OpenAPI auto-generee).

Principaux endpoints :

```
GET    /api/projects                              # Liste des projets
POST   /api/projects                              # Creer un projet
GET    /api/projects/{slug}/runs                   # Historique des runs
POST   /api/projects/{slug}/runs                   # Lancer un run
GET    /api/projects/{slug}/screens                # Ecrans Figma
PATCH  /api/projects/{slug}/discrepancies/{id}     # Modifier le statut d'un ecart
POST   /api/projects/{slug}/discrepancies/{id}/annotate  # Annoter un ecart
```

## Structure du projet

```
figma_audit/
  __main__.py          # CLI (Click)
  config.py            # Configuration Pydantic + chargement YAML
  models.py            # Modeles de donnees (FigmaScreen, FigmaManifest, etc.)
  phases/
    analyze_code.py    # Phase 1 : analyse du code source via Claude
    export_figma.py    # Phase 2 : export Figma via API REST
    match_screens.py   # Phase 3 : matching Figma/routes via Claude Vision
    capture_app.py     # Phase 4 : capture Playwright + seeding API
    compare.py         # Phase 5 : comparaison hybride
    report.py          # Phase 6 : generation du rapport HTML
  utils/
    claude_client.py   # Client Claude API avec tracking tokens/cout
    figma_client.py    # Client Figma REST avec rate limiting et cache
    color.py           # Conversions couleur et deltaE CIE2000
    progress.py        # Suivi de progression CLI et web
    checks.py          # Verifications pre-execution
  db/
    models.py          # Tables SQLModel (Project, Run, Screen, etc.)
    engine.py          # Engine SQLite et gestion des sessions
  api/
    app.py             # Factory FastAPI
    deps.py            # Injection de dependances
    routes/
      projects.py      # CRUD projets
      runs.py          # Gestion des runs
      screens.py       # Gestion des ecrans
      discrepancies.py # Gestion des ecarts
      htmx.py          # Fragments HTML pour htmx
      web.py           # Routes des pages web (Jinja2)
  web/
    static/            # htmx.min.js + style.css (dark theme)
    templates/         # Templates Jinja2 (dashboard, projet, run, etc.)
```

## Fichiers intermediaires

Un run produit les fichiers suivants dans le repertoire de sortie :

```
output/
  pages_manifest.json     # Phase 1 : routes, pages, design tokens
  figma_raw/
    file.json             # Arbre complet du fichier Figma (cache)
    file_meta.json        # Metadonnees du cache
  figma_manifest.json     # Phase 2 : ecrans, elements, tokens
  figma_screens/*.png     # Phase 2 : screenshots des ecrans Figma
  screen_mapping.yaml     # Phase 3 : mapping Figma <-> routes (editable)
  app_screenshots/*.png   # Phase 4 : screenshots de l'application
  app_captures.json       # Phase 4 : metadonnees des captures
  discrepancies.json      # Phase 5 : ecarts detectes
  report.html             # Phase 6 : rapport HTML autonome
```

## Gestion des ecarts

Chaque ecart detecte est classe par :

- **Severite** : `critical` (trahit l'intention du designer), `important` (ecart visible), `minor` (nuance subtile)
- **Categorie** : LAYOUT, COULEURS, TYPOGRAPHIE, COMPOSANTS, TEXTES, SPACING, ELEMENTS_MANQUANTS, ELEMENTS_AJOUTES, DONNEES_ABSENTES
- **Statut** : `open`, `ignored`, `acknowledged`, `fixed`, `wontfix`

La categorie `DONNEES_ABSENTES` distingue les ecarts lies a un etat vide de l'application (pas de donnees de test) des vrais ecarts de design.

## Configuration avancee

### figma-audit.yaml complet

```yaml
project: ~/dev/mon-projet
figma_url: "https://www.figma.com/design/XXXXXX/Mon-Projet"
app_url: "https://mon-app.example.com"
output: ./output

viewport:
  width: 390
  height: 844
  device_scale_factor: 1

figma:
  cache_dir: figma_raw
  request_delay: 3.0       # secondes entre chaque requete API
  batch_size: 8             # ecrans par batch d'export
  retry_wait_default: 60    # attente par defaut sur 429
  max_retries: 5

thresholds:
  color_delta_e: 5.0        # seuil deltaE pour "meme couleur"
  font_size_tolerance: 2    # pixels de tolerance
  spacing_tolerance: 4      # pixels de tolerance

seed_account:
  email: "test@example.com" # compte secondaire pour creer des donnees
  otp: "1234"
```

### Variables d'environnement

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Cle API Anthropic (requise) |
| `FIGMA_TOKEN` | Token Figma Personal Access (requis pour Phase 2 en ligne) |
| `FIGMA_URL` | URL du fichier Figma (alternative au YAML) |
| `APP_URL` | URL de l'application (alternative au YAML) |

Les variables sont chargees automatiquement depuis `~/.config/figma-audit/env`.

## Gestion du rate limiting Figma

L'API Figma impose des limites strictes sur l'export d'images (~30 req/min, avec un cooldown pouvant atteindre 48h en cas de depassement). Strategies :

1. **Cache local** : le tree du fichier Figma est telecharge une fois et cache dans `figma_raw/file.json` (69 MB pour un fichier de 77 ecrans). Les runs suivants utilisent le cache.
2. **Mode offline** : `figma-audit figma --offline` travaille uniquement depuis le cache.
3. **Import desktop** : exporter depuis Figma Desktop (File > Export) et importer avec `figma-audit import-screens export.zip`. Contourne completement le rate limit API.

## Cout d'un audit

Le cout depend du nombre d'ecrans et du modele utilise (Sonnet 4.5 par defaut) :

| Phase | Appels API | Cout typique |
|-------|-----------|--------------|
| Analyze (35 pages) | 1 | ~$0.17 |
| Match (70 ecrans) | 9 | ~$0.19 |
| Compare (62 paires) | 62 | ~$1.43 |
| Report (resume) | 1 | ~$0.01 |
| **Total** | **73** | **~$1.80** |

Le cout est affiche en temps reel pendant l'execution et dans le recap final.

## Developpement

```bash
git clone <your-repo-url>/figma-audit.git
cd figma-audit
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium

# Tests
pytest tests/ -v

# Lint
ruff check figma_audit/
ruff format figma_audit/
```

## Licence

MIT
