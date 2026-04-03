# CLAUDE.md — figma-audit

## Projet

**figma-audit** est un outil CLI Python qui automatise la vérification de conformité entre un design Figma et une application web déployée. Il compare sémantiquement (pas du pixel-diff) les écrans Figma aux pages réelles de l'application et produit un rapport d'écarts.

### Positionnement

Aucun outil existant ne fait cette comparaison sémantique end-to-end. Les outils de visual regression (BackstopJS, Percy, Chromatic, Applitools) comparent deux versions de la même app entre elles — pas un design Figma contre une implémentation. figma-audit comble ce vide.

### Licence

Open source (MIT). Potentiel futur : service cloud hébergé optionnel (historique, dashboard, CI/CD integration).

---

## Architecture

```
figma-audit/
├── figma_audit/
│   ├── __init__.py
│   ├── __main__.py              # CLI (click ou typer)
│   ├── config.py                # Dataclass de config, chargement YAML
│   ├── models.py                # Dataclasses: Page, FigmaScreen, Mapping, Discrepancy, etc.
│   ├── phases/
│   │   ├── __init__.py
│   │   ├── analyze_code.py      # Phase 1 : analyse du code source
│   │   ├── export_figma.py      # Phase 2 : export Figma via API REST
│   │   ├── match_screens.py     # Phase 3 : matching Figma ↔ routes (AI)
│   │   ├── capture_app.py       # Phase 4 : screenshots app via Playwright
│   │   ├── compare.py           # Phase 5 : comparaison hybride (programmatique + AI)
│   │   └── report.py            # Phase 6 : génération du rapport
│   └── utils/
│       ├── __init__.py
│       ├── figma_client.py      # Client REST Figma (requests)
│       ├── claude_client.py     # Client Claude API (SDK anthropic, vision)
│       ├── browser.py           # Playwright : navigation, capture, extraction DOM
│       ├── color.py             # Conversions et comparaisons de couleurs
│       └── image.py             # PIL : extraction couleurs dominantes, sampling
├── templates/
│   └── report.html.j2           # Template Jinja2 du rapport HTML
├── tests/
│   ├── __init__.py
│   ├── test_figma_client.py
│   ├── test_color.py
│   └── test_models.py
├── pyproject.toml
├── README.md
└── CLAUDE.md                    # Ce fichier
```

### Dépendances

```toml
[project]
name = "figma-audit"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "anthropic>=0.40",       # Claude API (vision)
    "playwright>=1.40",      # Navigation et capture
    "requests>=2.31",        # Client HTTP Figma
    "Pillow>=10.0",          # Analyse d'images (couleurs)
    "click>=8.1",            # CLI
    "pydantic>=2.0",         # Modèles de données
    "Jinja2>=3.1",           # Templates de rapport
    "pyyaml>=6.0",           # Config et données de test
    "rich>=13.0",            # Affichage console
]
```

---

## Les 6 phases

Chaque phase lit ses entrées depuis le répertoire de sortie (`--output`) et y écrit ses résultats. On peut relancer n'importe quelle phase indépendamment.

### Phase 1 : Analyse du code (`analyze_code.py`)

**Entrée** : chemin du projet (`--project`)
**Sortie** : `output/pages_manifest.json`
**Méthode** : AI (Claude API)

L'IA reçoit les fichiers du routeur et des pages, et produit un manifest structuré :

```json
{
  "framework": "flutter",
  "renderer": "canvaskit|html",
  "pages": [
    {
      "id": "courses_list",
      "route": "/courses",
      "name": "CoursesListPage",
      "file": "features/courses/presentation/pages/courses_list_page.dart",
      "auth_required": true,
      "description": "Liste des courses disponibles avec filtres",
      "params": [],
      "required_state": {
        "description": "Utilisateur connecté avec au moins 1 course disponible",
        "data_dependencies": ["courses list from API"]
      },
      "navigation_steps": [
        {"action": "navigate", "url": "/courses"}
      ],
      "form_fields": [],
      "interactive_states": ["empty", "loading", "populated", "error"]
    },
    {
      "id": "create_course",
      "route": "/courses/ambulance/new",
      "name": "CreateAmbulanceCoursePage",
      "auth_required": true,
      "description": "Wizard 4 étapes de création de course ambulance",
      "params": [{"name": "initialVehicleType", "type": "enum", "optional": true}],
      "navigation_steps": [
        {"action": "navigate", "url": "/courses/ambulance/new"}
      ],
      "form_fields": [
        {"name": "pickup_address", "type": "address", "step": 1},
        {"name": "destination_address", "type": "address", "step": 1},
        {"name": "date", "type": "datetime", "step": 2},
        {"name": "patient_name", "type": "text", "step": 3}
      ],
      "interactive_states": ["step1", "step2", "step3", "step4_confirm"]
    }
  ],
  "design_tokens": {
    "source_file": "shared/theme/design_tokens.dart",
    "colors": {"primary": "#1A1A2E", "accent": "#00C9B1"},
    "fonts": {"family": "Outfit", "weights": [400, 500, 600, 700]},
    "spacing_scale": [4, 8, 12, 16, 20, 24, 32, 40, 48, 64],
    "border_radius": {"sm": 4, "md": 8, "lg": 12, "xl": 16}
  },
  "test_data": {
    "phone": "+33612345678",
    "otp": "1234",
    "email": "test@example.com",
    "addresses": {
      "pickup": "12 rue de la Paix, 75002 Paris",
      "destination": "Hôpital Saint-Louis, 1 Avenue Claude Vellefaux, 75010 Paris"
    },
    "patient_name": "Jean Dupont"
  }
}
```

**Stratégie de prompt** : envoyer à Claude le fichier routeur + les fichiers page (un par un si trop gros) + le fichier de design tokens. Demander une réponse JSON stricte avec le schéma ci-dessus.

**Important** : Les `navigation_steps` sont le script de navigation que Playwright exécutera. Pour les pages nécessitant un état complexe (ex: détail d'une course), l'IA doit décrire les étapes pour y arriver depuis la page d'accueil.

### Phase 2 : Export Figma (`export_figma.py`)

**Entrée** : URL Figma + token API (ou cache local existant)
**Sortie** : `output/figma_screens/*.png` + `output/figma_manifest.json`
**Méthode** : 100% programmatique (Figma REST API, pas d'IA)

#### Rate limiting Figma — CRITIQUE

L'API Figma a des rate limits très restrictifs (~30 req/min pour les exports d'images). La stratégie est le **download complet en une passe, puis travail 100% local** :

1. **Phase de téléchargement (online, patiente)** :
   - `GET /v1/files/{key}` → sauvegarder l'arbre complet dans `output/figma_raw/file.json` (1 seul appel)
   - `GET /v1/files/{key}/images` → sauvegarder les image fills dans `output/figma_raw/image_fills.json`
   - Identifier tous les node IDs d'écrans à exporter
   - Batch les exports d'images : `GET /v1/images/{key}?ids={id1,id2,...}&scale=2` (grouper par lots de 5-10 IDs max par requête pour réduire le nombre d'appels)
   - **Délai entre chaque requête** : 3 secondes minimum, avec backoff exponentiel sur HTTP 429
   - **Retry avec patience** : sur 429, attendre le temps indiqué par le header `Retry-After`, sinon 60 secondes par défaut
   - Afficher une barre de progression avec estimation du temps restant (`rich.progress`)
   - Temps estimé pour un fichier de 30 écrans : ~3-5 minutes

2. **Cache local** :
   - Tout est sauvegardé dans `output/figma_raw/` (JSON + PNGs)
   - Si le cache existe et que le `lastModified` du fichier Figma n'a pas changé → **skip le téléchargement**
   - Option `--force-refresh` pour forcer le re-téléchargement
   - Option `--offline` pour travailler uniquement sur le cache (aucun appel Figma)

3. **Phase d'analyse (offline, rapide)** :
   - Parser `figma_raw/file.json` localement
   - Extraire les tokens, propriétés, structure
   - Aucun appel API

#### Structure du cache Figma

```
output/figma_raw/
├── file.json                 # Arbre complet du fichier Figma
├── file_meta.json            # {lastModified, version, downloadedAt}
├── image_fills.json          # Références des image fills
└── exports/                  # PNGs bruts par node ID
    ├── 123_456.png
    ├── 123_789.png
    └── ...
```

#### Étapes détaillées

1. Vérifier le cache : si `file_meta.json` existe, comparer `lastModified` via `GET /v1/files/{key}` (headers only, pas coûteux)
2. Si cache valide → passer directement à l'analyse locale
3. Sinon, téléchargement complet :
   a. `GET /v1/files/{key}` → `file.json` (arbre complet)
   b. Identifier les frames de premier niveau dans chaque page (= écrans probables)
   c. Batch export des PNGs : grouper les node IDs, 5-10 par requête, 3s entre chaque
   d. Télécharger chaque URL retournée → `exports/{node_id}.png`
4. Analyse locale de `file.json` — pour chaque frame/écran :
   - Extraire les propriétés : nom, dimensions, couleur de fond
   - Parcourir les enfants pour extraire les design tokens utilisés :
     - Couleurs (fills, strokes)
     - Typographie (fontFamily, fontSize, fontWeight, letterSpacing, lineHeight)
     - Spacing (padding, itemSpacing dans les auto-layouts)
     - Border radius
5. Produire `figma_manifest.json` :

```json
{
  "file_key": "6kTFQMSueuk1dSDgiuMur9",
  "file_name": "MedCorp - MedCourse",
  "screens": [
    {
      "id": "123:456",
      "name": "Splash Screen v2",
      "page": "MedExchange",
      "width": 390,
      "height": 844,
      "image_path": "figma_screens/splash-screen-v2.png",
      "background_color": "#0D0D1D",
      "elements": [
        {
          "type": "TEXT",
          "content": "MedCourses",
          "font_family": "Outfit",
          "font_size": 32,
          "font_weight": 700,
          "color": "#FFFFFF",
          "bounds": {"x": 95, "y": 380, "w": 200, "h": 40}
        },
        {
          "type": "RECTANGLE",
          "fill": "#3A82F7",
          "corner_radius": 12,
          "bounds": {"x": 40, "y": 700, "w": 310, "h": 52}
        }
      ]
    }
  ]
}
```

**Heuristique pour détecter les écrans** : les frames de premier niveau dans les pages Figma. Ignorer les frames nommées avec des préfixes conventionnels de composants (ex: commençant par `_`, `Component/`, `Icon/`). La taille est un indice : un frame de 390x844 est probablement un écran mobile.

### Phase 3 : Matching Figma ↔ Routes (`match_screens.py`)

**Entrée** : `pages_manifest.json` + `figma_manifest.json` + images Figma
**Sortie** : `output/screen_mapping.yaml` (éditable par l'humain)
**Méthode** : AI (Claude Vision)

L'IA reçoit :
- La liste des routes avec descriptions (du manifest)
- Chaque screenshot Figma avec son nom

Elle produit un mapping :

```yaml
# Mapping Figma → Routes
# Vérifié par un humain : oui/non
# Date : 2026-04-03
verified: false

mappings:
  - figma_screen_id: "123:456"
    figma_screen_name: "Splash Screen v2"
    route: "/welcome"
    page_id: "welcome"
    confidence: 0.95
    notes: "Écran d'accueil avec logo et bouton Commencer"

  - figma_screen_id: "123:789"
    figma_screen_name: "Login / Création compte"
    route: "/signin"
    page_id: "auth"
    confidence: 0.90
    notes: "Page unifiée login/inscription avec champ téléphone"

  - figma_screen_id: "123:101"
    figma_screen_name: "Mode invité - Courses"
    route: null
    page_id: null
    confidence: 0.0
    notes: "Pas de correspondance trouvée - peut-être un ancien design"
```

**Point de contrôle humain** : le fichier YAML est généré avec `verified: false`. L'outil refuse de continuer en phase 4 tant que `verified` n'est pas mis à `true`. L'utilisateur review le mapping, corrige si besoin, puis valide.

### Phase 4 : Capture de l'app (`capture_app.py`)

**Entrée** : `pages_manifest.json` + `screen_mapping.yaml` (verified) + URL de l'app
**Sortie** : `output/app_screenshots/*.png` + `output/app_styles.json`
**Méthode** : Playwright (programmatique, pas d'IA)

Étapes :
1. Lancer un navigateur Playwright (Chromium)
2. Dimensionner le viewport aux dimensions du Figma (ex: 390x844 pour mobile)
3. Si auth requise : exécuter le flow de login avec les `test_data` du manifest
4. Pour chaque route mappée :
   a. Exécuter les `navigation_steps` du manifest
   b. Attendre le chargement (`networkidle` ou sélecteur spécifique)
   c. Prendre un screenshot
   d. **Extraire les styles computed** (si DOM disponible, pas CanvasKit) :
      ```javascript
      // Exécuté via page.evaluate()
      const elements = document.querySelectorAll('*');
      // Pour chaque élément visible, extraire :
      // - tagName, textContent
      // - computedStyle: color, backgroundColor, fontFamily, fontSize,
      //   fontWeight, padding, margin, borderRadius
      // - boundingClientRect: x, y, width, height
      ```
   e. Sauvegarder screenshot + styles JSON

**Gestion Flutter CanvasKit** : si `renderer == "canvaskit"` dans le manifest, le DOM n'est pas exploitable. Dans ce cas :
- Ne pas extraire les styles computed (pas de DOM utile)
- Se reposer uniquement sur les screenshots + analyse vision en phase 5
- Alternative : recommander à l'utilisateur de builder avec `--web-renderer html` pour l'audit

**Gestion des états interactifs** : si une page a des `interactive_states` (ex: wizard multi-étapes), capturer chaque état séparément (screenshot + styles pour chaque étape).

### Phase 5 : Comparaison (`compare.py`)

**Entrée** : screenshots Figma + screenshots app + `figma_manifest.json` + `app_styles.json`
**Sortie** : `output/discrepancies.json`
**Méthode** : hybride (programmatique + AI)

#### Étape A : Comparaison programmatique (si styles DOM disponibles)

Pour chaque paire (écran Figma, page app) :
1. Comparer les couleurs : tokens Figma vs computed styles
   - Utiliser `deltaE` (CIE2000) pour la distance colorimétrique
   - Seuil : deltaE < 3 = identique, 3-10 = proche, >10 = différent
2. Comparer les fonts : family, size, weight
3. Comparer les border-radius
4. Détecter les textes manquants ou différents

#### Étape B : Comparaison par vision (toujours, y compris si étape A disponible)

Envoyer à Claude Vision :
- L'image Figma
- L'image app
- Les design tokens extraits du Figma (étape 2)
- Les résultats de la comparaison programmatique (étape A, si disponible)
- Les styles computed de l'app (si disponibles)

**Prompt structuré** demandant une analyse par catégorie :

```
Analyse ces deux images. La première est le design Figma (référence). La seconde est l'implémentation réelle.

Compare élément par élément sur ces critères :
1. LAYOUT : disposition générale, alignement, structure
2. COULEURS : fonds, textes, boutons, icônes (valeurs Figma fournies ci-dessous)
3. TYPOGRAPHIE : police, taille, graisse, espacement
4. COMPOSANTS : boutons, champs, cartes, icônes - présence et style
5. TEXTES : contenu textuel, labels, placeholder
6. SPACING : marges, paddings, écarts entre éléments
7. ÉLÉMENTS MANQUANTS : ce qui est dans le Figma mais pas dans l'app
8. ÉLÉMENTS AJOUTÉS : ce qui est dans l'app mais pas dans le Figma

Pour chaque écart, indique :
- category : une des 8 ci-dessus
- description : description concise de l'écart
- severity : critical | important | minor
- figma_value : la valeur attendue (si quantifiable)
- app_value : la valeur constatée (si quantifiable)
- location : zone de l'écran concernée (top/center/bottom + left/center/right)

Critères de sévérité :
- critical : l'élément trahit l'intention du designer (mauvais composant, élément manquant, palette incorrecte)
- important : écart visible qui dégrade l'expérience (mauvais weight, spacing notable, icône incorrecte)
- minor : nuance subtile, différence cosmétique peu visible

Réponds en JSON uniquement.
```

#### Sortie

```json
{
  "comparisons": [
    {
      "page_id": "welcome",
      "route": "/welcome",
      "figma_screen": "Splash Screen v2",
      "figma_image": "figma_screens/splash-screen-v2.png",
      "app_image": "app_screenshots/welcome.png",
      "discrepancies": [
        {
          "category": "COULEURS",
          "description": "Le bouton principal utilise un bleu plus foncé que le design",
          "severity": "important",
          "figma_value": "#3A82F7",
          "app_value": "#2563EB",
          "location": "bottom-center"
        },
        {
          "category": "TYPOGRAPHIE",
          "description": "Le titre utilise font-weight 600 au lieu de 700",
          "severity": "minor",
          "figma_value": "Outfit Bold (700)",
          "app_value": "Outfit SemiBold (600)",
          "location": "center"
        }
      ],
      "overall_fidelity": "good",
      "summary": "L'écran respecte globalement le design. 2 écarts mineurs sur les couleurs et la typographie."
    }
  ],
  "statistics": {
    "total_screens": 4,
    "total_discrepancies": 12,
    "by_severity": {"critical": 1, "important": 4, "minor": 7},
    "by_category": {"COULEURS": 3, "TYPOGRAPHIE": 4, "SPACING": 2, "COMPOSANTS": 1, "ÉLÉMENTS_MANQUANTS": 2}
  }
}
```

### Phase 6 : Rapport (`report.py`)

**Entrée** : `discrepancies.json` + toutes les images
**Sortie** : `output/report.html`
**Méthode** : Jinja2 (programmatique) + AI pour le résumé exécutif

Le rapport HTML contient :
1. **Résumé exécutif** : score global, tendances, recommandations prioritaires (généré par AI)
2. **Vue d'ensemble** : tableau récapitulatif par écran (nombre d'écarts, sévérité max)
3. **Détail par écran** :
   - Side-by-side : image Figma | image app (images encodées en base64 dans le HTML)
   - Liste des écarts avec sévérité (couleur-codée)
   - Valeurs attendues vs constatées
4. **Statistiques** : graphiques par catégorie et sévérité

Le rapport est un fichier HTML autonome (pas de dépendances externes), ouvrable dans n'importe quel navigateur.

---

## CLI

```bash
# Installation
pip install -e .
playwright install chromium

# Pipeline complet
figma-audit run \
  --project ~/dev/medcorp/MedExchange \
  --figma-url "https://www.figma.com/design/6kTFQMSueuk1dSDgiuMur9/..." \
  --figma-token "figd_xxx" \
  --app-url "https://medexchange.kaseilabs.com" \
  --output ./audit-results

# Phases individuelles
figma-audit analyze   --project ~/dev/medcorp/MedExchange --output ./audit-results
figma-audit figma     --figma-url "..." --figma-token "..." --output ./audit-results  # DL complet + cache
figma-audit figma     --offline --output ./audit-results                           # Analyse locale uniquement
figma-audit figma     --force-refresh --output ./audit-results                     # Force re-téléchargement
figma-audit match     --output ./audit-results
figma-audit capture   --app-url "https://..." --output ./audit-results
figma-audit compare   --output ./audit-results
figma-audit report    --output ./audit-results

# Reprendre depuis une phase (les phases précédentes doivent avoir leurs fichiers de sortie)
figma-audit run --from compare --output ./audit-results
```

### Configuration

Fichier optionnel `figma-audit.yaml` à la racine du projet audité ou dans `--output` :

```yaml
project: ~/dev/medcorp/MedExchange
figma_url: "https://www.figma.com/design/6kTFQMSueuk1dSDgiuMur9/..."
figma_token: "figd_xxx"  # ou variable d'env FIGMA_TOKEN
app_url: "https://medexchange.kaseilabs.com"
anthropic_api_key: "${ANTHROPIC_API_KEY}"  # variable d'env
output: ./audit-results

# Options
viewport:
  width: 390
  height: 844
  device_scale_factor: 2

# Filtres (optionnel)
include_routes:
  - /welcome
  - /signin
  - /courses
  - /account
exclude_routes: []

# Figma rate limiting
figma:
  cache_dir: ./figma_raw           # Cache local des données Figma
  request_delay: 3.0               # Secondes entre chaque requête API
  batch_size: 8                    # Nombre de node IDs par requête d'export
  retry_wait_default: 60           # Secondes d'attente sur 429 si pas de Retry-After
  max_retries: 5                   # Nombre max de retries par requête

# Seuils
thresholds:
  color_delta_e: 5.0        # deltaE CIE2000 max pour considérer "même couleur"
  font_size_tolerance: 2    # px de tolérance sur font-size
  spacing_tolerance: 4      # px de tolérance sur spacing
```

---

## MVP : Scope initial

Le MVP couvre **4 écrans** pour valider le concept :

1. **Welcome** (`/welcome`) — écran statique, facile, bon premier test
2. **Login/Auth** (`/signin`) — formulaire simple, teste la saisie
3. **Courses list** (`/courses`) — données dynamiques, layout complexe
4. **Account** (`/account`) — navigation interne, sous-sections

### Critères de validation du MVP

- [ ] Phase 1 produit un manifest correct pour les 4 pages
- [ ] Phase 2 exporte les bons écrans Figma avec leurs tokens
- [ ] Phase 3 matche correctement les écrans (validation humaine)
- [ ] Phase 4 capture les 4 pages avec login automatique
- [ ] Phase 5 identifie des écarts réels (pas que du bruit)
- [ ] Phase 6 produit un rapport HTML lisible et utile
- [ ] Le faux-positif rate est < 30% (sur les écarts reportés, <30% sont du bruit)

---

## Décisions techniques

### Pourquoi le SDK `anthropic` et pas le CLI `claude`

- Réponses JSON structurées (pas de parsing de texte)
- Gestion fine des erreurs et retries
- Envoi d'images en base64 natif
- Contrôle du modèle, température, max_tokens
- Le CLI est conçu pour l'interactif, pas le programmatique

### Pourquoi Playwright et pas Selenium/Puppeteer

- API Python native et bien maintenue
- `page.evaluate()` pour extraire les computed styles
- `networkidle` pour attendre le chargement complet
- Headless performant, screenshots de qualité

### Pourquoi pas de pixel-diff

Le pixel-diff (SSIM, perceptual hash) génère trop de faux positifs :
- Différences de rendu navigateur (anti-aliasing, font hinting)
- Données réelles vs placeholder
- Responsive vs maquette fixe

La comparaison sémantique (par vision + tokens) détecte les vrais écarts d'intention.

### Comparaison programmatique vs vision

Les deux sont complémentaires :
- **Programmatique** : précis pour couleurs (deltaE), fonts (exact match), dimensions mesurables. Mais ne fonctionne que si le DOM est inspectable.
- **Vision** : comprend le layout, l'intention, les éléments manquants/ajoutés. Imprécis sur les mesures exactes. Fonctionne sur n'importe quelle app (même canvas).

L'outil utilise les deux quand c'est possible, la vision seule sinon.

---

## Structure des données intermédiaires

```
output/
├── pages_manifest.json      # Phase 1
├── figma_manifest.json      # Phase 2
├── figma_screens/            # Phase 2
│   ├── splash-screen-v2.png
│   ├── login-creation-compte.png
│   └── ...
├── screen_mapping.yaml       # Phase 3 (éditable, point de contrôle humain)
├── app_screenshots/          # Phase 4
│   ├── welcome.png
│   ├── signin.png
│   └── ...
├── app_styles.json           # Phase 4 (si DOM inspectable)
├── discrepancies.json        # Phase 5
└── report.html               # Phase 6
```

---

## Commandes de développement

```bash
# Setup
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium

# Tests
pytest tests/
pytest tests/ -x -v          # Stop au premier échec, verbose

# Lint
ruff check figma_audit/
ruff format figma_audit/

# Type check
mypy figma_audit/
```

---

## Principes de code

- Python 3.11+, type hints partout
- Pydantic pour les modèles de données (validation, sérialisation JSON)
- `async` pour les I/O (Playwright est async, les appels API aussi)
- Logging avec `rich` pour le feedback console
- Chaque phase est une fonction `async def run(config: Config, output_dir: Path) -> Path` qui retourne le chemin du fichier produit
- Les appels Claude API doivent être idempotents : même entrée = même sortie (température 0)
- Gestion des erreurs : si un appel API échoue, retry 3 fois avec backoff exponentiel, puis skip la page et la marquer en erreur dans le rapport

## Notes pour l'implémentation

### Détection du framework

La phase 1 doit d'abord détecter le framework (Flutter, React, Vue, Angular, etc.) en cherchant les marqueurs :
- `pubspec.yaml` → Flutter
- `package.json` avec `react`/`next`/`vue`/`angular` → framework JS correspondant

Pour le MVP, seul Flutter est supporté. La détection d'autres frameworks est préparée mais pas implémentée.

### Extraction des tokens Figma

L'API Figma retourne un arbre de nœuds. Pour extraire les tokens utiles :
- `node.fills[].color` → couleurs de fond (format RGBA 0-1, convertir en hex)
- `node.style` (sur les TEXT nodes) → `fontFamily`, `fontSize`, `fontWeight`, `letterSpacing`, `lineHeightPx`
- `node.cornerRadius` → border radius
- `node.paddingLeft/Right/Top/Bottom` → padding (auto-layout)
- `node.itemSpacing` → gap entre enfants (auto-layout)

### Matching sémantique (phase 3)

Le prompt de matching doit inclure :
- Les noms des écrans Figma (souvent descriptifs : "Splash Screen", "Login", etc.)
- Les descriptions des routes (du manifest)
- Les screenshots Figma (pour que l'IA puisse voir le contenu)

Un bon matching repose sur : le nom Figma + le contenu visuel + la description de la route.

### Navigation Playwright (phase 4)

Le script de navigation est généré par la phase 1. Format des actions :

```json
[
  {"action": "navigate", "url": "/welcome"},
  {"action": "wait", "selector": "button:has-text('Commencer')"},
  {"action": "screenshot", "name": "welcome"},
  {"action": "click", "selector": "button:has-text('Commencer')"},
  {"action": "fill", "selector": "input[type='tel']", "value": "+33612345678"},
  {"action": "click", "selector": "button[type='submit']"},
  {"action": "fill", "selector": "input.otp", "value": "1234"},
  {"action": "wait", "timeout": 2000},
  {"action": "screenshot", "name": "courses_list"}
]
```

**Attention Flutter web** : les sélecteurs CSS standard ne fonctionnent pas avec CanvasKit. Pour le renderer HTML, Flutter génère des éléments `<flt-*>` avec des attributs spécifiques. Il faudra adapter les sélecteurs ou utiliser les coordonnées (moins robuste).

Alternative : utiliser le Semantics tree de Flutter (accessibility) pour trouver les éléments. Les widgets avec `Semantics` label sont détectables par les outils d'accessibilité et donc par Playwright (`getByRole`, `getByLabel`).

### Gestion du coût API

- Utiliser `claude-sonnet-4-6-20250514` (pas Opus) pour les analyses vision : bon rapport qualité/coût
- Redimensionner les images avant envoi (max 1568px de côté, recommandation Anthropic)
- Mettre en cache les résultats : si le screenshot Figma n'a pas changé, ne pas ré-analyser
- Afficher le coût estimé avant de lancer la phase 5 (nombre d'appels × coût moyen)
