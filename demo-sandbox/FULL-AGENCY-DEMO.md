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
préparés, un modèle documentaire et trois sociétés. Le module Email est absent de la démo.
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
  via un writer distinct. Il modifie `sandbox_visits` et `sandbox_tour` : première et dernière
  ouverture, compteur, empreintes bornées de 512 nonces d’onglet. Ni IP, ni cookie,
  ni identifiant utilisateur. Le compteur mesure les ouvertures d’onglets, pas des
  personnes distinctes. La visite ajoute son état, sa dernière étape et ses dates de début/fin, sans données personnelles supplémentaires. Les rafraîchissements d’un onglet ne l’incrémentent pas.
- Les métadonnées de gestion, dont l’email prospect, sont retirées de `/agencies/me`.
  Aucun catalogue des codes n’est embarqué dans les assets sandbox.

## Import réservé à la plateforme

`POST /admin/demos/agency-batch`, permission `agency.create`, utilise le canal HMAC
existant. Le corps contient un UUID `request_id` et de un à cinq `items` :
`email`, `company_name`, `language` (en/fr), `profession` (accountant/lawyer/formation/relocation),
`country`, `city`, `persona`, six `steps` et éventuellement `logo_data_url`.
Le logo est une image encodée validée ; aucune URL externe n’est téléchargée.
L’interface accepte les colonnes CSV `m2_s1` à `m2_s6` et restitue `email;demo_link`.
La phrase complète reste dans la note de l’étape (« Given to the client »).

Complément du 26/10, tâche `cd772589-f874-4a8d-bc48-282688ac7b1a` :

- CSV facultatif `step_title_1` à `step_title_6`, transmis dans `step_titles` :
  deux à cinq mots par titre, au plus 80 caractères. Une cellule vide prend le
  titre court du métier. La phrase longue n'est jamais tronquée pour faire un titre.
- CSV facultatif `journey_name` : titre du parcours (200 caractères maximum).
- CSV facultatif `documents` : noms exacts séparés par `|`, transmis en liste.
  Pas de libellé vide ou dupliqué, au plus 60 pièces et 200 caractères par nom.
  Les exigences, pièces présentes/manquantes et PDF fictifs utilisent cette même
  liste. Sans cette colonne, les pièces génériques du métier restent le repli.
- Noms fictifs déterministes selon le pays et le cabinet. Adresses lisibles sur
  un domaine `.example` réservé à chaque agence, suffixe numérique si homonyme ;
  aucun code public dans une adresse. Aucun client fictif partagé entre agences.
- Équipe : un administrateur, un gestionnaire de dossiers, un membre et un
  prestataire, avec les rôles système existants. Aucune modification des permissions.
- Pays du parcours et de la session renseigné. L'anglais est harmonisé en
  `advisor` pour US/CA, `adviser` pour GB/IE/AU/NZ. Le pays de la démo remplace le
  drapeau anglais générique sur le parcours ; le choix des langues reste disponible.
- Les actions de démonstration restent cliquables et montrent le refus habituel,
  y compris celles sans saisie préalable. Les sections mot de passe et double
  authentification du profil sont absentes. Les protections API/SQL ne changent pas.

Ce complément ne réécrit pas les données des démos existantes. Les cinq nouveaux
pilotes doivent être recréés depuis le CSV validé de Claude avant la clôture de la
recette commerciale. Ne pas générer le lot complet sans la validation d'Eric.

La transaction extérieure du canal de gestion englobe tous les commits des
managers. Une répétition avec le même UUID et le même contenu restitue les codes
existants ; une réutilisation avec un autre contenu est refusée. Les agences ont
une expiration choisie à l’import (30, 60 ou 90 jours, 60 par défaut). La liste admin expose lot, échéance, état, ouvertures et progression de la visite.
La désactivation coupe aussi les jetons déjà émis.

## Mise à jour de la sandbox existante

Ne pas relancer `initialize` sur la base enrichie de nouvelles démos.

1. Fusionner le backend après les contrôles CI. La table `demo_history` est créée
   uniquement dans la base sandbox, hors des migrations de production.
2. Avant le déploiement, avec le propriétaire hors ligne de la seule base sandbox, exécuter
   `python -m src.demo_sandbox.prepare_management`. Les droits du writer couvrent
   les tables fictives déclarées dans `WRITABLE_TABLES` et les DELETE de `DELETE_TABLES`
   nécessaires à la purge. Aucun droit TRUNCATE, administration ou écriture RBAC.
   Les tables de jetons ont uniquement SELECT/DELETE ; Email uniquement SELECT.
   Le lecteur public reste SELECT seul. Cette commande crée aussi `demo_history`,
   ses politiques RLS et les liaisons RBAC de gestion. Elle doit précéder le déploiement.
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

Le workflow sandbox est aussi déclenché par chaque release publiée. Il teste et
déploie le même commit backend dans l’environnement isolé, avec les secrets démo
existants. Le frontend applique la même règle pour ses propres releases ; la
copie ne reste donc pas figée sur une version historique. Une nouvelle migration
ou un nouveau droit de lecture demande toujours sa préparation sandbox habituelle.

## Compléments Eric, points 12 à 16

- Email est masqué dans la navigation, ses routes ne sont pas montées et les
  nouveaux imports ne créent aucune campagne fictive.
- Accueil et visite en huit étapes traversent automatiquement les deux faces ;
  les bulles restent en bas à 320 px. Le navigateur conserve uniquement, par code,
  la visite passée/terminée (localStorage) et l’étape courante (sessionStorage).
  Le bouton Visite relance le parcours ; dix aides contextuelles restent disponibles.
- `employee` est accepté et affiché dans le bandeau de l’espace client.
- `batch` et `lifetime_days` sont des options de l’import et de la création simple.
  Filtres lot/statut/ouvertures/recherche et sélection globale, avec exclusions,
  portent sur toutes les pages. L’import reste limité aux cinq pilotes tant
  qu’Eric n’a pas validé le lancement commercial.
- Export avant suppression ; confirmation du nombre exact, liée à l’opérateur
  et à la sélection par HMAC pendant quinze minutes. Une sélection modifiée est
  refusée. Le CSV protège les textes pouvant être interprétés comme des formules.
- Suppression uniquement sur `nidria_demo_sandbox`, agences marquées et dossiers
  `is_demo`, après refus de toute identité partagée hors du lot. Les données
  fictives sont supprimées par cascade ; l’historique des ouvertures reste exportable
  dans `demo_history`. Aucun adaptateur de stockage, email ou paiement n’est appelé.
- Purge automatique trente jours après expiration, par paquets de cent et verrou
  transactionnel. Les codes supprimés ne sont jamais recréés par le seed initial.

Validation du 26/10 : `make check`, 3 016 tests réussis (3 ignorés), aucun appel
réseau non simulé ; contrôles ciblés du cycle de vie et des privilèges SQL.
Le CSV enrichi annoncé par Claude n’est toujours pas joint à la tâche lors de
sa relecture à 12:06 UTC. Les cinq pilotes commerciaux ne sont pas recréés depuis
une source inventée.
