# Disponibilité de l’API Nidria

La machine Fly `app` sert le trafic. Sa sonde de routage `/health` vérifie PostgreSQL en trois secondes. Le processus privé de supervision vérifie aussi `/ping` sur la boucle locale, relève la charge CPU, la mémoire disponible et les connexions TCP établies vers le port PostgreSQL. Ces mesures restent accessibles à la supervision quand Uvicorn se bloque.

La machine `worker` compare toutes les vingt secondes la réponse publique aux relevés privés. Trois échecs consécutifs ouvrent un incident. Le redémarrage d’Uvicorn exige trois relevés distincts du même processus, une disponibilité vérifiée auparavant et un dernier contrôle local en échec. Une panne de base, du routage ou de la sonde seule déclenche une alerte. Une machine entièrement injoignable nécessite une intervention Fly.

Le journal SQLite `/watchdog/monitor.db` garde 24 heures de relevés dans `samples`. La table `monitor` conserve la dernière observation et la tentative de redémarrage. Trois contrôles sains ferment l’incident. Un délai de trente minutes sépare deux tentatives automatiques. Les alertes utilisent les destinataires internes configurés, une file persistante et une clé d’idempotence. L’authentification de la sonde privée utilise le secret Fly `API_WATCHDOG_KEY`.

L’ordonnanceur existant conserve les horaires. Il inscrit ses demandes `email_sync` dans `job_run`, comme le déclenchement manuel. Le worker reprend la même ligne après une interruption, sous verrou PostgreSQL. Une lecture de campagne dispose de 90 secondes. Le traitement reprend depuis les écritures confirmées et poursuit les autres campagnes. Les journaux du groupe `worker` indiquent l’identifiant et le statut de chaque exécution. Le pool des traitements de fond dispose de trois connexions sur `app` et deux sur `worker`.

Un déclenchement manuel renvoie le résultat du worker s’il est disponible sous une minute. Pour un traitement plus long, il renvoie l’identifiant et l’état courant de l’exécution, consultables dans le suivi des tâches.
