# Saturation des connexions, 13 novembre 2026

## Constat vérifié

À 09h46 UTC, `/health` répond 503 avec `EMAXCONNSESSION`, limite de 15
clients du pool Supabase en mode session. Les listes admin et plusieurs logos
échouent par intermittence. Le processus API unique possède 15 sockets PostgreSQL
établies à 09h52 UTC. Les URL SQLAlchemy async et sync pointent vers le même
pool de session, port 5432.

Une lecture SQL dans une transaction explicitement en lecture seule, via le
port transactionnel 6543 de la même base, confirme 30 dossiers actifs et
1 642 fiches clients pour Domiciliation Bulgarie. La configuration de
l'application n'a pas été changée pour ce diagnostic.

Les journaux persistés de `email_sync` contiennent la même erreur dès
00h30 UTC (01h30 Paris), avant le lot de 290 démonstrations signalé vers
07h20 UTC. Le lot ne peut donc pas être à l'origine de la première saturation
observée ; une éventuelle aggravation par ce trafic n'est pas mesurée.

La dernière release applicative est celle du 3 novembre. L'historique de la
machine Fly signale aussi un redémarrage automatique après manque de mémoire le
12 novembre à 16h35 UTC. Ce fait distinct ne prouve pas l'origine du problème de
connexions. Les journaux Fly disponibles au diagnostic ne couvrent que les
dernières minutes, pas toute la matinée.

## Remise en service et contrôle

Redémarrage propre de la même machine et de la même image à 09h53 UTC.
L'API démarre à 09h53m58s. À 09h55 UTC, les listes admin, les quatre logos
signalés et la santé répondent 200.

Le changement d'agence officiel, journalisé, ouvre les droits du compte utilisé
à 07h10 UTC. Trois lectures consécutives de `/cases` retrouvent exactement les
30 identifiants actifs de la base, et `/client-profiles` annonce 1 642 fiches
avec une première page non vide. Aucun dossier, client, rôle ou abonnement
n'a été modifié pour la recette. Nicolas avait déjà confirmé à Eric le retour
de son affichage à 08h44 Paris après rafraîchissement.

## Correctif des budgets

Les trois pools indépendants autorisaient chacun jusqu'à 15 connexions : cinq
conservées et dix supplémentaires. Leur plafond cumulé dépassait donc celui
du service PostgreSQL partagé. Le correctif fixe :

| Pool permanent | Maximum |
|---|---:|
| API async | 5 |
| Tâches sync | 5 |
| Observation Resend | 1 |
| Total | 11 |

`max_overflow=0` fait attendre localement lorsqu'un pool est plein. Quatre
places restent disponibles pour les moteurs éphémères des jobs, les migrations
et les diagnostics. Le contrôle de validité à l'emprunt est conservé et le
recyclage à 300 secondes est appliqué aux trois pools. Le recyclage dépend de
l'âge de la connexion : il ne libère pas les connexions simplement inactives.

Ce budget vise **un seul processus API**, comme la production observée. Il doit
être recalculé avant d'ajouter une réplique, un worker ou un nouveau moteur.
Les moteurs async éphémères des jobs restent en `NullPool`, avec fermeture en
fin d'exécution. Le mode session reste nécessaire aux verrous consultatifs
de session utilisés par la synchronisation ; aucun changement de port général.

Les tests utilisent PostgreSQL testcontainers : remplissage simultané des trois
pools, attente de la sixième connexion API, annulation sans fuite, puis reprise
après restitution ; mêmes contrôles d'attente et de réutilisation pour les
deux pools sync. Les tests de planification et du suivi des quotas complètent
ces régressions. Aucun appel externe réel dans les tests.

Références techniques :
[limite du mode session Supabase](https://supabase.com/docs/guides/database/prisma/prisma-troubleshooting),
[pools SQLAlchemy](https://docs.sqlalchemy.org/en/20/core/pooling.html).
