"""Canonical consent texts (point 16, definitive versions 2026-07-02).

Verbatim transcription of the passation document (Eric / Claude
acquisition), extracted programmatically: never rewrite these by hand,
never summarize. The CODE is the source of truth (same rule as the
permission catalogue): the seed publishes a NEW VERSION whenever the
latest active version's hash differs from these texts, which re-gates
every concerned actor. {agency_name} is resolved at read time.

Lawyer review recommended before the first paying agency (CGV art. 9
and 12 first)."""

_AGENCY_TERMS = """# Conditions générales de vente Nidria

### 1. Objet
Les présentes conditions régissent l'accès et l'utilisation du service Nidria (app.nidria.com),
un service en ligne de suivi de dossiers clients et d'espace client, édité par BETTERSOFT LLC,
société de droit américain constituée dans l'État du Wyoming (numéro 2024-001529388), 1317
Edgewater Drive, #7287, Orlando, FL 32804, États-Unis (ci-après "Nidria"), par la structure
professionnelle souscriptrice (ci-après "l'Agence"). Elles forment, avec l'accord de traitement
des données (DPA) et les conditions particulières convenues à la souscription, le contrat entre
Nidria et l'Agence.

### 2. Description du service
Nidria fournit un espace agence (gestion et suivi de dossiers, parcours personnalisables,
gestion documentaire, messagerie par étape, rappels, rôles et permissions, rattachement de
prestataires externes) et un espace client permettant aux clients de l'Agence de suivre leur
dossier et de déposer leurs documents. Le périmètre fonctionnel est celui effectivement
disponible dans le service ; les évolutions futures ne sont pas contractuelles tant qu'elles ne
sont pas livrées.

### 3. Comptes et accès
Les comptes de l'Agence sont créés lors de l'activation avec l'accompagnement de Nidria. Les
identifiants sont personnels ; l'Agence gère les rôles et habilitations de ses utilisateurs et
répond de l'usage fait de ses comptes. L'accès des clients finaux à leur espace est inclus et
gratuit pour eux.

### 4. Abonnement, prix et facturation
Le service est fourni par abonnement mensuel, facturé en euros, selon le plan souscrit :
un socle pour le premier utilisateur, puis un tarif par utilisateur supplémentaire, dans la
limite du plan. Chaque plan inclut un nombre de prestataires externes rattachables ; au-delà,
des extensions sont disponibles au tarif en vigueur communiqué avant activation. Les prix
s'entendent hors taxes ; les taxes applicables sont collectées lors du paiement. Le paiement
est opéré par notre partenaire Lemon Squeezy, qui agit en qualité de revendeur officiel
(merchant of record) et émet les factures. Les tarifs en vigueur sont présentés à la
souscription ; des offres promotionnelles peuvent s'appliquer et sont alors précisées par écrit.

### 5. Durée, résiliation
L'abonnement est sans engagement de durée : il se renouvelle mensuellement et chaque partie
peut y mettre fin à tout moment, avec effet à la fin de la période mensuelle en cours. Nidria
peut suspendre l'accès en cas d'impayé ou de violation grave des présentes, après notification
restée sans effet sous 15 jours.

### 6. Données personnelles
Les données des dossiers appartiennent à l'Agence et à ses clients. L'Agence est responsable de
traitement ; Nidria agit en qualité de sous-traitant conformément au DPA accepté avec les
présentes. Les données des dossiers sont hébergées dans l'Union européenne (Paris, France).
Nidria n'utilise pas les données de l'Agence pour entraîner des modèles d'intelligence
artificielle et ne les vend ni ne les partage à des fins commerciales.

### 7. Obligations de l'Agence
L'Agence garantit la licéité des données et contenus qu'elle traite dans le service, informe ses
clients conformément à la réglementation applicable, utilise le service conformément à sa
destination professionnelle et s'interdit toute tentative d'atteinte à la sécurité ou de
revente du service sans accord écrit.

### 8. Disponibilité et support
Nidria met en œuvre des efforts raisonnables pour assurer une disponibilité continue du service
et des sauvegardes régulières, sans garantie de disponibilité absolue. Des maintenances peuvent
survenir, planifiées autant que possible en dehors des heures ouvrées. Le support est fourni par
email (support standard) ou en priorité selon le plan souscrit.

### 9. Responsabilité
Nidria est tenue à une obligation de moyens. Sa responsabilité totale, toutes causes confondues,
est plafonnée aux sommes effectivement payées par l'Agence au cours des douze mois précédant le
fait générateur. Nidria ne répond pas des dommages indirects (perte d'exploitation, de
clientèle, d'image). Rien dans les présentes n'exclut la responsabilité qui ne peut l'être en
vertu du droit applicable.

### 10. Propriété intellectuelle
Le service, la marque et les éléments logiciels restent la propriété de BETTERSOFT LLC. L'Agence
bénéficie d'un droit d'utilisation non exclusif et non transférable pendant la durée de
l'abonnement. Les données et documents de l'Agence restent sa propriété.

### 11. Réversibilité
En fin de contrat, l'Agence peut exporter ses données ; les modalités de restitution et de
suppression sont celles de l'article 11 du DPA (suppression sous 60 jours, attestation sur
demande).

### 12. Droit applicable et litiges
Les présentes sont régies par le droit de l'État du Wyoming (États-Unis). Tout litige relatif
au contrat sera soumis aux juridictions compétentes de l'État du Wyoming, après tentative de
résolution amiable pendant 30 jours. Cette clause ne fait pas obstacle aux dispositions
impératives applicables, notamment le RGPD pour les traitements de données personnelles, qui
s'appliquent indépendamment du droit choisi.

### 13. Évolution des conditions
Nidria peut faire évoluer les présentes ; toute nouvelle version est notifiée à l'Agence et
soumise à acceptation dans le service. La version acceptée demeure applicable jusqu'à
acceptation de la suivante.
"""

_AGENCY_DPA = """# Accord de traitement des données (DPA)

Conclu en application de l'article 28 du Règlement (UE) 2016/679 ("RGPD"), entre la structure
professionnelle titulaire du compte, agissant en qualité de responsable de traitement (ci-après
"le Client"), et BETTERSOFT LLC, société de droit américain constituée dans l'État du Wyoming
(numéro d'enregistrement 2024-001529388), 1317 Edgewater Drive, #7287, Orlando, FL 32804,
États-Unis, exploitant le service Nidria, agissant en qualité de sous-traitant (ci-après "le
Prestataire"). Contact : contact@nidria.com. Le présent accord fait partie intégrante du contrat
de service ; son acceptation en ligne, journalisée, vaut signature.

### 1. Objet
Le DPA encadre les traitements de données personnelles que le Prestataire effectue pour le
compte du Client dans le cadre de la fourniture du service Nidria : espace agence, suivi de
dossiers, parcours clients, gestion documentaire, messagerie par étape, rappels et fonctions
associées.

### 2. Description du traitement
Nature et finalité : hébergement, stockage, structuration et mise à disposition des dossiers
gérés par le Client dans l'application, afin de fournir le service souscrit. Aucune autre
exploitation. Durée : celle du contrat de service. Personnes concernées : les clients du Client
et, le cas échéant, leurs proches (personnes accompagnées dans des démarches de création de
société, résidence, visa, relocation, implantation comptable, domiciliation ou démarches
comparables), ainsi que les contacts et prestataires rattachés aux dossiers. Catégories de
données : identité et coordonnées ; documents versés aux dossiers (pièces d'identité et
passeports, justificatifs, documents administratifs, comptables ou financiers) ; contenu des
échanges dans la messagerie ; métadonnées d'utilisation.

### 3. Instructions du Client
Le Prestataire traite les données personnelles uniquement sur instruction documentée du Client.
Le paramétrage et l'utilisation de l'application par le Client valent instructions. Si le
Prestataire est tenu de traiter en vertu du droit de l'Union ou d'un État membre, il en informe
le Client avant le traitement, sauf interdiction légale. Le Prestataire informe le Client si,
selon lui, une instruction viole le RGPD.

### 4. Obligations du Client
Le Client, en sa qualité de responsable de traitement : (a) garantit que les données confiées au
Prestataire sont collectées et traitées licitement et qu'il dispose des bases légales requises ;
(b) informe les personnes concernées conformément aux articles 13 et 14 du RGPD ; (c) ne verse
de catégories particulières de données (article 9 RGPD) que si cela est strictement nécessaire à
ses dossiers et licite ; (d) gère les comptes, rôles et habilitations de ses utilisateurs et la
confidentialité de leurs identifiants ; (e) adresse au Prestataire des instructions conformes au
RGPD.

### 5. Confidentialité
Le Prestataire veille à ce que les personnes autorisées à traiter les données s'engagent à en
respecter la confidentialité ou soient soumises à une obligation légale de confidentialité, et
n'y accèdent que dans la mesure nécessaire au service.

### 6. Sécurité
Le Prestataire met en œuvre les mesures techniques et organisationnelles décrites en Annexe 2,
conformément à l'article 32 du RGPD. En particulier : chiffrement en transit et au repos,
contrôle d'accès par rôles et permissions, authentification séparée entre espace agence et
espace client, hébergement des données dans l'Union européenne (Paris, France), sauvegardes et
journalisation. Le Prestataire n'utilise pas les données du Client pour entraîner des modèles
d'intelligence artificielle et ne les vend ni ne les partage à des fins commerciales.

### 7. Sous-traitants ultérieurs
Le Client autorise de manière générale le recours aux sous-traitants ultérieurs listés en
Annexe 3. Le Prestataire informe le Client par email, au moins 30 jours avant tout ajout ou
remplacement ; le Client peut émettre une objection raisonnable et, à défaut d'accord, résilier
le contrat de service. Le Prestataire impose à ses sous-traitants ultérieurs des obligations de
protection des données équivalentes à celles du présent DPA et demeure pleinement responsable
envers le Client de leurs prestations.

### 8. Assistance au Client
Compte tenu de la nature du traitement, le Prestataire aide le Client, par des mesures
techniques et organisationnelles appropriées, à répondre aux demandes d'exercice des droits des
personnes concernées (accès, rectification, effacement, limitation, portabilité, opposition),
notamment via les fonctions d'export, de rectification et de suppression de l'application. Il
assiste également le Client pour les analyses d'impact (AIPD) et les consultations préalables de
l'autorité de contrôle, dans la mesure du raisonnable. Si une personne concernée adresse une
demande directement au Prestataire, celui-ci la transmet au Client sans délai injustifié et n'y
répond pas lui-même, sauf instruction contraire du Client ou obligation légale. Toute assistance
excédant un effort raisonnable ou les fonctions standard de l'application peut donner lieu à une
facturation à un tarif raisonnable, communiqué au préalable.

### 9. Violation de données personnelles
Le Prestataire notifie au Client toute violation de données personnelles sans délai injustifié
et au plus tard 72 heures après en avoir pris connaissance, par email à l'administrateur du
compte du Client, en fournissant les informations utiles à la notification prévue aux articles
33 et 34 du RGPD : nature de la violation, catégories et volume approximatif de données et de
personnes concernées, conséquences probables, mesures prises ou proposées. Le Prestataire
coopère avec le Client et prend les mesures raisonnables pour limiter et remédier à la
violation.

### 10. Localisation et transferts
Les données des dossiers sont hébergées dans l'Union européenne (Paris, France). Certains
sous-traitants ultérieurs sont des sociétés américaines ; lorsqu'un transfert hors UE a lieu, il
est encadré par des garanties appropriées au sens du chapitre V du RGPD (clauses contractuelles
types et/ou certification au EU-U.S. Data Privacy Framework).

### 11. Sort des données en fin de contrat
Au terme du contrat de service, au choix du Client exprimé dans les 30 jours, le Prestataire
restitue les données (export) et/ou les supprime, ainsi que les copies existantes, dans un délai
de 60 jours, sauf obligation légale de conservation. À défaut de choix exprimé dans ce délai de
30 jours, le Prestataire procède à la suppression. Attestation de suppression sur demande.

### 12. Audit et documentation
Le Prestataire met à la disposition du Client la documentation nécessaire pour démontrer le
respect du présent DPA, y compris les certifications de ses hébergeurs (SOC 2 Type 2,
datacenters ISO 27001). Le Client peut réaliser ou faire réaliser, par un auditeur soumis à
confidentialité, un audit au maximum une fois par an, à ses frais, avec un préavis de 30 jours,
pendant les heures ouvrées, sans accès aux données d'autres clients du Prestataire ni
perturbation du service ; les coûts du Prestataire au-delà d'un effort raisonnable peuvent être
facturés à un tarif raisonnable, communiqué au préalable.

### 13. Dispositions générales
Le présent DPA prend effet à son acceptation en ligne et demeure en vigueur tant que le
Prestataire traite ou conserve des données personnelles pour le compte du Client, y compris
après la fin du contrat de service et jusqu'à suppression ou restitution effective. En cas de
contradiction entre le présent DPA et le contrat de service sur la protection des données, le
DPA prévaut. Le droit applicable et la juridiction compétente sont ceux du contrat de service,
sans préjudice de l'application du RGPD. La nullité d'une clause n'affecte pas les autres.

### Annexe 1 : description du traitement
Objet : fourniture du service Nidria (suivi de dossiers et espace client). Nature des
opérations : collecte via l'application, hébergement, stockage, structuration, consultation,
mise à disposition, effacement. Finalité : gestion des dossiers d'accompagnement du Client.
Personnes concernées : clients du Client et leurs proches, contacts et prestataires rattachés
aux dossiers. Catégories de données : identité, coordonnées, documents de dossier (pièces
d'identité, justificatifs, documents administratifs et financiers), échanges, métadonnées.
Durée : durée du contrat de service, puis Article 11.

### Annexe 2 : mesures de sécurité
Chiffrement des données en transit (TLS) et au repos. Hébergement dans l'Union européenne
(Paris) chez des prestataires audités SOC 2 Type 2, datacenters ISO 27001. Contrôle d'accès par
rôles et permissions ; principe du moindre privilège ; authentification séparée espace agence /
espace client. Cloisonnement logique des données entre agences clientes. Sauvegardes régulières
et journalisation des accès. Personnel et prestataires soumis à confidentialité. Pas
d'utilisation des données pour l'entraînement de modèles d'IA, pas de revente ni de partage
commercial.

### Annexe 3 : sous-traitants ultérieurs
Fly.io, Inc. (hébergement de l'application ; Paris, France ; SOC 2 Type 2, datacenters ISO
27001, CCT/DPF). Supabase, Inc. (base de données ; Paris, France ; SOC 2 Type 2, CCT/DPF).
Cloudflare, Inc. (diffusion et protection du site et de l'app, CDN, DNS ; réseau mondial, données
servies depuis l'UE ; ISO 27001, SOC 2, CCT/DPF). Resend, Inc. (envoi d'emails transactionnels ;
États-Unis ; CCT/DPF). Lemon Squeezy, LLC (facturation du Client, aucun accès aux données des
dossiers ; États-Unis ; CCT/DPF).
"""

_CLIENT_TERMS = """# Conditions d'utilisation de l'espace client

1. Cet espace vous est fourni par {agency_name}, avec l'outil Nidria, pour suivre l'avancement de
   votre dossier, échanger avec {agency_name} et transmettre les informations et documents demandés.
   Il est gratuit pour vous.
2. Vos identifiants sont personnels ; vous êtes responsable de leur confidentialité et de
   l'usage de votre compte.
3. Les informations et documents que vous déposez doivent être exacts, à jour et concerner
   votre dossier. Vous vous interdisez tout contenu illicite.
4. {agency_name} reste votre interlocuteur pour toute question relative à votre dossier, aux délais
   et aux décisions qui le concernent. Nidria fournit l'outil et n'intervient pas dans votre
   dossier.
5. Votre accès peut être fermé par {agency_name} à la clôture de votre dossier ou à la fin de sa
   relation avec vous. Vous pouvez demander à {agency_name} une copie des documents que vous avez
   déposés.
"""

_CLIENT_PRIVACY = """# Note d'information sur vos données

1. Responsable de traitement : {agency_name} est responsable du traitement de vos données
   personnelles dans le cadre de votre dossier.
2. Sous-traitant : Nidria (BETTERSOFT LLC) héberge et traite ces données pour le compte de
   {agency_name}, en qualité de sous-traitant au sens du RGPD, dans le cadre d'un accord de
   traitement des données.
3. Données traitées : votre identité et vos coordonnées, les documents que vous déposez et vos
   échanges avec {agency_name} dans cet espace.
4. Hébergement : vos données sont hébergées dans l'Union européenne (Paris, France). Elles ne
   sont ni vendues, ni utilisées pour entraîner des modèles d'intelligence artificielle.
5. Durée : vos données sont conservées pendant la durée de votre dossier et de la relation avec
   {agency_name}, puis selon les obligations légales qui s'imposent à {agency_name}.
6. Vos droits : vous disposez des droits d'accès, de rectification, d'effacement, de limitation,
   d'opposition et de portabilité. Pour les exercer, adressez-vous à {agency_name}, votre
   interlocuteur unique ; Nidria l'assiste techniquement dans le traitement de vos demandes.
   Vous pouvez également saisir l'autorité de contrôle compétente (en France, la CNIL).
"""

CANONICAL_DOCUMENTS: dict[str, str] = {
    "agency_terms": _AGENCY_TERMS,
    "agency_dpa": _AGENCY_DPA,
    "client_terms": _CLIENT_TERMS,
    "client_privacy": _CLIENT_PRIVACY,
}
