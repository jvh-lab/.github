# jvh-lab — aloituspohjien marketti

Juha Väyrysen uudelleenkäytettävät projektipohjat ja agenttisäännöt. Tavoite: uusi projekti syntyy yhdellä komennolla agenttivalmiina, ja pohjan korjaukset leviävät jo luotuihin projekteihin (`copier update`).

## Katalogi

| Repo | Tarkoitus | Tila | Käyttöönotto |
|---|---|---|---|
| [agent-baseline](https://github.com/jvh-lab/agent-baseline) | Yhteiset AGENTS.md-palikat, Claude/Codex-asetukset, hookit, dotfiles | 0.1.0 | kopioi käsin, myöhemmin Copier |
| tpl-python-cli | Python-CLI/skripti (uv, pytest, ruff) | suunniteltu | `copier copy gh:jvh-lab/tpl-python-cli <kohde>` |
| tpl-nextjs-app | Next.js-sovellus (TypeScript, Playwright) | suunniteltu | `copier copy gh:jvh-lab/tpl-nextjs-app <kohde>` |
| tpl-agent-bot | OpenClaw/Hermes-botin työtila | suunniteltu | `copier copy gh:jvh-lab/tpl-agent-bot <kohde>` |
| tpl-research | Tutkimus-/raporttiprojekti (lähteet, ADR:t, raportit) | suunniteltu | `copier copy gh:jvh-lab/tpl-research <kohde>` |

Topicit: `template`, `copier-template`, `agent-ready` + stack (`python`, `nextjs`) + tarkoitus (`cli`, `app`, `bot`, `research`).

## Säännöt

- Jokainen template: README (mitä luo · milloin käyttää · pikakäyttö · muuttujat · mitä agentti saa tehdä · CHANGELOG), CI joka renderöi ja testaa, SemVer-tagit.
- Pohjaa ei päivitetä projektissa käsin; korjaus tehdään templateen ja tuodaan `copier update` -komennolla.
- Salaisuuksia ei ole missään templatessa.
