# Mondje

Carnet de transactions en FCFA pour une coopérative de trois agences (Douala, Yaoundé, Bafoussam). Il absorbe des exports de fichiers .txt et .json sans rien perdre ni compter deux fois.

Stack : FastAPI (Python 3.12) + PostgreSQL 16 + frontend JavaScript sans framework servi par l'API.

## Lancer le projet

```
docker compose up --build
```

Ouvrir http://localhost:8000. L'API est sous `/api`. Le schéma se crée tout seul au démarrage.

Mesurer les défis (avec `pip install httpx`) :

```
docker compose exec -T db psql -U mondje -d mondje < tools/seed.sql
python tools/bench.py all
```

`seed.sql` charge 1 000 000 de transactions pour les défis de recherche et de solde. Chaque mesure est enregistrée et visible dans l'onglet Résultats. Pour un défi seul : `python tools/bench.py import|errors|parallel|cancel|export|search|balance|memory`.

Test de coupure brutale (défi 12) : lancer un import de 200 000 lignes puis `docker compose kill -s KILL api && docker compose up -d api`.

## Aucune authentification

Il n'y a volontairement aucune authentification : n'importe qui peut tout lire, modifier, supprimer et annuler des imports. C'est assumé.

## Choix de scalabilité et de performance

- **Unicité des UUID** : colonne `uuid` clé primaire. Majuscules et minuscules désignent la même valeur. Toute insertion (formulaire, API, import, requêtes simultanées) passe par `ON CONFLICT (id) DO NOTHING`, jamais par un « vérifier puis insérer ».
- **Import en flux** : le fichier est reçu en flux brut sur disque (SHA-256 calculé au vol), puis lu par lots de 5 000 lignes. La mémoire reste constante quelle que soit la taille du fichier. Chaque lot est inséré avec un seul `INSERT ... SELECT FROM unnest(...)`.
- **Reprise après coupure** : l'offset en octets, le numéro de ligne et les compteurs sont mis à jour dans la même transaction que les insertions du lot. Au redémarrage, les imports `running` reprennent à l'offset enregistré, sans relire le début ni créer de doublon.
- **Import qui ne s'arrête jamais** : chaque ligne est validée individuellement. Une ligne rejetée est stockée avec son numéro, ses octets de début et de fin dans le fichier, la colonne fautive et la raison. Un clic sur l'erreur relit le fichier d'origine à cet endroit exact.
- **Parallélisme** : un worker asynchrone par fichier (10 maximum en même temps), l'analyse du lot s'exécute dans un thread. L'écriture du lot est sérialisée par un verrou consultatif (`pg_advisory_xact_lock`), ce qui évite les interblocages et garantit que l'ordre de validation suit l'ordre de `seq`.
- **Curseur qui ne ment pas** : les listes sont triées par `seq` décroissant et paginées par `seq < curseur`. Grâce au verrou ci-dessus, `seq` croît dans l'ordre de validation : une page ne peut ni répéter ni sauter une transaction pendant qu'un import tourne.
- **Même fichier envoyé deux fois** : index unique partiel sur le SHA-256 des imports `running` ou `done`. Le second envoi reçoit le même import et donc le même rapport.
- **Annulation chirurgicale** : toute modification ou suppression manuelle pose `manual_edit = true`. L'annulation supprime uniquement les lignes de l'import avec `manual_edit = false` et renvoie le nombre retiré.
- **Solde instantané** : table `daily_balance (jour, agence, net)` tenue à jour par des déclencheurs de niveau instruction (tables de transition), donc un seul calcul par lot. Le solde à une date est une somme sur au plus quelques centaines de lignes, jamais un recalcul de la table.
- **Recherche** : colonne `tsvector` générée et index GIN `(search, type, amount)` via `btree_gin`, qui combine mot, type et plage de montants dans un seul parcours.
- **Exports** : curseur serveur dans une transaction `REPEATABLE READ`, sérialisation par lots, réponse en flux. `.json` contient une transaction par ligne (même forme que l'import NDJSON), `.csv` les mêmes transactions.
- **Erreurs et logs** : chaque requête reçoit un identifiant (`X-Request-ID`) présent dans les logs et dans les réponses. Une donnée invalide renvoie 422 avec la raison. Une erreur serveur renvoie un message générique sans trace.
- **Pagination** : aucune réponse ne renvoie de liste non paginée (limite 200 maximum).

## Règle pour les conflits entre fichiers

Premier validé gagne. Quand le même UUID apparaît dans plusieurs fichiers (ou deux fois dans un fichier), la première ligne validée en base est conservée avec sa source (fichier et numéro de ligne). Les occurrences suivantes sont ignorées et comptées dans « doublons » du rapport du fichier concerné. Elles ne sont ni écrasées ni signalées comme erreurs. Dans un même lot, la ligne la plus basse gagne.

## Formats d'import

- `.txt` : `id;type;montant;description`, une transaction par ligne.
- `.json` : une transaction par ligne, chaque ligne étant un objet JSON complet (NDJSON). Un tableau JSON unique est refusé avec un message explicite.

## Limites connues

- Le solde est calculé par jour UTC de création, pas à l'heure près.
- La recherche par mot suit la configuration `simple` de PostgreSQL : pas de racinisation, pas d'insensibilité aux accents. Une recherche très sélective avec un tri par `seq` peut dépasser 200 ms si le planificateur choisit un mauvais plan.
- L'écriture des lots est sérialisée : le débit d'insertion total ne croît pas avec le nombre de fichiers simultanés.
- Un seul processus API. Les imports en cours sont reconnus au redémarrage, pas répartis sur plusieurs instances.
- Défis bonus non réalisés : upload reprenable (6), temps réel à 100 écrans (7), quasi-doublons (8). Les bonus export CSV et rapport d'erreurs en CSV sont faits ; le solde par agence aussi.
- Le détail des lignes rejetées est tronqué à 2 000 caractères, les lignes de plus de 64 Ko sont rejetées.

## Résultats des défis

Les mesures se génèrent avec `tools/bench.py` sur la machine de référence et s'affichent dans l'onglet Résultats. Reporter ici les valeurs obtenues avant la soumission.

| Défi | Résultat mesuré |
| --- | --- |
| 1 Export express | à renseigner |
| 2 Import au chrono | à renseigner |
| 3 Recherche sous 200 ms | à renseigner |
| 4 et 5 Import qui ne s'arrête jamais, clic sur l'erreur | à renseigner |
| 6 Trois agences | à renseigner |
| 7 Qui a envoyé quoi | à renseigner |
| 8 Double clic | à renseigner |
| 9 Annulation | à renseigner |
| 11 Solde | à renseigner |
| 12 Coupure en plein import | test manuel, à renseigner |
| 13 Mémoire sous 256 Mo | à renseigner |
| 14 Export après correction | à renseigner |
