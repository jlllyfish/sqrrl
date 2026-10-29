# Sqrrl

Import des pièces jointes tableur (.xlsx, .xls, .ods) d'une démarche **Démarches Numériques** vers des tables **Grist** : une table par champ PJ, une ligne par ligne de fichier, avec `dossier_number` et `source_filename`.

## Fonctionnement

1. Saisie des tokens DS et Grist, du n° de démarche et de l'ID du document Grist.
2. Filtres : statuts (un ou plusieurs) et groupes instructeurs.
3. Analyse des 50 premiers dossiers : liste des champs PJ tableur (y compris dans les blocs répétables) avec aperçu et types déduits.
4. Import en flux (SSE) de tous les dossiers filtrés. Pour chaque champ :
   - la table est créée si besoin, les colonnes nouvelles sont ajoutées au fil de l'eau ;
   - doublons (même `dossier_number`) : **Ignorer**, **Remplacer** (nouvelles lignes insérées avant suppression des anciennes) ou **Ajouter quand même**.

Seule la première feuille de chaque classeur est lue.

## Installation

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python app.py
```

Puis http://localhost:5000.

Le [DSFR](https://github.com/GouvernementFR/dsfr/releases) doit être placé dans `static/dsfr/` (fichiers `dist`), et le logo dans `static/logo-sqrrl.svg`.

## Sécurité

Les tokens ne sont jamais stockés : ils sont envoyés en POST et conservés en mémoire le temps de démarrer l'import (5 min max). Ne pas lancer en production avec `FLASK_DEBUG=1`.
