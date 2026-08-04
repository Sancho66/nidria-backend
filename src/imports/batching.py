"""Les tailles de paquet de l'import — LA raison d'être, en un endroit.

Un import n'est pas lent parce qu'il calcule : il est lent parce qu'il
PARLE. Mesuré sur le fichier Teamleader réel (1844 lignes) avant ce lot :
~1685 allers-retours SQL séquentiels, soit ~0,9 requête par ligne. En
local (base sur la même machine, ~0,1 ms) ça ne se voit pas ; en prod
(API Fly ↔ base Supabase, ~2,4 ms mesurés par aller-retour) ça faisait
~8 s d'attente pour l'agence.

D'où les deux paquets, l'un en lecture, l'autre en écriture. Ils ne sont
pas égaux par hasard : ils bornent deux choses différentes — la taille
d'un `IN (...)` d'un côté, le nombre de lignes d'un `INSERT` multi-values
(et donc de paramètres liés, plafonnés à 32767 côté PostgreSQL) de
l'autre. Les garder distincts permet de bouger l'un sans l'autre.

Ni l'un ni l'autre ne touche à l'ATOMICITÉ : découper une requête n'est
pas découper une transaction. Un import commit une fois, à la fin.
"""

from collections.abc import Iterator, Sequence
from typing import Any

from sqlalchemy import inspect as sa_inspect

# Lecture : ids par requête `WHERE id IN (...)`. Large — un IN de 500
# uuid reste un index scan trivial, et 500 borne la taille du paquet
# réseau.
IMPORT_READ_CHUNK = 500

# Écriture : lignes par paquet d'INSERT. 500 lignes × ~20 colonnes =
# ~10 000 paramètres liés, bien sous le plafond de 32767 de PostgreSQL —
# c'est CE plafond que le paquet borne, rien d'autre. L'atomicité, elle,
# n'est pas découpée : tous les paquets d'un import vivent dans la MÊME
# transaction, un import reste tout ou rien.
IMPORT_WRITE_CHUNK = 500


def chunked[T](items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    """Découpe en paquets de `size` (le dernier peut être plus court)."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


# Horodatage posé par la base (`server_default=now()`) : ces colonnes
# sortent du payload, sinon on écraserait le défaut serveur par un NULL.
_SERVER_STAMPED = frozenset({"created_at", "updated_at"})


def insert_rows(objects: Sequence[Any]) -> list[dict[str, Any]]:
    """Objets ORM transients → payloads HOMOGÈNES pour `insert().values()`.

    L'homogénéité n'est pas cosmétique, c'est TOUT le gain, et il a fallu
    DEUX corrections pour l'obtenir — les deux mesurées, aucune devinée :

    1. L'ORM n'insère que les colonnes qu'un objet a effectivement reçues.
       Sur un fichier réel, chaque ligne remplit un sous-ensemble différent
       (celle-ci a un téléphone, celle-là une nationalité) : autant de
       formes d'INSERT que de combinaisons — 994 requêtes pour 1844 fiches,
       alors même que le `flush()` par ligne avait déjà disparu. D'où des
       dicts qui portent TOUJOURS les mêmes clés (None quand la ligne n'a
       rien dit).

    2. Même homogènes, ces dicts passés en `executemany` se re-découpaient
       en ~1200 exécutions : le pilote asyncpg infère le type de chaque
       paramètre, et `None` vs une vraie date ne donnent pas le même type
       — donc pas le même prepared statement. C'est `insert().values([…])`
       (un seul INSERT, N tuples de VALUES, types tirés du SCHÉMA et non
       des valeurs) qui tient en UNE requête par paquet.

    Les colonnes NOT NULL à défaut applicatif (`custom_fields`, `tags`,
    `preferred_channels`) doivent donc être posées par l'appelant à la
    construction de l'objet : le défaut ORM ne s'appliquerait qu'au flush,
    qui n'a plus lieu ici."""
    mapper = sa_inspect(type(objects[0]))
    keys = [column.key for column in mapper.columns if column.key not in _SERVER_STAMPED]
    return [{key: getattr(obj, key) for key in keys} for obj in objects]
