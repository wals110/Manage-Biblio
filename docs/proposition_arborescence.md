# Proposition d'arborescence — Bibliothèque BIBLIO

## Vue d'ensemble

```
BIBLIO/
├── 01-SCIENCES/
│   ├── INFORMATIQUE/
│   │   ├── 01-Fondamentaux-CS/
│   │   ├── 02-Algorithmes-Structures-Donnees/
│   │   ├── 03-Langages-Programmation/
│   │   │   ├── C-Cpp-CSharp/
│   │   │   ├── Java/
│   │   │   ├── Python/
│   │   │   ├── JavaScript-Web/
│   │   │   └── Autres/  (Delphi, Erlang, Objective-C, ActionScript...)
│   │   ├── 04-Genie-Logiciel/
│   │   ├── 05-Intelligence-Artificielle/
│   │   │   ├── Machine-Learning/
│   │   │   ├── Deep-Learning/
│   │   │   ├── NLP/
│   │   │   └── Vision-par-Ordinateur/
│   │   ├── 06-Data-Science/
│   │   │   ├── Big-Data/
│   │   │   └── Visualisation/
│   │   ├── 07-Bases-de-Donnees/
│   │   ├── 08-Reseaux-Telecom/
│   │   ├── 09-Systemes-OS/
│   │   │   ├── Linux-Unix/
│   │   │   ├── Windows/
│   │   │   └── MacOS-iOS/
│   │   ├── 10-Securite-Crypto/
│   │   ├── 11-Cloud-DevOps/
│   │   ├── 12-IoT-Embarque/  (Arduino, Raspberry Pi...)
│   │   ├── 13-Infographie-3D/  (GPU, OpenGL, Blender, 3ds Max...)
│   │   ├── 14-Developpement-Web/
│   │   ├── 15-Developpement-Mobile/
│   │   ├── 16-Jeux-Video/
│   │   │   ├── Game-Dev/  (moteurs, programmation de jeux)
│   │   │   └── Strategy-Guides/  (Final Fantasy, Dark Souls...)
│   │   └── 17-Conferences-Proceedings/  (actes de conférences Springer/IEEE)
│   │
│   ├── MATHEMATIQUE/
│   │   ├── 01-Algebre/
│   │   ├── 02-Analyse/
│   │   ├── 03-Geometrie-Topologie/
│   │   ├── 04-Probabilites-Statistiques/
│   │   ├── 05-Logique/
│   │   ├── 06-Theorie-des-Nombres/
│   │   ├── 07-Calcul-Numerique/
│   │   ├── 08-Mathematiques-Generales/  (manuels L1-Agreg, exercices...)
│   │   └── 09-Histoire-Philosophie-Maths/
│   │
│   ├── PHYSIQUE/
│   │   ├── 01-Mecanique/
│   │   ├── 02-Electromagnetisme-Electronique/
│   │   ├── 03-Thermodynamique/
│   │   ├── 04-Optique/
│   │   ├── 05-Relativite-Quantique/
│   │   ├── 06-Astrophysique-Cosmologie/
│   │   └── 07-Physique-Nucleaire/
│   │
│   ├── CHIMIE/
│   │   ├── 01-Chimie-Organique/
│   │   ├── 02-Chimie-Physique/
│   │   └── 03-Nanotechnologie/
│   │
│   ├── BIOLOGIE/  (si fichiers existants)
│   │
│   └── EPISTEMOLOGIE/
│
├── 05-RELIGIONS/
│
└── 08-LOISIRS/
    ├── DESSIN/
    │   ├── Fondamentaux/
    │   ├── Portrait-Figure/
    │   ├── Paysage/
    │   └── Techniques-Materiaux/
    ├── ECHECS/
    └── JEUX-VIDEO/  (guides de stratégie déplacés depuis INFORMATIQUE?)
```


## Détails et justifications

### INFORMATIQUE — la catégorie clé

L'enjeu principal est de fusionner deux sources :
- Les **~76 fichiers à la racine** d'INFORMATIQUE (en vrac)
- Les **~755 fichiers dans `Computers & Technology/`** (déjà organisés par Amazon/Springer)
- Et les fichiers de `Science & Math` qui relèvent de l'informatique

#### Correspondances entre l'existant et la nouvelle structure

| Existant (Computers & Technology)        | Nouveau dossier                      | ~Fichiers |
|------------------------------------------|--------------------------------------|-----------|
| Computer Science (331)                   | Réparti entre plusieurs sous-dossiers| 331       |
| Programming (128)                        | 04-Genie-Logiciel + langages         | 128       |
| Networking & Cloud Computing (91)        | 08-Reseaux-Telecom + 11-Cloud-DevOps | 91        |
| Hardware & DIY (44)                      | 12-IoT-Embarque + 01-Fondamentaux    | 44        |
| Operating Systems (35)                   | 09-Systemes-OS                       | 35        |
| Programming Languages (26)              | 03-Langages-Programmation             | 26        |
| Databases & Big Data (22)               | 07-Bases-de-Donnees + 06-Data-Science | 22        |
| Games & Strategy Guides (20)            | 16-Jeux-Video                         | 20        |
| Certification (20)                       | 10-Securite-Crypto (majorité CISSP)   | 20        |
| Software (12)                           | 04-Genie-Logiciel (Office, etc.)      | 12        |
| Web Development & Design (10)           | 14-Developpement-Web                  | 10        |
| Graphics & Design (7)                   | 13-Infographie-3D                     | 7         |
| Internet & Social Media (4)             | 10-Securite-Crypto / 14-Dev-Web       | 4         |
| ML flashcards (44) + ML (4)             | 05-Intelligence-Artificielle          | 48        |
| Racine INFORMATIQUE (28)                | Répartis selon contenu                | 28        |

#### Le cas des 331 fichiers "Computer Science"

C'est le gros morceau. Ce sont majoritairement des **actes de conférences** (Springer LNCS 2016-2018).
Je propose de créer `17-Conferences-Proceedings/` pour les y regrouper, car :
- Ce sont des recueils multi-thèmes (AI + réseaux + bio-info dans un même volume)
- Les classer individuellement serait artificiel
- On peut les sous-organiser par année si besoin

Les quelques livres individuels de cette catégorie (encyclopédies, manuels) iront dans leur sous-discipline.

### MATHEMATIQUE

Beaucoup de fichiers ont des noms pauvres (codes numériques, "poly.dvi.pdf", etc.).
Le script devra analyser le contenu PDF pour les classer. Ceux qui résistent iront dans
`08-Mathematiques-Generales/`.

### PHYSIQUE

33 fichiers assez hétérogènes. Quelques-uns sont mal classés :
- "Cryptographie.pdf" → devrait aller en INFORMATIQUE/10-Securite-Crypto
- "Modélisation des objets solides" → INFORMATIQUE/13-Infographie-3D
- "The SuperCollider book" → 08-LOISIRS (c'est un logiciel de musique)

### Science & Math (128 fichiers à fusionner)

Ces fichiers sont actuellement dans un sous-arbre séparé et doivent être redistribués :
- Mathematics (75) → MATHEMATIQUE/
- Physics (23) → PHYSIQUE/
- Chemistry (14) → CHIMIE/
- Astronomy (12) → PHYSIQUE/06-Astrophysique-Cosmologie/
- Evolution (2) → BIOLOGIE/ (à créer)


## Stratégie d'automatisation

### Phase 1 — Classification par mots-clés (rapide, fiable)
Fichiers avec des noms explicites → classement par règles de mots-clés.
Couvre environ 60% des fichiers.

### Phase 2 — Classification par extraction PDF
Pour les fichiers aux noms pauvres → extraction du titre, table des matières,
premiers paragraphes → classification par NLP/mots-clés enrichis.

### Phase 3 — Validation manuelle assistée
Liste des fichiers non classés avec suggestion → l'utilisateur valide ou corrige.
Possibilité de générer un CSV interactif.
