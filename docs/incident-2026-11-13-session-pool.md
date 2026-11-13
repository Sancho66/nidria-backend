# Budget des connexions PostgreSQL en mode session

## Problème

Plusieurs moteurs SQLAlchemy indépendants partageaient un pool Supabase de
session limité à 15 clients. Chaque moteur utilisait les valeurs par défaut
de cinq connexions conservées et dix connexions supplémentaires. Leur plafond
cumulé pouvait donc dépasser celui du service partagé et provoquer des refus
`EMAXCONNSESSION` sur les requêtes HTTP et les tâches de fond.

## Correctif

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

Ce budget vise **un seul processus API**. Il doit être recalculé avant d'ajouter
une réplique, un worker ou un nouveau moteur. Les moteurs async éphémères des
jobs restent en `NullPool`, avec fermeture en fin d'exécution. Le mode session
est conservé pour les verrous consultatifs de session de la synchronisation.

## Validation

Tests PostgreSQL testcontainers : remplissage simultané des trois pools,
attente de la sixième connexion API, annulation sans fuite, puis reprise après
restitution ; mêmes contrôles d'attente et de réutilisation pour les deux pools
sync. Les tests de planification et du suivi des quotas complètent ces
régressions. Aucun appel externe réel dans les tests.

Références techniques :
[limite du mode session Supabase](https://supabase.com/docs/guides/database/prisma/prisma-troubleshooting),
[pools SQLAlchemy](https://docs.sqlalchemy.org/en/20/core/pooling.html).
