# CLAUDE.md — Nidria Backend

> Ce fichier est lu automatiquement par Claude Code à chaque session.
> **Lis aussi `SETUP.md` pour les étapes step-by-step.**
> Prose en français, **tout le code en anglais** (identifiants, tables, colonnes, enums, fichiers).

---

## PARTIE 1 — CONTEXTE

### Le produit

**Nidria** est un SaaS B2B à deux faces pour les **agences d'expatriation** francophones.

> Renommé Nidria (ex-ExpatFlow) le 11/06/2026 — positionnement élargi au-delà de l'expat.

- **Face agence** : l'agence pilote ses dossiers clients, coordonne les interlocuteurs (agents, expatriés, pros externes), configure des parcours-types, et planifie des rappels — fini le combo Excel + WhatsApp + Drive + mail.
- **Face expatrié** : le client voit l'avancement de son dossier en temps réel (étapes faites / en cours / à venir, délais estimés, interlocuteurs), dépose ses documents, et arrête d'appeler l'agence.

Pitch : *« vos clients arrêtent de vous appeler pour savoir où ils en sont. »*

### Origine : refactor de Prism, pas un fork

Nidria réutilise l'infrastructure et la coquille CRM de **Prism** (un CRM de prospection B2B), mais c'est un **repo neuf** avec un **rename complet** des concepts. On **porte** le réutilisable depuis `_prism-reference/` (voir PARTIE 10), on **ne réécrit pas** ce qui marche déjà, on **ne forke pas** le repo Prism.

| Prism (CRM B2B)            | Nidria                         | Action                          |
|----------------------------|-----------------------------------|---------------------------------|
| `Project` / tenant         | `Agency`                          | Rename complet                  |
| `User` (membre cabinet)    | `Agent`                           | Rename, garde le concept rôle   |
| `Company` (prospect)       | `ClientCase` (dossier)            | Rename complet, repurpose       |
| `Contact`                  | `ExpatUser` + `FamilyMember`      | Split, repurpose                |
| `ActivityLog`              | `ActivityLog`                     | Gardé tel quel, types adaptés   |
| `Pipeline status`          | Étapes de parcours                | Concept proche, nouveau modèle  |
| `score` / `scoring_rules`  | —                                 | **Supprimé**                    |
| `engine/cfo`, `engine/mna` | —                                 | **Supprimé**                    |
| `APScheduler` + `JobConfig`| Dispatch des rappels              | Réutilisé                       |

### Les 4 features MVP (VERROUILLÉES)

1. **Timeline client visible** — l'expatrié voit son parcours : étape en cours, étapes faites, à venir, délais estimés, interlocuteurs.
2. **Espace agence** — liste des dossiers, création/édition, assignation d'un parcours, marquage d'étapes, notes, tags, owner.
3. **Rappels customisables** (demande Eloïse) — planifier des rappels (mail / WhatsApp / in-app) avec message éditable et **approbation manuelle obligatoire** avant envoi. Inclut les **relances auto J+20 / J+30** sur étape sans avancement (activables/désactivables) : elles **créent un reminder en `TO_APPROVE`**, jamais d'envoi automatique — la règle d'approbation tient toujours.
4. **Étapes verrouillées** (demande Didier) — une étape ne peut pas être validée tant que ses étapes prérequises ne le sont pas.

### Périmètre MVP — règle d'or

**Un MVP livrable avec les 4 features. Le reste = backlog gelé. Pas de scope creep.**

✅ **À CODER** (les 4 features + le strict nécessaire) :
- Double authentification : `Agent` (agence) + `ExpatUser` (client), flux distincts.
- Onboarding agence (créer une agence, inviter des agents).
- Création/édition de `JourneyTemplate` par l'agence.
- Création d'un dossier + invitation par mail de l'expatrié + activation par lien.
- Upload basique de documents (Supabase Storage).
- `ActivityLog` automatique sur les actions importantes.
- RBAC dynamique DB-driven (le **moteur**, voir PARTIE 5).
- Multilangue : **FR uniquement** au MVP (les labels FR vivent côté frontend).

❌ **À NE PAS CODER au MVP** (même si ça paraît facile « en même temps ») :
- Générateur de devis → V1.5
- Générateur de contrat / signature électronique → V2
- Lead-gen / commission entre pros → V2
- Multilangue EN/RU/KO/PT/ES/DE → V1.5
- OCR automatique sur documents → V2
- Versioning de documents → V2
- Statistiques avancées / dashboard analytique riche → V1.5
- Alertes dossiers en retard / détection d'inactivité → V1.5
- **UI de gestion des rôles/permissions au runtime** (Settings RBAC) → post-MVP (le moteur RBAC, lui, EST dans le scope ; voir PARTIE 5)
- Module `Task` (Kanban/Gantt) de Prism → non porté

> Si une feature n'est pas dans la liste « À CODER », elle n'entre pas. Ne l'ajoute pas par « bonne ingénierie ». Toute addition se valide en amont avec Eric (le product owner). En cas de doute : **demande, n'ajoute pas.**

---

## PARTIE 2 — STACK

| Couche            | Technologie                                            |
|-------------------|--------------------------------------------------------|
| Langage           | Python 3.12+                                           |
| Framework         | FastAPI                                                |
| ORM               | SQLAlchemy 2.0 (async)                                 |
| Migrations        | Alembic                                                |
| Validation        | Pydantic v2 (+ pydantic-settings)                      |
| Base de données   | PostgreSQL 16 (Supabase) — **une seule base**          |
| Storage           | Supabase Storage (documents)                           |
| Auth              | JWT (python-jose) + bcrypt direct (hash mdp)           |
| Scheduler         | APScheduler (in-process, dispatch des rappels)         |
| Mail              | Resend (ou SMTP) pour invitations + rappels mail       |
| Package manager   | uv                                                     |
| Tests             | pytest + pytest-asyncio + **testcontainers PostgreSQL**|
| HTTP client (test)| httpx (AsyncClient)                                    |
| Lint / types      | ruff + mypy                                            |
| Logs              | loguru                                                 |
| Déploiement       | Fly.io (`auto_stop_machines=false`)                    |

---

## PARTIE 3 — ARCHITECTURE & PATTERNS (non négociables, hérités de Prism)

### Pattern en couches

```
Model  →  Schema  →  Repository  →  Manager  →  Router
```

- **Model** (`shared/models/`) : tables SQLAlchemy.
- **Schema** (`src/{domain}/{domain}_schema.py`) : Pydantic in/out.
- **Repository** (`src/{domain}/{domain}_repository.py`) : accès DB pur (requêtes), aucune logique métier.
- **Manager** (`src/{domain}/{domain}_manager.py`) : logique métier, orchestration, appels cross-domain.
- **Router** (`src/{domain}/{domain}_router.py`) : endpoints FastAPI.

> **Le Router n'appelle JAMAIS un Repository directement.** Router → Manager → Repository. Toujours.

### Règles d'architecture

- **Modèles dans `shared/models/`**, schémas dans `src/{domain}/`.
- **API async, moteur/scheduler sync** (threads).
- **Tout est scopé par tenant** (`agency_id`). Aucune requête cross-agency sans raison explicite.
- **JSONB pour les champs spécifiques/extensibles** (ex. `client_case.tags`, `agency.settings`) — pas de colonnes ad hoc qui polluent le schéma.
- **OpenAPI committé** à la racine du repo, régénéré à chaque changement d'API (contrat-first).
- **Deny par défaut** côté accès (voir PARTIE 5).

---

## PARTIE 4 — MODÈLE DE DONNÉES

**22 tables cœur + 4 tables RBAC.** Une seule base PostgreSQL. UUID en PK partout, `created_at`/`updated_at` via `TimestampMixin`.

Pièce centrale : `client_case` (le dossier) est le hub. `agency` est la racine multi-tenant.

### Identités & personnes

> **Deux tables users distinctes, deux flux d'auth.** Ce n'est PAS deux bases (décision Alex : une seule base). La séparation se joue sur l'**identité**, pas l'infra. Un token expatrié ne peut pas atteindre un endpoint agent. En code, `Agent` et `ExpatUser` partagent un `PersonNameMixin` (first_name, last_name, email) — DRY au niveau code, pas une table commune.

- **`agency`** — `id`, `name`, `slug` (unique), `settings` (JSONB). Le tenant.
- **`agent`** — `id`, `agency_id` FK, `role_id` FK (**un seul rôle par agent**, modèle Prism — PARTIE 5), first/last_name, `email` (unique), `password_hash`. **Pas** d'enum `role` en dur sur la table.
- **`agent_invitation`** — `id`, `agency_id` FK, `email`, `role_id` FK, `token` (unique), `status` (PENDING/ACCEPTED/EXPIRED), `invited_by_agent_id` FK, `expires_at`, `accepted_at`.
- **`expat_user`** — `id`, first/last_name, `email` (unique), `preferred_lang`, `password_hash`, `activated_at`. **Pas d'`agency_id`** : un expatrié peut avoir des dossiers chez plusieurs agences. Le lien se fait via `client_case.principal_expat_user_id`.
- **`case_invitation`** — `id`, `case_id` FK, `email`, `token` (unique), `status`, `expires_at` (14 j), `accepted_at`. **La liaison au dossier, c'est `principal_expat_user_id`, posée à la création du dossier ; l'invitation est notification + trace d'audit, jamais le mécanisme de liaison.** Envoyée à chaque création de dossier, expat nouveau (mail d'activation) ou existant (mail « un dossier vous attend » ; s'il ne clique jamais, elle expire sans conséquence).
- **`external_contact`** — `id`, `case_id` FK, `name`, `email`, `phone`, `type` (NOTARY/LAWYER/BANK/TAX_ADVISOR/OTHER). **Pas de login au MVP.** Cible de rappels. Hook V2 : table d'auth `external_user` (1 login ↔ N `external_contact`).
- **`refresh_token`** — `jti` UUID PK, `actor_type` (AGENT/EXPAT), `actor_id` (UUID **sans FK**, même pattern polymorphe qu'`activity_log`), `expires_at`, `revoked_at` (nullable), `created_at`. **Rotation** : chaque `/refresh` révoque le jti consommé et en émet un nouveau ; un jti déjà consommé/révoqué → 401 + **révocation de tous les refresh actifs de l'acteur** (détection de réutilisation).
- **`password_reset_token`** — `id`, `actor_type`, `actor_id` (même pattern polymorphe), `token` (unique), `expires_at` (court, ~1h), `consumed_at` (nullable). Même pattern que les invitations. Consommation unique ; un reset réussi révoque tous les refresh actifs de l'utilisateur.

### Dossier

- **`client_case`** — `id`, `agency_id` FK, `principal_expat_user_id` FK, `owner_agent_id` FK, `journey_template_id` FK, `origin_country`, `dest_country`, `status` (PROSPECT/IN_PROGRESS/AWAITING_DOCUMENTS/SUBMITTED/VALIDATED/CLOSED), `source`, `tags` (JSONB).
- **`family_member`** — `id`, `case_id` FK, `name`, `relationship`. Membres famille sans login (épouse, enfants).
- **`case_note`** — `id`, `case_id` FK, `author_agent_id` FK, `body` (TEXT), `is_confidential` (bool, default false). Notes internes d'agence sur un dossier (feature 2) ; les notes confidentielles ne sont visibles qu'avec la permission dédiée — contrôle d'accès sur une vraie colonne, pas un flag JSONB dans un log.

### Parcours (modèle vs instance)

> `JourneyTemplate` = le **modèle réutilisable** que l'agence configure. `CaseStepProgress` = son **instanciation** pour un dossier donné. Les prérequis (étapes verrouillées) se déclarent au niveau **modèle**, l'enforcement se contrôle sur l'**instance**.

- **`journey_template`** — `id`, `agency_id` FK, `name`.
- **`journey_template_step`** — `id`, `template_id` FK, `name`, `position` (ordre), `estimated_days`, `default_responsible_type`, `required_documents` (JSONB, liste de libellés libres, default `[]` — étape 15, demande Eric : les pièces attendues par étape, affichées dans la timeline expat. **Informatif au MVP** : le verrou reste les prérequis seuls ; le matching pièce↔exigence = V1.5).
- **`step_prerequisite`** — `step_id` FK, `prerequisite_step_id` FK. M2M auto-référencée sur les étapes d'un même template. Validation : prérequis dans le même template, **aucun cycle**.
- **`case_step_progress`** — `id`, `case_id` FK, `template_step_id` FK, `status` **stocké** (TODO/IN_PROGRESS/DONE — **jamais BLOCKED en base** : BLOCKED est une **projection** calculée à la lecture contre les prérequis courants du template, appliquée aux seules étapes TODO ; le verrou reste enforced à l'écriture pour toutes les transitions. BLOCKED reste le vocabulaire API ; `status="blocked"` en PATCH → 422), `responsible_type` (AGENT/EXPAT/EXTERNAL, nullable), `responsible_agent_id` FK (nullable), `responsible_external_id` FK (nullable), `completed_at`, `completed_by_agent_id` FK.
  - **Responsable polymorphe** : selon `responsible_type`, on lit `responsible_agent_id`, ou `responsible_external_id`, ou (si EXPAT) le principal du dossier. Couvre les 3 cas du brief (agent / « client expatrié » / « pro externe »).
  - **Verrouillage** : une étape ne passe IN_PROGRESS/DONE que si toutes ses étapes prérequises (résolues via le template) sont DONE sur ce dossier. Sinon → erreur explicite.

### Communication & traçabilité

- **`message_template`** — `id`, `agency_id` FK, `name`, `body` (variables `{client_name}`, `{step_name}`, `{days_left}`).
- **`reminder`** — `id`, `case_id` FK, `step_progress_id` FK (nullable), `message_template_id` FK (nullable), `channel` (MAIL/WHATSAPP/IN_APP), `scheduled_at`, `status` (**TO_APPROVE → APPROVED → SENT**, + CANCELLED), `recipient_type` (EXPAT/EXTERNAL), `recipient_external_id` FK (nullable), `message_body` (interpolé serveur), `approved_by_agent_id` FK.
  - **Approbation manuelle obligatoire** (cœur de la demande Eloïse) : aucun envoi sans passage explicite TO_APPROVE → APPROVED par un agent.
  - WhatsApp au MVP = **render + copier-coller** par l'agent (l'API WhatsApp Business est V1.5).
- **`document`** — `id`, `case_id` FK, `step_progress_id` FK (nullable), `filename`, `storage_path`, `uploaded_by_type` (AGENT/EXPAT), `uploaded_by_id`, `validation_status` (OK/INCOMPLETE/TO_FIX), `expires_at`.
- **`activity_log`** — `id`, `case_id` FK, `actor_type` (AGENT/EXPAT/SYSTEM), `actor_id` (UUID **sans FK**, polymorphe, NULL si SYSTEM), `action_type`, `details` (JSONB : old/new values de la mutation — PAS les notes manuelles, qui vivent dans `case_note`), `created_at` (immuable, pas d'`updated_at`). La traçabilité « 50/50 » (qui a fait quoi, quand). Logué auto sur chaque mutation importante.

### Scheduler (porté de Prism — le moteur configurable est in-scope, son UI Settings ne l'est pas)

- **`job_config`** — `id`, `job_id` (unique, ex. `dispatch_reminders`), `name`, `cron_expression`, `timezone`, `is_enabled`, `paused_until`, `starts_at`/`ends_at`, `last_run_at`/`last_run_status`/`next_run_at`, `config` (JSONB). **Plateforme, pas tenant** (pas d'`agency_id`) : les 2 jobs MVP (`dispatch_reminders`, `auto_reminders`) servent toutes les agences ; le réglage par agence (relances auto on/off) vit dans `agency.settings`. Seedés à la création seulement — jamais d'écrasement d'une édition runtime (même règle que les rôles système).
- **`job_run`** — `id`, `job_config_id` FK CASCADE, `job_id`, `status` (RUNNING/SUCCESS/FAILED/SKIPPED), `started_at`, `finished_at`, `duration_seconds`, `stats` (JSONB), `error`, `log_output` (progressif), `triggered_by` (SCHEDULER/MANUAL), `triggered_by_agent_id` (SET NULL).

---

## PARTIE 5 — RBAC DYNAMIQUE (DB-driven) — le moteur EST dans le scope

> Décision verrouillée : **rien en dur, tout dynamique.** Aucun mapping rôle→permission gravé dans le code. Mais une frontière physique demeure : **la donnée pilote l'accès à des comportements, elle ne crée pas de comportement.** Ajouter un rôle / une permission / recâbler qui-a-le-droit = 100 % data, zéro déploiement. Ajouter une **action que le produit sait faire** = du code (+ une ligne de permission et de binding).

### Le seul élément en code : le *catalogue* de permissions

Une permission ne veut rien dire si aucun endpoint ne la vérifie. Le code est donc la **source de vérité du catalogue** (typo-safe, autocomplété, refactorable), **synchronisé** vers la table `permission` au démarrage (insert des manquantes, **jamais** de delete). La base est le miroir relationnel + les affectations.

```python
class Permission(str, Enum):
    CASE_VIEW         = "case.view"
    CASE_EDIT         = "case.edit"
    STEP_COMPLETE     = "step.complete"
    REMINDER_CREATE   = "reminder.create"
    REMINDER_APPROVE  = "reminder.approve"
    JOURNEY_CONFIGURE = "journey.configure"
    DOCUMENT_VALIDATE = "document.validate"
    AGENT_MANAGE      = "agent.manage"
    ROLE_MANAGE       = "role.manage"
    # ... une entrée par action gardée dans le produit
```

### Le modèle (tout en base, éditable)

```
permission         (id, key UNIQUE, label, category)                       -- synchronisée depuis l'enum
role               (id, agency_id NULL, name, is_system)                    -- agency_id NULL = rôle système partagé
role_permission    (role_id FK, permission_id FK)                           -- LA matrice, éditable
-- l'affectation vit sur agent.role_id (FK role, NOT NULL) : UN rôle par agent
-- role.cloned_from_role_id (FK role, NULL) : lien copy-on-write d'un clone vers son rôle système d'origine
protected_resource (id, method, route, audience, permission_id NULL)        -- binding route→audience(+permission), en data
```

- **`audience` ∈ {PUBLIC, AGENT, EXPAT}** (remplace `is_public`) : PUBLIC = passe sans token (login, activate, accept-invitation — de vraies lignes auditables) ; AGENT = token agent requis + check `permission_id` contre la matrice ; EXPAT = token expat valide requis, **pas de check matrice** (droits expat intrinsèques), l'**ownership** (« ses propres dossiers ») est vérifié dans le **Manager** (filtrage sur l'`expat_user_id` authentifié).

- **Rôles système** (`agency_id` NULL, `is_system=true`) : admin / member / viewer / case_manager livrés par défaut, partagés par toutes les agences (pas dupliqués).
- **Rôles custom** (`agency_id` rempli) : une agence crée les siens.
- **Validation** : `agent.role_id` pointe un rôle système (non masqué) OU un rôle de la propre agence de l'agent (jamais d'une autre).
- **Un rôle unique** : `effective_permissions` = les permissions du rôle de l'agent — pas de cumul, pas d'union.
- **Copy-on-write des rôles système** : une agence qui édite un rôle système (PATCH nom / PUT matrice) obtient un CLONE custom (`cloned_from_role_id`) avec rebind de ses agents ; le clone **masque** son origine pour cette agence (listing ET assignation — assigner l'original masqué → 409 nommant le clone) ; DELETE du clone = retour des agents sur le rôle système (sauf anti-lockout). Les rôles système ne sont JAMAIS modifiés en base par une agence — le seed les réaligne au boot sans risque.

### L'enforcement (dépendance globale, ne nomme aucune permission)

> ⚠️ **Pas un middleware Starlette** : dans un `BaseHTTPMiddleware`, `request.scope["route"]` n'est pas encore peuplé avant `call_next`. L'enforcement est une **dépendance FastAPI globale** (`FastAPI(dependencies=[Depends(enforce)])`) : au moment où elle s'exécute, le routing a eu lieu → le template de route est disponible, et on bénéficie de l'injection (session DB). Le « deny par défaut » reste centralisé.

```python
async def enforce(request: Request, db=Depends(get_db)):
    route   = request.scope["route"].path            # template matché (gère les path params)
    binding = await resolve_binding(db, request.method, route)
    if binding is None:
        raise HTTPException(403)                      # non lié = fermé (DENY PAR DÉFAUT)
    if binding.audience == Audience.PUBLIC:
        return
    actor = await resolve_actor(request, db, binding.audience)  # décode le token de la bonne audience, 401 sinon
    request.state.actor = actor                       # dispo pour l'endpoint, pas de re-décodage
    if binding.audience == Audience.EXPAT:
        return                                        # pas de matrice ; ownership vérifié en Manager
    if binding.permission is None:
        return                                        # AGENT sans permission = tout agent authentifié (/me, /logout)
    if binding.permission.key not in effective_permissions(actor):
        raise HTTPException(403)
```

**Comportement par branche** (matérialisé par le CHECK `audience IN ('public','expat') ⇒ permission_id IS NULL ; audience='agent' ⇒ permission_id libre`) :
- `PUBLIC` → passe sans token, `actor=None`.
- `AGENT` + permission → token agent valide (401 sinon) **et** permission dans la matrice (403 sinon).
- `AGENT` sans permission → token agent valide suffit (endpoints d'identité : `/me`, `/logout`) — symétrique d'EXPAT.
- `EXPAT` → token expat valide suffit, jamais de matrice ; l'ownership se vérifie en Manager.

### Garde-fou : vérif d'intégrité au boot (deny par défaut + cohérence)

Le risque d'un système 100 % data, c'est qu'une route sans binding soit silencieusement ouverte (trou) ou bloquée (bug). On l'élimine — **le démarrage plante** si une route n'a pas de binding :

```python
INFRA_ROUTES = {"/ping", "/health", "/docs", "/openapi.json", "/redoc"}  # endpoints framework, whitelist en code

def assert_all_routes_bound(app, db):
    declared = {(m, r.path) for r in app.routes for m in r.methods if r.path not in INFRA_ROUTES}
    bound    = {(b.method, b.route) for b in db.query(ProtectedResource)}
    missing  = declared - bound
    if missing:
        raise StartupError(f"routes sans binding: {missing}")
```

- **Routes infra** (`/ping`, `/health`, `/docs`…) : whitelist **en code** dans le checker — ce sont des endpoints framework, pas des ressources produit.
- **Routes publiques produit** (login, activate, accept-invitation) : de **vraies lignes** `audience=PUBLIC` dans `protected_resource`, auditables.
- **En test, on ne désactive PAS le boot check** : le harnais seed la **baseline RBAC** (catalogue + rôles système + matrice + bindings) sur la DB testcontainer, donc le check passe sur une DB réaliste — plus un test dédié qui vérifie qu'il **plante** quand on retire un binding.

### Cloisonnement par type d'acteur (ne pas sur-ingénierer)

- **`ExpatUser` ne passe PAS par ce moteur.** Ses droits sont intrinsèques (« ses propres dossiers »). Lui coller des rôles serait de la complexité gratuite.
- **`external_user` (V2, l'avocat)** réutilisera *exactement* ce moteur avec ses **propres** rôles externes (ex. `external.step.validate` sur les seules étapes assignées), jamais mélangés aux rôles agents.
- On s'arrête au niveau **route→permission**. Pas de moteur ABAC à conditions (« si le dossier est dans tel état… ») — coûteux, inutile ici.

### Moteur (scope) vs UI Settings (hors scope)

- **Le moteur** (les 5 tables + le middleware + la sync catalogue + la vérif boot) : **dans le MVP**, invisible pour le testeur, zéro débat. La config initiale est pilotée par **seed/migration**.
- **L'écran Settings** d'édition runtime des rôles/permissions/bindings : **surface produit hors des 4 features** → post-MVP (le frontend de Prism sera porté ensuite). Si on le tire dans le MVP, un mot à Eric.

---

## PARTIE 6 — AUTH

- **Deux flux, deux audiences JWT.** `create_access_token(sub, audience)` avec `audience ∈ {agent, expat}`. Un token `agent` et un token `expat` ne sont pas interchangeables : signés/résolus différemment. `get_current_agent` et `get_current_expat` sont deux dépendances distinctes.
- **Secrets** : `JWT_AGENT_SECRET` + `JWT_EXPAT_SECRET` (access, un par audience) + `JWT_REFRESH_SECRET` (unique). Le refresh token **porte un claim custom `audience`**, et `/auth/agent/refresh` / `/auth/expat/refresh` **valident que ce claim correspond** à leur audience (rejet sinon) → un refresh ne peut jamais émettre un access d'une autre audience.
- **Pas de superadmin au MVP.** L'agence **et son premier admin** sont créés par le **script de seed/onboarding** (lancé à la main par nouvelle agence). Pas de `POST /agencies` live, pas de flag `is_superadmin`, `agent.agency_id` reste non-null. Les invitations d'agents *au sein* d'une agence restent live (permission `agent.manage`). Si un jour la création d'agence devient self-serve → 3e identité `platform_admin` (sa propre audience), additive, hors MVP.
- **Agent** : login / refresh / logout / me / forgot-password / reset-password. Premier admin seedé ; agents suivants via `agent_invitation`.
- **Expatrié** : activation par lien (`case_invitation.token` → set password → `expat_user.activated_at`), puis login / refresh / logout / me / forgot-password / reset-password.
- **Rotation/révocation des refresh** (table `refresh_token`) : à l'émission (login, refresh, reset), insert du jti ; chaque `/refresh` valide signature + claim `audience` + **jti actif en base**, émet un **nouveau** couple access+refresh et révoque l'ancien jti. Réutilisation d'un jti consommé/révoqué → 401 + révocation de **tous** les refresh actifs de l'acteur (détection de vol). `POST /auth/{agent|expat}/logout` révoque le jti courant.
- **Reset password** (agent ET expat, table `password_reset_token`) : `forgot-password` répond **200 identique** que l'email existe ou non (non-révélateur) ; si oui, mail avec lien (mock en test). `reset-password` : token valide + non consommé + non expiré → set password + consomme le token + **révoque tous les refresh actifs**. Un expat **non activé** qui demande un reset : 200 silencieux, **pas de mail** (son chemin, c'est l'activation).
- **RLS Supabase : PAS au MVP.** Le frontend tape notre API FastAPI, pas Supabase en direct ; le backend se connecte avec une connexion privilégiée → la RLS ne serait pas la frontière effective. **La frontière de sécu est applicative** : enforcement RBAC (PARTIE 5) + ownership en Manager. La RLS sera branchée si/quand un accès DB direct est exposé.
- **V2** : table d'auth `external_user` + `get_current_external_user` — **additif**, aucune migration destructive, aucun endpoint agent/expat touché. Le hook (`external_contact` + responsable polymorphe) existe déjà.

---

## PARTIE 7 — STRUCTURE

```
nidria-backend/
├── src/
│   ├── main.py                       # FastAPI + lifespan (sync catalogue, boot check, scheduler)
│   ├── core/
│   │   ├── config.py                 # pydantic-settings
│   │   ├── database.py               # async engine + session + get_db
│   │   ├── exceptions.py             # NotFound/Forbidden/Conflict/Validation + handlers
│   │   ├── enums.py                  # ActorType, CaseStatus, StepStatus, ResponsibleType, ...
│   │   ├── security.py               # hash/verify, tokens (audiences agent/expat)
│   │   ├── dependencies.py           # get_db, get_current_agent, get_current_expat
│   │   └── rbac/
│   │       ├── permissions.py        # Permission enum (catalogue) + sync au boot
│   │       ├── enforcement.py        # enforce() — dépendance globale (deny par défaut)
│   │       └── integrity.py          # assert_all_routes_bound()
│   ├── auth/                         # schema + manager + router (agent + expat)
│   ├── agencies/                     # agence + onboarding + invitations agents
│   ├── journeys/                     # JourneyTemplate + steps + prerequisites
│   ├── cases/                        # ClientCase + family + external_contact
│   ├── progress/                     # CaseStepProgress + enforcement verrouillage
│   ├── documents/                    # upload Supabase + validation
│   ├── reminders/                    # Reminder + message_template + approbation
│   ├── jobs/                         # contrôle scheduler (JobConfig/JobRun, pause/trigger)
│   ├── activity/                     # ActivityLog (endpoints agence)
│   ├── expat/                        # portail expat (mes dossiers, timeline, notifications) — le portail est une face, pas un appendice
│   └── dashboard/                    # KPIs simples (compte par statut/pays)
├── shared/
│   └── models/                       # TOUS les modèles SQLAlchemy
│       ├── base.py                   # DeclarativeBase + TimestampMixin + PersonNameMixin
│       ├── agency.py, agent.py, expat_user.py, external_contact.py, auth_tokens.py
│       ├── client_case.py, family_member.py, case_note.py, invitation.py
│       ├── journey.py, case_step_progress.py
│       ├── reminder.py, message_template.py, document.py, activity.py, job.py
│       └── rbac.py                   # permission, role (+cloned_from), role_permission, protected_resource
├── scripts/
│   └── seed.py                       # permissions, rôles système+matrice, bindings, agences, 3 dossiers
├── tests/
│   ├── conftest.py                   # testcontainers PG, isolation par truncate (teardown), AsyncClient
│   └── plugins/                      # make_*(**overrides) + fixtures par domaine
├── alembic/                          # migrations
├── _prism-reference/                 # PORTAGE Prism, lecture seule (voir PARTIE 10)
├── openapi.json                      # committé, régénéré à chaque changement d'API
├── Makefile                          # commandes dev quotidiennes (`make help`) — mêmes commandes que la CI
├── pyproject.toml, .env.example, Dockerfile, fly.toml, start.sh, alembic.ini
├── CLAUDE.md
└── SETUP.md
```

---

## PARTIE 8 — TESTS (rigueur Prism, non négociable)

- **testcontainers PostgreSQL. JAMAIS SQLite.** Le schéma JSONB + les FK doivent tourner sur du vrai Postgres.
- **`conftest.py`** : container PG, **isolation par truncate en teardown** (les Managers committent → un rollback simple ne suffit pas ; approche éprouvée de Prism, on évite le pattern SAVEPOINT fragile en async), `AsyncClient` httpx, `pytest_plugins` listant les plugins de domaine. Le harnais **seed la baseline RBAC** (catalogue + rôles système + matrice + bindings) pour que le boot check passe en test.
- **Plugins** (`tests/plugins/{domain}_plugin.py`) : `DEFAULTS` + `make_{entity}(**overrides)` + fixtures (`agent`, `admin_agent`, `expat_user`, `martin_case`, etc.).
- **≥ 1 test par endpoint.** Inclure systématiquement les cas d'autorisation (deny par défaut, mauvais type de token, permission absente).
- **Tests critiques spécifiques** :
  - RBAC : route non bindée → 403, public passe, permission présente/absente, multi-rôles, **boot plante si binding manquant**.
  - Auth : **rejet d'un token expat sur un endpoint agent**.
  - Verrouillage : blocage si prérequis non DONE → erreur claire ; cycle de prérequis rejeté.
  - Rappels : flux d'approbation, interpolation, **zéro envoi réel** (mocks).
- **Mocks** pour tout service externe (mail, WhatsApp, Storage). Aucun appel réseau réel en test.

---

## PARTIE 9 — RÈGLES

1. **TOUT LE CODE EN ANGLAIS** (identifiants, tables, colonnes, enums, commentaires). Les labels FR sont une affaire de frontend.
2. **Pattern `Model → Schema → Repository → Manager → Router`.** Le Router n'appelle jamais un Repository.
3. **Modèles dans `shared/models/`, schémas dans `src/{domain}/`.**
4. **API async, moteur/scheduler sync.**
5. **Tout scopé par `agency_id`.** Pas de fuite cross-agency.
6. **JSONB** pour les champs spécifiques/extensibles, pas de colonnes ad hoc.
7. **RIEN EN DUR pour le RBAC** : aucun mapping rôle→permission en code. Seul le *catalogue* de permissions est en code (synchronisé vers la DB au boot). Bindings et affectations en base.
8. **Deny par défaut** : route sans binding = 403, et le boot plante (`assert_all_routes_bound`).
9. **Deux identités séparées** (`Agent`, `ExpatUser`), deux audiences JWT. L'expatrié ne passe pas par le moteur RBAC.
10. **Tests** : testcontainers PG, plugins `make_*(**overrides)`, ≥ 1 test/endpoint, isolation par truncate en teardown, mocks pour l'externe.
11. **`ruff` + `mypy` clean** avant toute validation d'étape.
12. **OpenAPI committé**, régénéré à chaque changement d'API.
13. **Pas de scope creep.** Respecter la liste « À NE PAS CODER » (PARTIE 1). En cas de doute, demander.
14. **Porter, pas réécrire.** Réutiliser `_prism-reference/` (PARTIE 10) pour le socle.

---

## PARTIE 10 — PORTAGE PRISM

Les repos Prism existent déjà en local. On ne copie rien : **`_prism-reference/` est un lien symbolique (gitignoré, lecture seule) vers le repo `prism-backend` réel.** À créer une fois, avant l'étape 1, depuis la racine de ce repo :

```bash
ln -s /Users/alexandre/Desktop/FreelanceProject/prism/prism-backend _prism-reference
echo "_prism-reference" >> .gitignore
```

On **porte** depuis ce lien (en adaptant les noms au domaine Nidria), on ne recopie rien aveuglément.

**À porter quasi tel quel (infra + harnais)** :
- `core/security.py` (JWT) — adapter pour les 2 audiences agent/expat.
- `core/database.py`, `core/exceptions.py`.
- `conftest.py` + le harnais testcontainers + le pattern des plugins `make_*`.
- `alembic/` (env + config), la CI (`ci.yml`), `Dockerfile`, `fly.toml`, `start.sh`.

**À porter en adaptant (la coquille CRM → dossiers)** :
- Le pattern repo/manager/router de `Company` → `ClientCase`.
- Les filtres + pagination de la liste companies → liste des dossiers.
- `ActivityLog` (modèle + manager) → gardé, types d'actions adaptés.
- `APScheduler` + `JobConfig`/`JobRun` → dispatch des rappels.

**À reconstruire à neuf (le domaine)** :
- Tout l'ERD nouveau : parcours / étapes / instances, rappels, documents, expatriés.
- Le moteur RBAC dynamique (PARTIE 5) — Prism a un RBAC configurable proche, s'en inspirer mais réécrire propre.

> Chaque sous-étape « porter depuis Prism » dans `SETUP.md` pointe un chemin précis sous `_prism-reference/`.
