# Rapport de démos et prénom, 27/10/2026

Tâches Nidria : `9c926277-d640-4d6e-919d-ee809d0cc0e0` (rapport) et
`09819e4d-dfeb-439b-b06b-5846c432c846` (prochaine vague).

## Route de lecture

`GET https://api.nidria.com/reports/demo-stats?lot=EN-2026-10&date=2026-10-27`

Le Bearer dédié est vérifié contre `DEMO_STATS_TOKEN_SHA256`, jamais contre les
secrets JWT. Sans empreinte configurée, la route refuse toute requête. Ce jeton
n'est accepté par aucune route admin. Retirer ou remplacer l'empreinte le révoque.
Ne pas mettre le jeton en URL, dans une tâche, un commentaire, Git ou un journal.

La route utilise une transaction PostgreSQL **READ ONLY** pour les inscriptions
réelles. Elle appelle le canal HMAC distinct `/_control/stats`, qui utilise le
rôle SELECT de la seule sandbox et une seconde transaction READ ONLY. Le chemin
fait partie de la signature : une signature de lecture ne peut pas être rejouée
sur le canal de création. Le rapport ne provisionne aucune table ou démo.

La date est facultative : par défaut, la veille à Paris. Les bornes sont les
minuits de `Europe/Paris`, avec les journées de 23 et 25 heures. Une date future
est refusée. Le nom du lot est comparé exactement.

## Lecture du JSON, version 1

- `totals` : `created`, `opened`, `visits`, `tour_started`, `tour_completed`,
  `trial_clicked`, `signups`.
- `created` compte les démos ; `opened`, les démos ouvertes au moins une fois.
  `visits` compte les onglets anonymes distincts, sans incrément au rafraîchissement
  ou lors du passage agence/client. Le détail journalier compte les onglets actifs
  ce jour-là : un onglet utilisé sur deux jours apparaît dans chacun de ces jours.
- `tour_started`, `tour_completed` et `trial_clicked` comptent chacun les démos
  distinctes ayant produit cet événement, et non chaque pression répétée.
- `signups` compte les agences réellement créées à l'issue de l'inscription, avec
  `utm_source=demo` et la référence opaque de cette démo dans `utm_content`.
  Une ouverture du formulaire ou un clic ne constitue pas une inscription.
- `daily` contient la date, le fuseau, les bornes UTC, `complete` et les mêmes
  compteurs. Si `complete=false`, les événements sont partiels ou inconnus avant
  l'installation : ne pas annoncer un zéro historique comme un résultat établi.
- `demos` contient seulement les démos ouvertes : `email`, `company_name`,
  `first_opened_at`, `last_opened_at`, `visits`, `tour_status`, `tour_last_step`,
  `trial_clicked`, `signup`. `demo_id` est une référence opaque, pas une URL
  permettant d'ouvrir un dossier. `openings_on_date` porte les observations du jour
  demandé avec `probably_robot` et `excluded_reason`.
- `coverage.detailed_since` indique le début de l'instrumentation. Les compteurs
  existants de l'admin sont conservés. S'ils précèdent cette date,
  `historical_totals_include_unqualified_visits=true` signale qu'il est impossible
  de retirer rétrospectivement les tests admin/robots. Les clics et journées
  historiques ne sont pas reconstruits artificiellement.
- `coverage.unattributed_demo_signups_all_batches` compte séparément les anciennes
  inscriptions portant seulement la source démo, sans référence exploitable.
  Ne pas les affecter à ce lot ou à une adresse email par supposition. La première
  source d'inscription reste conservée par le mécanisme d'attribution existant.

Le navigateur transmet une référence SHA-256 spécifique à la démo et le nom du
lot. Aucun code public, nonce de visite, email, prénom ou clé de campagne ne passe
dans l'URL d'inscription. Les aperçus admin/robots n'ajoutent aucune attribution.

## Installation

1. Dans la seule base `nidria_demo_sandbox`, préparer `demo_measurement` et
   `demo_telemetry_visit` via `prepare_measurement`, puis les grants/politiques
   de `prepare_management`. Les nouvelles tables restent hors des migrations et
   du metadata de production. Le lecteur reçoit SELECT ; le writer reçoit
   SELECT/INSERT/UPDATE sur les observations, sans DELETE/TRUNCATE. Aucun droit
   d'écriture sur `demo_measurement` n'est accordé au serveur.
2. Livrer par les workflows habituels le backend et l'API sandbox. Le drapeau
   `DEMO_MEASUREMENT_ENABLED=true` n'est utilisé que par la sandbox.
3. Dans le dépôt frontend, lancer **Demo reporting configuration**, phase
   `prepare`. Le script crée le jeton dans D1 s'il n'existe pas, et ne publie
   que son SHA-256. Installer cette empreinte comme `DEMO_STATS_TOKEN_SHA256`
   sur `nidria-api` ; aucune copie du jeton brut n'est nécessaire sur Fly.
4. Phase `activate` : la CI relit le jeton en mémoire, vérifie un GET authentifié
   du lot, puis enregistre l'URL. Phase `verify` : même lecture, sans mutation.
   D1 `nidria-audience` (`fea1d2bc-7330-4c6b-9a34-ce6e41c2c3db`), table `cfg`,
   clés `demo_stats_token` et `demo_stats_url`, toutes deux `part=0`.
5. Vérifier l'égalité des compteurs communs avec `/admin/demos` et les limites de
   couverture. Le run du point Telegram doit être observé par Claude dans son
   environnement ; la vérification HTTP ne prouve pas qu'un message a été envoyé.

## Prénom pour les prochains imports

`first_name` est facultatif dans le CSV par paquets et dans les objets de
`POST /admin/demos/agency-batch`. Un prénom non vide (100 caractères maximum,
sans caractères de contrôle) remplace seulement le prénom du compte fictif
connecté ; son nom de famille est vide. L'interface existante affiche le prénom
et son initiale. Un champ absent ou vide conserve le membre fictif actuel.

Les autres membres et clients restent fictifs. L'email du compte fictif ne
change pas. Le dédoublonnage lot/email réutilise une démo existante sans modifier
son prénom, même si le nouveau fichier en comporte un. Ne pas réimporter ou
réécrire `EN-2026-10` pour appliquer cette option.

Contrôles : PostgreSQL réel, limites de journée/DST, jeton absent/admin/révoqué,
refus d'écriture dans les deux transactions, import avec et sans prénom,
non-modification d'un lot réutilisé, puis Chrome mobile/bureau et comparaison
des observations du navigateur aux compteurs du lecteur.
