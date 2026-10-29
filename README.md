# Sqrrl

Import des pièces jointes tableur (.xlsx, .xls, .ods) d'une démarche **Démarches Numériques** vers des tables **Grist** : une table par champ PJ, une ligne par ligne de fichier, avec `dossier_number` et `source_filename`.

## Fonctionnement

1. Saisie des tokens DS et Grist, du n° de démarche et de l'ID du document Grist.
2. Filtres : statuts (un ou plusieurs) et groupes instructeurs.
   Le token DS et le numéro de démarche sont vérifiés à la saisie (titre affiché), de même que le token et l'ID Grist (nom du document affiché).
3. Analyse : tous les champs PJ du schéma de la démarche (y compris blocs répétables), avec un aperçu pris dans les 100 dossiers les plus récents. Un champ ajouté tardivement à la démarche est donc bien proposé.
4. Import en flux (SSE) de tous les dossiers filtrés. Pour chaque champ :
   - la table est créée si besoin, les colonnes nouvelles sont ajoutées au fil de l'eau ;
   - doublons (même `dossier_number`) : **Ignorer**, **Remplacer** (nouvelles lignes insérées avant suppression des anciennes) ou **Ajouter quand même**.

### Tableurs mal formatés

Seule la première feuille est lue. Sont gérés automatiquement :
- en-têtes écrits différemment d'un fichier à l'autre (retours à la ligne, espaces, ponctuation) : rapprochés dans la même colonne Grist ;
- blocs de colonnes recopiés côte à côte au lieu d'ajouter des lignes : empilés ;
- lignes de titre au-dessus du tableau ignorées, ligne d'en-tête repérée automatiquement ;
- fichiers sans en-tête : colonnes nommées d'après le fichier montré en aperçu (même ordre) ;
- colonnes et lignes vides, nombres entiers lus comme décimaux.

Options : ignorer les lignes sans valeur en 1re colonne (totaux, notes), supprimer les espaces dans les valeurs texte (identifiants type « FR64 1514 3204 »).

## Installation

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python app.py
```

Puis http://localhost:5000.

`static/dsfr/` contient une version allégée du [DSFR](https://github.com/GouvernementFR/dsfr) : CSS principal, polices Marianne (woff2), icônes système et icônes utilisées par les composants. Pas de JS DSFR (aucun composant interactif).

## Sécurité

Les tokens ne sont jamais stockés : ils sont envoyés en POST et conservés en mémoire le temps de démarrer l'import (5 min max). Ne pas lancer en production avec `FLASK_DEBUG=1`.
