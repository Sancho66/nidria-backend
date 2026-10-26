# Démonstration de l’agence complète, 26 octobre 2026

Lot demandé par Eric dans la tâche `0aedb97b-ea50-446c-b1af-1c817b9ccdbb`.
Ce document décrit le contrat du code ; la publication et ses preuves sont consignées séparément.
Les cinq cabinets pilotes et leurs six phrases doivent être fournis et validés avant leur création.
Aucun import massif ni lancement de campagne ne fait partie de ce lot.

## Interfaces et données

Le frontend sandbox compile les mêmes composants que la production. Le lien
`https://app.nidria.com/demo/<code>` ouvre l’agence. Le code reste dans chaque
sous-adresse ; la vue client porte aussi `?case=<id-fictif>`. Copier cette adresse
ou la rafraîchir dans un navigateur vierge rétablit le même dossier, sans compte.
Les anciens codes continuent d’ouvrir uniquement leur dossier client initial.

Le Worker de production relaie les pages, `/demo-assets/*` et `/demo-api/*` vers
la sandbox à origine fixe, sans cookie ni redirection vers un domaine technique.
Chaque nouveau cabinet reçoit une agence, trois collaborateurs, un prestataire,
dix clients et dix dossiers fictifs, six étapes issues des phrases fournies,
une vue enregistrée, des pièces PDF fictives, une signature et des rappels
préparés, un modèle documentaire, trois sociétés et deux campagnes simulées.
Les gestionnaires existants créent les parcours et dossiers. Les écrans sont les
routes de lecture de l’app ; l’aperçu IA est déterministe et le modèle de signature
montre ses zones fictives sans charger DocuSeal.

## Frontières

- La base, les trois secrets JWT et le compte lecteur restent propres à la sandbox.
- Les routes publiques métiers sont une liste explicite de GET. Leur dépendance
  vérifie à chaque appel le code, la version, l’agence, l’état et l’expiration.
  Les gestionnaires habituels appliquent ensuite leur périmètre d’agence.
- Le JWT client est limité au dossier choisi. Il n’ouvre pas l’interface agence.
  Un autre code, un dossier étranger, un JWT ordinaire ou un lien désactivé est refusé.
- Tous les verbes d’écriture métier sont refusés par le Worker, l’API et le rôle
  PostgreSQL SELECT seul. Les composants bloquent les actions avant le transport,
  les dépôts de fichiers et les liens email/téléphone ou externes ; l’inscription
  normale reste accessible avec `utm_source=demo`, sans code de prospect.
- Les adaptateurs externes restent simulés, sans identifiants, et les tâches
  planifiées sont arrêtées. Aucun email, paiement, webhook, Lemlist, IA ou DocuSeal.
- La seule écriture publique est le relevé d’ouverture, après validation du code,
  via un writer distinct. Il modifie seulement `sandbox_visits` : première et dernière
  ouverture, compteur, empreintes bornées de 512 nonces d’onglet. Ni IP, ni cookie,
  ni identifiant utilisateur. Le compteur mesure les ouvertures d’onglets, pas des
  personnes distinctes. Les rafraîchissements d’un onglet ne l’incrémentent pas.
- Les métadonnées de gestion, dont l’email prospect, sont retirées de `/agencies/me`.
  Aucun catalogue des codes n’est embarqué dans les assets sandbox.

## Import réservé à la plateforme

`POST /admin/demos/agency-batch`, permission `agency.create`, utilise le canal HMAC
existant. Le corps contient un UUID `request_id` et de un à cinq `items` :
`email`, `company_name`, `language` (en/fr), `profession` (accountant/lawyer/formation/relocation),
`country`, `city`, `persona`, six `steps` et éventuellement `logo_data_url`.
Le logo est une image encodée validée ; aucune URL externe n’est téléchargée.
L’interface accepte les colonnes CSV `m2_s1` à `m2_s6` et restitue `email;demo_link`.
La phrase complète reste dans la note de l’étape ; son titre est borné à 80 caractères.

La transaction extérieure du canal de gestion englobe tous les commits des
managers. Une répétition avec le même UUID et le même contenu restitue les codes
existants ; une réutilisation avec un autre contenu est refusée. Les agences ont
une expiration à 60 jours. La liste admin expose échéance, état et ouvertures.
La désactivation coupe aussi les jetons déjà émis.

## Mise à jour de la sandbox existante

Ne pas relancer `initialize` sur la base enrichie de nouvelles démos.

1. Fusionner le backend après les contrôles CI. Aucun changement de schéma
   n’est nécessaire pour ce lot.
2. Avant le déploiement, avec le propriétaire hors ligne de la seule base sandbox, exécuter
   `python -m src.demo_sandbox.prepare_management`. Les droits du writer couvrent
   les tables fictives déclarées dans `WRITABLE_TABLES`, sans DELETE ni droits
   d’administration/RBAC. Le lecteur public ne change pas. Cette commande pose
   également les nouvelles liaisons RBAC et les permissions email de l’admin fictif.
3. Exécuter `python -m src.demo_sandbox.repair_labels` dans le même environnement
   hors ligne. Seuls les anciens noms fictifs FR exactement égaux à « Demo Equipe »
   et « Client Demonstration » sont accentués ; aucun code ni parcours ne change.
4. Déployer l’API sandbox avec son workflow dédié, puis publier le backend de
   production, le frontend sandbox et le frontend normal (outil admin et proxy).
5. Vérifier les codes anciens et nouveaux, les adresses copiées, 320 px, les deux
   langues, les PDF fictifs, les refus croisés et l’absence d’écriture métier.
6. Importer les cinq lignes validées ; remettre les cinq liens et le CSV à Eric.
   Attendre sa validation avant tout lot supplémentaire.

Tests ciblés : `tests/test_agency_demo.py`, `tests/test_demo_sandbox.py`,
`tests/test_demo_admin.py`. Les tests utilisent PostgreSQL éphémère et la garde réseau.
Les deux contrats OpenAPI et les types frontend doivent être régénérés ensemble.
