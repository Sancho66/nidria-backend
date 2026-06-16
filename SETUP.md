# SETUP.md — Nidria Backend (Instructions Claude Code)

> ⚠️ **LIS `CLAUDE.md` EN ENTIER AVANT DE COMMENCER.**
> Suis les étapes dans l'**ORDRE EXACT**. Une étape = une session validée.
> **DEMANDE VALIDATION après chaque étape.**

---

## RÈGLE ABSOLUE (rythme AVANT / APRÈS)

**AVANT chaque étape** : explique ce que tu vas faire, **liste les fichiers** à créer/porter, pose tes questions. **ATTENDS ma validation.**

**APRÈS chaque étape** : résumé de ce qui a été fait, lance les vérifications (`uvicorn` démarre, `pytest`, `mypy`, `ruff`), confirme que rien n'est cassé. **ATTENDS « OK, suivant. »**

> Ne code jamais l'étape N+1 avant mon « OK, suivant » sur l'étape N. Ne dépasse jamais le périmètre des 4 features (voir CLAUDE.md PARTIE 1 — liste « À NE PAS CODER »). En cas de doute : demande, n'ajoute pas.

---

## ÉTAPE 1 — Lis les specs

Lis `CLAUDE.md` puis ce `SETUP.md` en entier. Inspecte `_prism-reference/` pour repérer les fichiers à porter (PARTIE 10 de CLAUDE.md). Pose **toutes** tes questions (data model, RBAC dynamique, double auth, périmètre).

**Attends ma validation.**

---

## ÉTAPE 2 — Init projet

**Objectif** : un squelette FastAPI qui démarre et répond `/ping`.

Sous-étapes :
- `pyproject.toml` (uv) : fastapi, sqlalchemy[asyncio], alembic, pydantic, pydantic-settings, apscheduler, python-jose, bcrypt, httpx, loguru, resend ; dev : pytest, pytest-asyncio, testcontainers[postgres], ruff, mypy.
- `src/main.py` : `FastAPI()` + `GET /ping` → `"pong"`.
- `src/core/config.py` : pydantic-settings — `DATABASE_URL`, `DATABASE_URL_SYNC`, `JWT_AGENT_SECRET`, `JWT_EXPAT_SECRET`, `JWT_REFRESH_SECRET`, `SCHEDULER_ENABLED` (false en dev), `RESEND_API_KEY` / SMTP, `SUPABASE_STORAGE_*`, `CORS_ORIGINS`, `ENVIRONMENT`.
- `src/core/database.py` : async engine + sessionmaker + `get_db`.
- `shared/models/__init__.py`, `.env.example`, `Dockerfile`, `fly.toml` (`auto_stop_machines=false`), `start.sh`, `alembic.ini` + `alembic/env.py`.

Porter depuis `_prism-reference/` : structure de `config.py`, `database.py`, `Dockerfile`, `fly.toml`, `alembic/`.

**Vérif APRÈS** : `uv run uvicorn src.main:app` démarre, `/ping` → `"pong"`.

**Attends ma validation.**

---

## ÉTAPE 3 — Core

**Objectif** : les briques transverses (exceptions, enums, base modèle, sécurité, dépendances).

Sous-étapes :
- `src/core/exceptions.py` : `NotFoundError`, `ForbiddenError`, `ConflictError`, `ValidationError` + handlers FastAPI.
- `src/core/enums.py` : `Audience` (PUBLIC/AGENT/EXPAT, pour `protected_resource`), `ActorType` (AGENT/EXPAT/SYSTEM), `CaseStatus`, `StepStatus` (TODO/IN_PROGRESS/BLOCKED/DONE), `ResponsibleType` (AGENT/EXPAT/EXTERNAL), `ReminderChannel` (MAIL/WHATSAPP/IN_APP), `ReminderStatus` (TO_APPROVE/APPROVED/SENT/CANCELLED), `RecipientType` (EXPAT/EXTERNAL), `DocValidationStatus` (OK/INCOMPLETE/TO_FIX), `ExternalContactType`, `InvitationStatus`.
- `shared/models/base.py` : `DeclarativeBase` + `TimestampMixin` (created_at/updated_at) + `PersonNameMixin` (first_name/last_name/email) + PK UUID.
- `src/core/security.py` : `hash_password`, `verify_password`, `create_access_token(sub, audience)`, `create_refresh_token`, `decode_token(token, audience)` — **deux audiences agent/expat**.
- `src/core/dependencies.py` : `get_db`, `get_current_agent`, `get_current_expat` (deux dépendances distinctes selon l'audience du token).

Porter depuis `_prism-reference/` : `exceptions.py`, `security.py` (adapter pour les 2 audiences).

**Vérif APRÈS** : `mypy` + `ruff` clean, imports OK.

**Attends ma validation.**

---

## ÉTAPE 4 — Tous les modèles + migration

**Objectif** : les 16 tables cœur + 5 tables RBAC, en une migration.

Sous-étapes (crée TOUS les modèles d'un coup, voir CLAUDE.md PARTIE 4) :
- `shared/models/agency.py` — `Agency`
- `shared/models/agent.py` — `Agent` (pas d'enum role)
- `shared/models/expat_user.py` — `ExpatUser`
- `shared/models/external_contact.py` — `ExternalContact`
- `shared/models/client_case.py` — `ClientCase`
- `shared/models/family_member.py` — `FamilyMember`
- `shared/models/case_note.py` — `CaseNote` (notes internes, `is_confidential`)
- `shared/models/invitation.py` — `AgentInvitation` + `CaseInvitation`
- `shared/models/journey.py` — `JourneyTemplate` + `JourneyTemplateStep` + `StepPrerequisite`
- `shared/models/case_step_progress.py` — `CaseStepProgress` (responsable polymorphe)
- `shared/models/reminder.py` — `Reminder`
- `shared/models/message_template.py` — `MessageTemplate`
- `shared/models/document.py` — `Document`
- `shared/models/activity.py` — `ActivityLog`
- `shared/models/rbac.py` — `Permission`, `Role` (+`cloned_from_role_id`), `RolePermission`, `ProtectedResource` (l'affectation vit sur `agent.role_id`)

Update `shared/models/__init__.py` + `alembic/env.py`.
Migration : `alembic revision --autogenerate -m "create_all_tables"` puis `alembic upgrade head`.

**Vérif APRÈS** : migration autogénérée cohérente (relire le diff), `upgrade head` passe sur un PG de test, `mypy`/`ruff` clean.

**Attends ma validation.**

---

## ÉTAPE 5 — Moteur RBAC dynamique

**Objectif** : le moteur DB-driven (sync catalogue + dépendance d'enforcement + vérif boot). Invisible pour le testeur, fondation de tout l'accès.

Sous-étapes :
- `src/core/rbac/permissions.py` : l'enum `Permission` (catalogue) + `sync_permissions(db)` (insert des manquantes au boot, jamais de delete).
- `src/core/rbac/enforcement.py` : `enforce()` en **dépendance FastAPI globale** (`FastAPI(dependencies=[Depends(enforce)])` — PAS un middleware Starlette : le template de route n'est dans le scope qu'après routing). Résout le binding (`protected_resource`) sur le **template de route**, deny par défaut si non lié ; branche sur **`audience`** : PUBLIC passe ; AGENT → token agent + check `effective_permissions` ; EXPAT → token expat valide, pas de matrice (ownership en Manager). Stocke l'acteur résolu dans `request.state.actor`.
- `src/core/rbac/integrity.py` : `assert_all_routes_bound(app, db)` — **plante le démarrage** si une route déclarée (hors whitelist infra `/ping`, `/health`, `/docs`, `/openapi.json`, `/redoc`) n'a pas de binding.
- Résolution `effective_permissions` (les permissions du rôle unique de l'agent — `agent.role_id` → `role_permission` ; modèle Prism, pas d'union).
- Brancher dans `src/main.py` lifespan : `sync_permissions` → `assert_all_routes_bound` → (scheduler plus tard).
- Harnais : fixture qui **seed la baseline RBAC** (catalogue + rôles système + matrice + bindings) sur la DB testcontainer — le boot check tourne aussi en test, jamais désactivé.

Tests (`tests/test_rbac.py`) :
- route non bindée → 403 ; binding `audience=PUBLIC` → passe sans token ; AGENT avec permission → 200 ; AGENT sans permission → 403 ; binding EXPAT + token expat → passe (pas de matrice) ; permissions = exactement la matrice du rôle unique ; **`assert_all_routes_bound` lève si binding manquant** (et ignore la whitelist infra).

**Vérif APRÈS** : `pytest tests/test_rbac.py`, `mypy`/`ruff` clean.

**Attends ma validation.**

---

## ÉTAPE 6 — Double auth (agent + expatrié)

**Objectif** : les deux flux d'authentification, étanches.

Sous-étapes :
- Tables (migration **additive**) : `refresh_token` (jti PK, actor polymorphe sans FK, expires_at, revoked_at, created_at) + `password_reset_token` (token unique, expires_at ~1h, consumed_at) — `shared/models/auth_tokens.py`.
- `src/auth/` : `auth_schema.py`, `auth_repository.py`, `auth_manager.py`, `auth_router.py` (+ `BINDINGS`).
- Agent : `POST /auth/agent/login`, `/refresh`, `/logout`, `GET /auth/agent/me`, `POST /auth/agent/forgot-password`, `/reset-password`. **Pas de superadmin** : le premier admin de chaque agence est créé par le script de seed/onboarding (étape 14) ; en attendant, les fixtures de test créent les agents.
- Expatrié : `POST /auth/expat/activate` (via `case_invitation.token` → set password → `activated_at` ; expat déjà activé → 200 `already_active`, mdp **inchangé**), `/login`, `/refresh`, `/logout`, `GET /auth/expat/me`, `POST /auth/expat/forgot-password`, `/reset-password`.
- Refresh : secret unique `JWT_REFRESH_SECRET`, claims **`audience`** (validé par chaque endpoint, mismatch → 401) et **`jti`**. **Rotation** : jti actif requis → nouveau couple access+refresh, ancien jti révoqué ; réutilisation d'un jti mort → 401 + révocation de **tous** les refresh actifs de l'acteur. Logout révoque le jti courant.
- Reset : `forgot-password` → 200 identique que l'email existe ou non ; mail (mock en test) seulement si compte existant (expat non activé : 200 silencieux, pas de mail). `reset-password` → set password + consomme le token + révoque tous les refresh actifs.
- Login : **401 générique** (même réponse email inconnu / mauvais mdp).
- Bindings `protected_resource` (via `BINDINGS` du router) : login/activate/refresh/forgot/reset = `audience=PUBLIC` ; me/logout = l'audience correspondante.

Tests (`tests/test_auth.py`, plugins `agent`/`expat`/`case`) :
- agent login OK / mauvais mdp et email inconnu → 401 générique identique / me / unauthorized ;
- **rotation** : refresh → nouveau couple, l'ancien refresh meurt ; **réutilisation détectée → toute la famille révoquée** ;
- mismatch d'audience sur refresh (et access) dans les deux sens → 401 ;
- logout → le refresh révoqué ne fonctionne plus ;
- expat activate (+ cas déjà activé, mdp inchangé) + login + me ; login avant activation → 401 ;
- forgot-password non-révélateur (existant/inconnu → même 200 ; mail seulement si existant ; expat non activé → pas de mail) ;
- reset-password : succès (mdp changé, token consommé, **tous les refresh tués**), token réutilisé/expiré → 400 ;
- **token expat rejeté sur un endpoint agent** (et inversement).

**Vérif APRÈS** : `pytest tests/test_auth.py tests/test_rbac.py`, lint/types clean.

**Attends ma validation.**

---

## ÉTAPE 7 — Agence + onboarding

**Objectif** : créer une agence, inviter et activer des agents, scoping tenant.

Sous-étapes :
- `src/agencies/` : schema + repo + manager + router (+ `BINDINGS`).
- **Pas de `POST /agencies` live au MVP** (pas de superadmin) : la création d'une agence + son premier admin passe par le **script de seed/onboarding** (`scripts/seed.py`, lancé à la main par nouvelle agence). Endpoints live : `GET /agencies/me` (AGENT sans permission), `PATCH /agencies/me` (permission `agency.manage`, admin-only ; `slug` immutable).
- **Scoping me-based** : le tenant vient du token, jamais d'`agency_id`/slug en URL.
- Invitations agents (permission `agent.manage`) : `POST /agencies/me/invitations` (validation du rôle **à la création** : système OU de cette agence ; email déjà agent — toute agence — → 409 ; PENDING en cours → 409 ; mail mock ; expiry 7 j en config), `GET /agencies/me/invitations` (liste), `DELETE /agencies/me/invitations/{id}` (cancel → status `CANCELLED` dédié), `POST /agencies/invitations/accept` (PUBLIC : token PENDING non expiré → crée `Agent` **dans l'agence de l'invitation** + assigne le rôle + couple de tokens).

Tests (`tests/test_agencies.py`) : get/patch me (+403 sans permission, slug immutable), invitations (validation rôle, 409 ×3, liste, cancel → accept 400), accept (rôle assigné, **création chez l'agence de l'invitation**), **un agent ne voit pas une autre agence**.

**Vérif APRÈS** : suite verte, lint/types clean.

**Attends ma validation.**

---

## ÉTAPE 8 — JourneyTemplate + étapes + prérequis

**Objectif** : les parcours-types configurables, avec prérequis (base de la feature 4).

Sous-étapes :
- `src/journeys/` : schema + repo + manager + router.
- CRUD `JourneyTemplate` (scopé agence) ; CRUD `JourneyTemplateStep` (name, position, estimated_days, default_responsible_type) ; gestion des `StepPrerequisite` (M2M).
- Validations métier : prérequis **dans le même template** ; **détection de cycle** (refus si l'ajout d'un prérequis crée un cycle).

Tests (`tests/test_journeys.py`) : création template, ajout d'étapes ordonnées, set prérequis, **cycle rejeté**, prérequis hors template rejeté.

**Vérif APRÈS** : suite verte, lint/types clean.

**Attends ma validation.**

---

## ÉTAPE 9 — ClientCase + famille + pros externes

**Objectif** : le dossier, hub du produit, avec invitation de l'expatrié.

Sous-étapes :
- `src/cases/` : schema + repo + manager + router.
- `POST /cases` : crée le dossier ; relie l'expatrié principal **par email** (crée-ou-relie `ExpatUser` + crée `CaseInvitation` + envoie le mail — mock en test) ; membres famille ; contacts externes ; owner ; tags (JSONB) ; source ; statut.
- `GET /cases` paginé + **filtres** (statut, pays destination, owner, langue client, tags) ; `GET /cases/{id}` (avec famille, externes, progress) ; `PATCH /cases/{id}`.
- CRUD notes de dossier (`case_note`) : création/édition par un agent ; les notes `is_confidential` sont **filtrées par permission** (seuls les agents ayant la permission dédiée les voient).
- Export dossier — **un seul format minimal** au MVP (PDF basique : infos clés + journal), pas de double format PDF+Excel. *Limitation connue : polices core latin-1 (fpdf2) — un nom cyrillique/coréen est dégradé en `?` ; police unicode embarquée → V1.5.*
- **ActivityLog auto** sur les mutations (statut, owner, etc.) via `activity_manager.log_action()`.

Porter depuis `_prism-reference/` : pattern repo/manager/router + filtres/pagination de `Company`.

Tests (`tests/test_cases.py`) : create (+ invitation créée), list, filtres, detail, `PATCH` crée un ActivityLog, scoping agence.

**Vérif APRÈS** : suite verte, lint/types clean.

**Attends ma validation.**

---

## ÉTAPE 10 — CaseStepProgress + étapes verrouillées (FEATURE 4)

**Objectif** : instancier un parcours sur un dossier et **enforcer le verrouillage**.

Sous-étapes :
- `src/progress/` : schema + repo + manager + router.
- Assignation d'un `JourneyTemplate` à un dossier → **instancie** un `CaseStepProgress` par étape du template (statut initial TODO/BLOCKED selon prérequis).
- Copie du responsable à l'instanciation : `default_responsible_type=EXPAT` se copie directement (le principal du dossier est implicite) ; AGENT/EXTERNAL → `responsible_type` reste **NULL** jusqu'à assignation explicite d'une personne (le CHECK interdit un type avec FK nulle).
- **Backfill (décision étape 8, option A — édition libre des templates assignés)** : l'ajout d'une étape à un template assigné crée automatiquement un `CaseStepProgress` (TODO/BLOCKED selon prérequis) sur tous les dossiers vivants utilisant le template. **DONE est irrévocable** : un changement de prérequis ne dé-valide jamais une étape DONE, il ne contraint que les transitions futures.
- **À trancher ici (conséquence de l'option A)** : le statut stocké `BLOCKED` peut être périmé après mutation des prérequis d'un template assigné (un BLOCKED dont le prérequis a été retiré, un TODO qui devrait être BLOCKED). Options : **BLOCKED recalculé à la lecture** (statut stocké réduit à TODO/IN_PROGRESS/DONE, BLOCKED = projection) vs **resynchronisation des statuts stockés** à chaque mutation de prérequis. Argumenter et trancher à cette étape.
- Passage d'étape : `PATCH /cases/{id}/steps/{step_id}` (IN_PROGRESS / DONE) → **refus + erreur explicite** si une étape prérequise n'est pas DONE.
- **Responsable polymorphe** : set `responsible_type` + la FK correspondante (agent / externe / — si expat).
- `completed_by_agent_id` + ActivityLog auto sur complétion.

Tests (`tests/test_progress.py`) : l'assignation instancie toutes les étapes ; validation d'une étape libre ; **blocage si prérequis non DONE → erreur claire** ; déblocage en cascade ; polymorphisme du responsable (3 cas).

**Vérif APRÈS** : suite verte, lint/types clean.

**Attends ma validation.**

---

## ÉTAPE 11 — Documents

**Objectif** : upload et validation des pièces.

Sous-étapes :
- `src/documents/` : schema + repo + manager + router.
- Upload vers **Supabase Storage** (mock en test) ; rattachement dossier et/ou étape ; `validation_status` (OK/INCOMPLETE/TO_FIX) ; `expires_at` ; historique.
- `uploaded_by` **polymorphe** (AGENT/EXPAT).
- `PATCH` validation par un agent (permission `DOCUMENT_VALIDATE`).

Tests (`tests/test_documents.py`) : upload agent, upload expat, list par dossier, validation, scoping.

**Vérif APRÈS** : suite verte, lint/types clean.

**Attends ma validation.**

---

## ÉTAPE 12 — Rappels + scheduler (FEATURE 3)

**Objectif** : rappels avec **approbation manuelle obligatoire** + dispatch APScheduler.

Sous-étapes :
- `src/reminders/` : schema + repo + manager + router.
- CRUD `MessageTemplate` (scopé agence, variables).
- CRUD `Reminder` : date, canal, destinataire (EXPAT ou EXTERNAL), message éditable, lien optionnel étape/template.
- **Flux d'approbation** : `TO_APPROVE` → `POST /reminders/{id}/approve` (set `APPROVED` + `approved_by_agent_id`) → dispatch → `SENT`. **Aucun envoi sans approbation.**
- Canaux : MAIL (Resend/SMTP), IN_APP (**pas de table notifications en plus** : un reminder `channel=IN_APP, recipient=EXPAT` envoyé EST la notif, lue par l'espace expat), **WHATSAPP = render le message + le retourner pour copier-coller** (pas d'API au MVP).
- Interpolation **serveur** des variables, **à la création/édition** (c'est CE texte qui est approuvé) ; `{days_left}` **projeté à `scheduled_at`** (= `estimated_days` − jours entre le démarrage de l'étape et la date d'envoi planifiée, plancher 0) ; variable insoluble → 422 nommant la variable ; édition d'un APPROVED → **retour TO_APPROVE** + `approved_by` effacé.
- Idempotence des relances auto : colonne `reminder.auto_threshold_days` (nullable) + **unique `(step_progress_id, auto_threshold_days)`** — un palier ne peut physiquement être créé deux fois. Activable par agence : `agency.settings["auto_reminders_enabled"]` (défaut true), seuils `[20, 30]` en config. Acteur des logs : SYSTEM.
- **Portage Prism entier du moteur de jobs** (le brief le mappe explicitement ; seule l'UI Settings/Engine Control reste hors scope) :
  - Tables `job_config` + `job_run` (adaptées : UUID PK, **plateforme sans `agency_id`**, `triggered_by_agent_id`) — même migration additive que `auto_threshold_days`.
  - `job_wrapper` porté (`src/core/job_wrapper.py`, sync) : enabled/paused/fenêtre → run **SKIPPED** ; JobRun RUNNING committé upfront ; callback `log()` progressif dans `log_output` ; succès/échec + stats + durée + maj `last_run_*`. Les 2 jobs passent dedans.
  - `src/core/scheduler.py` : lit les `JobConfig` au boot (CronTrigger + timezone), registry `{dispatch_reminders, auto_reminders}`, `max_instances=1` + `coalesce`, dispatch en `FOR UPDATE SKIP LOCKED`. Brancher dans le lifespan (`SCHEDULER_ENABLED`).
  - Les 2 jobs **seedés** (création si absents, jamais d'écrasement d'une édition runtime — règle des rôles système).
  - `src/jobs/` : `GET /jobs`, `GET /jobs/{job_id}/runs` (+ run detail avec `log_output`), `PATCH /jobs/{job_id}` (cron/enabled → **hot-reload** du scheduler), `POST /jobs/{job_id}/pause`, `/resume`, `/trigger` (avec `dry_run`). Permission **`job.manage`** (catalogue +1, admin-only, lectures comprises — c'est de l'ops, pas de la référence tenant).

Tests (`tests/test_reminders.py` + `tests/test_jobs.py`, **mocks, zéro envoi réel**) :
- **L'INVARIANT nommé : un TO_APPROVE échu traverse un tick → rien ne part** ; approve → dispatch → SENT ;
- édition d'un APPROVED → retour TO_APPROVE + `approved_by` effacé ; interpolation correcte (`days_left` projeté à J+10) ; insoluble → 422 ;
- WhatsApp : le dispatcher le saute, `mark-sent` requis (refusé sur non-whatsapp / non-approved) ; IN_APP dispatché sans mail ;
- palier J+20 créé **une seule fois** sur deux ticks (l'unique en action) ; `auto_reminders_enabled=false` → aucun palier ;
- jobs : PATCH cron → reschedule effectif ; pause → le tick saute (run SKIPPED) ; trigger manuel → JobRun créé ; `dry_run` ne mute rien ; job désactivé au boot non programmé.

**Vérif APRÈS** : suite verte, lint/types clean.

**Attends ma validation.**

---

## ÉTAPE 13 — ActivityLog (endpoints) + dashboard

**Objectif** : exposer la timeline et un dashboard simple.

Sous-étapes :
- `src/activity/` : `GET /cases/{id}/activity` (timeline paginée desc, filtre `action_type` répétable, `case.view`). **Pas de POST manuel** (décision étape 13) : le journal n'enregistre que des faits produits par le système — un POST manuel serait un second système de notes contournant la frontière `is_confidential` de `case_note`. Les managers cases/progress/reminders/documents appellent déjà `log_action()` auto.
- `src/expat/` (domaine portail) : contrat de champs **par exclusion** (pas de notes, pas de journal brut, pas de tags/source, pas de staffing interne, **zéro UUID dans la timeline**) ; `responsible` affichable (`agency`/`you`/`external`+nom) ; referent = owner (nom+email) ; `GET /expat/cases/{id}/notifications` = les reminders IN_APP `sent` (Q8). Test du contrat = assert sur l'ensemble **exact** des clés.
- **Endpoints EXPAT (feature 1 — la timeline client)** : `GET /expat/cases` (mes dossiers : agence, statut, pays, avancement résumé) et `GET /expat/cases/{case_id}` (détail : **timeline projetée** — étapes faites/en cours/à venir/bloquées, délais estimés —, **référent/owner** de l'agence, interlocuteurs/responsables). Bindings `audience=EXPAT` sans matrice, **ownership en Manager** (`principal_expat_user_id` = acteur, sinon **404**) — le moteur RBAC est prêt pour ça depuis l'étape 5. Les rappels IN_APP envoyés s'y ajoutent à l'étape 12 (la « notif » lue par l'espace expat).
- `src/dashboard/` : `GET /dashboard` — comptes simples (dossiers par statut, par pays). **Pas d'analytics riche** (V1.5).

Tests (`tests/test_activity.py`, `tests/test_dashboard.py`) : timeline, log manuel, auto sur mutation, comptes corrects.

**Vérif APRÈS** : suite verte, lint/types clean.

**Attends ma validation.**

---

## ÉTAPE 14 — CI/CD + seed + vérification finale

**Objectif** : pipeline vert + données réalistes seedées.

Sous-étapes :
- `.github/workflows/ci.yml` : `ruff` → `mypy` → `pytest` (testcontainers) → deploy Fly.io.
- `scripts/seed.py` :
  - **catalogue de permissions** (sync), **4 rôles système** (admin/member/viewer/case_manager) + leur **matrice** `role_permission`, **bindings** `protected_resource` de toutes les routes MVP ;
  - 3 agences + agents (Reside Paraguay, Domiciliation Bulgarie, Expatriation.io) ;
  - **3 dossiers seed** avec parcours/étapes/progress/rappels : **Famille Martin** (5 étapes, étape 3/5, rappel J+10 à approuver), **Aleksei Volkov** (7 étapes, étape 2/7), **Sophie Dupont** (4 étapes, étape 1/4, **étape 3 verrouillée**).
- Régénérer `openapi.json` (`make openapi`).

> **Espace prestataires — VAGUE C : assignation nominale sur les étapes** : le responsable d'étape passe d'un TYPE à une **personne nommée** (agent interne OU prestataire externe). Modèle (Option A, sans changement du CHECK) : `responsible_type=agent` + `responsible_agent_id` porte **n'importe quel agent**, le discriminant est `agent.is_external` ; `responsible_type=external`+`responsible_external_id` (external_contact legacy) conservé ; `expat`→principal. **Template** : `journey_template_step.default_responsible_agent_id` (FK agent — **interne OU externe DURABLE de l'agence**, modèle révisé : un externe est un partenaire durable, donc valable comme défaut ; validation = appartient à l'agence, agent d'une autre agence rejeté) ; copié à l'instance au rattachement (`_initial_responsible`). **Auto-assignation (invariant RGPD)** : à l'instanciation (`assign_journey` ET `backfill_step`), si le défaut copié est un externe → `ensure_external_assignment(case, agent)` **idempotent** (une seule ligne par (dossier, externe) même si défaut de plusieurs étapes) → l'externe a donc toujours accès au dossier dont il est responsable. Le 409 du B-unassign protège le retrait. Éditer le défaut d'un template **après coup** ne re-propage pas aux dossiers existants (copie one-shot, comportement inchangé). **Endpoint dédié** `PUT /cases/{id}/steps/{progress_id}/responsible` (gate **`case.edit`** ; le PATCH step garde `step.complete` pour les transitions ; les champs responsible ont quitté `StepProgressUpdateRequest`). **COHÉRENCE B↔C (invariant RGPD)** : nommer un agent externe responsable EXIGE qu'il soit déjà assigné au dossier (B) → sinon **422** (validé via `assignment_exists`, pas `get_agent_in_agency` qui exclut les externes) ; réciproquement, le **DELETE d'assignation dossier (B) refuse 409** si l'externe est encore responsable d'une étape. L'état « responsable sans accès » est **inatteignable** dans les deux sens. **Affichage** : la résolution du responsable (nom + is_external) est centralisée et **batchée** dans `timeline_for_case` (pas de N+1) ; `StepProgressResponse` expose `responsible_name`/`responsible_is_external`. **Anti-staffing** : un agent **interne** responsable n'expose jamais son nom aux faces expat/externe (« agency »/« votre conseiller ») ; un **externe** responsable expose bien son nom (« Me Robert gère cette étape »). Migration additive `a7d9c1e3f5b4`.

> **Espace prestataires — VAGUE B : scoping par dossier + portail externe (isolé RGPD)** : on ouvre l'accès des externes aux dossiers **assignés uniquement**, via un **portail dédié `/external/cases/*`** (jumeau du portail expat) — les routes internes `/cases/*` restent **100 % fermées** aux externes (garde de la vague A inchangé). **Assignation** : table `case_external_assignment(case_id, agent_id)` ; un externe accède à un dossier ⟺ une ligne existe. **Scoping** : helper UNIQUE `src/external/scoping.py::get_case_for_external` (jumeau de `get_case_for_expat`) — assigné + même agence + non soft-deleted, sinon **404** (jamais 403). Toute lecture/écriture externe le traverse (portail, documents, commentaires). **Permissions** : 3 perms dédiées `external.case.view` / `external.document.upload` / `external.case.comment`, accordées aux 6 rôles externes — **exclues des rôles internes** (admin/case_manager) : le préfixe `external.` ne peut structurellement pas ouvrir une route interne, et vice-versa. `permission ∧ scoping` toujours (chaque route externe est gated par une perm `external.*` ET appelle `get_case_for_external`). **Garde élargi** : l'allowlist passe de l'identité seule à + les 9 routes `/external/cases/*` (nommées) ; un test de complétude asserte que toute route `/external/*` déclarée est dans l'allowlist ET couverte par un test « non-assigné → 404 ». **Vue réduite (non-fuite RGPD)** : `ExternalCaseDetailResponse` = résumé + référent (owner) + timeline (toutes étapes, réquisits) ; **JAMAIS** notes (confidentielles ou non), activity, reminders, external_contacts, autres externes, staff, ni la **`value`** des réquisits (le n° de passeport du client reste au client — l'externe voit `status`/`person_label`/`reference`/`kind`, pas la valeur). Capacités : voir le dossier assigné, déposer un document sur un réquisit (`fulfill_requirement_as_external`), commenter (même fil que l'agence, pas de notif client). Côté agence (gate **`agent.manage`**) : `GET/POST /cases/{id}/external-assignments`, `DELETE …/{agent_id}` (assigner = acte d'admin, qui décide qui accède aux données d'un client). Migration additive `f5c7e9a1b3d2`. Désassigner coupe l'accès immédiatement (relecture de la table, pas de cache).

> **Espace prestataires — VAGUE A : l'externe comme agent restreint (fail-closed)** : un prestataire (avocat, notaire, banque, comptable, traducteur, autre) **réutilise le système d'agents** (même table `agent`, même auth, même audience JWT AGENT) avec un **rôle externe**. Décision figée : PAS de 3e audience. Deux flags : **`role.is_external`** (autoritaire — les 6 rôles système externes : `external_lawyer/notary/bank/accountant/translator/other`, seedés avec **ZÉRO permission**) + **`agent.is_external`** (dénormalisé, posé à la création depuis le rôle ; filtre bon marché lu par `enforce()` et toutes les listes « agents de l'agence »). **GARDE FAIL-CLOSED** (cœur sécu) dans `enforce()`, AVANT le check de permission : un acteur externe est refusé (403) sur **toute** route hors de son allowlist d'identité (`GET /auth/agent/me`, `POST /auth/agent/logout`) — ça ferme aussi les routes « token agent suffit » (`/agencies/me/members`, `/journeys`, `/roles`, `/message-templates`…) que zéro permission NE ferme PAS. Donc en vague A l'externe se connecte et voit son `/me`, **RIEN d'autre**. Les permissions + le scoping par assignation atterriront **ensemble en vague B** (donner `case.*` sans scoping = fuite : les managers scopent par `agency_id`). Endpoints (gate `agent.manage`) : `GET /agencies/me/external-roles`, `GET /agencies/me/external-members`, `POST /agencies/me/external-invitations` (invite avec un rôle externe ; accept = flux agent existant). **Anti-régression (sweep intégral)** : exclusion des externes partout où « agents de l'agence » = internes — `list_agents_with_roles` (members/owner picker), `list_agents_with_permissions` (anti-lockout), `rebind_agents`, `get_agent_in_agency` de cases (owner), progress (responsible=agent), impersonation (cible), roles (set_member_role) ; `list_roles` (picker interne) exclut les rôles externes ; les deux flux d'invitation ne se croisent jamais (invite interne refuse un rôle externe et inversement, 422). Migration additive `e4b6d8f0a2c1` (deux bools, défaut false).

> **Compteur de jours / deadline d'étape** : (c) multi-deadlines **repoussé** (backlog : change de modèle + espace prestataire inexistant). (a)+(b) : colonne **`case_step_progress.due_at`** (timestamptz nullable) — deadline FERME optionnelle posée/éditée par l'agence via le `PATCH /cases/{id}/steps/{progress_id}` existant (gate `case.edit` ; `due_at=null` efface). Les deux timelines (`StepProgressResponse` + `ExpatTimelineStepResponse`) exposent un objet **`counter` {target_date, days_remaining, source}** calculé **une seule fois** dans `timeline_for_case` (source unique, lu par les deux faces). Priorité de la date cible : (1) `due_at` ferme → `source="deadline"` ; (2) sinon `started_at + estimated_days` → `source="estimated"` ; (3) sinon `null` (pas de jauge). `days_remaining` = delta en jours (négatif si dépassé). **`started_at` dérivé de l'`activity_log`** (premier `step.started`) et **batché** : un seul `MIN ... GROUP BY details->>'step_progress_id'` pour toute la timeline (pas de N+1 ; testé). `estimated_days` reste **read-through du template** (pas de gel par dossier — autre débat) donc `days_remaining` se recalcule live. Le **vert/orange/rouge est un rendu FRONT** (seuils sur `days_remaining`), le backend ne renvoie que le nombre. Migration additive `d3f5a7c9e1b2`.

> **Documents — dépôt agent par réquisit + liste agrégée enrichie** : on garde le dépôt libre (`POST /cases/{id}/documents`, `POST /expat/cases/{id}/documents`) pour l'imprévu, et on comble le trou côté agence : **`POST /cases/{case_id}/requirements/{requirement_id}/document`** (audience AGENT, gate `case.edit`) — miroir du dépôt-réquisit client de la vague 2 : upload + lie le doc au réquisit (`case_step_requirement.document_id`) + `status=provided` + recompute (auto→DONE). **Cœur factorisé** : `ProgressManager.fulfill_document_requirement(case, requirement, document_id)` partagé par les deux faces (le chemin expat l'utilise aussi) ; seuls le périmètre (agence vs ownership) et l'audience de l'upload diffèrent. La **liste agrégée** (`GET /cases/{id}/documents` et `GET /expat/cases/{id}/documents`) est enrichie : `step_name` (résolu), `requirement_reference` (jointure inverse `case_step_requirement.document_id = document.id`) et **`is_requirement`** (lié vs libre — déterminé PAR LA JOINTURE INVERSE, pas par `step_progress_id` : un doc libre peut porter un `step_progress_id` sans répondre à aucun réquisit). Résolution **batchée** (pas de N+1). Côté client, nouvelle réponse `ExpatDocumentResponse` qui **n'expose AUCUN UUID interne** (plus de `uploaded_by_id`) : `uploaded_by_type` + `is_mine` (le front rend « Vous » / « Votre conseiller »). Pas de migration (route + champs de réponse seulement).

> **Fil de commentaires par étape (vague 5 — ping-pong agent ↔ client)** : un fil de discussion attaché à l'étape d'un DOSSIER (`case_step_progress`, pas le template — il suit l'avancement réel). Table `step_comment` (`case_step_progress_id` FK, `author_type` AGENT|EXPAT + `author_id` UUID **sans FK** polymorphe comme `activity_log`, `body`, `edited_at` colonne dédiée, `deleted_at` **soft-delete**). Module `src/comments/` (mirroir de `documents/` : `agent_router` préfixe `/cases`, `expat_router` préfixe `/expat/cases`, un `CommentsManager` bi-audience). **8 routes** : list/create/patch-own/delete-own × {agent, expat}. **Périmètre** : lecture agent = `case.view` ; écriture/édition agent = **nouvelle permission `case.comment`** (écrire dans un canal vu du client ≠ voir/éditer le dossier ; matrice admin/case_manager/member, **viewer lit mais ne poste pas**) ; client = ownership `get_case_for_expat` (404, jamais de matrice), uniquement SES dossiers. **Chacun n'édite/supprime QUE son propre message** (vérifié serveur via l'identité JWT `author_type`+`author_id`, jamais le payload — un agent éditant un msg client → 403 et inverse). Réponse unifiée `CommentResponse` : `author_label` **résolu** (prénom de l'agent — **exception ASSUMÉE à la règle anti-staffing vague 1** : une conversation n'est pas un statut, un prénom humanise ; cf. `new_comment_to_client` — / nom+prénom du client) + `is_mine`, **jamais d'`author_id` brut** (contrat d'exclusion expat). Soft-delete → `body=null` + `deleted=true` (contenu non fuité, fil intact) ; éditer/supprimer un supprimé → 404. **Notif anti-rafale** : table `step_comment_notification` (`case_step_progress_id`, `recipient_type`, `last_notified_at`) — `last_notified_at` posé UNIQUEMENT après un envoi réussi (≠ dérivation : un 1er mail qui échoue ne supprime pas le 2e), fenêtre **15 min**, best-effort APRÈS commit (un échec n'annule jamais le commentaire), gate via le **même** `agency.settings["step_notifications_enabled"]` que la vague 2. **Câblage front** : `ExpatTimelineStepResponse` expose `progress_id` (l'id `case_step_progress` — nécessaire à la route `/expat/cases/{id}/steps/{progress_id}/comments` ; pas une fuite, c'est une étape du dossier DU client) ; `StepProgressResponse` ET `ExpatTimelineStepResponse` portent `comment_count` (commentaires non supprimés, **COUNT groupé batché** dans `timeline_for_case`, pas de N+1 — badge « X messages » sans déplier le fil).

> **Réquisits d'étape (vague 1/4, lecture agence)** : une étape de parcours peut EXIGER des infos/documents par personne. Définition : table `step_requirement` sur le step (`kind` base_field|custom_field|document, `reference`, `scope` principal|each_person) + `journey_template_step.completion_mode` (auto|agency_validation, défaut agency_validation = flux actuel inchangé). CRUD sous `/journeys/{tid}/steps/{sid}/requirements` (gate `journey.configure`). MATÉRIALISATION : quand une étape devient active (TODO→in_progress), `case_step_requirement` fige un réquisit concret par (définition, personne existante à l'instant t) — FIGÉ (ajout de personne ultérieur = aucun nouveau réquisit), idempotent au reopen. COMPLÉTION dérivée à la lecture : base_field/custom_field `provided` = valeur non-vide lue live sur case_person (jamais copiée) ; document = statut explicite. `GET /cases/{id}` expose par étape `requirements`, `all_requirements_met`, `completion_mode`. Whitelist base_field = les 7 champs d'état civil. AUCUNE écriture client ni auto→DONE dans cette vague. Backlog : un réquisit d'adresse nécessiterait un scope=case (modèle person-centric non percé).

> **Réordonnancement des réquisits d'étape (dégel ciblé)** : `PUT /journeys/{tid}/steps/{sid}/requirements/order` body `{requirement_ids: [...]}` (liste complète des ids du step dans l'ordre voulu) — **même convention que le reorder d'étapes** (`PUT .../steps/order` + `step_ids`), un niveau plus bas. Gate `journey.configure`. Scopé : la liste doit être EXACTEMENT l'ensemble des réquisits du step `{sid}` du template `{tid}` de l'agence — un id étranger (autre step/agence) fait diverger l'ensemble → 422, jamais d'application silencieuse ; template d'une autre agence → 404. Renumérotation dense 0..n-1 (two-phase comme les étapes), idempotent. Renvoie la liste réordonnée.

> **Surface de lecture expat des réquisits (alignement des deux faces)** : la face expat lit désormais la MÊME donnée que la face agence sur les réquisits, sans résolution dupliquée. Sur `ExpatRequirementResponse` : `value` (valeur courante lue à la source — `case_person.<reference>` pour base_field, `custom_fields[key]` pour custom_field ; `null` si pending ou kind=document) et `document_id` (le doc déposé ; joindre à `GET /expat/cases/{id}/documents` pour nom de fichier + lien de téléchargement, comme la face agence). Sur `ExpatTimelineStepResponse` : `completion_mode` (auto vs agency_validation, pour que le client formule « tout est fourni » vs « en attente de validation »). Sur `ExpatCaseDetailResponse` : `custom_field_definitions[]` (mêmes `CustomFieldDefinitionInline` que la face agence, définitions archivées filtrées) pour rendre un custom_field correctement (select, label humain, options) en matchant `reference` ↔ `key`. **Factorisation** : `value` est résolu une seule fois dans `timeline_for_case` (helper partagé `requirements_eval.current_value`, jumeau de `is_provided`) et porté sur le `RequirementStateResponse` partagé ; la face expat le réutilise. Un test de cohérence verrouille `expat.value == agent.value` et `expat.def == agent.def` pour un même réquisit.

> **Réquisits d'étape (vague 2/4, écriture client + auto→DONE + notifs)** : on perce — avec précaution — le principe « expatrié = lecture seule ». Le client peut désormais REMPLIR les réquisits de SON dossier via deux endpoints expat dédiés : `PUT /expat/cases/{case_id}/requirements/{requirement_id}` (valeur base_field/custom_field, écrite sur `case_person` = source de vérité unique ; null efface → retour pending) et `POST /expat/cases/{case_id}/requirements/{requirement_id}/document` (upload, réutilise le chemin documents puis marque le réquisit `provided` + lie le `document_id`). **Quatre bornes serveur, aucune ne fait confiance au payload** : (1) dossier du client (`get_case_for_expat` → 404) ; (2) réquisit rattaché à ce dossier (join scopé → 404) ; (3) étape ACTIVE (in_progress, sinon 409 lecture seule — donc read-only après DONE) ; (4) la personne ciblée est celle déjà matérialisée sur le réquisit (jamais un person_id du payload) — le principal remplit pour la famille. **Recompute partagé** branché aux 3 points d'écriture (fulfillment client, PATCH person agent, upload document) : pour chaque étape active dont tous les réquisits sont fournis → `completion_mode=auto` ferme l'étape en SYSTEM **si le verrou prérequis est respecté** (idempotent : ne touche pas une étape déjà DONE) ; `agency_validation` ne s'auto-ferme JAMAIS mais arme un mail « prêt à valider » à l'owner **sur la seule transition pending→met** (snapshot avant écriture). **3 notifications**, gatées par `agency.settings["step_notifications_enabled"]` (défaut true), best-effort APRÈS commit (un échec d'envoi ne rollback ni ne bloque l'écriture) : (a) activation d'une étape à ≥1 réquisit pending → mail client ; (b) prêt à valider → mail owner ; (c) reopen → mail client au **ton distinct** (« besoin de précisions »). Le portail expat expose les réquisits par étape (`requirements`: status, `person_label` résolu, kind ; réquisits custom_field archivés filtrés). Pas de migration (logique + flag JSONB).

> **Champs personnalisés par agence (custom fields, DÉGEL 2)** : `custom_field_definition` (scopée agence) — une agence définit ses propres champs sur les personnes (`text/number/date/boolean/select/multi_select`, `required`, ordre, `options` pour les select). `key` et `field_type` **immuables** ; suppression = **archive soft** (valeurs conservées). Les valeurs vivent en JSONB `case_person.custom_fields` (isolation RGPD : jamais sur expat_user ; une valeur orpheline après archivage est conservée mais non exposée). Définir = `field.manage` (admin) ; saisir une valeur = `case.edit` via le PATCH person (merge partiel ; required bloquant seulement si la clé est explicitement présente-mais-vide). CRUD sous `/agencies/me/custom-fields`. `GET /cases/{id}` expose `custom_field_definitions` (schéma du formulaire) + `custom_fields` par personne ; le PDF les inclut.

> **Adresse complète collectable (chantier sections, VAGUE B — la plus simple)** : rue/ville/code postal rejoignent le pays comme champs-dossier collectables, origine ET destination. `COLLECTABLE_CASE_FIELDS` passe de 2 à **8** (`{origin,dest} × {country, street, city, postal_code}` dans `src/cases/case_fields.py`) — les 8 colonnes existent déjà sur `client_case`. La validation whitelist de `journey_template_case_field` s'élargit **automatiquement** (elle lit `COLLECTABLE_CASE_FIELDS`, aucune valeur en dur ailleurs — vérifié). Écriture à la création : les 6 nouvelles clés (`origin_street/city/postal_code` + `dest_*`) ajoutées à `CaseCreateRequest` (optionnelles, rétro-compat) et passées à `add_case` → colonnes `client_case`. L'enforcement `required_at_creation` des champs-dossier (vague 2) couvre les 6 nouveaux **sans code neuf** (`getattr(case, case_field)` + check vide). **Non-régression pays** : zéro modif des 7 points de l'écosystème (tri/filtre/KPI/vues/faces/export) — street/city/postal sont collectables uniquement, ni triables ni filtrables. **Pas de migration** (colonnes déjà là). 3 fichiers touchés (whitelist + schéma + manager).

> **Sections de parcours (chantier sections, VAGUE A — socle, 100 % additive)** : un parcours organise ses champs de création en **sections nommées librement** par l'agence. Table `journey_section` (`template_id` FK CASCADE, `name` libre — pas d'unicité, `description` nullable, `position`). Les deux tables de champs gagnent un **`section_id` NULLABLE** (`ondelete=SET NULL`) : `journey_template_field` ET `journey_template_case_field` pointent les **mêmes** sections — une section est un **conteneur plan-agnostique** (peut mélanger champs-personne ET champs-dossier ; l'unification est présentationnelle, le stockage reste deux plans). **Filet anti-trou** : `section_id` NULL = bucket « sans section » → les parcours existants (champs à plat) marchent sans **aucune migration de données** (migration = 1 `CREATE TABLE` + 2 `ADD COLUMN NULL`, les legacy atterrissent en bucket NULL). CRUD `/journeys/{tid}/sections` (5 endpoints GET/POST/PUT order/PATCH/DELETE, gate `journey.configure`, calque : reorder two-phase dense 0..n-1, ensemble exact sinon 422, scoping cross-agence 404). **`DELETE` = `SET NULL`** sur les champs (jamais CASCADE) : supprimer une section ne détruit jamais une déclaration de champ, ses champs retombent dans le bucket. Rattachement champ↔section via les **PATCH existants** étendus (`section_id` optionnel sur `TemplateFieldUpdateRequest`/`CaseFieldUpdateRequest` ; validation : même template ou null ; `required_at_creation` devient optionnel — l'appel front existant « required seul » reste compatible). Détail template : **`sections[]` (groupé) + `unsectioned` (bucket NULL)** ajoutés **à côté** des clés plates `fields[]`/`case_fields[]` **conservées** → le front actuel ne voit aucune différence (non-breaking). **Ordre à deux niveaux — OPTION 1 (segmenté) retenue** (pas l'entrelacé) : dans une section, champs-personne puis champs-dossier, chacun dans son ordre `position` — segmentation **structurelle** (deux listes par section dans la réponse). Choix : vague-socle **purement additive**, zéro endpoint d'ordre existant touché ; le drag mixte (option 2) reste possible plus tard si le besoin réel se confirme. **Non-régression** : zéro modification hors `journeys/*` + les 2 modèles ; l'écosystème pays intouché (vérifié au diff). Migration additive `d1a4f7c2e9b6`.

> **Champs-dossier par parcours (refonte champs de base, vague A — option b)** : les pays (origine/destination) deviennent des **champs-dossier collectables**, attachables à un parcours et affichés à la création seulement s'il les demande — SANS jamais quitter `client_case`. Décision figée **option (b)** (pas (a2)/`target`) : deux plans de stockage = deux mécanismes nets. L'invariant « `journey_template_field.reference` ∈ univers champ-personne » est **préservé intact** ; les champs-dossier vivent dans une table SÉPARÉE `journey_template_case_field` (`template_id` FK CASCADE, `case_field` ∈ `COLLECTABLE_CASE_FIELDS` = `{origin_country, dest_country}` dans `src/cases/case_fields.py`, `required_at_creation`, `position`, `UNIQUE(template_id, case_field)`) — jumelle de `journey_template_field` mais **sous-ensemble strict** : pas de `kind`, pas de résolution de définition, pas de `is_archived` (les pays sont des colonnes figées). CRUD `/journeys/{tid}/case-fields` (5 endpoints GET/POST/PUT order/PATCH required/DELETE, gate `journey.configure`), calque exact (reorder two-phase dense 0..n-1, ensemble exact sinon 422, dup → 409, `case_field` hors whitelist → 422, scoping cross-agence/id étranger → 404). Exposé dans `JourneyTemplateDetailResponse.case_fields` **à côté** de `fields` (deux listes distinctes ; l'UI les unifiera en vague B, le backend les sépare proprement). **Création** : le pays s'écrit via les clés top-level **existantes** `origin_country`/`dest_country` de `CaseCreateRequest` (aucun nouveau chemin d'écriture) ; enforcement `required_at_creation` des champs-dossier en **méthode SÉPARÉE** `_enforce_required_case_fields` (lue sur le `case`, pas sur `case_person`), dans la transaction atomique (422 → rien créé, pas d'orphelin) ; uniquement si un parcours est assigné (sans parcours = pas d'enforcement, rétrocompat). **Non-régression GARANTIE** : ZÉRO modification de l'écosystème pays — `cases_repository` (tri/filtres), `filter_builder`, `dashboard_manager` (KPI), `views_schema`, `expat_schema`, `external_schema`, `case_export` (PDF) tous intouchés (vérifié au diff). Migration additive `c9f1a3e5d7b2` (aucun `ALTER` sur `client_case`). Vague B (front) : picker catégorisé (pays dans « Dossier »), sortir le pays du fixe vers le reveal-parcours, owner/tags restent fixes.

> **Champs par parcours (VAGUE 2/3, création transactionnelle)** : `POST /cases` enrichi (option A) — le parcours ET les valeurs du principal arrivent dans le MÊME appel, en **une transaction atomique**. `CaseCreateRequest` accepte EN PLUS, tous **optionnels** : `journey_template_id` (assigne le parcours via le **cœur commit-less `ProgressManager.apply_journey`** extrait de `assign_journey` — même pattern que `backfill_step`, pour que dossier + parcours vivent dans la même transaction) + les **valeurs du principal** (champs civils hérités de `_CivilStatusFields` + `custom_fields` JSONB). Validation **réutilisée** (zéro réécriture) : `_apply_civil_fields` pour les base, `validate_and_merge` pour les custom — identique au PATCH person. **Enforcement `required_at_creation`** (différé de la vague 1) : SI un parcours est fourni ET qu'il a des champs `required_at_creation`, ces valeurs doivent être non-vides (notion « provided » réutilisée via `requirements_eval.field_provided`) → sinon 422 ; un champ custom **archivé** ne bloque jamais (il a quitté le picker) ; **sans parcours, aucun enforcement** (rétrocompat). **ATOMICITÉ** : création dossier + assignation + valeurs = une transaction ; toute erreur (valeur invalide, required manquant, template d'une autre agence → 404) → **rien créé**, pas de dossier orphelin (en prod, `get_db` rollback via son `async with` sur l'exception ; le test l'asserte en rollback+count sur la session partagée). **RÉTROCOMPAT STRICTE** (point non négociable) : `POST /cases` SANS les nouveaux champs = comportement **identique** à avant (dossier nu, principal nom/email/langue seuls) — tous les nouveaux champs sont défaultés. Pas de migration (schéma de requête + logique). Vague 3 : picker front + `CaseCreateModal` dynamique.

> **Champs par parcours (VAGUE 1/3, modèle + CRUD)** : un parcours déclare la liste explicite des champs collectés à la **création d'un dossier** — distinct des réquisits (qui demandent en cours de route). Table `journey_template_field` (`template_id` FK CASCADE, `kind` base_field|custom_field, `reference`, `position`, `required_at_creation` bool posé dès la vague 1, `UNIQUE(template_id, kind, reference)`) — jumeau du `step_requirement`, un cran plus haut (rattaché au TEMPLATE, pas au step). CRUD sous `/journeys/{tid}/fields` (gate `journey.configure`) : `GET` liste + `POST` + `DELETE {field_id}` + `PUT /order` (réordonnancement two-phase dense 0..n-1, **même convention** que steps/order et requirements/order — ensemble exact sinon 422). Validation **calquée sur les réquisits** : base_field → whitelist `COLLECTABLE_BASE_FIELDS` ; custom_field → définition **active** de l'agence (sinon 422) ; `kind=document` rejeté (un document est un réquisit, pas un champ de création) ; doublon → **409** (pré-check + contrainte au sol). Exposition **DOUBLE** : `GET /journeys/{tid}/fields` dédié ET embarqué dans `JourneyTemplateDetailResponse.fields`. La résolution du rendu (`label`/`field_type`/`options` pour un custom, `is_archived` si la définition a été archivée APRÈS coup — la ligne reste, flaggée) est **batchée** (un seul fetch des définitions, pas de N+1). **AUCUNE écriture de valeur** dans cette vague : configuration de template uniquement, zéro impact `case_person`. Migration additive `b8e0d2f4a6c9`. (Vague 2 : `POST /cases` enrichi, valeurs civiles/custom **optionnelles**, rétrocompat stricte. Vague 3 : picker front + modal de création dynamique.)

> **Personnes & état civil (case_person) + adresses** : `case_person` unifie le principal (`kind=principal`, lié à l'`expat_user` partagé pour l'identité/login) et la famille (`kind=family`) — porteur de l'ÉTAT CIVIL scopé dossier (passeport, date/lieu de naissance, nationalité, sexe, statut marital, téléphone), JAMAIS sur `expat_user` (isolation RGPD : deux agences sur le même expat ne partagent pas l'état civil). Invariant : 1 principal/dossier (index unique partiel), non supprimable. CRUD : `POST/PATCH/DELETE /cases/{id}/persons[/{person_id}]` (gate `case.edit`). Adresses origine/destination à plat sur `PATCH /cases/{id}` (`origin_country`/`dest_country` inchangés = le `country` de chaque adresse, donc filtres/tri/vues pays intacts ; + street/city/postal_code). Le détail expose `persons: [...]` (principal inclus) + `principal_person_id`. Migration additive avec reprise de données (1 principal par dossier + family_member → case_person).

> **Actions de masse & soft delete** : `POST /cases/bulk-action` (gate `case.edit` — `set_status`/`set_owner`/`add_tags`/`remove_tags` via le discriminant `action`) et `POST /cases/bulk-delete` (gate **`case.delete`**, nouvelle permission : admin + case_manager, pas member/viewer). Cap 500 ids ; ids cross-agence ignorés silencieusement ; réponse `{examined, affected, affected_ids}` ; un ActivityLog par dossier. **Soft delete** : `client_case.deleted_at` (migration additive) ; un dossier supprimé disparaît de TOUTES les lectures — listing/vues/détail (404), espace expat, file d'approbation des rappels, dashboard, ET le scheduler (ni envoi ni relance auto). Re-delete = no-op.

> **Vues sauvegardées & filtres (parité Prism)** : `GET /cases` porte les filtres complets Prism — params par champ + arbre `filters` JSON-encodé (conditions/groupes and-or, opérateurs eq…between, dates coercées) + multi-tri `sort_by`/`order` whitelisté (422 strict). Les vues (filtres + colonnes + tri) persistent en table `saved_view` (scope agent, `is_shared` visible agence, vue par défaut, « All » personnalisable via `/views/default-all`) ; catalogue des colonnes sur `GET /cases/columns`.

> **Boot = migrations + seed** : `start.sh` enchaîne `alembic upgrade head` → `seed.py` (mode dérivé d'`ENVIRONMENT` : `production` → `--mode prod`, baseline seule sans comptes démo ; sinon `--mode dev`) → uvicorn. Le seed est idempotent par construction — **un déploiement initial n'a besoin d'aucune étape manuelle**. Échec du seed = boot refusé (exit non-zéro).
Checklist finale (les commandes du quotidien vivent dans le `Makefile` — `make help`) :
- [ ] `make test-cov` — tout passe
- [ ] `make typecheck` — clean
- [ ] `make lint` — clean (ou `make check` pour le gate pre-push complet)
- [ ] API démarre, `/ping` OK, **boot check RBAC passe** (toutes les routes bindées)
- [ ] Seed crée permissions + rôles + bindings + 3 agences + 3 dossiers
- [ ] `GET /cases` (en tant qu'agent Eloïse) retourne le dossier Martin
- [ ] L'étape 3 du dossier Dupont est **BLOCKED** (prérequis)
- [ ] Le rappel J+10 du dossier Martin est en **TO_APPROVE**
- [ ] Tenter de valider une étape verrouillée → **erreur claire**
- [ ] Scheduler démarre (logs), aucun envoi réel

**Attends ma validation.**

---

## ÉTAPE 15 — Documents requis par étape (dégel ciblé, demande Eric) — RETIRÉ

> **RETIRÉ** (dégel ultérieur). Le système legacy `journey_template_step.required_documents` (libellés libres de pièces attendues, informatif) faisait **doublon** avec les réquisits de type `document` (vague 1 : suivis, liés à un fichier, statut pending/provided). Supprimé partout — modèle, schémas (TemplateStep Create/Update/Response), projection agent (`StepProgressResponse`), timeline expat (`ExpatTimelineStepResponse`), seed, tests. Migration `b7f3c1a9d2e4` : `DROP COLUMN` (downgrade = `ADD COLUMN` nullable JSONB, symétrique ; perte de données acceptée, legacy remplacé). Les pièces attendues s'expriment désormais comme des **réquisits document** (CRUD `/journeys/{tid}/steps/{sid}/requirements`).

---

## RAPPELS

- Rythme **AVANT / APRÈS** + « attends validation » à chaque étape.
- **TOUT LE CODE EN ANGLAIS** ; labels FR = frontend.
- Pattern `Model → Schema → Repository → Manager → Router` (Router n'appelle jamais un Repository).
- Modèles `shared/models/`, schémas `src/{domain}/`.
- **RIEN EN DUR** pour le RBAC (catalogue en code synchronisé vers DB ; bindings + affectations en base ; deny par défaut ; boot check).
- Deux identités (`Agent`/`ExpatUser`), deux audiences JWT. L'expatrié ne passe pas par le moteur RBAC.
- Tests : **testcontainers PG**, plugins `make_*(**overrides)`, ≥ 1 test/endpoint, isolation par truncate en teardown, **mocks pour l'externe**.
- `ruff` + `mypy` clean avant chaque validation. OpenAPI committé.
- **Pas de scope creep** (liste « À NE PAS CODER », CLAUDE.md PARTIE 1). En cas de doute, demander.
- **Porter, pas réécrire** : `_prism-reference/`.
