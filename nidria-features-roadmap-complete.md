# 🗺️ Nidria - Spécifications complètes des features

**Version du document** : v4.0 (spec exhaustive par pôles fonctionnels, 12/06/2026)
**Sources** : Sidney call 1 (20/05) + Artur call 1 (21/05) + Didier+Rachel (3/06) + Eloïse Reside Paraguay (3/06) + Sidney call 2 (10/06) + Greg call 1 (12/06) + Alexis + Eloïse + Inès Reside Paraguay 2x1h (12/06) + brief dev complet (4/06)

> **Note d'organisation** : ce document décrit l'ensemble des features de Nidria, regroupées par pôle fonctionnel et non par version ou palier de livraison. Toutes les features décrites ici font partie du produit. Aucune hiérarchie temporelle n'est impliquée par l'ordre de présentation.

---

## 📋 Table des matières

1. [Philosophie produit](#philosophie-produit)
2. [Glossaire](#glossaire)
3. [🎯 Suivi et expérience du client expatrié](#suivi-et-experience-du-client-expatrie)
4. [🏢 Espace agence et gestion des dossiers](#espace-agence-et-gestion-des-dossiers)
5. [🔧 Configuration et workflows](#configuration-et-workflows)
6. [📨 Communication et relances](#communication-et-relances)
7. [📄 Documents et automatisation administrative](#documents-et-automatisation-administrative)
8. [🌍 International et intégrations](#international-et-integrations)
9. [💰 Commercial, facturation et monétisation](#commercial-facturation-et-monetisation)
10. [📊 Pilotage, IA et migration](#pilotage-ia-et-migration)
11. [Hors-scope](#hors-scope)
12. [Validation marché](#validation-marche)
13. [Pricing](#pricing)

<a name="philosophie-produit"></a>
## 🛡️ Philosophie produit

Quatre principes non négociables qui structurent toutes les décisions produit :

### 1. L'outil supporte l'agent, ne le remplace pas

* Validation manuelle obligatoire sur tous les envois automatiques (mails, WhatsApp, SMS)
* Pas d'IA générative qui répond à la place du conseiller
* L'humain garde toujours la main sur ce qui sort de l'outil vers le client
* Verbatim Sidney : "Pour moi c'est l'humain avant l'humain"
* Verbatim Eloïse : "Pas de relance robotique, garder le côté humain"
* Verbatim Greg : "Je ne veux pas que ça soit non plus trop tourné un peu IA. Je veux qu'il y ait un lien humain"

### 2. Aucun workflow imposé

* Chaque agence configure son propre process via l'éditeur clic-glisser
* Pas de templates pré-définis obligatoires (squelette vide à l'arrivée)
* Adaptation possible par typologie de cabinet, pays, type de client, package
* Verbatim Sidney : "Pour moi, des étapes qui sont importantes ne sont pas nécessairement pour une autre agence. Chacun a son process, chacun a sa recette à lui"
* Verbatim Didier : "Tu vas avoir un cadre de structuration de l'action, mais si c'est pas agile à l'intérieur du cadre, le cadre va vite devenir trop contraignant"

### 3. RGPD strict et sécurité maximale

* Hébergement Supabase (Postgres européen)
* Certification SOC 2 visée
* Authentification séparée agence vs client expat (deux bases isolées)
* Zéro traitement IA sur les pièces sensibles (passeports, casiers judiciaires, etc.)
* Tenant isolé par agence
* Application de l'extraterritorialité RGPD pour les clients européens, même quand l'agence est hors UE
* Verbatim Eric (à Alexis) : "À partir du moment où vous traitez des clés d'identité qui sont d'origine française, vous êtes également soumis au RGPD. C'est ce qu'on appelle une influence étendue"

### 4. Authenticité avant marketing

* Pas de promesses survalorisées dans les communications
* Verbatim Eloïse : "Notre idée c'est de rester au plus près du terrain, quitte à perdre le client, pour pas lui mentir"
* Outil au service de la qualité d'accompagnement, pas du volume à tout prix
* Possibilité d'admettre les limites (pays blacklistés visa, langues sans traducteurs, etc.)

---

<a name="glossaire"></a>
## 📖 Glossaire

**Agence** : la structure cliente de Nidria (cabinet d'expatriation, fiduciaire, cabinet d'avocats, etc.)

**Agent agence** : un utilisateur appartenant à une agence (CEO, employé, comptable, etc.)

**Espace agence** : l'interface utilisée par les agents d'une même agence

**Espace expat** : l'interface utilisée par le client expatrié

**Espace prestataire** : l'interface utilisée par un partenaire externe (avocat, notaire, banque, comptable)

**Dossier** : l'unité de travail dans Nidria, généralement une démarche d'expatriation/installation pour un client

**Parcours-type** : un modèle de workflow réutilisable créé par l'agence (par exemple "Setup Autónomo Espagne" ou "Résidence temporaire Paraguay fast")

**Étape** : un sous-élément d'un parcours-type (par exemple "Récupération apostille casier judiciaire")

**Requirement** : un document ou une condition à fournir/remplir pour valider une étape

**Heartbeat / rappel** : une relance programmée vers le client, validée manuellement par l'agent avant envoi

**Tenant** : l'instance isolée d'une agence dans la base de données (sécurité multi-locataires)

**RTL / LTR** : Right-To-Left / Left-To-Right, sens de lecture (pour le multilangue arabe, hébreu, etc.)

---

<a name="suivi-et-experience-du-client-expatrie"></a>
## 🎯 Suivi et expérience du client expatrié

### Timeline client expat

#### Description détaillée

Côté espace expatrié, le client connecté visualise son parcours administratif complet sous forme de timeline verticale ou horizontale. Chaque étape du dossier est représentée par un bloc visuel comportant :

* Le statut (à faire / en cours / bloqué / validé / refusé)
* L'estimation de durée pour cette étape (en jours ouvrés)
* L'interlocuteur responsable (Eloïse, Sélim, l'agence en général, ou un prestataire externe)
* Les documents requis pour cette étape avec leur statut (manquant / fourni / validé)
* Les éventuels blocages avec leur raison explicite ("En attente de complétion de l'étape précédente")
* La date de complétion pour les étapes finies
* Un compteur de jours restants visuel (vert/orange/rouge) pour les étapes avec deadline

L'expatrié peut interagir avec sa timeline pour :

* Cliquer sur une étape et voir le détail (description, documents nécessaires, contact responsable)
* Uploader directement les documents demandés (drag and drop ou clic)
* Lire les notes/infobulles explicatives associées à chaque étape
* Voir l'historique complet des actions sur son dossier

#### Use cases concrets

* **Cas Reside Paraguay** : un retraité français a déposé son dossier à l'immigration paraguayenne. La timeline lui indique "Dépôt immigration validé le 15/06" → "Récupération résidence temporaire (en cours, estimation 30 à 45 jours)" → "Carte d'identité (à venir, estimation 3 à 9 mois)". Le client voit son avancement sans appeler Eloïse.

* **Cas Expatriation.io (Sidney)** : un entrepreneur français qui s'installe à Prague voit sa timeline avec "Diagnostic fiscal (validé)" → "Création société tchèque (en cours, estimation 2 semaines)" → "Ouverture compte bancaire (à venir, estimation 1 semaine)" → "Inscription Trade Licence Office (à venir, estimation 5 jours)". Chaque étape a son responsable et ses requirements.

* **Cas Domiciliation Bulgarie (Artur)** : un digital nomade voit "Récupération NIE (en cours, estimation 3 jours)" → "Ouverture compte UniCredit (à venir, estimation 5 jours)" → "Inscription registre commercial (à venir, estimation 10 jours)". L'autonomie totale qu'Artur a qualifiée de "x100 game changer".

#### Verbatims qui justifient cette feature

> "La clé, je pense, c'est l'autonomie du client. Aujourd'hui, le client est complètement dépendant de son interlocuteur comptable ou juridique pour ses démarches. Et nous, ce qu'on aimerait, c'est que le client soit autonome dans ses démarches. C'est Game Changer. C'est x100. Parce que ça n'existe pas." — **Artur Pouget, Domiciliation Bulgarie (21/05/2026)**

> "Tu connais Interactive Broker tu dois sûrement avoir un compte... t'as justement dans ton espace compte tu as normalement chaque année si tu veux une timeline... Je pensais avoir un système pareil." — **Artur Pouget**

> "Estimation de durée, c'est super important. Plus on est transparent avec le client, mieux c'est." — **Artur Pouget**

> "The most useful tool until now is just a simple date counter to something to calculate the deadline, because it's always time that is the most important for the applicants." — **Inès Heu, Reside Paraguay (12/06/2026)**

#### Sous-features incluses

* **Estimation de durée par étape** : chaque étape porte une durée estimée en jours ouvrés, modifiable côté agence. Côté expat, affichage "estimé 15 jours" ou "estimé 2 à 3 mois" selon précision.

* **Statuts agnostiques et anti-jargon** : pas de "Pending KYC validation" ou de termes techniques. Statuts simples : "à faire", "en cours", "validé", "bloqué", "refusé". Verbatim Artur : "Ça doit être extrêmement généralisé, agnostique de tout vocabulaire".

* **Compteur de jours visuel multi-couleurs** : pour chaque étape avec deadline, un compteur visuel passe du vert (large marge) à l'orange (proche) au rouge (dépassé). Inès le décrit comme "the most useful tool" de leur CRM AppSheet actuel.

* **Multi-deadlines partagées** : une étape peut avoir plusieurs deadlines (agence à 45 jours, expatrié à fournir un papier dans 15 jours, prestataire externe à 30 jours). Chacun voit la deadline qui le concerne.

* **Interlocuteur responsable visible** : chaque étape indique qui s'en occupe ("Alexis", "Eloïse", "Notaire partenaire", "Vous"). Évite les "vous en êtes où" du client.

* **Documents requis associés à l'étape** : chaque étape peut demander des documents (passeport, casier judiciaire, etc.) avec statut individuel par document. Le client voit ce qu'il manque et upload.

* **Statut de blocage explicite** : si une étape est bloquée par un prérequis, le message est clair : "Cette étape est bloquée. Pour la débloquer, complétez d'abord : Étape 2 (Récupération apostille)".

* **Mode lecture seule / interactif** : selon les permissions, certains champs sont juste consultables, d'autres modifiables (par exemple uploader un doc).

#### Bénéfice métier

* L'expatrié arrête d'appeler l'agence en permanence pour savoir "où ça en est"
* Le client comprend sa propre démarche, ce qui réduit le stress et améliore la satisfaction
* L'agence économise plusieurs heures par semaine de support téléphonique/WhatsApp
* La transparence sur les délais évite les attentes irréalistes
* Le client a un visuel rassurant en cas de longue procédure (Carte d'identité Paraguay = 3 à 9 mois)

#### Détails UX

* Timeline verticale par défaut (mobile-friendly)
* Sur desktop, option horizontale (Gantt simplifié)
* Couleurs sobres avec accent sur les statuts (rouge/orange/vert)
* Iconographie claire (check pour validé, sablier pour en cours, cadenas pour bloqué)
* Animations légères sur les changements de statut
* Pas de surcharge d'informations : un clic sur une étape pour voir le détail

#### Intégrations et dépendances

* Aucune dépendance externe pour le socle de base (pas d'API tierce nécessaire)
* Backend Postgres (Supabase) pour la persistance
* Frontend React pour la timeline interactive
* Authentification séparée agence vs expat

---

### Étapes verrouillées par prérequis

#### Description détaillée

Une étape d'un parcours-type peut être configurée pour ne devenir accessible qu'après la validation d'une ou plusieurs étapes prérequises. C'est ce qui permet de structurer le tunnel administratif sans saturer le client avec 30 demandes simultanées.

Le système fonctionne ainsi :

1. Dans la configuration du parcours-type (côté agence), chaque étape peut déclarer "Cette étape nécessite la validation préalable de : Étape A, Étape B".

2. Côté espace expat, les étapes verrouillées apparaissent grisées avec un cadenas et un message explicite "En attente de la complétion de l'étape précédente : [nom de l'étape]".

3. Quand l'étape prérequise est validée par l'agence, l'étape suivante se débloque automatiquement et le client est notifié.

4. L'agence peut, pour un dossier individuel, débloquer manuellement une étape en cas de cas spécial (override admin).

#### Use cases concrets

* **Cas Reside Paraguay** : la cédula (carte d'identité) ne peut pas être demandée tant que la résidence temporaire n'est pas obtenue. L'expat ne voit pas l'étape "Demande cédula" tant que l'étape "Résidence temporaire validée" n'est pas verte.

* **Cas Didier (Residir Portugal) — verbatim importation véhicule** : "Par exemple, je pensais à l'importation d'un véhicule. Ça, c'est très compliqué au Portugal. Il y a toute une procédure avec des passages obligés. Et tant que tu n'as pas le statut vert à ce passage, tu ne peux pas passer au passage d'après."

* **Cas Sidney (Expatriation.io)** : "Tant qu'imaginons que la personne, si tu veux, n'a pas en fait balancé son passeport ou différents prérequis en termes de documents, il n'y a absolument aucun intérêt à venir lui demander d'autres documents."

* **Cas Mathias (Autónomo Espagne)** : la déclaration Modelo 030 (premier dossier fiscal) ne peut pas être faite tant que le NIE n'a pas été obtenu. Étapes verrouillées garantissent la séquence logique.

#### Verbatims qui justifient cette feature

> "Par exemple, je pensais à l'importation d'un véhicule. Ça, c'est très compliqué au Portugal. Il y a toute une procédure avec des passages obligés. Et tant que tu n'as pas le statut vert à ce passage, tu ne peux pas passer au passage d'après." — **Didier, Residir Portugal (3/06/2026)**

> "Tant qu'imaginons que la personne n'a pas balancé son passeport ou différents prérequis en termes de documents, il n'y a absolument aucun intérêt à venir lui demander d'autres documents." — **Sidney Lakehal (10/06/2026)**

#### Sous-features incluses

* **Configuration des prérequis dans le parcours-type** : drag-and-drop visuel pour lier les étapes ("Étape B nécessite Étape A").

* **Affichage explicite côté espace expat** : pas juste un cadenas, un message clair "Étape débloquée quand : [liste des étapes prérequises]".

* **Notification automatique au client** quand une étape se débloque ("Bonne nouvelle, votre étape Cédula vient de se débloquer, vous pouvez désormais procéder").

* **Override admin** : un admin peut, pour un dossier spécifique, débloquer manuellement une étape (cas exceptionnel, par exemple urgence client).

* **Prérequis multiples (AND/OR)** : "Étape C nécessite Étape A ET Étape B" ou "Étape C nécessite Étape A OU Étape B".

* **Prérequis conditionnels** : "Étape C nécessite Étape A SI nationalité = X, sinon directement accessible".

* **Visibilité côté agence** : dans la vue dossier, l'agence voit l'arbre des dépendances et identifie d'un coup d'œil les blocages.

#### Bénéfice métier

* Structure le tunnel administratif sans surcharger le client (il voit seulement ce qui est actionnable maintenant)
* Évite les erreurs de séquence (le client ne tente plus de faire l'étape 4 avant l'étape 2)
* Réduit les questions du type "pourquoi je ne peux pas faire X avant Y ?"
* Permet de modéliser fidèlement les contraintes administratives réelles

#### Détails UX

* Cadenas visuel sur les étapes verrouillées
* Tooltip explicatif au survol
* Animation de "déblocage" quand une étape devient disponible
* Code couleur : verrouillé (gris), disponible (bleu), en cours (orange), validé (vert)

---

### QR code statique côté espace expatrié

#### Description détaillée

L'agence peut intégrer des éléments QR code dans le parcours-type du client. Quand le client clique sur l'étape contenant le QR code, il est redirigé vers le service externe correspondant (suivi gouvernemental, prise de RDV externe, etc.).

#### Use cases concrets

* **Reside Paraguay** : pour le suivi de la carte d'identité paraguayenne, un nouveau système de QR code a été annoncé par l'administration. L'agence intègre ce QR code dans la timeline du client, qui peut lui-même suivre son dossier sur le site gouvernemental sans appeler.

#### Verbatims qui justifient cette feature

> "Vous pouvez carrément créer un élément QR code qui devient statique côté espace expatrié. Et après, il se rend directement sur le site. Il pense que le QR code, il sort de chez vous. Mais en fait, c'est juste un truc que vous avez balancé côté agence." — **Eric à Alexis (12/06/2026)**

#### Sous-features incluses

* **Génération de QR codes pour des URLs spécifiques**
* **Intégration dans les étapes du parcours**
* **Statistiques d'utilisation** (combien de fois cliqué)

#### Bénéfice métier

* Le client peut se renseigner lui-même sans solliciter l'agence
* Permet d'intégrer des services externes (gouvernementaux ou partenaires)
* Modernise l'image de l'agence (l'agence "donne accès" à des services)

---

### Pédagogie / devoir d'information avec infobulles contextuelles

#### Description détaillée

Côté espace expat, chaque étape, document ou statut est accompagné d'infobulles explicatives, de guides, de FAQ contextuelles. Le client n'est pas seulement informé de ce qu'il doit faire, il comprend POURQUOI.

#### Verbatims qui justifient cette feature

> "Il faut avoir un bon devoir d'information auprès du client. Sauf que si les cabinets étaient 100% honnêtes là-dessus, ils pourraient perdre du business... Le rôle d'une interface, c'est de donner cette information." — **Artur Pouget (21/05/2026)**

#### Sous-features incluses

* **Infobulles configurables par l'agence** (à partir des blocs documents customisables du socle de base)
* **Articles longs intégrés** (markdown / texte riche)
* **FAQ contextuelles** par étape
* **Liens externes** (sites gouvernementaux, articles de référence)
* **Vidéos courtes** (Loom embeddé) explicatives

#### Bénéfice métier

* Réduction des questions répétitives au support
* Confiance du client renforcée (transparence)
* Différenciation pédagogique forte

---

### Chatbot interrogeant les données déjà transmises

#### Description détaillée

Optionnel. Un chatbot intégré à l'espace expat permet au client de poser des questions sur son propre dossier. Le chatbot répond uniquement à partir des données du dossier (pas de connaissance externe).

Exemples :
* "Où est-ce que je dois aller demain ?" → "Demain à 9h, vous avez RDV au bureau de l'Interpol à Asunción. Adresse : ..."
* "Quel document me reste à fournir ?" → "Il vous reste à fournir : 1) Casier judiciaire apostillé, 2) Photo d'identité"
* "Combien de temps pour ma carte d'identité ?" → "Estimation 3 à 9 mois selon les délais administratifs"

#### Verbatims qui justifient cette feature

> "Après il y a un chatbot qui pourra interroger localement les données qu'il a déjà transmis si tu veux pour être intéressant de le faire." — **Artur Pouget (21/05/2026)**

#### Sous-features incluses

* **Chatbot sur l'espace expat**
* **Restriction aux données du dossier client**
* **Multilangue**
* **Désactivable par agence** (certaines préfèrent garder le contact direct)

#### Bénéfice métier

* Réduction encore plus drastique des questions à l'agence
* Disponibilité 24/7 (le client peut demander à 3h du matin)
* Modernité

---

### Tracker de présence sur territoire (compteur 365 jours)

#### Description détaillée

Pour les démarches qui imposent une présence physique minimum dans le pays (résidence permanente après résidence temporaire), le système suit la présence du client sur le territoire.

L'expat indique ses sorties (date, durée) dans son espace, le compteur s'incrémente. Une alerte est émise si le seuil critique approche (365 jours d'absence cumulée au Paraguay = perte de l'éligibilité à la résidence permanente).

#### Use cases concrets

* **Reside Paraguay** : un retraité français vit au Paraguay depuis 18 mois mais part 4 mois en France, puis 3 mois aux Émirats, puis 2 mois au Brésil. Le système calcule "Absence cumulée : 9 mois sur 24". Alerte à 11 mois "Vous approchez du seuil critique de 365 jours".

#### Verbatims qui justifient cette feature

> "Pour passer du statut de résidence temporaire à résidence permanente, il y a des conditions à respecter. La condition, c'est que tu ne dois pas être parti du Paraguay pendant plus de 365 jours. Donc, dans ton système, tu dois aussi avoir un agenda où le client note dans l'agenda quel jour il est parti et à quelle heure. Il doit avoir un recordatory qui lui dit qu'il doit revenir avant un an." — **Alexis, Reside Paraguay (12/06/2026)**

#### Sous-features incluses

* **Calendrier de présence côté espace expat**
* **Calcul automatique des jours cumulés**
* **Configuration du seuil par pays** (365 j Paraguay, 270 j autres, etc.)
* **Alertes progressives** (J-100, J-50, J-10)
* **Vue agence** pour suivre tous ses clients en risque

#### Bénéfice métier

* Évite la perte d'éligibilité (impact financier énorme pour le client)
* Différenciateur (aucun outil ne fait ça aujourd'hui)
* Sécurité juridique pour le client

---

<a name="espace-agence-et-gestion-des-dossiers"></a>
## 🏢 Espace agence et gestion des dossiers

### Espace agence centralisé

#### Description détaillée

Côté espace agence, les agents d'une même agence accèdent à un tableau de bord unifié où ils gèrent tous leurs dossiers actifs en parallèle. C'est le cœur opérationnel de Nidria pour l'agence.

L'espace agence comprend plusieurs vues :

* **Vue liste** : tous les dossiers actifs avec filtres et tri
* **Vue dossier individuel** : la fiche complète d'un dossier (timeline, documents, notes, agents responsables, historique)
* **Vue parcours-types** : la bibliothèque des templates de workflows de l'agence
* **Vue équipe** : qui fait quoi, qui est responsable de quel dossier
* **Vue notifications** : ce qui requiert attention (rappels à valider, blocages, retours en attente)

Chaque dossier dans l'espace agence porte :

* Le nom du client expatrié et ses informations clés (nationalité, type de projet, pays de destination)
* Le parcours-type appliqué (ou le custom)
* L'avancement global (par exemple "3 étapes terminées sur 8")
* L'agent référent
* Les notes internes par dossier (avec option "confidentielle admin uniquement")
* Le journal d'activité (qui a fait quoi quand)
* Les documents uploadés (par l'expat ou par l'agence)
* Les rappels programmés

#### Use cases concrets

* **Cas Reside Paraguay** : Alexis se connecte le matin et voit en un coup d'œil ses 8 dossiers actifs. Il filtre par "client arrivé cette semaine" et identifie 2 dossiers à traiter en priorité. Il clique sur le premier et voit que la photocopie passeport tampon est manquante.

* **Cas Sidney** : Sidney gère 5 dossiers en parallèle entre Paris et Prague. Il filtre par "pays de destination = République tchèque" pour préparer sa journée à Prague la semaine prochaine.

* **Cas MonEntreprise.es (Mathias)** : Mathias a 50 dossiers actifs (Autónomo + Sociedad confondus). Il filtre par "Sociedad" pour préparer un point comptable avec son associée Angela.

* **Cas Smart Traveller (Em+Perle)** : Em et Perle coordonnent leur réseau d'experts à Maurice. Ils filtrent par "expert affecté = comptable partenaire X" pour voir les dossiers en attente de retour comptable.

#### Verbatims qui justifient cette feature

> "Nous, on a un AppSheet, c'est un CRM qui a été fait, qui a été adapté pour nous... C'est juste un bon moyen de centraliser tout, parce qu'il y a tellement de clients, tellement de documents chacun." — **Inès Heu, Reside Paraguay (12/06/2026)**

> "Avoir tout en un endroit, d'être en mesure d'uploader les images et les fichiers PDF sans avoir à traverser toutes les e-mails." — **Inès Heu**

> "Je voudrais que mon équipe ait une vision partagée sur les dossiers." — **Hypothèse confirmée Greg / Sidney / Reside**

#### Sous-features incluses

* **Liste de dossiers avec filtres multiples** : par statut, pays d'origine, pays destination, agent responsable, langue du client, tags personnalisés, date de création, type de package.

* **Système de tags personnalisés** : chaque agence définit ses propres tags (par exemple "VIP", "Urgent", "Boomer", "Famille", "Solo").

* **Création de dossier + invitation expatrié** : un clic pour créer un dossier vide, remplir les infos minimales du client (nom, prénom, mail, nationalité, type projet), et un autre clic pour envoyer une invitation au client par mail. L'expat reçoit un lien magique pour activer son espace.

* **Vue dossier individuel avec sections claires** : Identité client, Timeline du dossier, Documents, Notes internes, Rappels, Historique, Prestataires associés, Communications.

* **Notes par dossier avec option confidentielle** : l'agent peut taper des notes visibles par toute l'agence ou marquées "admin uniquement". Verbatim Reside Paraguay : "Je mets la nationalité, quel type de prestations, et un peu les particularités de la personne".

* **Journal d'activité automatique (audit log)** : chaque action est tracée avec horodatage et auteur. "Eloïse a uploadé passeport client le 12/06 à 14:23". Critique pour la traçabilité, la conformité, et les conflits internes ("c'est qui qui a validé ça ?").

* **Tableau de bord avec compteurs** : nombre de dossiers actifs, par statut, par pays, en retard, bloqués. Pas un dashboard élaboré au lancement, juste des compteurs utiles.

* **Export d'un dossier en un clic** : génération d'un PDF ou ZIP avec toutes les pièces et la timeline. Pour archivage ou transfert au client à la fin.

* **Vue équipe simple** : qui est l'agent référent de chaque dossier, qui peut prendre le relais.

* **Recherche transverse** : taper un nom client / passeport / numéro de dossier dans une barre de recherche globale.

#### Système de rôles fine-grain (sous-feature critique)

Trois niveaux de permissions au lancement :

* **Admin** : peut tout faire (créer des dossiers, modifier les parcours-types, gérer les agents, voir les notes confidentielles, supprimer)
* **User** : peut voir et modifier les dossiers, créer des dossiers, uploader des documents, valider les rappels. Ne peut pas modifier les parcours-types ni gérer les agents.
* **Lecteur seul** : peut consulter uniquement. Utile pour un CEO qui veut voir l'activité sans intervenir.

Verbatim Alexis (12/06/2026) : "Tu pourras créer un type d'utilisateur, un type lecteur en ligne. En gros, vous aurez une personne dans l'agence qui gère le CRM et un accès utilisateur. Ce n'est pas con."

Verbatim Alexis (autre passage) : "T'as des admins qui ont plus d'accès et t'as d'autres personnes qui travaillent, qui ont de toute façon besoin de moins d'accès. Les boîtes qui commencent à avoir une certaine taille critique, elles travaillent comme ça. Il y a le Alexis de chez eux, il est derrière son ordinateur dans son bureau, ce n'est pas lui qui va faire les papiers avec les clients. C'est un accompagnateur qui va. Évidemment que l'accompagnateur, il n'a pas les mêmes privilèges admin."

#### Bénéfice métier

* Fini le chaos Excel + WhatsApp + Drive + mail + post-it
* Toute l'équipe travaille au même endroit, voit la même chose
* Plus de "qui s'occupe de Madame X ?" puisque c'est tracé
* Onboarding d'un nouvel agent simplifié (il voit tout l'historique)
* Audit possible en cas de problème (qui a validé quoi quand)

#### Détails UX

* Interface dense mais pas surchargée (style Linear / Notion / Pipedrive)
* Raccourcis clavier (cmd+K pour la recherche globale)
* Mobile-friendly (responsive), même si usage principal desktop
* Mode sombre/clair
* Onboarding interactif à la première connexion (tour guidé)

---

### Filtrage des prospects par critères

#### Description détaillée

L'espace agence offre des filtres avancés sur les leads/dossiers : patrimoine, nationalité, projet, profil d'activité, langue, source d'acquisition (formulaire court, formulaire long, importation, manuel), etc.

L'agence peut sauvegarder des filtres comme "Vues" pour y revenir rapidement.

#### Use cases concrets

* **Sidney** : Sidney crée une vue "Leads fortunés Europe" filtrée sur patrimoine > 300K€ + nationalité européenne + langue française. Il traite ces leads en priorité.

* **Reside Paraguay** : Alexis crée une vue "Clients à arriver cette semaine" filtrée par date d'arrivée. Il organise ses runs aéroport.

* **Mathias** : Mathias filtre sur "Type = Sociedad" pour préparer une session compta avec Angela.

#### Verbatims qui justifient cette feature

> "Toi tu pourras dire, tu pourras carrément filtrer dans tes bits, si tu veux. Pour le moment, j'ai uniquement envie de m'occuper des personnes qui ont un projet avec, en l'occurrence, une expatriation avec des personnes fortunées, donc plus de 200 000, 300 000, 1 million." — **Sidney Lakehal (10/06/2026)**

#### Sous-features incluses

* **Filtres standards** : statut, pays, nationalité, langue, date de création, agent responsable
* **Filtres custom** : champs personnalisés créés par l'agence (par exemple "VIP", "Famille", "Boomer")
* **Combinaisons logiques** AND/OR
* **Sauvegarde de filtres comme vues** : "Mes leads chauds", "Dossiers en retard", "Clients à appeler cette semaine"
* **Partage de vues entre agents** : un admin crée une vue, la partage à toute l'équipe

#### Bénéfice métier

* Focus opérationnel quotidien (un agent voit immédiatement ses priorités)
* Coordination d'équipe (vues partagées)
* Reporting interne (filtres = base des dashboards)

---

### Avatars / templates client prédéfinis

#### Description détaillée

À la création d'un dossier, l'agence peut choisir un "avatar" qui pré-remplit automatiquement les champs typiques :

* Avatar "Retraité français" : champ activité = "Aucune", justificatif revenu = "Avis d'imposition + relevé bancaire 3 derniers mois", besoin compta = "Non"
* Avatar "Digital nomade EU" : activité = "Freelance", justificatif = "Bilan comptable + relevé bancaire", besoin compta = "Oui mensuelle"
* Avatar "Famille avec enfants" : champ "Nombre d'enfants" actif, étape "Scolarisation enfants" ajoutée automatiquement
* Etc.

#### Verbatims qui justifient cette feature

> "Comme quand tu viens créer un personnage, t'as des personnages prédéfinis, tu vois, tu pourras avoir un personnage qui est prédéfini, retraité tu vois, ça remplit automatiquement différents champs." — **Artur Pouget (21/05/2026)**

#### Sous-features incluses

* **Bibliothèque d'avatars standards** (10-20 profils types)
* **Création d'avatars custom par agence**
* **Combinaisons d'avatars** : "Retraité + Famille avec enfants" = fusion intelligente
* **Édition après application** : un avatar pré-remplit mais l'agence peut tout modifier

#### Bénéfice métier

* Réduction du temps de création de dossier
* Standardisation des champs (évite les "champ vide oublié")
* Onboarding rapide pour nouveaux agents

---

### Bloc notes par client lié au WhatsApp

#### Description détaillée

Pour chaque contact client, un bloc notes synchronisé avec le profil. L'agent peut taper des notes rapides (nationalité, particularités, préférences, anecdotes) qui sont immédiatement visibles sur n'importe quel canal où l'agent interagit avec le client.

#### Verbatims qui justifient cette feature

> "Quand je prends des notes, c'est après coup de l'appel, j'ai le contact de la personne. Souvent sur WhatsApp, on peut mettre un petit bloc de notes. Et moi, je mets la nationalité, quel type de prestations, et un peu les particularités de la personne." — **Eloïse, Reside Paraguay (03/06/2026)**

#### Sous-features incluses

* **Bloc notes rapide accessible depuis n'importe où dans Nidria** (raccourci `N` par exemple)
* **Synchronisation avec WhatsApp Business** : les notes apparaissent quand l'agent ouvre la conversation
* **Tags rapides** (#VIP, #famille, #urgent)
* **Recherche transverse dans les notes**

#### Bénéfice métier

* L'agent retrouve immédiatement le contexte d'un client appelé il y a 6 mois
* Personnalisation des interactions (mentionner le prénom des enfants, etc.)

---

### Score de priorité automatique des dossiers

#### Description détaillée

Algorithme qui score chaque dossier sur la priorité de traitement, selon plusieurs critères :

* Dossier en retard (delai dépassé)
* Dossier inactif (client n'a rien fait depuis X jours)
* Dossier à forte valeur (panier > X €)
* Dossier critique (client VIP, partenaire stratégique)
* Dossier en danger (étape bloquée critique)

Le score s'affiche dans la liste des dossiers et permet à l'agent de visualiser immédiatement où concentrer son énergie.

#### Sous-features incluses

* **Configuration de l'algorithme par agence** (poids des critères)
* **Affichage du score** (1-100 ou 5 étoiles)
* **Filtre par score**
* **Notifications quand un score critique apparaît**

#### Bénéfice métier

* Pilotage opérationnel data-driven
* Focus sur les dossiers à risque

---

### Différenciation agence vs cabinet dans l'interface

#### Description détaillée

À la création d'un compte, l'utilisateur indique son type d'organisation :

* Agence d'expatriation hub
* Cabinet d'avocats spécialisé international
* Fiduciaire / expertise comptable
* Cabinet conseil en patrimoine
* Fiscaliste / fiscaliste international
* Spécialiste immobilier expat
* Etc.

Selon le type, l'interface s'adapte : vocabulaire, features mises en avant, parcours-types proposés, dashboard, etc.

#### Verbatims qui justifient cette feature

> "La création de ton compte tu mets vous êtes quoi une agence d'expatriation ou un cabinet d'avocats. Si cabinet d'avocats bam ça balance en fait un ensemble de features, un affichage ou alors on pourrait faire un autre affichage par rapport aux agences." — **Eric à Artur (21/05/2026)**

#### Sous-features incluses

* **Détection du type au signup**
* **UI/UX adaptée par type**
* **Vocabulaire personnalisé** ("dossier client" vs "mandat" vs "case")
* **Features mises en avant** (les cabinets d'avocats verront plus la signature électronique, les agences hub verront plus le matching prestataires)

#### Bénéfice métier

* Sentiment de "outil fait pour moi"
* Onboarding optimisé par typologie
* Élargissement du marché adressable

---

### Espace test / Guest Codename Show

#### Description détaillée

Un mode "démo" / "sandbox" où l'agence peut configurer son outil avec des jeux de données factices sans risquer ses vraies données. Particulièrement utile pour :

* Tester un nouveau parcours-type avant de le déployer
* Former un nouvel agent
* Présenter Nidria à des prospects (agences partenaires)
* Comprendre comment Nidria fonctionne avant de migrer pour de vrai

#### Verbatims qui justifient cette feature

> "On a déjà pensé à faire un système de ce qu'on appelle des Guest Codename Show, et là en effet tu pourras jouer dessus, tu pourras jouer avec un faux jeu donné, tu pourras mettre des fausses données." — **Sidney Lakehal (10/06/2026)**

#### Sous-features incluses

* **Mode sandbox indépendant des vraies données**
* **Jeu de données pré-configuré** (10-20 dossiers fictifs)
* **Reset à zéro** en un clic
* **Pas d'impact sur les compteurs / facturation**

#### Bénéfice métier

* Risque zéro pour les essais
* Onboarding ludique
* Outil de vente puissant (montrer Nidria sans révéler les vrais dossiers)

---

<a name="configuration-et-workflows"></a>
## 🔧 Configuration et workflows

### Éditeur de workflow vierge clic-glisser

#### Description détaillée

C'est **la feature game-changer** identifiée par Sidney. L'agence ne se voit imposer aucun parcours par défaut. À la création d'un nouveau parcours-type, le canevas est vierge. L'agence construit son propre process en glissant-déposant des "blocs" :

* Blocs Étape (avec nom, statut, durée estimée, responsable, prérequis)
* Blocs Document (requirement avec nom, format attendu, infobulle explicative)
* Blocs Conditionnel (branche selon nationalité, type de projet, etc.)
* Blocs Communication (rappel automatique programmé)
* Blocs Prestataire (assignation d'un partenaire externe à une étape)

Ces blocs se connectent visuellement (style n8n, Bubble, Zapier visual builder) pour former le workflow complet.

Une fois le parcours-type sauvegardé, il devient un template réutilisable. Quand l'agence crée un nouveau dossier, elle choisit le parcours-type approprié ("Setup Autónomo Espagne", "Résidence temporaire Paraguay fast", "Création Sociedad Madrid") et le parcours s'applique. L'agence peut customiser pour ce dossier spécifique sans modifier le template général.

#### Use cases concrets

* **Cas Sidney** : Sidney configure un parcours "Expatriation France → République Tchèque entrepreneur" avec 12 étapes incluant des prérequis stricts (NIE tchèque avant compte bancaire, compte bancaire avant déclaration fiscale). Il configure un parcours différent pour "Expatriation France → Émirats retraite" avec seulement 5 étapes.

* **Cas Reside Paraguay** : Alexis crée deux parcours-types distincts :
 * "Résidence Paraguay Fast" : 6 étapes en 45 jours
 * "Résidence Paraguay Standard" : 6 étapes en 3 mois
 * "Cédula seule" : 4 étapes
 Verbatim Alexis : "Selon le package, tu n'as pas les mêmes papiers à émettre". Validation directe.

* **Cas Didier (Residir Portugal)** : Didier crée un parcours "Importation véhicule France → Portugal" avec ses étapes verrouillées et ses points de contrôle obligatoires.

* **Cas Greg** : Greg crée un parcours "Structuration internationale HK + Indo" avec affectation automatique du cabinet HK pour l'étape A, du cabinet visa Indonésie pour l'étape B, et du fiscaliste FR pour l'étape C.

* **Cas Mathias** : Mathias configure deux parcours distincts "Autónomo express" (5 étapes) et "Sociedad Limitée complète" (12 étapes avec compta et apports).

#### Verbatims qui justifient cette feature

> "Plutôt que de dire qu'on passe système avec un template par défaut puis après les personnes viennent rajouter ce qu'ils veulent dessus, on va littéralement laisser un squelette vide. Toi en fait en gros tu n'as rien à faire face, tu vas faire clic et glisser, première étape c'est quoi ? Je veux un minimum d'avoir un passeport..." — **Sidney Lakehal (10/06/2026)**

> "Pour moi, des étapes qui sont importantes ne sont pas nécessairement pour une autre agence. Parce que chacun a son process, chacun a un peu sa recette à lui." — **Sidney Lakehal**

> "Tu vas avoir un cadre de structuration de l'action, mais si c'est pas agile à l'intérieur du cadre, le cadre va vite devenir trop contraignant et pas suffisamment capable de s'adapter à des situations particulières." — **Didier, Residir Portugal (3/06/2026)**

> "C'est pas mal apparemment. Vous pourrez littéralement customiser bloc par bloc, si vous voulez, par rapport au package qu'a pris la personne." — **Eric confirmant à Alexis, validation directe (12/06/2026)**

#### Sous-features incluses

* **Canevas vierge à l'arrivée** : pas de template imposé. Carré blanc, drag les blocs.

* **Bibliothèque de blocs typés** : Étape, Document/Requirement, Conditionnel, Communication, Prestataire, Sous-process.

* **Blocs documents customisables** : verbatim Sidney call 2 — "tu crées un bloc document, tu lui donnes un nom... le type de document attendu, l'extension (PDF ou Word), tu auras également une mini-infobulle, des informations pour ce document, comment l'obtenir".

* **Documents facultatifs vs bloquants** : verbatim Sidney call 2 — "ça peut être un document de type facultatif, de type informationnel... ça peut ouvrir un truc dans son indicateur". Un doc facultatif ne bloque pas la timeline mais reste demandé.

* **Connexions visuelles entre blocs** : style flowchart, lignes qui montrent les dépendances.

* **Sauvegarde comme parcours-type réutilisable** : un workflow construit = un template nommé, réutilisable.

* **Versioning des parcours-types** : si l'agence modifie son workflow, les anciens dossiers gardent l'ancien parcours, les nouveaux utilisent la nouvelle version.

* **Clonage de parcours-types** : "Setup Autónomo Espagne" → cloner → "Setup Autónomo Portugal" avec quelques modifications.

* **Edition d'un dossier individuel par dérogation** : sur un dossier précis, l'agence peut ajouter ou retirer une étape sans modifier le template général.

* **Aperçu côté expat** : avant publication, l'agence peut prévisualiser à quoi le parcours ressemblera côté espace client.

* **Importation depuis modèles communautaires** : à terme, une marketplace de parcours-types partagés (un parcours "Création société Espagne" qu'une autre agence a fait peut être adopté).

#### Bénéfice métier

* Aucune agence ne se sent "forcée" dans le moule Nidria
* Chaque agence garde sa recette propriétaire (avantage compétitif conservé)
* Évolutivité : un parcours peut être amélioré au fil du temps
* Onboarding rapide : à l'arrivée, l'agence configure son premier parcours en 30-60 min
* L'éditeur peut être utilisé pour les workflows non-expatriation aussi (cabinets d'avocats, fiduciaires, etc.)

#### Détails UX

* Canevas drag-and-drop type whiteboard (React Flow ou similaire)
* Zoom in/out
* Mini-map de navigation pour les grands workflows
* Sidebar avec la bibliothèque de blocs
* Properties panel à droite pour configurer le bloc sélectionné
* Auto-save toutes les 30 secondes
* Bouton "Publier" pour rendre le parcours utilisable

---

### Socle technique transverse

Ces éléments ne sont pas des "features" à présenter aux testeurs mais constituent le socle technique indispensable au fonctionnement de l'ensemble.

* **Authentification séparée agence vs client expat** : deux bases de données isolées, deux systèmes d'auth distincts, deux UI distinctes. Un expat ne peut JAMAIS accéder à un backend agence.

* **Onboarding agence** : création de compte agence, choix du plan, invitation des premiers agents, création du premier parcours-type guidée.

* **Création de dossier et invitation de l'expatrié** : flux fluide pour démarrer un nouveau dossier (5 champs minimum) et envoyer l'invitation par mail.

* **Upload de documents (Supabase Storage)** : interface drag-and-drop, support PDF / images / Word / Excel. Stockage sécurisé.

* **Journal d'activité automatique (audit log)** : traçabilité immutable de toutes les actions. Critique pour la conformité.

* **Interface en français** : le multilangue complet est décrit dans le pôle International et intégrations.

* **Hébergement Supabase Postgres européen** : conformité RGPD natif, performance.

* **Stack frontend React, backend TypeScript** : avec wrappers custom d'Alex (ex-Airbus). Performance et sécurité.

---

### Génération automatique de check-lists par pays/destination

#### Description détaillée

Quand l'agence crée un dossier, elle sélectionne le pays d'origine du client et le pays de destination. Le système propose automatiquement une check-list de documents standard requis pour ce couple origine-destination, basée sur une base de données pré-poolée.

Par exemple :
* France → Paraguay (résidence temporaire) : acte de naissance apostillé, casier judiciaire apostillé, passeport, formulaires Interpol locaux, etc.
* Belgique → Espagne (Autónomo) : carte d'identité belge, certificat de résidence belge à apostiller, etc.
* Brésil → Portugal (D7 visa) : certificat de naissance, comprovação de meios financeiros, etc.

L'agence peut ensuite ajouter ou retirer des documents pour ce dossier spécifique.

#### Use cases concrets

* **Reside Paraguay** : Eloïse crée un dossier pour un Belge. Le système suggère immédiatement la liste France-Paraguay (Belgique-Paraguay reconnue automatiquement comme proche) avec une note "Spécificité Belge : prénoms abrégés sur passeport, certificat complémentaire requis".

* **Mathias** : un Suisse veut créer Autónomo en Espagne. Le système suggère la check-list Suisse-Espagne avec NIE, certificat de résidence suisse apostillé via la convention de La Haye, etc.

#### Verbatims qui justifient cette feature

> "On peut récupérer l'API de Légifrance. Dès qu'en gros, tu as une nouvelle loyauté qui est posée en France, automatiquement, elle est balancée côté API. Ça, on peut le récupérer." — **Eric (12/06/2026)**

> "Ce serait bien quand même d'apporter des liens pour les y apostiller, etc. Pour faciliter la vie aux gens, parce qu'il y a plein de clients qui sont même pas au courant et donc ils prennent leur papier en physique, ils vont chez le notaire, le notaire leur dit non, finalement, allez à tel endroit." — **Alexis (12/06/2026)**

#### Sous-features incluses

* **Base de données pré-poolée par couple origine-destination**
* **Notes spécifiques par nationalité** (Belges : prénoms abrégés / Coréens : pas de lieu de naissance sur passeport / Russes : papiers via agence externe / Pakistanais : visa probablement refusé)
* **Liens vers les services d'apostille par pays d'origine** : le système suggère directement où le client doit aller pour apostiller en France, Belgique, Espagne, etc.
* **Indications de durée typique par étape** par pays
* **Liens vers les sites gouvernementaux pertinents** (immigration paraguayenne, AEAT espagnol, etc.)
* **Mise à jour communautaire** : les agences signalent les changements de législation, validés par modérateurs

#### Bénéfice métier

* Onboarding ultra-rapide d'un nouveau dossier (5 min au lieu de 30)
* Réduction des oublis (la liste suggérée est complète)
* Différenciateur fort vs Notion/Trello qui n'ont aucune intelligence métier
* Particulièrement précieux pour les agences débutantes ou peu de volume sur un pays donné

---

### Requirements par catégorie d'activité

#### Description détaillée

Au-delà du couple origine-destination, le profil d'activité du client influence aussi les requirements. Un pâtissier qui veut importer de la farine européenne au Paraguay a des exigences différentes d'un développeur freelance qui veut juste sa résidence.

Le système propose une bibliothèque de "profils d'activité" :
* Retraité
* Digital nomade / freelance digital
* Entrepreneur PME
* Restaurateur / pâtissier / artisan alimentaire
* Médecin / professionnel de santé
* Investisseur / rentier
* Famille avec enfants
* Étudiant
* Etc.

Selon le profil sélectionné, des étapes et documents supplémentaires sont automatiquement ajoutés au parcours.

#### Use cases concrets

* **Reside Paraguay (pâtissier)** : un client pâtissier veut s'installer au Paraguay et importer sa farine d'Europe. Le système ajoute automatiquement : "Étape importation alimentaire", "Licence sanitaire SENACSA", "Déclaration douanière spécifique aliments transformés".

* **MonEntreprise.es (consultant digital)** : un consultant français veut créer Autónomo. Le système ajoute "Inscription au registre des freelances digitaux", "Déclaration trimestrielle Modelo 130", etc.

* **Sidney (médecin)** : un médecin français part en République tchèque. Système ajoute "Reconnaissance diplôme médical", "Inscription Ordre des médecins tchèque".

#### Verbatims qui justifient cette feature

> "Par exemple, par rapport à son activité, il y aura une liste de requirements. Donc savoir par exemple pour les pâtissiers, c'est que déjà par exemple ils peuvent pas utiliser la même farine, ils peuvent pas utiliser le même lait. Le beurre n'a pas le même goût. Donc souvent, ils doivent tout faire importer d'Europe." — **Eloïse, Reside Paraguay (03/06/2026)**

#### Sous-features incluses

* **Bibliothèque de 20+ profils d'activité prédéfinis**
* **Création de profils custom par agence** (Mathias peut créer "Coach business francophone" comme profil spécifique)
* **Combinaison nationalité + activité** : un Belge pâtissier vs un Coréen pâtissier auront des nuances
* **Suggestions de prestataires partenaires** : un pâtissier au Paraguay nécessite un comptable spécialisé import alimentaire, suggéré automatiquement
* **Estimation budgétaire spécifique à l'activité** : "Pour pâtissier au Paraguay, prévoir 2000-3000$ pour licence SENACSA"

#### Bénéfice métier

* Évite les oublis lourds (un pâtissier qui découvre 3 mois après qu'il faut une licence SENACSA)
* Permet à l'agence de proposer un parcours complet sans devoir connaître toutes les spécificités
* Différenciateur fort sur les niches (Mathias = entrepreneurs digitaux)

---

### Auto-suggestion de documents spécifiques par nationalité

#### Description détaillée

Le système connaît les spécificités documentaires par nationalité et les ajoute automatiquement aux requirements du dossier.

Spécificités captées dans les calls :
* **Belges** : abréviation du prénom sur passeport → nécessite un papier complémentaire "Confirmation prénom complet"
* **Grecs** : nécessitent le nom de jeune fille de la mère
* **Coréens** : pas de lieu de naissance sur passeport → nécessite certificat de naissance séparé
* **Russes** : ne peuvent souvent pas retourner en Russie → apostille via agence externe
* **Pakistanais, Irakiens, pays du Golfe** : 90% du temps, visa refusé → alerte préventive

#### Verbatims qui justifient cette feature

> "Pour les Belges, il y a un document plus à la con sur leur différents certificats, il y a éventuellement le nom complet, mais sur le passeport, à partir du deuxième prénom, ils font une abréviation et ils mettent juste la première lettre du prénom. Et ici, pour eux, par exemple, ce n'est pas bon. Ils veulent un papier de chez eux qui dit que leur prénom est abrégé, mais que le prénom complet, c'est untel." — **Alexis (12/06/2026)**

> "Pour les coréens, par exemple, parce qu'Ines, vu qu'elle est coréenne... Quand tu vas demander ton Interpol, ce n'est pas seulement la photocopie du passeport, parce que sur le passeport, il n'y a pas marqué le lieu de naissance, sur les passeports coréens. Donc, tu dois amener un certificat de naissance en même temps." — **Alexis (12/06/2026)**

> "On a pareil en Grèce. En Grèce, par exemple, ils te demandent le nom de famille de ta mère, le nom de jeune fille de ta mère." — **Eric (12/06/2026)**

#### Sous-features incluses

* **Base de données des spécificités par nationalité**
* **Suggestion automatique au moment de créer le dossier** (à partir de la nationalité du client)
* **Notes contextuelles** ("Spécificité Belge : prénoms abrégés")
* **Liens vers les services pour obtenir les documents complémentaires**

#### Bénéfice métier

* Évite les dépôts ratés à cause d'un document spécifique manquant
* Capitalise sur la connaissance institutionnelle des agences expérimentées
* Particulièrement précieux pour les agences débutantes ou qui rencontrent une nationalité rare

---

### Alerte pays blacklisté pour visa

#### Description détaillée

Quand un prospect renseigne sa nationalité dans le formulaire d'intake, le système vérifie immédiatement si ce pays est "blacklisté" pour le pays de destination de l'agence (visa quasiment impossible). Si oui, le prospect reçoit un message clair et l'agence est notifiée.

#### Use cases concrets

* **Reside Paraguay** : les Pakistanais, Irakiens, pays du Golfe ont rarement leur visa pour le Paraguay. Quand un Pakistanais remplit le formulaire, il reçoit immédiatement "Désolé, les chances d'obtenir un visa paraguayen pour les ressortissants pakistanais sont très faibles. Nous ne pouvons pas vous accompagner avec une garantie raisonnable."

* **Sidney** : pour la République tchèque, certaines nationalités ont des restrictions. Alerte automatique.

#### Verbatims qui justifient cette feature

> "Je ne sais même pas si c'est intéressant pour toi de mettre dans ton CRM les nationalités, comme ils appellent ça, les nationalités exotiques où il faut demander des visas parce que, par exemple ici, si c'est chinois, pakistanais, pays du golfe, un truc comme ça, directement, les trois quarts du temps, si ce n'est même pas, c'est plus que les trois quarts, c'est 90 % du temps, ils ne leur donnent même pas leur visa." — **Alexis, Reside Paraguay (12/06/2026)**

> "Il faut pouvoir vous faire économiser le plus de temps possible. Donc, si en effet, en amont, la personne peut directement faire son autodiagnostic pour savoir si c'est possible ou pas, avant même de pouvoir vous contacter, je pense que ça irait faire un gros écrémage." — **Eric (12/06/2026)**

#### Sous-features incluses

* **Base de données par pays origine x pays destination** : compatibilité visa
* **Messages standardisés** par cas (refus poli)
* **Alerte agence** quand un prospect blacklisté apparaît
* **Possibilité d'override** : l'agence peut décider de traiter quand même

#### Bénéfice métier

* Économie de temps massive (plus de calls de qualification avec des prospects sans espoir)
* Évite les espoirs déçus (le client est immédiatement informé)
* Réputation agence préservée

---

### 106 destinations prédéfinies avec spécificités par nationalité

#### Description détaillée

Base de données structurée des 106 destinations les plus prisées pour l'expatriation, avec pour chacune :

* Visa requis ou non par nationalité
* Documents standards pour résidence
* Durée moyenne du processus
* Coûts gouvernementaux indicatifs
* Conventions fiscales bilatérales
* Notes culturelles utiles
* Partenaires locaux recommandés (avocats, comptables, traducteurs)

#### Verbatims qui justifient cette feature

> "T'as quand même une liste de 106 destinations qui sont les plus prisées." — **Alexis, Reside Paraguay (12/06/2026)**

#### Sous-features incluses

* **Fiches pays complètes** (106 destinations)
* **Mise à jour communautaire** par les agences spécialisées
* **Pré-remplissage automatique des parcours-types**
* **Comparaisons** (pour le client qui hésite entre 2-3 destinations)

#### Bénéfice métier

* Encyclopédie expat intégrée
* Onboarding ultra-rapide d'une nouvelle destination pour l'agence
* Différenciateur stratégique massif

---

<a name="communication-et-relances"></a>
## 📨 Communication et relances

### Rappels customisables avec validation manuelle

#### Description détaillée

Le système de rappels est conçu pour automatiser les relances **sans déshumaniser** la communication. Aucun message n'est envoyé sans qu'un agent l'ait validé manuellement. C'est un anti-pattern voulu : l'IA et l'automatisation n'écrivent pas à la place de l'humain.

Le système fonctionne en plusieurs étapes :

1. L'agence définit, dans ses parcours-types ou directement sur un dossier, des "règles de rappel" :
 * Quand : J+15 sans réponse, ou date fixe (15 juillet), ou condition (étape X complétée sans avoir reçu le doc Y depuis 7 jours)
 * Vers qui : le client expatrié, un prestataire externe, ou un membre de l'équipe interne
 * Quel canal : mail, WhatsApp (copier-coller manuel ou envoi automatique via API Business), in-app, SMS, Telegram
 * Quel message : modèle personnalisable avec variables d'interpolation

2. Quand le déclencheur est atteint, le système prépare le message et le met dans la "file d'attente de validation" de l'agent responsable.

3. L'agent voit ses rappels à valider du jour dans son tableau de bord. Pour chaque rappel :
 * Il peut valider et envoyer tel quel
 * Il peut modifier le message avant envoi (cas humain spécifique)
 * Il peut reporter le rappel (snooze 3 jours)
 * Il peut annuler le rappel

4. Le rappel est envoyé après validation.

5. Le système trace dans l'historique du dossier : "Rappel mail envoyé par Eloïse le 12/06 à 14:23, message validé manuellement".

#### Use cases concrets

* **Cas Reside Paraguay** : Eloïse a un dossier où elle a déposé à l'Interpol hier. Le système lui prépare un rappel interne "Aller chercher dossier Interpol pour Madame Y - 5 minutes" pour demain matin. Eloïse voit le rappel, marque "fait", et passe au suivant.

* **Cas Reside Paraguay 2** : un prospect russe basé en Lettonie n'a plus répondu depuis 12 jours. Le système propose à Sélim un message de relance : "Bonjour Pavel, juste un petit point sur votre dossier d'expatriation au Paraguay. Avez-vous pu récupérer votre apostille via votre agence à Riga ?". Sélim valide tel quel et envoie.

* **Cas Sidney** : un client à Prague a uploadé son passeport il y a 5 jours mais l'apostille traîne. Le système prépare un rappel WhatsApp "Bonjour [prénom], votre apostille du casier judiciaire est encore en attente. Avez-vous des nouvelles de votre côté ?". Sidney customise pour ajouter une touche personnelle et envoie.

* **Cas Greg** : Greg a recommandé un fiscaliste à un client, mais le fiscaliste n'a pas donné de news depuis 10 jours. Le système prépare un rappel pour le fiscaliste : "Bonjour Manon, où en es-tu sur le dossier de Pierre Durand que je t'ai référé le 02/06 ?". (le rappel basique est disponible immédiatement ; la version complète avec affiliation/commission est décrite dans le pôle Commercial).

#### Verbatims qui justifient cette feature

> "Un calendrier qui apparaîtrait où tu auras un petit point où tu saurais peut-être recontacter telle personne. Notifié automatiquement. Lié avec le WhatsApp pour qu'il voit quand est-ce qu'on a contacté le client, ou avec le mail." — **Eloïse, Reside Paraguay (03/06/2026)**

> "Souvent les gens ne répondent pas à mes relances." — **Eloïse, Reside Paraguay (03/06/2026)** — douleur exprimée

> "Des rappels customisables avec une approbation manuelle d'un membre de l'équipe en question." — **Sidney Lakehal (10/06/2026)**

> "On pourrait mettre aussi un espèce de timer pour dire hier on a déposé le dossier à l'Interpol, normalement aujourd'hui il est prêt, il faut aller le chercher. Si tu commences à avoir beaucoup de clients, c'est bien que ton CRM fasse aussi un peu agenda recordatory de 'il faut aller chercher tel truc, de tel dossier'." — **Alexis, Reside Paraguay (12/06/2026)**

> "C'est en gros quand je pense à la personne je lui écris." — **Greg (12/06/2026)** — état actuel ad hoc à remplacer

#### Sous-features incluses

* **Création de rappel sur un dossier** : depuis n'importe quelle vue, l'agent peut créer un rappel en deux clics (date, canal, destinataire, message ou template).

* **Templates de messages réutilisables par agence** : chaque agence crée sa bibliothèque de modèles ("Relance apostille J+10", "Confirmation arrivée", "Demande infos manquantes"). Verbatim Eloïse : "Pas une IA, mais un template humain que tu réutilises".

* **Variables d'interpolation** : `{client_name}`, `{step_name}`, `{days_left}`, `{document_name}`, `{agency_name}`, `{agent_name}`, `{deposit_date}`, etc. L'agent insère les variables dans son modèle, le système remplit avant envoi.

* **Calendrier visible par tous les agents** : vue calendaire des rappels à venir, par agent ou globale. Utile pour la planification de la semaine.

* **Validation manuelle obligatoire** : aucun envoi automatique sans clic d'un agent. Verbatim Sidney : "approbation manuelle d'un membre de l'équipe en question".

* **Logique de cascade de relances** : configurable par agence. Par exemple "Pas de réponse en 5 jours → relance 1. Pas de réponse en 10 jours → relance 2 plus ferme. Pas de réponse en 15 jours → notification interne à l'agent".

* **Rappels internes (équipe)** : pas que des relances clients. Verbatim Alexis : "il faut aller chercher tel truc de tel dossier". Rappels internes pour les tâches opérationnelles.

* **Rappels externes (prestataires)** : relancer un avocat, comptable, notaire qui a un retard sur sa partie. L'envoi d'un mail au prestataire externe est possible directement ; la version avec espace dédié prestataire est décrite dans le pôle Commercial.

* **Historique des rappels envoyés** : visible dans le dossier, pour ne pas envoyer deux relances en doublon.

* **Pour WhatsApp** : deux modes possibles. Mode manuel — génération du message + bouton "Ouvrir WhatsApp" qui pré-remplit la conversation, l'agent clique et envoie depuis WhatsApp Pro. Mode automatique — envoi direct via WhatsApp Business API officielle (voir feature dédiée dans le pôle Communication).

* **Pour mail** : envoi direct depuis Nidria via Resend ou similaire (`noreply@nidria.com` côté backend), avec adresse from personnalisable à terme (`agence@nidria.com`).

* **Snooze (reporter)** : un agent peut reporter un rappel de 1h, 24h, 3 jours, 1 semaine.

#### Bénéfice métier

* Plus jamais d'oubli de relance (la douleur #1 d'Eloïse)
* Ton humain conservé (chaque envoi est validé/customisé)
* Économie de temps : la rédaction est pré-faite, l'agent valide en 10 secondes
* Traçabilité complète (qui a relancé qui quand)
* Scalabilité : un seul agent peut gérer 50 dossiers actifs au lieu de 10

#### Détails UX

* Une "boîte de rappels à valider" en haut du dashboard, comme un mini-Gmail
* Code couleur : urgent (rouge), aujourd'hui (orange), à venir (gris)
* Édition inline du message avant validation (un seul écran)
* Validation par clic ou raccourci clavier
* Confirmation visuelle après envoi

---

### Intégration WhatsApp Business API automatique

#### Description détaillée

Au socle de base, le système prépare le message WhatsApp et l'agent fait copier-coller dans WhatsApp Pro. En une brique dédiée, l'agence connecte son compte WhatsApp Business via API officielle (Meta Business Suite) et les messages sont envoyés automatiquement depuis Nidria.

Côté agence, plus rien à faire manuellement : un rappel validé est envoyé directement au numéro WhatsApp du client.

#### Use cases concrets

* **Reside Paraguay** : Eloïse a 100 prospects en attente. Elle configure des rappels automatiques bi-mensuels validés par lots. Les WhatsApp sont envoyés directement, sans copier-coller.

* **Sidney** : Sidney connecte son WhatsApp Business à Nidria. Toutes ses communications avec les expatriés passent par Nidria (traçabilité), mais le client reçoit toujours dans son WhatsApp.

#### Verbatims qui justifient cette feature

> "Tu pourras directement depuis ton espace Meta Business Suite venir balancer directement tes identifiants pour qu'automatiquement, depuis ton espace d'agence, dès que tu as une personne qui vient automatiquement répondre à un formulaire, directement sur l'application, que toi, tu reçois une notification sur ton téléphone." — **Sidney Lakehal (10/06/2026)**

#### Sous-features incluses

* **Configuration WhatsApp Business API depuis l'espace agence**
* **Pré-validation manuelle gardée** (philosophie produit : pas d'envoi sans validation humaine)
* **Réception des réponses dans Nidria** : si le client répond sur WhatsApp, le message arrive dans la timeline du dossier
* **Notifications temps réel sur le téléphone de l'agent** quand un client uploade un document ou répond
* **Templates WhatsApp approuvés par Meta** (Meta exige des templates pré-validés pour les messages business)

#### Bénéfice métier

* Suppression de l'étape copier-coller (10 secondes économisées x 50 messages/jour = 8 min/jour)
* Réception des réponses centralisée dans Nidria (plus de "j'ai pas vu sur WhatsApp")
* Trace écrite et consultable

---

### Bot Telegram comme alternative à WhatsApp

#### Description détaillée

Pour les agences qui ne veulent pas utiliser WhatsApp Business (coûts API Meta, complexité d'approbation), un bot Telegram alternatif est proposé. Configuration similaire à WhatsApp : l'agence crée son bot Telegram, donne le token à Nidria, et les messages partent via Telegram.

#### Verbatims qui justifient cette feature

> "Ce qui vient, c'est qu'on peut également faire un bot sur Telegram. Je pense qu'il y a peut-être du Telegram, peut-être du WhatsApp." — **Sidney Lakehal (10/06/2026)**

#### Sous-features incluses

* **Création de bot Telegram guidée**
* **Templates de messages Telegram**
* **Réception des réponses dans Nidria**

#### Bénéfice métier

* Alternative économique à WhatsApp Business
* Couvre les marchés où Telegram est plus utilisé (Russie, Europe de l'Est, Émirats)

---

### Notifications temps réel multi-canaux

#### Description détaillée

L'agent reçoit des notifications immédiates sur les événements critiques d'un dossier, via les canaux qu'il a choisis (push mobile, push desktop, mail, WhatsApp, Telegram).

Événements notifiables :
* Nouveau lead via formulaire d'intake
* Document uploadé par un client
* Étape validée/refusée
* Rappel à valider du jour
* Mention @ d'un agent dans une note
* Message reçu du client (WhatsApp / Telegram)
* Blocage d'un dossier (étape critique en retard)

#### Verbatims qui justifient cette feature

> "Si imaginons, tu as une personne qui... si la personne a sur son espace uploadé une pièce d'identité ou un passeport en format PNG ou JPEG, automatiquement, tu recevras une notification sur ton téléphone." — **Sidney Lakehal (10/06/2026)**

#### Sous-features incluses

* **Centre de notifications dans l'espace agence**
* **Configuration des notifs par agent et par type d'événement**
* **Canaux multiples** (push mobile, push web, mail, WhatsApp/Telegram)
* **Mode "ne pas déranger"** sur plages horaires (sommeil, week-end)
* **Aggregation intelligente** : pas 10 notifs pour 10 uploads d'un même client, mais une seule "Le client X a uploadé 10 documents"

#### Bénéfice métier

* Réactivité (l'agent voit immédiatement le doc qu'il attendait)
* Moins de check manuel de l'outil

---

### Système de mentions @ entre agents

#### Description détaillée

Dans les notes d'un dossier, dans les commentaires d'une étape, dans le tchat interne, un agent peut mentionner un autre agent avec `@prénom` (style Slack/Notion). L'agent mentionné reçoit une notification.

#### Sous-features incluses

* **Auto-complétion** lors de la frappe `@`
* **Notification au mentionné** (in-app, mail, push)
* **Lien direct vers le contexte** (le dossier, l'étape, la note)
* **Historique des mentions** par agent

#### Bénéfice métier

* Coordination équipe fluide
* Évite les "Eloïse tu peux regarder le dossier de M. Durand ?" en WhatsApp ou par mail

---

### Notifications automatiques pour livreurs/runners

#### Description détaillée

Les agences qui ont des coursiers internes (Reside Paraguay a un chauffeur) peuvent automatiser les notifications de courses à faire.

Quand un document est prêt à être récupéré (chez un traducteur, à l'immigration, à l'Interpol), le système envoie automatiquement un message au coursier avec :
* L'adresse à laquelle aller (lien Google Maps)
* Ce qu'il doit récupérer
* Pour quel dossier client
* Toute note spéciale ("Demander le tampon X")

#### Verbatims qui justifient cette feature

> "Si éventuellement, il pouvait y avoir un message automatique qui s'envoie au livreur pour pouvoir lui dire tu as une traduction de prêt chez tel traducteur, c'est ça son adresse, lien Google Maps, tu dois aller la chercher." — **Alexis, Reside Paraguay (12/06/2026)**

#### Sous-features incluses

* **Type d'utilisateur "Coursier"** (rôle spécifique)
* **Notifications WhatsApp/SMS au coursier**
* **Lien Google Maps intégré**
* **Confirmation de récupération** par le coursier (un bouton dans le message)

#### Bénéfice métier

* Coordination logistique fluide
* Plus de "Sélim, tu peux aller chercher chez X ?" par WhatsApp manuel
* Traçabilité des récupérations

---

### Inbox unifiée

#### Description détaillée

L'agent dispose d'une inbox unifiée centralisant TOUTES les communications entrantes : mails, messages WhatsApp Business, messages Telegram, commentaires sur l'espace expat, mentions @ internes, notifications de partenaires.

Tout est trié par dossier client, avec recherche transverse.

#### Sous-features incluses

* **Vue unifiée multi-canaux**
* **Filtres par canal, dossier, agent assigné, statut (non lu, lu, traité)**
* **Réponses depuis l'inbox** (le canal de réponse est celui d'origine)
* **Tags personnalisés**
* **Recherche full-text**

#### Bénéfice métier

* Plus de "j'ai pas vu sur WhatsApp"
* Vue 360° d'un client en un clic
* Productivité accrue

---

### Chat intégré agence ↔ client

#### Description détaillée

Pour les agences qui préfèrent tout centraliser dans Nidria plutôt que d'utiliser WhatsApp/Telegram, un module de chat natif est intégré. Le client a sa conversation avec son agence directement dans son espace.

#### Sous-features incluses

* **Chat 1-to-1 client / agence**
* **Pièces jointes**
* **Réactions emoji**
* **Notifications push**
* **Multi-langues (avec traduction automatique optionnelle, voir une brique dédiée.16)**

#### Bénéfice métier

* Alternative à WhatsApp pour les agences qui veulent tout dans Nidria
* Trace écrite native
* Moins de friction de canal (WhatsApp Pro paramétrage, etc.)

---

### Traduction automatique des messages

#### Description détaillée

Le client russe écrit en russe à son agent francophone. L'agent voit le message traduit en français (avec original en hover). L'agent répond en français, le client reçoit en russe.

Activable / désactivable par dossier ou globalement.

#### Sous-features incluses

* **Traduction temps réel multi-langues**
* **Affichage original + traduction**
* **Détection automatique de la langue**
* **Note "Message traduit automatiquement"**
* **Désactivation possible** (si l'agent parle la langue)

#### Bénéfice métier

* Élargissement massif du marché adressable
* Suppression de la barrière linguistique
* Service plus humain pour le client (qui écrit dans sa langue maternelle)

---

### Connecteur communauté (WhatsApp groupe / Skool / Discord)

#### Description détaillée

L'agence peut connecter sa communauté tierce (groupe WhatsApp, école Skool, serveur Discord, groupe Telegram) à Nidria. Permet de :

* Notifier des sous-groupes depuis Nidria
* Tagger des membres comme leads potentiels
* Synchroniser les nouveaux membres comme prospects

#### Verbatims qui justifient cette feature

> "J'ai créé une communauté WhatsApp gratuite sans nombre à peu près... j'ai créé aussi sur, en ce moment, sur une communauté que j'aimerais mettre en place, avec tout ce que j'ai généré déjà depuis deux ans... je sais que eux, je vais les renvoyer vers la school. Et peut-être, tu as des petits messages en fait, par droite à gauche liés avec le, ça peut être sympa, tu vois." — **Greg (12/06/2026)**

#### Sous-features incluses

* **Connecteurs natifs** (WhatsApp groupe via API, Skool API, Discord bot, Telegram bot)
* **Synchronisation des membres** (avec opt-in)
* **Notifications croisées**
* **Conversion membre → prospect** en un clic

#### Bénéfice métier

* Capitalisation sur les communautés existantes des agences
* Source de leads identifiée et exploitable
* Différenciateur pour les agences avec forte présence communautaire (Greg, Sidney)

---

<a name="documents-et-automatisation-administrative"></a>
## 📄 Documents et automatisation administrative

### OCR auto-extraction passeport

#### Description détaillée

Quand le client uploade son passeport (PDF ou image), un système OCR extrait automatiquement les champs :

* Nom de famille
* Prénoms
* Date de naissance
* Lieu de naissance
* Nationalité
* Numéro de passeport
* Date de délivrance
* Date d'expiration
* Sexe

Ces données pré-remplissent la fiche client et tous les formulaires qui en dépendent.

L'agent **valide manuellement** les données extraites (pour éviter les erreurs). C'est cohérent avec la philosophie "validation manuelle".

#### Use cases concrets

* **Alexis (Reside Paraguay)** : Alexis a déjà codé son propre OCR Python (V7 actuelle). Il a confirmé que c'est "ultra important" : "Les trois quarts des agences font ça à la main, je ne connais personne qui fasse de l'OCR. Il y a des boîtes où ils ont 40 employés et il y en a une dizaine qui font de l'administratif."

* **Inès Heu** : à Reside Paraguay, elle saisit manuellement les passeports clients dans AppSheet. OCR éliminerait cette tâche.

#### Verbatims qui justifient cette feature

> "Si tu veux vendre un CRM, enfin louer un SaaS CRM à des entreprises, je pense que ça [l'OCR], c'est un truc qui est ultra important. Ça te fait gagner du temps parce que les gens ici, les trois quarts des agences, je te dis les trois quarts, je ne connais personne qui fasse ça." — **Alexis, Reside Paraguay (12/06/2026)**

> "Il y aura un peu de reconnaissance optique au niveau des documents." — **Artur Pouget (21/05/2026)**

#### Sous-features incluses

* **OCR multilingue** (passeports en plusieurs langues/scripts : latin, cyrillique, arabe, coréen, etc.)
* **Auto-extraction des champs MRZ** (Machine Readable Zone) : c'est la zone standardisée OACI en bas du passeport, très lisible automatiquement
* **Validation manuelle obligatoire** avant utilisation
* **Stockage du passeport original** (le scan reste accessible)
* **Détection des passeports périmés** : alerte si la date d'expiration est dépassée ou proche
* **Auto-calcul de l'âge** depuis la date de naissance

#### Bénéfice métier

* Économie de 5-10 minutes par dossier sur la saisie passeport
* Réduction des erreurs de saisie manuelle
* Pré-remplissage de tous les formulaires administratifs
* Particulièrement précieux pour les agences avec 50+ dossiers/an

---

### Génération automatique de PDFs administratifs pré-remplis

#### Description détaillée

L'agence configure ses formulaires administratifs récurrents (formulaire Interpol, certificat vie résidence, déclarations diverses) une fois comme templates. À chaque nouveau dossier, le système pré-remplit automatiquement ces formulaires avec les données du client.

L'agent valide, imprime, et le formulaire est prêt pour signature physique.

#### Use cases concrets

* **Alexis (Reside Paraguay)** : Alexis a déjà codé son propre générateur Python (V7). Il génère 3 à 5 formulaires par client : Interpol, certificat vie/résidence, etc. Système de "tampons" pour les témoins (toujours les mêmes deux personnes).

* **Mathias (MonEntreprise.es)** : Modelo 030 (premier enregistrement fiscal), Modelo 036/037 (déclaration d'activité). Pré-remplissage automatique.

#### Verbatims qui justifient cette feature

> "J'ai une petite fenêtre graphique où j'automatise par exemple les formulaires des clients. Je mets leurs infos dessus, je fais générer et ça me génère tous les formulaires que tu as besoin pour chaque client selon les cas, ça peut être de 3 à 5 formulaires." — **Alexis, Reside Paraguay (12/06/2026)**

> "Si tu prends ton temps à remplir 5 formulaires par client à la main, ça te prend énormément de temps, il peut y avoir des erreurs." — **Alexis**

#### Sous-features incluses

* **Configuration des templates PDF** par l'agence (upload du PDF original + définition des champs et leurs positions)
* **Pré-remplissage automatique** depuis les données du dossier
* **Annuaire de témoins prédéfinis** (verbatim Alexis : "comme c'est toujours les deux mêmes personnes, tu choisis la personne dans l'annuaire")
* **Génération en un clic**
* **Multi-formats** (PDF, Word, image)
* **Versioning des templates** (formulaire mis à jour par l'administration → nouvelle version, anciens dossiers gardent l'ancienne)

#### Bénéfice métier

* Économie massive de temps (5 formulaires x 5 min = 25 min/client) → 4h économisées par lot de 10 clients
* Réduction drastique des erreurs (plus de fautes de frappe)
* Standardisation des envois administratifs
* Différenciateur fort vs les outils génériques (Notion ne fait pas ça)

---

### Vérification automatique des dates d'expiration

#### Description détaillée

Tous les documents uploadés ayant une date de validité (apostilles, casiers judiciaires, certificats, passeports) sont surveillés. Quand une date d'expiration approche, une alerte est émise.

#### Use cases concrets

* **Reside Paraguay** : un casier judiciaire français a une validité de 3 mois. Si le client a uploadé son casier le 1er mars et que le dépôt immigration prévu le 15 juin, le système alerte "Casier judiciaire expirera le 1er juin, refaire avant dépôt".

* **Mathias** : un certificat de résidence apostillé a une validité limitée. Le système alerte avant qu'il soit refusé par l'AEAT.

#### Verbatims qui justifient cette feature

> "Ce serait un autre outil d'un autre côté qui regarderait un peu les dates des documents où tu pourrais mettre les dates de venue du client et qui vérifie automatiquement que les apostilles et les documents soient signés déjà d'une et de deux qui soient dans les bonnes dates pour les dépôts de dossiers." — **Alexis, Reside Paraguay (12/06/2026)**

#### Sous-features incluses

* **Configuration de la durée de validité** par type de document
* **Alertes progressives** (J-30, J-15, J-7, J-0)
* **Suggestion de refaire le document** avant expiration
* **Lien direct vers le service qui délivre le document** (mairie, ministère, etc.)

#### Bénéfice métier

* Évite les dépôts ratés à cause d'un document expiré
* Réduction des situations de stress de dernière minute
* Amélioration de la qualité de service perçue par le client

---

### OCR automatique sur tous documents

#### Description détaillée

Au-delà du passeport , l'OCR s'étend à tous les documents administratifs uploadés : casiers judiciaires, certificats de naissance, factures, contrats, etc. Extraction automatique des champs pertinents.

#### Sous-features incluses

* **OCR multi-format** (PDF, image, scan)
* **Détection automatique du type de document**
* **Extraction des champs** spécifiques au type
* **Validation manuelle obligatoire**
* **Stockage de l'original**

#### Bénéfice métier

* Auto-remplissage massif de tous les formulaires administratifs
* Économie de temps phénoménale sur les agences à fort volume

---

### Versioning des documents

#### Description détaillée

Chaque document uploadé garde son historique. Si un client uploade une nouvelle version (casier judiciaire renouvelé, par exemple), l'ancien reste accessible mais marqué "périmé". Permet de retrouver l'évolution d'un dossier.

#### Sous-features incluses

* **Historique des versions par document**
* **Comparaison entre versions** (diff visuel)
* **Restauration d'une version antérieure**
* **Notation manuelle** (qui a uploadé quoi quand)

#### Bénéfice métier

* Audit possible
* Traçabilité réglementaire (RGPD : qui a vu quoi)
* Confiance client (rien ne se perd)

---

### Générateur de contrat avec signature électronique

#### Description détaillée

L'agence configure ses contrats types (devis-contrat, lettre de mission, mandat) avec des variables d'interpolation. À chaque nouveau dossier, le contrat se génère automatiquement avec les bonnes infos.

Le client signe électroniquement (style DocuSign light intégré). Le contrat signé est archivé dans le dossier.

#### Verbatims qui justifient cette feature

> "Il y a la partie génération de contrat avec le lead pour pouvoir sceller le truc. On comptait même mettre en place un système avec DocuSign pour aller sceller par une signature numérique le contrat." — **Eric (12/06/2026)**

> "GÉNÉRATEUR DE CONTRAT : agence remplit formulaire → contrat généré → envoyé côté expatrié sur son espace Nidria." — **Eloïse, Reside Paraguay (03/06/2026)**

#### Sous-features incluses

* **Templates de contrats configurables**
* **Variables d'interpolation** (nom client, montant, services, etc.)
* **Génération PDF avec branding agence**
* **Signature électronique légalement valable** (eIDAS / e-Sign Act)
* **Archivage dans le dossier**
* **Suivi du statut** (envoyé / lu / signé)
* **Relances automatiques** si non signé après X jours

#### Bénéfice métier

* Suppression de l'aller-retour mail + impression + scan + envoi
* Légalité garantie
* Trace immuable

---

<a name="international-et-integrations"></a>
## 🌍 International et intégrations

### Multilangue complet (côté espace client et agence)

#### Description détaillée

Tout l'interface utilisateur est traduisible dans plusieurs langues, configurable par utilisateur. La langue n'est pas seulement "FR ou EN" : c'est un choix par utilisateur dans son profil. Donc dans une même agence, un agent peut utiliser l'interface en français pendant que son associé l'utilise en anglais.

Côté expat, la langue est définie à la création du dossier (champ "langue préférée du client") et l'invitation lui parvient déjà dans sa langue.

Langues supportées (par ordre de priorité) :

1. **Français** (langue de lancement)
2. **Anglais**
3. **Espagnol**
4. **Portugais**
5. **Allemand**
6. **Russe**
7. **Coréen**

Langues additionnelles selon demande testeurs : italien, arabe (avec RTL), polonais, ukrainien, hongrois, chinois mandarin.

#### Use cases concrets

* **Reside Paraguay** : Inès (coréenne) utilise l'interface en anglais, Alexis (français) en français, Mathias (paraguayen) en espagnol. Tous travaillent sur les mêmes dossiers.

* **Reside Paraguay 2** : un client russe basé en Lettonie reçoit son invitation en russe et navigue son espace expat en russe. Eloïse reçoit ses messages internes en français.

* **Smart Traveller Maurice** : Em et Perle servent des clients francophones (FR), des clients britanniques (EN), des Indiens (EN). Chaque client a son espace dans sa langue.

* **Mathias (MonEntreprise.es)** : tous ses clients sont francophones, mais ses partenaires locaux (avocats, comptables) sont espagnols. Quand il invite un prestataire, l'espace prestataire est en espagnol.

#### Verbatims qui justifient cette feature

> "Moi, actuellement, mon marché, les gens avec qui je parle, j'ai russes, ukrainiens, allemands, espagnols, américains. J'ai des Indiens qui parlent avec moi. J'ai des Polonais, Macedone du Nord. Là, actuellement, j'ai vraiment beaucoup de pays." — **Eloïse, Reside Paraguay (03/06/2026)**

> "Ines, elle se balade en anglais." — **Alexis (12/06/2026)**

#### Sous-features incluses

* **Langue par utilisateur dans le profil**
* **Langue par dossier (côté expat)** : envoi d'invitation dans la langue du client
* **Traductions des templates de rappels** : un template peut avoir plusieurs versions linguistiques
* **Traductions des noms d'étapes/documents** : l'agence peut traduire ses propres parcours-types
* **Système RTL pour arabe / hébreu** : l'interface se retourne automatiquement
* **Date/heure localisées** : 06/12/2026 (US) vs 12/06/2026 (FR) vs 12.06.2026 (DE)
* **Devises locales** : € pour Europe, $ pour US, autres selon pays
* **Formats numériques localisés** : 1,000.50 (US) vs 1.000,50 (FR/DE)

#### Bénéfice métier

* Élargit le marché adressable bien au-delà de la francophonie
* Permet aux agences avec équipe internationale (Reside, Smart Traveller, Greg) de travailler nativement
* Le client expat se sent compris dans sa langue
* Différenciateur fort vs les CRM génériques mono-langue ou anglais-only

---

### Annuaire de traducteurs avec onglet "pas de traducteur disponible"

#### Description détaillée

L'agence maintient une liste de traducteurs assermentés par langue. Pour chaque langue, on a :
* Liste des traducteurs disponibles localement (avec contact)
* Onglet "Pas de traducteur disponible pour cette langue" avec message automatique au client

Quand l'agence assigne une traduction à un dossier :
* Si traducteur disponible : assignation automatique + notif au traducteur
* Si pas de traducteur (cas Lettonie, Pakistan, langues exotiques) : message standard au client "Pas de traducteur disponible pour le letton au Paraguay. Demandez votre document en anglais directement à votre administration et nous le ferons traduire localement."

#### Verbatims qui justifient cette feature

> "Tu as une liste avec tous les traducteurs autorisés, avec un petit truc pour chercher selon les langues. Ici, tu peux mettre la langue. Et là, tu as les traducteurs en français ou dans n'importe quelle langue qui sortent." — **Alexis, Reside Paraguay (12/06/2026)**

> "Ce serait pas mal d'avoir un système de liste et tu peux assigner toi ton traducteur de telle langue à chaque langue mais pouvoir aussi avoir un dans la liste un petit onglet custom où il n'y a pas de traducteur de telle, telle, telle et telle langue et que ça envoie un message automatique pour tel cas." — **Alexis, Reside Paraguay (12/06/2026)**

#### Sous-features incluses

* **Annuaire de traducteurs structuré**
* **Filtres par langue source/cible, certification, localisation**
* **Onglet "langues exotiques sans traducteur" avec message standard**
* **Scrap automatique des listes officielles** (liste des traducteurs assermentés du gouvernement paraguayen est open source en CSV/JSON/PDF — Alexis l'a mentionné)
* **Tarification indicative**

#### Bénéfice métier

* Gestion automatique des cas complexes (Lettonie, Pakistan, langues du Caucase)
* Évite les explications répétées au client ("Pourquoi je dois demander mon doc en anglais ?")
* Optimisation des partenariats traducteurs

---

### Gestion des jours fériés internationaux

#### Description détaillée

Le système connaît les jours fériés des principaux pays et les utilise pour calculer les délais réalistes.

Exemple : "Dépôt immigration Paraguay le 18/06" → estimation "30 jours ouvrés" → ne compte pas les week-ends ni les jours fériés paraguayens → résultat affiché : "Récupération prévue le 04/08".

#### Sous-features incluses

* **Base de données des fériés internationaux** (Paraguay, France, Espagne, Portugal, Allemagne, Royaume-Uni, etc.)
* **Mise à jour annuelle automatique**
* **Possibilité d'ajouter des fériés custom par agence** (jours de fermeture spécifiques)
* **Distinction jours ouvrés vs jours ouvrables**

#### Bénéfice métier

* Estimations réalistes (un délai en juillet/août dans la France hispanophone = chiffre divisé par 2)
* Évite les promesses ratées au client

---

### Calendrier personnel pour appels et rappels

#### Description détaillée

Chaque agent dispose d'un calendrier personnel intégré qui affiche :
* Les rappels à valider du jour
* Les calls planifiés avec des clients
* Les deadlines de dossiers
* Les rendez-vous en physique

Le calendrier peut être synchronisé avec Google Calendar, Outlook, iCal, Skype, Calendly.

#### Verbatims qui justifient cette feature

> "J'ai mon petit calendrier personnel, j'ai un appel, j'ai des petits résumés, deux trois mots juste d'où la personne elle vient, quel papier il lui faut, quel plan elle veut." — **Eloïse, Reside Paraguay (03/06/2026)**

> "J'ai besoin de Skype tout le temps parce qu'en fait, en soi, moi, j'ai plusieurs personnes... je vais développer un peu les calls de une heure, les calls de 45 minutes." — **Greg (12/06/2026)**

#### Sous-features incluses

* **Vue calendrier semaine/mois**
* **Synchronisation bidirectionnelle Google Calendar / Outlook / iCal**
* **Intégration Calendly / Cal.com** pour les prises de RDV externe
* **Intégration Skype** (Greg)
* **Notifications avant événements**
* **Drag-and-drop des événements**

#### Bénéfice métier

* Vue unifiée de la journée
* Plus de double-saisie entre Nidria et Google Calendar
* Le client peut prendre RDV directement via Calendly intégré

---

### Intégration calendrier Skype/Calendly/Google Calendar

(Voir détails dans Feature une brique dédiée.19 Calendrier personnel — feature dédiée au lien avec les outils externes.)

---

### Intégration Google Maps pour localisation client

#### Description détaillée

À la création d'un dossier, l'agence peut ajouter l'adresse de résidence du client (hôtel, Airbnb, résidence permanente future). L'adresse est validée via Google Maps et l'agent peut accéder à un lien direct pour aller à l'adresse en voiture.

Les adresses de prestataires (notaires, traducteurs, administrations) sont aussi géolocalisées pour faciliter les déplacements.

#### Verbatims qui justifient cette feature

> "Ce serait pas mal qu'il puisse aussi avoir une option où il te met le lieu lien Google Maps de là où il va résider pendant son voyage, son séjour. Comme ça, tu as déjà le lien du Google Maps, donc tu sais où tu dois l'amener en avance." — **Alexis, Reside Paraguay (12/06/2026)**

#### Sous-features incluses

* **Champ adresse avec auto-complétion Google Maps**
* **Validation de l'adresse** (existe vraiment)
* **Lien direct Google Maps / Waze / Apple Maps**
* **Carte intégrée** dans la fiche dossier (vue map)
* **Itinéraire optimisé** entre plusieurs adresses (pour les agents qui font 5-6 RDV/jour)

#### Bénéfice métier

* Économie de temps logistique pour les agents terrain (Alexis, Sélim)
* Évite les erreurs d'adresse
* Préparation des courses agence/notaire/immigration optimisée

---

### Intégration flight tracker (FlightAware / FlightScanner)

#### Description détaillée

L'agence peut entrer le numéro de vol du client à venir. Le système synchronise automatiquement avec un service de tracking de vols (FlightAware, FlightScanner) et alerte si :

* Le vol est en retard
* Le vol est annulé
* L'horaire change
* Le vol arrive en avance

L'agent reçoit la notif et peut adapter sa logistique.

#### Use cases concrets

* **Reside Paraguay** : Alexis a un client qui était censé arriver jeudi à 6h. En réalité, son vol est avancé au mardi nuit. Le système alerte Alexis, qui peut s'organiser pour aller à l'aéroport à minuit et demi au lieu de 6h jeudi.

#### Verbatims qui justifient cette feature

> "Il y a d'autres boîtes qui font, qui ont ça sur leur CRM ou c'est pas mal, ou directement, tu mets ça dans ton CRM, le numéro de vol, et tu as un auto-checking sur un fly scanner qui te prévient si le vol arrive bien au bon moment, s'il est en retard, délayé, ou pas, parce que ça, ça peut arriver très souvent... Le client en particulier, il vient pas d'Europe, il vient du Brésil. Le vol, depuis San Paolo, c'est à peu près une heure et demie, deux heures. Donc, à partir du moment où il n'est pas encore monté dans l'avion, il peut se passer n'importe quoi." — **Alexis, Reside Paraguay (12/06/2026)**

#### Sous-features incluses

* **Champ "numéro de vol" sur le dossier**
* **Tracking automatique via API FlightAware ou similaire**
* **Alertes en temps réel**
* **Le client peut changer son numéro de vol facilement**
* **Notification 1h avant arrivée**

#### Bénéfice métier

* Pas de course aéroport ratée
* Service client premium (l'agent sait avant le client)
* Économie de temps logistique

---

### Intégrations comptables (Pennylane, QuickBooks, Sage, Holded)

#### Description détaillée

Les agences peuvent connecter leur outil comptable à Nidria. Les factures émises depuis Nidria sont automatiquement synchronisées avec l'outil compta.

#### Sous-features incluses

* **Connecteurs natifs** pour les 4 principales solutions (Pennylane FR, Holded ES, QuickBooks US/UK, Sage international)
* **Synchronisation automatique des factures**
* **Mapping des comptes comptables**
* **Configuration TVA**

#### Bénéfice métier

* Suppression de la double-saisie
* Conformité comptable automatique
* Différenciateur fort sur le marché européen

---

### Connexion API outils tiers (Bifacto, Holded, autres)

#### Description détaillée

Au-delà des intégrations comptables standards (une brique dédiée.10), Nidria propose une API publique permettant aux agences d'intégrer leurs outils internes custom.

Exemples concrets :

* **Domiciliation Bulgarie (Artur)** : connexion API avec Bifacto (leur outil compta interne)
* **Reside Paraguay** : connexion API avec AppSheet (leur CRM)
* **Agences custom** : webhook pour notifier d'autres outils internes

#### Verbatims qui justifient cette feature

> "Vous avez Bifacto qui est votre outil de comptabilité... il y aura une API connectée au Revolut, au Unicredit..." — **Artur Pouget (21/05/2026)**

> "L'API après, donc elle sera hébergée, elle sera externe, elle pourrait être appelée avec un système de connexion." — **Eric à Artur**

#### Sous-features incluses

* **API REST publique documentée**
* **Authentification OAuth 2.0 / API keys**
* **Webhooks pour événements clés**
* **SDK Python et Node.js**
* **Sandbox pour tests**

#### Bénéfice métier

* Intégration aux écosystèmes existants des agences
* Différenciateur fort pour les agences techniques
* Permet aux développeurs internes d'agences de personnaliser

---

### Mobile app (PWA puis natif)

#### Description détaillée

Application mobile en PWA d'abord (Progressive Web App), puis applications natives iOS et Android. Permet aux agents terrain (Alexis qui fait les courses à Asunción) d'accéder à Nidria en mobilité.

#### Sous-features incluses

* **PWA installable** (offline limité possible)
* **App iOS native** (Apple App Store)
* **App Android native** (Google Play)
* **Push notifications natives**
* **Scan document caméra** (intégration OCR)
* **Mode hors-ligne partiel** (consultation de dossiers téléchargés)

#### Bénéfice métier

* Mobilité terrain (cas Alexis)
* Adoption augmentée (les agents préfèrent souvent le mobile)
* Modernité de l'image

---

<a name="commercial-facturation-et-monetisation"></a>
## 💰 Commercial, facturation et monétisation

### Formulaire d'intake public avec double opt-in court+long

#### Description détaillée

L'agence dispose d'un formulaire public partageable via un lien (sur son site, dans ses bios LinkedIn/Instagram, dans ses mails de prospection). Le formulaire est configurable visuellement.

Deux versions du formulaire :

* **Court (3-5 questions)** : nom, mail, nationalité, projet en quelques mots, budget approximatif. Pour qualification minimale rapide.
* **Long (15-30 questions)** : tout le détail (situation familiale, patrimoine, lieu de naissance, antécédents fiscaux, projet précis, timeline, etc.). Pour les prospects qualifiés post-court.

Le double opt-in fonctionne ainsi :
1. Prospect remplit le court depuis n'importe quel canal (site, mail, LinkedIn).
2. Le prospect est créé automatiquement dans l'espace agence avec statut "Lead non qualifié".
3. L'agence reçoit une notification (mail/WhatsApp/in-app).
4. Si l'agence valide le lead comme intéressant, elle envoie au prospect le lien vers le formulaire long.
5. Une fois le long rempli, le prospect bascule en "Lead qualifié" et un dossier peut être créé.

#### Use cases concrets

* **Sidney (Expatriation.io)** : Sidney met le lien court en bio Instagram et sur ses vidéos YouTube. Quand un follower clique, il remplit le court. Sidney reçoit la notif, filtre les leads intéressants (patrimoine > 200K€), et leur envoie le long.

* **Reside Paraguay** : Eloïse remplace son formulaire Google actuel (24 questions) par le formulaire Nidria. Les réponses tombent directement dans son espace agence au lieu d'un Sheets externe.

#### Verbatims qui justifient cette feature

> "Il y aura juste un bouton à côté d'agence, un bouton, copier un lien si tu veux, en gros ça sera littéralement un lien, le formulaire que toi-même également tu pourras customiser, tu l'enverras à n'importe quel prospect... Un double opt-in. Voilà. Et la deuxième en fait ça sera un formulaire un peu plus long que la personne devra remplir." — **Sidney Lakehal (10/06/2026)**

> "Peut-être bien d'avoir une fonctionnalité où t'as un formulaire, où t'ouvres un link et il s'ouvre en HTML sur ton browser et tu peux remplir le formulaire, faire up-save et le truc il se garde directement sur le CRM pour les questions pré-voyage dans la liste de questions qu'on a." — **Alexis (12/06/2026)**

#### Sous-features incluses

* **Constructeur visuel de formulaire** : drag-and-drop des questions, types (texte, choix multiple, dropdown, upload fichier, slider, date)
* **Logique conditionnelle** : "Si nationalité = Pakistan, afficher l'alerte 'visa improbable'"
* **Validations en temps réel** (email valide, etc.)
* **Plusieurs formulaires par agence** : un par pays de destination, un par typologie client, etc.
* **Aperçu mobile/desktop**
* **Personnalisation visuelle** : logo agence, couleurs, message d'accueil
* **Anti-spam** (rate limiting, captcha)
* **Multilangue** (le formulaire peut être en plusieurs langues, choix au début)
* **Export des réponses en CSV** pour analyses externes

#### Bénéfice métier

* Pré-qualification automatique des prospects (les non-fortunés sont écartés sans effort agence)
* Réduction du temps perdu sur les leads inappropriés
* Centralisation : pas besoin de Typeform / Google Forms / Calendly externes
* Création automatique de dossier potentiel = gain de temps

---

### Générateur de devis

#### Description détaillée

L'agence configure son catalogue de services avec prix unitaires. Au moment de créer un dossier, elle sélectionne les services applicables au client et le système génère automatiquement un devis PDF.

Options avancées : packages, réductions famille, réductions multi-services, remises personnalisées.

#### Use cases concrets

* **Reside Paraguay** : Alexis a 5 packages standards (Solo Fast 2500€, Solo Standard 1800€, Famille Fast 3500€, etc.). Quand il crée un dossier, il sélectionne le package et le devis se génère.

* **Eloïse** : elle veut une "réduction famille" automatique de 15% à partir du 2e enfant. Le système calcule.

#### Verbatims qui justifient cette feature

> "GÉNÉRATEUR DE DEVIS : sélection de services à la carte côté agence → somme automatique + réduction famille en % par tête." — **Eloïse, Reside Paraguay (03/06/2026)**

> "Notre CRM Caféinesse... maintenant elle a rajouté le billing automatique. Donc, ça te fait ton invoice tout seul. Avec la TVA." — **Alexis, Reside Paraguay (12/06/2026)**

#### Sous-features incluses

* **Catalogue de services configurable** (nom, prix, durée estimée)
* **Packages prédéfinis** (Fast, Standard, Premium, etc.)
* **Réductions automatiques** (famille, multi-services, repeat client)
* **TVA / taxes locales configurables par pays**
* **Génération PDF avec branding agence**
* **Envoi automatique au client par mail**
* **Suivi de validité du devis** (30, 60, 90 jours)
* **Conversion devis → facture en un clic**

#### Bénéfice métier

* Professionnalisation des propositions commerciales
* Réduction du temps de création (de 30 min à 2 min)
* Cohérence tarifaire (plus d'erreurs de calcul)

---

### Offre en package

#### Description détaillée

L'agence regroupe plusieurs services en "packages" prédéfinis avec un prix global et des étapes/documents liés au package. Quand l'agence vend un package à un client, le parcours-type associé est appliqué automatiquement au dossier.

#### Verbatims qui justifient cette feature

> "On va maintenant offrir des offres packagées de manière à ce que les gens comprennent ce qu'on veut faire." — **Didier, Residir Portugal (3/06/2026)**

> "Selon le package, tu n'as pas les mêmes papiers à émettre." — **Alexis, Reside Paraguay (12/06/2026)**

> "Intéressant la partie package... On a également pensé à ça sur l'outil où en gros tu viens littéralement lister l'ensemble des prestations dans ce package, automatiquement ça vient à découler dans ce package." — **Eric à Didier (3/06/2026)**

#### Sous-features incluses

* **Création de packages avec services groupés**
* **Liaison package ↔ parcours-type**
* **Tarification de package** (souvent avec discount vs services séparés)
* **Marketing du package** dans le formulaire d'intake (le client choisit son package au formulaire)

#### Bénéfice métier

* Simplifie l'offre commerciale
* Augmente le panier moyen (le package premium est plus tentant)
* Le client comprend mieux ce qu'il achète

---

### Liste des prestataires avec filtres

#### Description détaillée

L'agence maintient un annuaire de ses prestataires partenaires : avocats, comptables, notaires, traducteurs, médecins, etc.

Chaque prestataire a une fiche avec :
* Nom, contact (mail, téléphone, WhatsApp)
* Catégorie (avocat / comptable / notaire / traducteur / etc.)
* Spécialité (M&A, immigration, fiscalité, etc.)
* Localisation
* Langues parlées
* Tarifs indicatifs
* Notes internes
* Historique des dossiers où ce prestataire est intervenu

Filtres disponibles : par catégorie, localisation, langue, spécialité.

#### Verbatims qui justifient cette feature

> "L'avocat il soit un peu spécialisé dans les affaires, qu'il soit suffisamment implanté dans différentes villes, qu'ils aient quelques succursales, qui soient francophones." — **Didier, Residir Portugal (3/06/2026)**

#### Sous-features incluses

* **Fiche prestataire complète**
* **Filtres multi-critères**
* **Assignation d'un prestataire à un dossier** (drag-and-drop ou clic)
* **Historique des collaborations** par prestataire
* **Tags personnalisés** (préféré, à éviter, en test)

#### Bénéfice métier

* Choix rapide du bon prestataire pour le bon client
* Mémoire institutionnelle (un dossier raté avec tel prestataire = note interne)
* Base pour les futurs partenariats formels 

---

### Suivi off post-livrable

#### Description détaillée

Quand un dossier est officiellement "terminé" (résidence obtenue), il bascule en mode "suivi off". L'agence garde la visibilité sur le client pour les suivis périodiques (renouvellements, déclarations annuelles, demandes ad hoc).

L'espace expat reste accessible au client (avec ses documents archivés). L'agence peut envoyer des rappels périodiques ("Votre résidence expire dans 6 mois").

#### Verbatims qui justifient cette feature

> "Il y a du networking qui se fait et donc j'arrive à savoir si ça se passe bien ou pas. J'ai un petit suivi mais il est plutôt off." — **Didier, Residir Portugal (3/06/2026)**

#### Sous-features incluses

* **Statut "Terminé suivi off"**
* **Archivage automatique des documents**
* **Rappels périodiques** (renouvellement, déclaration fiscale annuelle, etc.)
* **Visibilité expat conservée** (en lecture seule)
* **Réactivation facile** (transformer en dossier actif si nouvelle demande)

#### Bénéfice métier

* Fidélisation long terme du client
* Opportunités de up-sell (renouvellement, nouvelle démarche)
* Données historiques précieuses pour le client

---

### Calculateur convention fiscale

#### Description détaillée

Outil intégré qui permet à un prospect ou un agent de simuler la situation fiscale d'un expatrié selon son pays d'origine et son pays de destination. Le système pose une centaine de questions (situation familiale, patrimoine, revenus, activité, etc.) et génère un rapport personnalisé indiquant :

* Quelle convention fiscale s'applique
* Quels impôts sont dus dans chaque pays
* Quels mécanismes de non-double imposition sont applicables
* Quelles obligations déclaratives
* Estimations chiffrées d'imposition

Eric a déjà codé une grande partie de cet outil avant le lancement de Nidria.

#### Use cases concrets

* **Sidney** : un prospect français riche envisage la République tchèque. Le calculateur indique "Convention fiscale franco-tchèque applicable, imposition principale en RT au taux X, exit tax France à prévoir sur certains actifs".

* **Mathias** : un consultant français en Espagne demande "Suis-je résident fiscal espagnol ou français ?". Le calculateur tranche selon les critères de la convention.

#### Verbatims qui justifient cette feature

> "Vu que je compte également avorter là-dessus une partie convention fiscale où en gros tu mets ton pays, tu mets également où tu es, automatiquement ça te sort... en fait, à la fin, ça te sort carrément un rapport de ta situation avec toutes les lois françaises, etc. les lois belges, les lois suisses, les lois canadiennes." — **Eric (à Artur le 21/05/2026)**

#### Sous-features incluses

* **100 questions structurées** par catégorie (identité, situation familiale, patrimoine, revenus, activité, projet)
* **4 pays d'origine principaux** (France, Belgique, Suisse, Canada) + extension progressive
* **200 pays de destination supportés**
* **Génération de rapport PDF personnalisé**
* **Pull automatique des lois fiscales en vigueur** via API gouvernementales (Légifrance, etc.)
* **Mise à jour continue de la base juridique**
* **Recommandations de prestataires** (fiscalistes, comptables) selon le cas

#### Bénéfice métier

* Différenciateur ULTRA-fort : aucun outil concurrent ne propose ça
* Outil d'acquisition de leads très puissant (mis en bas de chaque mail prospection, par exemple)
* Crédibilité expert renforcée
* Possibilité de partenariats / commissions avec les fiscalistes recommandés

---

### Lead-gen avec commissions paramétrables

#### Description détaillée

Système d'échange de leads entre agences et prestataires partenaires, avec commissions configurables par l'agence cédant le lead. Important : ce système est OPTIONNEL. Les agences qui refusent par déontologie (Reside Paraguay, Residir Portugal) peuvent ne jamais l'activer.

Le système fonctionne ainsi :

1. L'agence A reçoit un lead qu'elle ne peut pas (ou ne veut pas) traiter elle-même (capacité pleine, hors-périmètre, etc.).
2. L'agence A "cède" le lead à un partenaire (autre agence, comptable, avocat) via Nidria.
3. Le partenaire reçoit une notification avec un profil anonymisé du prospect.
4. Si le partenaire accepte le lead, il peut voir le contact complet.
5. Une commission convenue est versée à l'agence A (sur facturation finale du partenaire au client).

#### Verbatims qui justifient cette feature

> "Pour des leads, je pourrais demander par lead 1000 euros." — **Sidney Lakehal (20/05/2026)**

> "Vous mettez capacité à être en groupe pour un sûr, trois personnes, trois cases. Les trois tests sont déjà remplis. Tu vois un nouveau profil qui arrive. Appuyer, renvoyer vers un contact... La personne reçoit directement un mail en mode voici un lead gratuit, par contre pour avoir accès à l'adresse mail qui est floutée, tu dois payer." — **Artur Pouget (21/05/2026)**

> "C'est pas mal, c'est pas mal parce que c'est vrai que moi, en fait, ce qui se passe, c'est que j'ai pas mal d'affiliation... Tu pourras manœuvrer ça [pourcentages par prestataire]." — **Greg (12/06/2026)**

> "Non, c'est une question de déontologie. Je ne veux pas gonfler les factures artificiellement de mes clients." — **Didier, Residir Portugal (3/06/2026) — REFUS**

> "On donne les contacts par gentillesse, on ne tire aucun profit du contact qu'on donne." — **Eloïse, Reside Paraguay (3/06/2026) — REFUS**

#### Sous-features incluses

* **Marketplace de leads** entre agences partenaires
* **Pourcentages d'affiliation paramétrables par prestataire** (Greg : "10% pour Manon, 15% pour Mathieu")
* **Profils prospect anonymisés** pour preview avant achat (verbatim Artur : "voici son patrimoine, voici son projet, c'est anonymisé")
* **Capacity management / overflow** (Artur : redirection automatique quand agence pleine)
* **Workflow d'acceptation/refus** du lead par le récepteur
* **Notification systématique à l'agence référente** quand le prestataire prend contact (douleur Greg : "Manon ne m'a pas tenu au courant")
* **Tracking de paiement** (commission versée ou en attente)
* **Configuration par contrat avec partenaires**
* **Modèle "lead gratuit + paiement pour contact" possible** (verbatim Artur)
* **Désactivation totale** pour les agences refusant ce modèle

#### Bénéfice métier

* Nouvelle source de revenus pour les agences ouvertes à ce modèle
* Capacity management : aucun lead perdu, redirection vers partenaire
* Networking professionnel intégré

#### Position des testeurs

* **POUR** : Sidney, Artur, Greg
* **CONTRE** (par déontologie) : Didier, Eloïse, Reside Paraguay
* **À découvrir** : Mathias, Em+Perle, autres futurs testeurs

Cohérence : feature optionnelle, jamais imposée.

---

### Espace pros externes avec login dédié

#### Description détaillée

Les prestataires partenaires de l'agence (avocats, notaires, comptables, banques, médecins) disposent de leur propre login sur Nidria. Ils accèdent uniquement aux dossiers où ils sont assignés.

Le prestataire voit :
* La liste des dossiers où il intervient
* Les étapes spécifiques qui le concernent
* Les documents à fournir / valider
* La timeline du client
* Un canal de communication avec l'agence (notes ou tchat)

#### Verbatims qui justifient cette feature

> "Il y avait un espace expatrié, ou alors un espace PME, ou un espace en structure, un espace agence, après un espace prestataire." — **Eric à Didier (3/06/2026)**

> "Avec Nidria, l'avocat partenaire pourra carrément faire des photocopies et tout ce qu'il faut, il y aura un espace prestataire avec login pour les avocats, notaires, comptables." — **Eric**

#### Sous-features incluses

* **Type d'utilisateur "Prestataire"** distinct d'agence et expat
* **Visibilité restreinte aux dossiers assignés**
* **Possibilité de valider/refuser des étapes** côté prestataire
* **Upload de documents** par le prestataire
* **Canal de communication agence ↔ prestataire**
* **Notifications dédiées**
* **Facturation client via Nidria** (option pour le prestataire)
* **Tarification spécifique pour les prestataires** (moins cher que l'agence)

#### Bénéfice métier

* Coordination tripartite agence-client-prestataire fluide
* Le prestataire est partie prenante du process, pas externe
* Réduction des allers-retours mail / WhatsApp
* Différenciation forte (peu d'outils gèrent cette dimension)

---

### Affiliations banking (Mercury, Panama, Gibraltar, Iles Vierges britanniques)

#### Description détaillée

Catalogue de partenaires banking proposé à l'agence. Quand un client a besoin d'ouvrir un compte (USA, Panama, Gibraltar, BVI, Suisse, Émirats, etc.), l'agence peut le rediriger vers un partenaire banking via Nidria. Si le compte est ouvert, l'agence touche une commission.

#### Verbatims qui justifient cette feature

> "Pour les banques américaines style mercury eux ils ont de la filiation, les banques Panama ils en ont aussi, les banques Gibraltar en ont, Ile Vierge britannique ils en ont aussi donc éventuellement proposer toutes ces solutions là aux clients." — **Alexis, Reside Paraguay (12/06/2026)**

#### Sous-features incluses

* **Catalogue de partenaires banking** par juridiction
* **Workflow d'introduction du client**
* **Tracking de la commission**
* **Conditions d'éligibilité du client par banque**

#### Bénéfice métier

* Service complémentaire de valeur
* Nouvelle source de revenus pour l'agence
* One-stop-shop pour l'expat

---

### Affiliations services touristiques (city tour, location voiture)

#### Description détaillée

Quand le client arrive dans un nouveau pays, il a besoin de services touristiques annexes : location de voiture, city tour, hôtel, restaurant, etc. L'agence peut proposer des partenaires via Nidria et toucher une commission.

#### Verbatims qui justifient cette feature

> "Le client arrive, alors, ce serait bien de m'organiser un petit city tour pendant que je suis en train de faire ma résidence, parce que nous, on les prend le premier jour où ils arrivent ou le deuxième... ils ont un gap de deux à trois jours pendant la semaine où ils sont libres. Éventuellement, il y en a, ils veulent louer une voiture pour aller se balader." — **Alexis, Reside Paraguay (12/06/2026)**

> "Mais si tu as un CRM qui s'en occupe et que c'est assez smooth, pourquoi pas monétiser là-dessus aussi." — **Alexis**

#### Sous-features incluses

* **Catalogue de services touristiques par ville**
* **Intégration Booking / Airbnb / Hertz / Avis / GetYourGuide**
* **Tracking de la commission**
* **Suggestion contextuelle** dans la timeline (entre l'étape A et B, le client a 2 jours libres → proposer city tour)

#### Bénéfice métier

* Up-sell automatique
* Expérience client premium
* Revenu complémentaire

---

### Billing automatique avec génération invoice + TVA

#### Description détaillée

À la complétion d'un dossier (ou par étape facturable), le système génère automatiquement une facture pour le client avec :
* Le détail des services rendus
* Le calcul TVA selon les règles du pays de l'agence
* Les conditions de paiement
* Branding agence

La facture est envoyée au client par mail, archivée dans le dossier, et peut être exportée vers l'outil comptable de l'agence (une brique dédiée.10).

#### Verbatims qui justifient cette feature

> "Notre CRM Caféinesse... maintenant elle a rajouté le billing automatique. Donc, ça te fait ton invoice tout seul. Avec la TVA." — **Alexis, Reside Paraguay (12/06/2026)**

#### Sous-features incluses

* **Génération automatique de factures**
* **Configuration TVA par agence et par type de service**
* **Numérotation automatique**
* **Multi-devises**
* **Envoi automatique au client**
* **Tracking de paiement** (intégration Stripe, Wise, virement)
* **Relance auto si paiement non reçu**

#### Bénéfice métier

* Automatisation de la facturation (gain énorme de temps)
* Conformité comptable
* Cash-flow amélioré (relances auto)

---

<a name="pilotage-ia-et-migration"></a>
## 📊 Pilotage, IA et migration

### Dashboard analytique enrichi

#### Description détaillée

L'agence dispose d'un dashboard avec métriques clés :

* Taux de conversion par étape (combien de prospects passent du court au long ?)
* Délais moyens par étape (combien de jours en moyenne pour récupérer un casier judiciaire ?)
* Performance par agent (qui ferme le plus de dossiers ?)
* Statistiques par pays/nationalité (d'où viennent les clients ?)
* Revenus par dossier / par mois
* Taux d'abandon par étape (où perd-on les clients ?)

#### Sous-features incluses

* **Métriques pré-définies**
* **Vues graphiques** : courbes, barres, donuts
* **Export CSV des métriques**
* **Comparaisons temporelles** : ce mois vs le mois dernier
* **Drill-down** : cliquer sur une métrique pour voir les dossiers sous-jacents

#### Bénéfice métier

* Pilotage data-driven de l'agence
* Identification des goulets d'étranglement
* Décisions d'investissement basées sur données

---

### Alertes intelligentes

#### Description détaillée

Le système monitore en arrière-plan les dossiers et émet des alertes proactives :

* Dossier en retard sur une étape (estimation dépassée)
* Dossier inactif depuis X jours (client n'a rien fait)
* Document expiré (apostille vieille de 6 mois alors que le max est 6 mois)
* Document de mauvais format
* Étape bloquée par un prérequis qui traîne

#### Verbatims qui justifient cette feature

> "Ce serait un autre outil d'un autre côté qui regarderait un peu les dates des documents où tu pourrais mettre les dates de venue du client et qui vérifie automatiquement que les apostilles et les documents soient signés déjà d'une et de deux qui soient dans les bonnes dates pour les dépôts de dossiers." — **Alexis, Reside Paraguay (12/06/2026)**

#### Sous-features incluses

* **Configuration des seuils d'alerte par agence** (5 jours d'inactivité ? 10 jours ?)
* **Niveaux de criticité** (info, warning, urgent, bloquant)
* **Canal d'alerte** (in-app, mail, WhatsApp)
* **Snooze des alertes** (reporter)
* **Auto-résolution** : si la cause de l'alerte disparaît, l'alerte se résout d'elle-même

#### Bénéfice métier

* Anticipation des problèmes au lieu de les subir
* Évite les apostilles périmées au moment du dépôt

---

### Wiki interne / base de connaissances par agence

#### Description détaillée

Chaque agence dispose d'un espace wiki où elle documente :

* Procédures internes par pays de destination
* Check-lists types
* Notes consulaires
* Tarifs officiels
* Adresses utiles
* Contacts gouvernementaux
* FAQ internes

Le wiki est accessible à toute l'équipe agence. Il peut être structuré en pages et sous-pages.

#### Sous-features incluses

* **Éditeur riche markdown / WYSIWYG**
* **Hiérarchie de pages**
* **Recherche full-text**
* **Liens internes entre pages**
* **Upload de fichiers (PDF de référence)**
* **Permissions de lecture** (certaines pages admin uniquement)

#### Bénéfice métier

* Onboarding accéléré des nouveaux agents
* Centralisation de la connaissance institutionnelle
* Diminution des "comment on fait pour X déjà ?"

---

### Import depuis top CRM (AppSheet, Pipedrive, Notion, HubSpot, Excel)

#### Description détaillée

L'agence qui vient d'un autre outil peut importer ses dossiers existants en quelques clics. Système d'import multi-formats :

* AppSheet (export Google Sheets)
* Pipedrive (export CSV)
* HubSpot (export CSV)
* Notion (export CSV ou markdown)
* Excel (.xlsx, .xls)
* CSV générique

L'utilisateur uploade son fichier, mappe les colonnes (colonne "Client name" du CSV → champ "Nom" de Nidria), et lance l'import. Les dossiers existants sont créés avec leur historique.

#### Use cases concrets

* **Reside Paraguay** : Inès a 50 dossiers actifs dans AppSheet. Elle exporte en Google Sheets, importe dans Nidria, mappe les colonnes. En 30 minutes, tous les dossiers sont migrés.

* **Mathias** : Mathias a 50 dossiers actifs dans Holded ou autre outil espagnol. Export CSV, import Nidria.

#### Verbatims qui justifient cette feature

> "Est-ce qu'il n'y aurait pas moyen de faire une fonctionnalité pour faire du client migration d'une plateforme à une autre ?" — **Alexis, Reside Paraguay (12/06/2026)**

> "On va faire l'effort de le faire, mais... [migration friction]." — **Alexis**

> "Si, c'est prévu. Ce qui est bien, c'est que maintenant, l'ensemble des CRM proposent des systèmes d'export au niveau des lignes. On va créer une fonctionnalité type qu'importe le format d'export de ton CRM. On va prendre, je pense, le top 5. Parce que tu sais que ton ennemi numéro un, c'est quand même la friction." — **Eric (12/06/2026)**

#### Sous-features incluses

* **Détection automatique du format** (par extension et structure)
* **Mapping visuel des colonnes** (drag-and-drop CSV → champs Nidria)
* **Aperçu avant import** (10 premières lignes pour vérifier)
* **Gestion des doublons** (skip / merge / overwrite)
* **Import par batchs** (gros fichiers)
* **Logs d'import détaillés** (combien réussis, combien échoués et pourquoi)
* **Possibilité de rollback** (annuler l'import en cas d'erreur)
* **Migration des documents associés** (PDF / images si liens fournis)

#### Bénéfice métier

* Suppression de la friction principale d'adoption (le "je dois ressaisir 50 dossiers")
* Onboarding rapide même pour les agences avec 100+ dossiers existants
* Différenciateur fort vs les outils qui ne proposent pas d'import

---

**une brique dédiée ouvre l'écosystème (prestataires externes avec login dédié), pousse les automatisations IA (résumés, traductions, OCR avancé), introduit la monétisation cross-agences (lead-gen avec affiliations paramétrables) et les modules lourds (signature électronique, calculateur fiscal).**

---

### Résumé automatique des dossiers via IA

#### Description détaillée

Chaque dossier peut être résumé en 3-5 lignes par une IA (Claude Sonnet). L'agent qui prend un dossier en cours de route (remplacement, après congés, transfert) lit le résumé en 30 secondes au lieu de fouiller l'historique pendant 15 min.

Le résumé est généré sur demande (bouton "Résumer") et peut être actualisé.

#### Sous-features incluses

* **Bouton "Résumer ce dossier"**
* **Résumé multi-niveau** (1 ligne, 3 lignes, 1 paragraphe)
* **Inclusion des actions en attente**
* **Multilingue**

#### Bénéfice métier

* Onboarding ultra-rapide d'un nouveau dossier
* Continuité de service en cas d'absence
* Réunions d'équipe efficaces

---

### Suivi analytique avancé

#### Description détaillée

Extension du dashboard une brique dédiée avec analyses plus poussées :

* Cohort analysis (combien de clients de cette cohorte sont allés au bout ?)
* Funnels détaillés
* Prédictions (à ce rythme, combien de dossiers fermés ce mois ?)
* Performance par typologie de client
* Saisonnalité de l'activité

#### Bénéfice métier

* Compréhension business profonde
* Décisions stratégiques data-driven

---

<a name="hors-scope"></a>
## 🚫 Hors-scope (jamais dans Nidria)

* **Compta interne agence** (paiements salariés, marges, charges) — autre produit
* **Suivi cashflow agence** — autre produit
* **Gestion devises pour comptabilité agence** — autre produit
* **CRM marketing classique** (campagnes mail nombreuses, segmentation marketing, lead scoring marketing-driven) — pas notre cœur
* **Services juridiques / fiscaux directs** — Nidria fournit l'outil, les conseils sont donnés par les pros (avocats, fiscalistes) via l'espace prestataires
* **Compétition frontale avec les CRM généralistes** (AppSheet, Notion, Trello, Airtable, HubSpot) — Nidria est complémentaire, pas remplaçant pour les usages génériques
* **Prestations administratives directes** — Nidria coordonne, ce sont les agences/prestataires qui prestent

---

<a name="validation-marche"></a>
## 📊 Validation marché — Verbatims clés

### Validation TAM

> "Les trois quarts des agences font ça à la main, je ne connais personne qui fasse de l'OCR." — **Alexis (12/06/2026)**

> "Il y a des boîtes où ils ont 40 employés et il y en a une dizaine qui font de l'administratif." — **Alexis (12/06/2026)**

> "Il y en a, ils ont des cahiers, ils notent les trucs sur des cahiers." — **Inès Heu (12/06/2026)**

> "Je pense qu'il y a peut-être entre 30 et 40 % des agences qui ne sont pas des papilles et à eux, ça pourrait leur servir." — **Alexis (12/06/2026)**

### Validation produit

> "La clé, je pense, c'est l'autonomie du client. Chose qui n'existe pas dans ce type de service. C'est Game Changer. C'est x100." — **Artur Pouget (21/05/2026)**

> "Pour moi c'est l'humain avant l'humain." — **Sidney Lakehal (10/06/2026)**

> "Tu vas avoir un cadre de structuration de l'action, mais si c'est pas agile à l'intérieur du cadre, le cadre va vite devenir trop contraignant." — **Didier, Residir Portugal (3/06/2026)**

> "C'est exactement ce que je voudrais... Ça me fait penser à Doctolib." — **Greg (12/06/2026)**

> "Si tu as un CRM qui te règle pas mal de tes problèmes, je pense qu'il y a plein de gens à qui ça peut servir." — **Alexis (12/06/2026)**

> "The most useful tool until now is just a simple date counter to calculate the deadline." — **Inès Heu (12/06/2026)**

### Validation pricing

> "80 balles par mois pour un seul utilisateur, c'est que dalle." — **Greg (12/06/2026)**

### 7 typologies cibles

1. **Agences d'expatriation hub** (Reside Paraguay, Sidney Expatriation.io, Domiciliation Bulgarie, Greg Horizons Possibles, Smart Traveller, Residir Portugal)
2. **Cabinets d'avocats spécialisés international** (DS Avocats type, M&A cross-border, IP, immigration corporate)
3. **Expertise comptable / fiduciaire** (In Extenso, MonEntreprise.es, fiduciaires Suisse/Lux)
4. **Conseil en patrimoine international**
5. **Immobilier expat / Golden Visa**
6. **Visa et immigration spécialisé**
7. **Services tax / fiscalité internationale**

---

<a name="pricing"></a>
## 💶 Pricing

### Plans

* **Free trial** : 3 mois gratuits pour les testeurs early adopters (sans carte de crédit, sans engagement)
* **Standard** : 80 EUR / agent / mois (multi-devises Stripe)

### Multi-devises

| Cible géographique | Devise | Montant |
|--------------------|--------|---------|
| France / UE / Suisse | EUR | 80 €/agent/mois |
| US / Canada / Amériques | USD | ~89 USD/agent/mois |
| Émirats / Asie / autres | USD | ~89 USD/agent/mois |
| Royaume-Uni | GBP | ~68 £/agent/mois |

### Offres testeurs

* 3 mois gratuits
* Code de réduction à vie sur le tarif standard
* Renouvelable automatique avec opt-out à tout moment
* Tarification préférentielle négociable à la sortie des 3 mois pour les early adopters

### Modèles de revenus complémentaires

* **Lead-gen / commissions** : commission par lead/affiliation entre agences et prestataires
* **Affiliations banking** : commission par compte ouvert
* **Affiliations services touristiques** : commission par réservation
* **Marketplace de parcours-types** (à long terme) : revenu sur partage de templates

---

## 🎯 Synthèse pitch oral 30 secondes

> "Nidria, c'est la plateforme SaaS dédiée aux cabinets qui orchestrent des dossiers documentaires complexes à l'international. Que vous soyez agence d'expatriation, cabinet d'avocats, fiduciaire ou conseil en patrimoine, on vous donne un espace centralisé avec timeline partagée côté client, rappels customisables avec validation manuelle, étapes verrouillées par prérequis, et un éditeur de workflow vierge clic-glisser pour que vous configuriez votre propre process. On y ajoute multilangue, OCR, intégrations WhatsApp Business, génération automatique de PDFs administratifs, et un écosystème complet pour les prestataires partenaires. Pricing 80 EUR par utilisateur. Le client expat ne paie rien."

---

*Document créé le 12 juin 2026 (v4.0 — spec exhaustive par pôles fonctionnels, zéro hiérarchie de version).*
