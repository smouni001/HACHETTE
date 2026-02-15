# STD - Gestion NUFAC par classe TRACO

## 1. Objet
Mettre en place une numerotation facture dissociee par classe `TRACO`, afin d'eviter un comptage global intercale.

Exemple attendu:
1. `TRACO='0'` => `NUFAC=1`
2. `TRACO='1'` => `NUFAC=90001`
3. `TRACO='0'` => `NUFAC=2`

## 2. Perimetre
Programme concerne:
1. `IDP470RA.pli` (generation et propagation du numero facture)

Modules lies (lecture du numero courant uniquement):
1. `IDP470B2.pli` (messages d'anomalie)

## 3. Contexte technique actuel
1. Les ruptures de traitement sont pilotees par la cle d'entree (`RUPT1..RUPT10` / `ZRUPT1..ZRUPT10`) dans `SPLAFAC`.
2. Le numero facture courant `WNUFAC` est actuellement unique et incremente au point central de creation facture.
3. `WNUFAC` est ensuite diffuse dans les sorties (`ENTET70.REFFA`, journal, index pages, fichiers demat, fichier stock demat, etc.).

Conclusion: la numerotation est decouplee du calcul des ruptures.

## 4. Besoin fonctionnel
1. Conserver un seul passage du fichier d'entree.
2. Avoir 2 sequences distinctes:
1. `TRACO='0'`: sequence dediee a partir de `1`.
2. `TRACO<>'0'`: sequence dediee a partir de `90001`.
3. Conserver les mecanismes de rupture existants sans modification.

## 5. Solution retenue
Approche "2 compteurs en parallele":
1. `WNUFAC_T0` pour `TRACO='0'`, initialise a `0`.
2. `WNUFAC_AUT` pour `TRACO<>'0'`, initialise a `90000`.
3. `WNUFAC` reste le numero courant de compatibilite aval.
4. Increment conditionnel au point unique de creation facture.

## 5 bis. Comparatif des 2 solutions les plus fiables

### 5 bis.1 Solution A - Double comptage en parallele (retenue)
Principe:
1. Conserver un seul run et un seul passage du fichier d'entree.
2. Creer deux compteurs internes (`WNUFAC_T0`, `WNUFAC_AUT`) et alimenter `WNUFAC` selon `TRACO`.
3. Laisser inchanges les formats de sortie et les structures existantes.

Fiabilite:
1. Faible surface de changement (principalement `A010D` et `A030D1`).
2. Ruptures metier preservees (`IRAFAC` / cles de rupture inchanges).
3. Exploitation batch stable (pas de nouvelle orchestration complexe).

### 5 bis.2 Solution B - Separation en 2 flux / 2 runs
Principe:
1. Decouper l'entree en 2 sous-flux (`TRACO='0'` et `TRACO<>'0'`).
2. Executer le traitement une premiere fois pour `TRACO='0'`, puis une seconde fois pour `TRACO<>'0'`.
3. Consolider les sorties si necessaire pour consommation aval.

Fiabilite:
1. Fiable d'un point de vue numerotation (chaque run a son espace de numerotation).
2. Plus sensible en exploitation (split, double run, suivi, eventuelle fusion).
3. Risque plus eleve de divergence entre flux en cas d'incident ou de reprise partielle.

### 5 bis.3 Difference d'implementation
| Axe | Solution A - Double comptage | Solution B - 2 flux / 2 runs |
|---|---|---|
| Zone de changement principale | Code PL/I (`IDP470RA`) | JCL + ordonnancement + eventuelle fusion |
| Impact JCL | Faible | Eleve |
| Ordre de traitement global | Conserve | Peut etre modifie selon split/merge |
| Risque de regression fonctionnelle | Faible a moyen | Moyen a fort |
| Effort de tests d'integration | Modere | Important |
| Cout d'exploitation quotidien | Faible | Plus eleve |

### 5 bis.4 Pourquoi la solution A est retenue
1. Elle respecte l'architecture existante: un seul passage, un seul point de numerotation, aucune rupture de flux batch.
2. Elle minimise le risque: peu de lignes modifiees, pas de changement de format fichier, pas de mecanisme de fusion.
3. Elle simplifie la maintenance: la regle metier est centralisee dans `A030D1` et reste lisible.
4. Elle limite le cout projet: moins d'impacts JCL, moins de tests systeme transverses, mise en production plus sure.
5. Elle repond au besoin metier immediat: sequences distinctes par classe `TRACO` sans impacter la gestion des ruptures.

## 6. Regles de gestion
1. Si `WTFAC.TRACO='0'`:
1. Incrementer `WNUFAC_T0`.
2. Affecter `WNUFAC = WNUFAC_T0`.
3. Plage autorisee: `00001..90000`.
2. Si `WTFAC.TRACO<>'0'`:
1. Incrementer `WNUFAC_AUT`.
2. Affecter `WNUFAC = WNUFAC_AUT`.
3. Plage autorisee: `90001..99999`.
3. En cas de depassement de plage:
1. Message d'erreur via `MACMES3`.
2. Arret controle du traitement (`PLIRETC(15)` puis sortie programme).

## 7. Modifications de code

### 7.1 Variables a ajouter
Dans la zone des variables internes de `IDP470RA.pli`, a proximite de `WNUFAC`:

```pli
DCL WNUFAC_T0  DEC FIXED(5,0) INIT(0);      /* TRACO='0'   */
DCL WNUFAC_AUT DEC FIXED(5,0) INIT(90000);  /* TRACO<>'0'  */
```

### 7.2 Initialisation run
Dans `A010D`, bloc d'initialisation execute une seule fois en debut de run:

```pli
IF WEDIPA = ' ' THEN DO;
   WNUFAC                  = 0;
   WNUFAC_T0               = 0;
   WNUFAC_AUT              = 90000;
   XNUFAC1,XNUFAC2,XNUFAC3 = 0;
END;
```

### 7.3 Point unique de numerotation facture
Remplacer le bloc actuel:

```pli
WNUFAC        = WNUFAC + 1;
ENTET70.REFFA = WNUFAC;
WNUFAC_5      = WNUFAC;
```

Par:

```pli
IF WTFAC.TRACO = '0' THEN DO;
   IF WNUFAC_T0 >= 90000 THEN DO;
      ZONCTL = '*** DEPASSEMENT NUFAC TRACO=0 (MAX 90000) ***';
      CALL MACMES3(DESTI,'2',ZONCTL);
      CALL PLIRETC(15);
      GO TO A000F;
   END;
   WNUFAC_T0 = WNUFAC_T0 + 1;
   WNUFAC    = WNUFAC_T0;
END;
ELSE DO;
   IF WNUFAC_AUT >= 99999 THEN DO;
      ZONCTL = '*** DEPASSEMENT NUFAC TRACO<>0 (MAX 99999) ***';
      CALL MACMES3(DESTI,'2',ZONCTL);
      CALL PLIRETC(15);
      GO TO A000F;
   END;
   WNUFAC_AUT = WNUFAC_AUT + 1;
   WNUFAC     = WNUFAC_AUT;
END;

ENTET70.REFFA = WNUFAC;
WNUFAC_5      = WNUFAC;
```

## 8. Impacts attendus
1. Ruptures (`IRAFAC`) : aucun impact fonctionnel.
2. Sorties aval : aucun changement de format, seul le numero evolue selon la classe TRACO.
3. Ordre global des numeros : non strictement croissant au global (normal et attendu).

## 9. Capacite et limites
1. Classe `TRACO='0'` : 90 000 factures max par run.
2. Classe `TRACO<>'0'` : 9 999 factures max par run.
3. Limite due au format 5 digits present dans plusieurs structures (`NUFAC`, `REFFA`, `WNUFAC_5`).

## 10. Strategie de tests

### 10.1 Tests fonctionnels
1. Cas simple `TRACO='0'`: numeros `1,2,3`.
2. Cas simple `TRACO<>'0'`: numeros `90001,90002,90003`.
3. Cas alterne: `0,1,0,2,0` => `1,90001,2,90002,3`.
4. Cas `TRACO=' '` (blanc): traite comme `TRACO<>'0'`.

### 10.2 Tests non regression
1. Verifier les ruptures par comparaison run avant/apres sur meme jeu d'entree.
2. Verifier les fichiers:
1. journal (`FEDITI`)
2. index pages (`FIPAGE`)
3. demat (`FICDEMA`)
4. stock demat (`FICSTOD`)
5. flux PCF (`IDDPCF*`)

### 10.3 Tests de limites
1. Forcer `WNUFAC_T0=90000` puis creation facture `TRACO='0'` => anomalie + arret controle.
2. Forcer `WNUFAC_AUT=99999` puis creation facture `TRACO<>'0'` => anomalie + arret controle.

## 11. Criteres d'acceptation
1. Absence de comptage global intercale entre classes TRACO.
2. Absence de regression sur la gestion des ruptures.
3. Coherence du numero facture sur tous les fichiers de sortie.
4. Gestion explicite des depassements de plages.

## 12. Deploiement
1. Compiler `IDP470RA`.
2. Executer la batterie de tests fonctionnels et non regression.
3. Valider en pre-prod sur un run representatif volumetrique.
4. Mettre en production.

## 13. Plan de repli
1. Restaurer la version precedente du module.
2. Rejouer le run impacte.
3. Ouvrir incident de capacite si depassement de plage constate.

## 14. Evolutions futures recommandees
1. Si la volumetrie `TRACO<>'0'` depasse regulierement 9 999 factures, lancer une etude d'extension de format `NUFAC` au-dela de 5 digits.
2. En alternative, migrer vers une sequence persistante par classe (VSAM/DB2) avec gestion restart fine.
