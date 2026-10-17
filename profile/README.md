# jvh-lab – projektialusta

Juha Väyrysen projektialusta: uusi projekti syntyy yhdellä komennolla agenttivalmiina, ja pohjan parannukset leviävät jo luotuihin projekteihin `copier update` -PR:inä. Repot ovat yksityisiä; tämä sivu on julkinen hakemisto.

## Katalogi

| Repo | Tarkoitus | Kerros | Versio | Käyttöönotto |
|---|---|---|---|---|
| [project-template](https://github.com/jvh-lab/project-template) | Copier-pohja: menetelmä, agenttisäännöt, visio → PRD → ADR → tiketti, testiportti, CI. Profiilit software (python/none) ja research | projekti | v0.2.0 | `bin\new-project.ps1 -Name … -Purpose …` tai `uvx --from copier==9.19.1 copier copy --trust gh:jvh-lab/project-template <kohde>` |
| [agent-pipeline](https://github.com/jvh-lab/agent-pipeline) | Moottori: tiketti → toteuttaja → erillinen katselmoija → kuitti → PR → merge, omassa worktreessä | ajo | v0.4.0 | `pwsh -File launch\claude-leaf.ps1 -Repo <projekti> -Leaf <ID> -TaskFile docs\work\<ID>.md` |
| [agent-foundation](https://github.com/jvh-lab/agent-foundation) | Kerros 1 asentajan perusta (`install/`), kerros 2 skillit ja toimintatieto (`skills/`, `knowledge/`) | kone | v0.2.0 | `.\install.ps1` |

Arkistoitu: [work-method](https://github.com/jvh-lab/work-method) ja [agent-baseline](https://github.com/jvh-lab/agent-baseline) (yhdistetty project-templateen 17.10.2026).

Topicit: `template`, `copier-template`, `agent-ready`, `agent-pipeline`, `agent-skills`.

## Säännöt

- Jokaisessa repossa README, AGENTS.md, CHANGELOG (Keep a Changelog) ja SemVer-tagit `vX.Y.Z`.
- Pohjaa ei päivitetä projektissa käsin: korjaus tehdään project-templateen ja tuodaan `copier update` -PR:nä (`bin\update-projects.ps1`).
- Projekti puhuu moottorille vain sopimustiedostoilla: `scripts/verify.ps1`, `leaf.config.json`, `docs/work/<ID>.md`, `.reviews/`, `docs/gates.md`.
- Salaisuuksia ei ole missään repossa. Ne ovat 1Passwordissa ja luetaan ajonaikaisesti.
