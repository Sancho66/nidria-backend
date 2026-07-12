# Paddle en local — mode d'emploi et pièges

> Éprouvé le 2026-07-12 (E2E complet sandbox : checkout payé, webhooks signés,
> rejeu, push de sièges avec proration, annulation). Ce savoir a coûté une
> soirée — le voici pour qu'il ne revive pas dans trois mois.

## Le principe

Tout est **déclaratif et provisionné par script** — on ne clique jamais dans le
dashboard pour créer le catalogue ou la destination webhook :

- la **déclaration** vit dans `src/billing/catalog.py` (2 products, 8 prices,
  les événements webhook) ;
- `scripts/provision_paddle_catalog.py` réconcilie (get-or-create idempotent,
  **dry-run par défaut**, `--execute` pour écrire) ;
- le matching est par **stable key** (`custom_data` sur les prices, la
  *description* sur la destination) — jamais par nom affiché ni URL ;
- **divergence = erreur explicite, jamais d'update silencieux** (un prix Paddle
  est immuable par principe — le gel founding en dépend ; une divergence est
  une décision humaine : rotation de prix, ou suppression de la destination).

## La séquence locale

```bash
# 1. l'API locale
uv run uvicorn src.main:app --port 8000

# 2. le tunnel (voir piège n°1)
ngrok http 8000

# 3. provisionner catalogue + destination avec l'URL du tunnel
PADDLE_WEBHOOK_URL="https://<tunnel>.ngrok-free.app/billing/webhooks/paddle" \
  uv run python scripts/provision_paddle_catalog.py --execute

# 4. le script affiche le secret UNE seule fois
#    → le coller dans .env : PADDLE_WEBHOOK_SECRET=pdl_ntfset_...
#    (il ne sera plus jamais affiché ; une destination existante ne relit
#     JAMAIS son secret — si perdu : supprimer la destination et re-provisionner)

# 5. relancer uvicorn (recharge l'env)
```

Variables d'env attendues (`.env` local / secrets Fly ailleurs) :

```
BILLING_CHECKOUT_ENABLED=true      # kill switch d'offre — FALSE par défaut :
                                   # checkout fermé (409), gestion/webhooks intacts
PADDLE_ENV=sandbox                 # sandbox | live
PADDLE_API_KEY=...                 # ne JAMAIS logger, même tronquée
PADDLE_PRICE_IDS={"cabinet_mensuel":"pri_...", ...}   # sortie du script
PADDLE_WEBHOOK_SECRET=pdl_ntfset_...                  # affiché 1 fois à la création
PADDLE_WEBHOOK_URL=https://.../billing/webhooks/paddle  # requis pour provisionner la destination
```

## Le test de fumée

Deux options équivalentes :

- **Dashboard** : Notifications → la destination → *Send test* → un 200 doit
  apparaître dans les logs uvicorn ;
- **API simulations** (sans dashboard) : `POST /simulations`
  (`notification_setting_id`, `type`) puis `POST /simulations/{id}/runs`.
  Nécessite la destination en `traffic_source: "all"` — c'est ce que le script
  crée (voir piège n°4).

Un événement simulé porte un `custom_data` vide → il emprunte le chemin
« agence inconnue » : **200 + log ERROR d'alerte + zéro création**. C'est le
comportement attendu, pas un bug.

## Les pièges (chacun a été payé)

1. **ngrok trop vieux** : les comptes récents exigent ≥ 3.20
   (`ERR_NGROK_121`) → `ngrok update`.
2. **L'URL ngrok free tourne à chaque session** → le run suivant du script
   sort `DIVERGENCE — destination URL ... != env ...` (c'est voulu). Deux
   sorties : réserver le **domaine statique gratuit** ngrok
   (`ngrok http --url=<domaine>.ngrok-free.app 8000`), ou supprimer la
   destination dans le dashboard et re-provisionner (⚠️ nouveau secret).
3. **`transaction_default_checkout_url_not_set` au checkout** : le compte
   Paddle doit avoir un **Default Payment Link** (dashboard → Checkout →
   Checkout settings). N'importe quelle URL en sandbox (ex.
   `http://localhost:5173`). Réglage de compte, une seule fois.
4. **Simulations refusées** (`invalid_field` sur `notification_setting_id`) :
   la destination doit être `traffic_source: "all"` (défaut Paddle :
   `platform`, qui refuse les simulations). Le script crée en `all`.
5. **`.env` sans saut de ligne final** : un `cat >> .env` colle la nouvelle
   variable à la dernière ligne (`...API_KEY=xxxPADDLE_...`) → valeur
   silencieusement corrompue **et** risque d'afficher un secret en
   diagnostiquant. Toujours garantir le `\n` avant d'append — et ne jamais
   grepper `.env` avec affichage de la ligne.
6. **La clé API ne doit jamais apparaître** — ni en clair, ni tronquée, ni
   dans une stacktrace. Le client lève `PaddleApiError` (statut + corps de
   réponse Paddle uniquement, jamais la requête ni ses headers). Si une clé
   fuite quand même : révoquer immédiatement dans le dashboard, c'est deux
   minutes.

## Payer en sandbox

Le checkout renvoie `transaction_id` ; l'overlay s'ouvre avec Paddle.js
(`Paddle.Environment.set("sandbox")`, `Paddle.Initialize({token})`,
`Paddle.Checkout.open({transactionId})`). Le `token` est un **client-side
token** (dashboard → Developer Tools → Authentication → *Client-side tokens*,
commence par `test_`) — **publiable par design**, il ne permet que d'ouvrir des
checkouts. Carte de test : `4242 4242 4242 4242`, date future, CVC libre.
⚠️ Les tokens font ~30 caractères après `test_` — les copier au bouton *Copy*
(la sélection à la main tronque).

## Staging puis live

Le même script, la même déclaration — seule l'env change :

- **staging** : `PADDLE_WEBHOOK_URL` stable (plus de tunnel), mêmes clés
  sandbox ;
- **live** (post-KYB) : `PADDLE_ENV=live` + clé live → lancer le script →
  coller le JSON `PADDLE_PRICE_IDS` imprimé. Le go-live du catalogue, c'est ça.
