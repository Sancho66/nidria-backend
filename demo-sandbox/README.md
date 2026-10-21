# Sandbox des 40 courriers, tâche 2c

État au 21/10/2026 : implémentation locale, aucune infrastructure créée ni livraison.
La base backend est `f278d1d` (v0.160.0), la base frontend `79e134b`, dont le code
app correspond à la production v1.244.1. Les composants de l'espace client et les
gestionnaires de lecture sont ceux de ces dépôts, sans copie de l'interface.

## Frontière de la démonstration

- Entrée dédiée `src.demo_sandbox.application:app`. `src.main` ne l'importe pas.
- Base distincte `nidria_demo_sandbox`, hôte privé `nidria-demo-db.internal`.
- 40 agences, 40 clients fictifs et 40 dossiers. Les imports passent par le vrai
  `JourneyImportManager`, puis les états approuvés sont matérialisés dans les mêmes
  tables que l'app. Les 40 codes, les 25 langues FR et les 15 EN sont conservés.
- Le code public ouvre un JWT expat limité au dossier, valable deux heures, sans
  refresh token. Les identifiants d'authentification appartiennent uniquement à la
  sandbox. Un second dossier rattaché par erreur invalide le lien.
- Seuls les gestionnaires GET du portail sont montés. Les mutations sont refusées
  avant leur exécution. RBAC, appartenance au dossier, rôle PostgreSQL SELECT et
  transactions en lecture seule se cumulent.
- Aucun ordonnanceur, webhook, envoi, checkout ou appel de signature. Les deux
  PDF par dossier sont générés à partir des données fictives et servis en mémoire.
- Le frontend utilise uniquement `/api`, relayé vers l'API sandbox fixe. CSP
  limitée à l'origine, noindex, no-store, aucun cookie. Jetons limités à l'onglet.
- Sur les écrans réels : bandeau de démonstration, écritures inertes, sécurité du
  compte et déconnexion masquées, car aucun compte personnel n'est créé par le visiteur.

## Mise en ligne préparée

Infrastructure proposée : une machine API 512 Mo, une machine PostgreSQL 1 Go
dans la région CDG, un volume de 1 Go, et le projet Pages `nidria-demo`.
PostgreSQL n'a aucun service Internet public. Une instance unique suffit pour ces
données fictives reconstructibles ; elle n'apporte pas de haute disponibilité.

Estimation : environ 12 à 15 USD/mois, hors taxes et trafic exceptionnel, à confirmer
à la création. Les [tarifs Fly](https://docs.fly.io/about/pricing) dépendent de la
région et de la durée de fonctionnement. Cette dépense nouvelle n'est pas engagée.

1. Après validation de cet hébergement, créer `nidria-demo-db` et `nidria-demo-api`
   dans l'organisation Fly existante. Ne jamais attacher la base de production.
2. Créer dans la base dédiée `nidria_demo_seed` (propriétaire) et
   `nidria_demo_reader` (aucun droit administratif, SELECT seulement). Mettre
   `default_transaction_read_only=on` sur le lecteur. Générer des mots de passe
   et trois secrets JWT indépendants. Aucun secret de production n'est réutilisé.
3. Par un tunnel Fly privé vers cette seule base, exécuter
   `python -m src.demo_sandbox.initialize` avec le rôle seed et les drapeaux mock
   du `fly.toml`. Cela applique les migrations, importe les 40 parcours et pose
   les politiques RLS SELECT du lecteur. Ne jamais passer ce rôle au serveur.
4. Enregistrer dans `nidria-demo-api` uniquement les deux URL du lecteur et les
   trois secrets JWT. Créer un jeton de déploiement limité à cette app et le
   stocker dans le secret GitHub `DEMO_FLY_API_TOKEN`, sans l'afficher.
5. Fusionner après CI, activer `DEMO_SANDBOX_DEPLOY_ENABLED=true` dans les deux
   dépôts et déclencher leurs workflows **Demo sandbox**. Ne pas déclencher le
   workflow backend **CI & Deploy** manuellement : il vise la production.
6. Raccorder `demo.nidria.com` au projet Pages, ou utiliser d'abord
   `nidria-demo.pages.dev`. Les deux origines sont autorisées par le code.
7. Exécuter le témoin navigateur frontend `scripts/check-demo-sandbox.mjs`
   sur la véritable origine. Comparer les empreintes au build CI. Contrôler les
   40 liens, le rechargement, 320 px, les langues, le cloisonnement, les PDF,
   la relance, la signature fictive et l'absence de cookies ou d'appels externes.
8. Seulement après ces contrôles, définir `DEMO_SANDBOX_ORIGIN` dans le dépôt
   frontend et publier sa release normale. Le Worker de production redirigera
   chaque ancien QR vers **le même code** dans la sandbox. Contrôler les 40 URL
   `app.nidria.com/demo/<code>` avant d'annoncer les lettres prêtes.

Retour arrière : vider `DEMO_SANDBOX_ORIGIN` et republier le frontend. Les pages
statiques précédentes restent le comportement par défaut ; aucun code n'est régénéré.

## Vérification et maintien

`python -m src.demo_sandbox.export_openapi` exporte le contrat isolé ; le contrat
de production `openapi.json` ne change pas. Les tests `tests/test_demo_sandbox.py`
utilisent PostgreSQL Testcontainers et le garde réseau obligatoire.

Le rôle seed est utilisé hors ligne uniquement. En cas de changement du catalogue,
préparer une nouvelle base fictive et la vérifier avant bascule : le script refuse
un catalogue différent ou un import partiel, il ne détruit rien automatiquement.
Les codes restent ceux du manifeste original. Pour chaque évolution de production,
repartir du même commit applicatif, appliquer les migrations à la sandbox via le
rôle seed et rejouer la recette avant sa republication. Les workflows dédiés ne
suivent pas automatiquement une release encore non validée.
