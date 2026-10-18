# Fábrica agêntica do Project Hestia

Este documento descreve a fábrica que roda localmente em `~/agent-sandbox`. Ele cobre o que ela faz, com quais ferramentas, como as peças conversam entre si e o que dá para melhorar. O código e a configuração vivem fora do repositório. As regras que os agentes seguem vivem no `AGENTS.md` do repositório, na seção "Agent cycle".

## 1. Visão geral

É uma fábrica "clara" (light factory): pessoas decidem o que construir e revisam o resultado, e os agentes fazem o trabalho do meio.

```text
Discover (pessoa)  →  Ready for agent + label factory  →  [fábrica]  →  In review (pessoa)  →  merge (pessoa)
                                                            │
                         Plan → Build → CI → sincroniza base → Verify → Judge → (Fix → CI → Verify → Judge)* → Demo → pronto
```

- **Pessoa:** faz o Discover (`tlc-discover`), escreve a issue, coloca em **Ready for agent** com a label `factory`, responde quando a fábrica pergunta, revisa o PR e faz o merge.
- **Fábrica:** pega a issue, planeja (quando falta plano), implementa, verifica de forma independente, revisa o código, corrige até a the-judge aprovar, grava a demo do commit aprovado (prints de antes e depois e vídeos dos casos de uso) e entrega um PR pronto para revisão.
- **Aprovação pela feature:** a demo existe para você aprovar o que a feature faz olhando prints e vídeos, sem precisar ler o código. O código já passou pelo Verify e pela the-judge.
- **Nada é aceito por autodeclaração.** Cada etapa termina numa conversa de agente, mas só avança quando o orquestrador confere os artefatos no git e no GitHub.

## 2. Componentes

| Peça                      | Onde vive                                                                                                    | Papel                                                                                                                                                                                                                                     |
| ------------------------- | ------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Orquestrador              | `~/agent-sandbox/factory/factory.py`, container `agent-sandbox-factory`                                      | Processo Python sempre ligado. Lê Linear e GitHub, abre uma conversa por etapa, confere artefatos, move status, fala no Slack. Não usa LLM.                                                                                               |
| Configuração              | `~/agent-sandbox/factory/config.json`                                                                        | Time do Linear, nomes de status e labels, perfil do agente, limites (vagas, rodadas, sincronizações), canais do Slack.                                                                                                                    |
| Prompts das etapas        | `~/agent-sandbox/factory/prompts/*.md`                                                                       | `plan`, `build`, `verify`, `judge`, `fix`, `demo`, `answer` e `_result` (o contrato de saída comum a todos).                                                                                                                              |
| Estado                    | `~/agent-sandbox/factory-state/state.json`                                                                   | Um registro por issue: etapa, branch, base, worktree, PR, rodada, conversas, thread do Slack, respostas já vistas.                                                                                                                        |
| OpenHands Agent Canvas    | container `agent-sandbox`, http://localhost:8200/canvas                                                      | Interface web e API (agent-server) que criam e executam as conversas. Imagem própria em cima de `ghcr.io/openhands/agent-canvas:1.27.1`.                                                                                                  |
| Cursor CLI (ACP)          | dentro do `agent-sandbox`, `/usr/local/bin/agent`                                                            | O agente que de fato escreve código. O OpenHands o executa pelo Agent Client Protocol, com o perfil **Cursor-ACP**, logado na sua conta pessoal do Cursor.                                                                                |
| Clone dedicado            | `~/agent-sandbox/projects/project-hestia` (no container: `/projects/project-hestia`)                         | Cópia só da fábrica. A sua cópia de trabalho em `~/projects/vita/project-hestia` nunca é montada nem tocada.                                                                                                                              |
| Worktrees                 | `~/agent-sandbox/projects/worktrees/<ISSUE>`                                                                 | Um diretório e um branch por issue. Etapas da mesma issue usam o mesmo worktree, e issues diferentes nunca se misturam.                                                                                                                   |
| Skills globais            | `~/.agents`, `~/.claude`, `~/.cursor`, `~/.codex` (`skills/`), montadas só para leitura                      | O Cursor dentro do sandbox enxerga as mesmas skills que você usa no Mac, além das skills do repositório.                                                                                                                                  |
| Segredos                  | `~/agent-sandbox/.env` (permissão 600)                                                                       | `LINEAR_API_KEY`, `GH_TOKEN`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_PERSON_ID`. Entram como variáveis de ambiente nos dois containers.                                                                                             |
| Imagem                    | `~/agent-sandbox/Dockerfile`                                                                                 | Base do Agent Canvas + Cursor CLI + pnpm 11.17.0 (corepack) + `slack_sdk`.                                                                                                                                                                |
| Evidências da demo        | `~/agent-sandbox/projects/factory-evidence/<ISSUE>/<commit>/`                                                | Prints, vídeos e `manifest.json` de cada demo, fora do repositório. As ferramentas da gravação (`playwright-core`, `ffmpeg-static`) ficam em `projects/.demo-tools`.                                                                      |
| Branch `factory-evidence` | GitHub                                                                                                       | Só mídia, para os prints e GIFs aparecerem no comentário do PR. O orquestrador escreve nele sem work tree. Tem um `vercel.json` que desliga os deploys e nunca recebe merge.                                                              |
| Supabase local            | Mac, visto do sandbox em `http://host.docker.internal:54321`                                                 | Backend da demo, com o seed (`admin@hestia.local`). A demo nunca usa staging nem produção.                                                                                                                                                |
| Proxy ACP                 | `~/agent-sandbox/openhands-state/bin/cursor-acp-params.py` (no container: `/home/openhands/.openhands/bin/`) | Fica entre o Canvas e o `agent acp` e aplica contexto, esforço e fast, que o Canvas não sabe pedir. O **Cursor-ACP**, usado pela fábrica, roda `grok-4.7` em 256k, high e fast; o **Cursor-Grok-500k-xhigh-fast**, em 500k, xhigh e fast. |
| Entrada do container      | `~/agent-sandbox/sandbox-entrypoint.sh`                                                                      | Liga as skills e o `mcp.json` do Cursor dentro do home do container, configura o git para usar o `GH_TOKEN` e inicia o Canvas ou o orquestrador.                                                                                          |

Os dois containers sobem com o mesmo `compose.yaml` e compartilham `/projects` e o estado do Canvas. O orquestrador fala com o Canvas pela rede interna do compose (`http://canvas:8000`) e se autentica com a chave de sessão do Canvas.

## 3. Ferramentas e integrações

| Ferramenta                        | Quem usa               | Para quê                                                                                                                                                                                   | Credencial                                                                                                                                       |
| --------------------------------- | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Linear GraphQL API**            | Orquestrador           | Achar issues prontas, ler descrição, relações e comentários, mover status, labels e comentários `[factory]`, e subir prints e vídeos da demo (`fileUpload`)                                | `LINEAR_API_KEY` (sua chave pessoal)                                                                                                             |
| **Linear MCP**                    | Cursor                 | Ler a issue e registrar respostas traduzidas para o inglês                                                                                                                                 | Mesma chave, via `cursor-mcp.json`                                                                                                               |
| **GitHub (`gh` e git por HTTPS)** | Cursor e orquestrador  | Push do branch da issue, PR em rascunho, revisão da the-judge, leitura de CI, merge da base, marcar PR como pronto, push da mídia da demo em `factory-evidence` e comentário da demo no PR | `GH_TOKEN` fine-grained, só no `LeoVitale/project-hestia`: Contents e Pull requests com escrita; Actions, Commit statuses e Metadata com leitura |
| **GitHub Actions**                | Orquestrador (leitura) | CI do PR: o workflow `PR checks` roda em PRs para `main` e `feat/**`                                                                                                                       | O token não lê a API de Checks, então o CI é lido pelas execuções do Actions e pelos commit statuses                                             |
| **Slack Web API + Socket Mode**   | Orquestrador           | Canal por projeto, thread por issue, menções, receber as suas respostas na hora                                                                                                            | `SLACK_BOT_TOKEN` (bot "OpenHands") e `SLACK_APP_TOKEN` (`connections:write`)                                                                    |
| **OpenHands agent-server API**    | Orquestrador           | Criar conversas, mandar mensagens, ler status, contagem de eventos e resposta final                                                                                                        | Chave de sessão em `openhands-state/agent-canvas/api-key.txt`                                                                                    |
| **Cursor CLI**                    | OpenHands              | Executar a conversa: ler código, editar, rodar testes, git e `gh`                                                                                                                          | Seu login no Cursor, guardado no volume `home`                                                                                                   |
| **pnpm / Node 24**                | Cursor                 | Instalar dependências e rodar lint, typecheck, testes e build no worktree                                                                                                                  | —                                                                                                                                                |

**Skills usadas em cada etapa:**

| Etapa  | Skill                                                                                            | Papel                                                                                                                                                                                                                                                                                     |
| ------ | ------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Plan   | `tlc-plan`                                                                                       | Transforma a issue em tasks com critérios observáveis. Recusa adivinhar e registra perguntas em `Unresolved`.                                                                                                                                                                             |
| Build  | `tlc-implement` (`profile: ui`, `handoff: on`) + `iterate` (só para o CI)                        | Escreve o checklist com provas, implementa e roda cada prova.                                                                                                                                                                                                                             |
| Verify | Verifier do `tlc-implement` (`references/verify.md`)                                             | Agente novo, que não escreveu o código. Roda as provas, confere asserções e injeta falhas. Escreve o `.verified.md` com `**Verdict**`.                                                                                                                                                    |
| Judge  | `the-judge`                                                                                      | Roda as checagens do repo, pesquisa documentação oficial e posta uma revisão consolidada com veredito.                                                                                                                                                                                    |
| Fix    | regras do `tlc-implement`                                                                        | Corrige o que falhou sem enfraquecer testes e sem mudar checks.                                                                                                                                                                                                                           |
| Demo   | sem skill: `prompts/demo.md`, com Playwright (`playwright-core`), o Chromium do sandbox e ffmpeg | Roda o app duas vezes, antes (a base) e depois (o PR), contra o Supabase local. Tira prints das telas alteradas em 1440 e 390 px, claro e noturno, com o mock de design ao lado, e grava um vídeo por caso de uso. Escreve `manifest.json` e lista em `gaps` o que não conseguiu mostrar. |

## 4. Formato da issue que a fábrica entende

A fábrica pega uma issue quando ela está em **Ready for agent** e tem a label **`factory`**. A label é o "sim" explícito para fazer push do branch da issue e abrir um PR em rascunho para a base. Da descrição ela lê:

| Linha na descrição                                              | Efeito                                                             | Sem a linha              |
| --------------------------------------------------------------- | ------------------------------------------------------------------ | ------------------------ |
| ``Base: `<branch>` ``                                           | O branch nasce de `origin/<base>`, e o PR vai para a base          | `main`                   |
| ``Branch: `<branch>` ``                                         | Nome do branch da issue. Se já existir no GitHub, é continuado     | `feat/<feature>-<issue>` |
| ``Task: `.specs/features/<f>/tasks/<NN>-<slug>.md` ``           | Se o arquivo existe na base, pula o Plan e vai direto para o Build | O Plan roda              |
| `Start only after pull request #N is merged…`                   | Espera o PR #N receber merge                                       | Não espera               |
| `.specs/features/<feature>/` (em qualquer lugar, ou no `Task:`) | Define a pasta da feature                                          | A fábrica recusa a issue |

Uma relação **"blocked by"** no Linear também segura a issue até a bloqueadora chegar a **Done**. A fábrica recusa uma issue cujo `Branch:` é igual à base, a `main` ou a `master`.

## 5. Máquina de estados

Cada issue no `state.json` está em uma destas etapas:

| Etapa                                             | Ocupa vaga? | Significado                                                                                                                            |
| ------------------------------------------------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `queued`                                          | Não         | Registrada, esperando vaga. Recebe uma vez "Queued, position N".                                                                       |
| `blocked`                                         | Não         | Esperando um bloqueador do Linear ou um PR. Recebe "Waiting on …" quando os motivos mudam.                                             |
| `plan`, `build`, `verify`, `judge`, `fix`, `demo` | Sim         | Uma conversa da etapa está rodando, ou acabou e o portão está sendo conferido.                                                         |
| `ci`                                              | Sim         | Esperando o CI do commit atual.                                                                                                        |
| `final`                                           | Sim         | APPROVE recebido. Conferindo se o branch tem a base atual e se o PR está `MERGEABLE`, e esperando vaga para a demo do commit aprovado. |
| `waiting`                                         | Não         | Parada pedindo a pessoa (`factory:needs-human`).                                                                                       |
| `review`                                          | Não         | Entregue: PR pronto e issue em In review.                                                                                              |
| `dropped`                                         | Não         | A issue saiu de Ready for agent ou perdeu a label antes de começar.                                                                    |

O ciclo principal roda a cada 2 minutos (`poll_seconds`), ou na hora quando chega uma mensagem pelo Slack. Em cada ciclo, o orquestrador:

1. avança as issues ativas (lê o resultado da conversa e confere o portão);
2. entrega respostas às issues paradas;
3. reafirma In review nas issues entregues (veja a seção 10);
4. atende pedidos de demo para PRs já em In review (`factory-state/requests/demo-<ISSUE>`);
5. registra issues novas;
6. preenche vagas livres da mais antiga para a mais nova, respeitando bloqueios;
7. salva o estado.

## 6. Etapas em detalhe

Toda conversa recebe um prompt que nomeia a skill, o worktree, o branch, a base e os artefatos esperados. Todo prompt termina com o contrato `_result.md`: o agente encerra com uma linha `FACTORY_RESULT: {"status": "done" | "needs_human" | "handoff", …}`. O orquestrador só considera a conversa terminada quando o status é `finished` e houve eventos novos desde a última mensagem enviada.

| Etapa             | Início                                                                           | Portão antes da próxima etapa                                                                                                                                                                                                                                                                                                                      |
| ----------------- | -------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Plan**          | Só quando não há `Task:` válido na base                                          | Tasks file commitado em `.specs/features/<f>/tasks/`, sem linha `blocks` em `Unresolved`. Linhas `blocks go-live` são avisadas no Slack.                                                                                                                                                                                                           |
| **Build**         | Depois do Plan, ou direto quando a task já existe                                | Checklist commitado em `checks/`, árvore limpa, branch enviado ao GitHub, PR aberto da issue para a base. Se a mudança toca `supabase/`, para uma vez e pede o resultado do `test:rls` (veja a seção 11).                                                                                                                                          |
| **Sincronização** | Depois do Build e de cada Fix                                                    | Merge de `origin/<base>` no branch, sem rebase e sem force-push, seguido de push. Um conflito vira rodada de Fix.                                                                                                                                                                                                                                  |
| **CI**            | Depois da sincronização                                                          | Todas as execuções do Actions e os commit statuses do commit atual verdes. Falha vira Fix. Sem nenhum check em 15 minutos, para e chama você.                                                                                                                                                                                                      |
| **Verify**        | CI verde                                                                         | `.verified.md` commitado como último commit, com `**Verdict**: PASS`. FAIL vira Fix.                                                                                                                                                                                                                                                               |
| **Judge**         | Verify PASS                                                                      | Revisão no commit atual, postada depois do início da etapa. O veredito vem do corpo da revisão, porque o PR é seu e o GitHub não deixa o autor aprovar. APPROVE vai para `final`; COMMENT ou REQUEST_CHANGES viram Fix.                                                                                                                            |
| **Fix**           | Falha de CI, de Verify, de Judge ou conflito                                     | Um commit novo. Depois sincroniza, roda o CI, o Verify e o Judge de novo. Limite de 3 rodadas (`max_fix_rounds`).                                                                                                                                                                                                                                  |
| **Final**         | APPROVE                                                                          | Se a base andou, sincroniza e refaz Verify e Judge (até 3 vezes, `max_base_syncs`). Se o PR conflita, Fix. Se está `MERGEABLE` e a demo do commit foi publicada, marca como pronto, move para In review e te avisa com os links da demo.                                                                                                           |
| **Demo**          | Na etapa final, com o PR `MERGEABLE` e a base atual, uma vez por commit aprovado | `manifest.json` com pelo menos um print e um vídeo, cada arquivo dentro da pasta da demo, árvore limpa e head igual ao aprovado. A fábrica publica no Linear (comentário com prints e vídeos tocando), no PR (tabela Design / Antes / Depois, GIF de cada caso de uso e link do MP4) e na thread do Slack. Uma demo por vez (`demo.max_parallel`). |

Cada etapa usa uma conversa nova. Quem escreve nunca verifica nem julga o próprio trabalho.

## 7. Paradas e respostas

A fábrica para (etapa `waiting`) quando:

- uma skill se recusa a adivinhar (`needs_human`) ou uma prova fica vermelha sem solução;
- o tasks file tem `Unresolved` do tipo `blocks`;
- o branch não foi enviado, a árvore ficou suja ou falta um artefato;
- a mudança toca schema ou RLS e o `test:rls` não pôde rodar;
- 3 rodadas de Fix terminam sem APPROVE;
- a conversa termina em `error` ou `stuck`, ou fica esperando confirmação;
- a base anda depois da revisão mais de 3 vezes.

A ordem é sempre a mesma: **Slack primeiro**, com uma resposta na thread que menciona você e aparece também no canal. Depois vêm a label `factory:needs-human` e um comentário `[factory]` na issue.

Você responde de duas formas:

- **na thread do Slack**, e a resposta chega na hora pelo Socket Mode;
- **como comentário na issue do Linear**, que a fábrica lê no ciclo seguinte.

Só as suas mensagens contam: o `SLACK_PERSON_ID` no Slack e o dono da chave no Linear. A resposta vai para a conversa da etapa que parou, com a instrução de registrá-la em inglês no Linear (`answer.md`). Depois a conversa continua e o portão roda de novo. Se a parada aconteceu antes de existir conversa, por exemplo um problema de base ou de tasks file, a resposta faz a fábrica conferir a issue de novo.

## 8. Paralelismo, fila e dependências

- **Vagas:** até 3 issues ativas ao mesmo tempo (`max_active`). Cada uma tem o próprio worktree e branch.
- **Fila:** issues sem vaga ficam em `queued` e recebem um único aviso com a posição.
- **Dependências:** `blocked by` no Linear e `Start only after pull request #N`. Uma issue dependente só começa quando a bloqueadora chega a **Done**, ou seja, depois do seu merge, e nasce da base já com aquele código. Não existe encadeamento de branches antes do merge.
- **Base em movimento:** a sincronização antes de cada Verify e na etapa final garante que o que foi verificado e julgado bate com a base atual. Depois de In review, a fábrica não acompanha mais a base. Se você fizer merge de um PR irmão, um conflito no outro fica para você resolver no merge.

## 9. Slack

- **Canal por projeto:** `#hestia-<projeto>`, por exemplo `#hestia-visual-refresh`, criado pelo bot, que te convida. Issues sem projeto vão para `#hestia-factory`. Para usar um canal que já existe, mapeie em `slack.project_channels`.
- **Thread por issue:** a mensagem-raiz mostra a issue, a etapa, o PR e a rodada, e é editada a cada mudança. Cada etapa vira uma resposta com o link da conversa no Canvas.
- **Menções avulsas:** uma menção ao bot no `#visual-refresh` (`mentions.channels`) abre uma conversa Cursor-ACP fora do fluxo de issues. A fábrica reage com 👀, posta o link e depois a resposta final. Menções seguintes na mesma thread vão para a mesma conversa. Regras fixas: o agente não posta no Slack e só lê o Linear. Isso substituiu a automação "Slack Channel Monitor", que está pausada no Canvas.
- **Pré-requisitos do app:** Socket Mode ligado, evento `message.channels` assinado, Save Changes e reinstalação do app no workspace.

## 10. Linear

| Momento                | Status                      | Quem move                               |
| ---------------------- | --------------------------- | --------------------------------------- |
| A pessoa libera        | Ready for agent + `factory` | Pessoa                                  |
| Primeira etapa começa  | In Progress                 | Orquestrador                            |
| APPROVE e PR mergeable | In review                   | Orquestrador                            |
| Merge do PR            | Done                        | Integração GitHub ↔ Linear, ou a pessoa |

- **Conflito com a automação do Linear:** quando o PR sai do rascunho, a integração do Linear com o GitHub move a issue de volta para In Progress, 1 segundo depois de a fábrica mover para In review. O orquestrador confere isso nos 3 ciclos seguintes (`keep_in_review`) e recoloca em In review. A solução definitiva fica em Team settings → Workflow → Pull request automations.
- **Comentários da fábrica** começam com `[factory]`. Assim ela distingue os próprios comentários das suas respostas, já que as duas coisas usam a mesma chave do Linear.

## 11. Como as peças se conversam

```text
             ┌──────────── Linear ─────────────┐
             │ issues, status, labels, comments │◀──── Linear MCP ───┐
             └───────────────▲──────────────────┘                    │
                    GraphQL  │                                       │
┌─────────────────────────────┴─────────────┐   REST + session key   ┌┴───────────────────────────┐
│ agent-sandbox-factory (orquestrador)       │──────────────────────▶ │ agent-sandbox (Canvas)       │
│  - ciclo de 2 min + wake por Slack         │  cria conversas,       │  agent-server → Cursor CLI   │
│  - portões: git, gh, Actions, statuses     │  manda mensagens,      │  (ACP, skills, pnpm, git, gh)│
│  - estado em factory-state/state.json      │  lê status e resposta  │                              │
└───────▲─────────────────────┬──────────────┘                        └──────────────┬──────────────┘
        │ Socket Mode (eventos)│ Web API (posts)                                     │ git push, gh pr
┌───────┴─────────────────────▼──────┐                                ┌──────────────▼──────────────┐
│ Slack: #hestia-<projeto>, threads,  │                                │ GitHub: branch da issue, PR, │
│ menções no #visual-refresh          │                                │ Actions, reviews da the-judge│
└─────────────────────────────────────┘                                └─────────────────────────────┘
                      /projects (clone + worktrees) é compartilhado pelos dois containers
```

**Exemplo, a vida de uma issue:**

1. Você move a HES-104 para Ready for agent e coloca a label `factory`.
2. No ciclo seguinte, o orquestrador lê a issue pelo GraphQL: base `feat/visual-refresh`, branch `feat/visual-refresh-cleanings-list`, task `06-cleanings-list.md`, espera pelo PR #67. O #67 já recebeu merge e não há bloqueadores, então a issue começa.
3. Ele cria o worktree a partir de `origin/feat/visual-refresh` e move a issue para In Progress. A task já existe, então o Plan é pulado.
4. Ele abre no Canvas a conversa de Build. O Cursor implementa, faz push e abre o PR em rascunho com `gh`.
5. Ele confere o checklist, o push e o PR. Sincroniza com a base, espera o PR checks e abre a conversa de Verify.
6. Com o Verify em FAIL, abre um Fix. Depois, sincroniza, roda o CI e faz um Verify novo, desta vez com PASS.
7. Abre a conversa de Judge. A the-judge posta a revisão com APPROVE.
8. Na etapa final, confere que a base está incluída e que o PR está `MERGEABLE`. Marca o PR como pronto, move para In review e te menciona no Slack.

## 12. Configuração e operação

**Principais chaves do `config.json`:** `team_key`, `states`, `label`, `needs_human_label`, `agent_profile_id` (Cursor-ACP), `github_repo`, `base_branch`, `max_active` (3), `max_fix_rounds` (3), `max_base_syncs` (3), `poll_seconds` (120), `no_checks_timeout_seconds` (900), `schema_paths`, `mentions.channels`, `slack.project_channels`, `canvas_public_url` e `demo` (`enabled`, `supabase_url`, `supabase_anon_key`, `evidence_path`, `evidence_branch`, `max_parallel`, `max_file_mb`, `port_before`, `port_after`). Sem `supabase_anon_key`, vale a chave anon padrão do `supabase start`, e a variável `DEMO_SUPABASE_ANON_KEY` tem precedência.

**Comandos úteis:**

```bash
docker compose -f ~/agent-sandbox/compose.yaml up -d
```

```bash
docker compose -f ~/agent-sandbox/compose.yaml logs -f factory
```

```bash
docker compose -f ~/agent-sandbox/compose.yaml restart factory
```

```bash
jq '.issues | map_values({stage, pr_number, round})' ~/agent-sandbox/factory-state/state.json
```

Gravar a demo de um PR que já está em In review (ele volta para In review sem mudar o status no Linear):

```bash
touch ~/agent-sandbox/factory-state/requests/demo-HES-104
```

Testes da etapa de demo, sem tocar em Linear, GitHub ou Slack:

```bash
python3 tools/agent-factory/factory/tests/test_demo_station.py tools/agent-factory/factory
```

- **Mudanças:** prompts e `factory.py` são montados só para leitura no container, então basta reiniciar o serviço `factory`. Uma mudança no `Dockerfile` exige `docker compose build` e depois `up -d`.
- **Teste sem efeitos:** `FACTORY_DRY_RUN=1 python3 factory.py --once`, dentro do serviço `factory` com `docker compose run --rm`, roda um ciclo sem gravar nada.
- **Não use `docker compose down -v`.** O `-v` apaga o volume `home`, que guarda o seu login do Cursor.
- **Worktrees aparecem como `prunable` quando vistos do Mac,** porque os caminhos são do container. Não rode `git worktree prune` nesse clone a partir do Mac.

## 13. Limitações conhecidas

- **Sem `test:rls` no sandbox:** não há Docker lá dentro, e mudanças em `supabase/` sempre param pedindo você.
- **A the-judge não aprova de verdade:** ela posta com a sua conta, e o GitHub não deixa o autor aprovar. A aprovação não conta para proteção de branch.
- **CI só pelo Actions e commit statuses:** um app que publique só _check runs_ fica invisível para a fábrica.
- **O token não roda jobs de novo:** um teste instável vira rodada de Fix, ou uma parada pedindo você.
- **Cota do Cursor:** cada etapa é uma conversa, e as três primeiras issues usaram de 5 a 8 conversas cada.
- **Tudo é local:** o Mac precisa estar ligado e com o Docker rodando. Não há webhooks: o Linear é lido por polling, e o Slack chega pelo Socket Mode.
- **A demo depende do Supabase local do Mac:** ele precisa estar rodando (`pnpm exec supabase start`), com o seed e com as migrations da base. Um banco atrasado aparece como `gaps` na demo, como as abas que deram 400 na HES-104.
- **Vídeo só toca no Linear:** no GitHub, o comentário mostra prints e GIFs, e o MP4 fica como link.
- **Depois de In review, a fábrica não acompanha mais o PR:** comentários de revisão humana, mudanças na base e merge ficam com você.

## 14. Primeiros resultados

Três issues do Visual refresh rodaram em paralelo em 18/10/2026. Os horários estão em UTC e as durações em minutos. O número entre parênteses é a espera da fábrica até abrir a próxima etapa.

| Issue / PR    | Sequência de etapas                                                                                                                                                        | Rodadas | Conversas | Tempo total |
| ------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- | --------- | ----------- |
| HES-104 / #72 | Build 38.9 (4.0) → Verify FAIL 10.7 (1.7) → Fix 17.3 (3.5) → Verify PASS 6.3 (2.0) → Judge APPROVE 8.4                                                                     | 1       | 5         | 95          |
| HES-103 / #73 | Build 21.6 (3.2) → Verify FAIL 10.3 (2.1) → Fix 13.4 (3.0) → Verify PASS 5.1 (1.3) → Judge COMMENT 7.0 (1.3) → Fix 14.2 (3.5) → Verify PASS 5.0 (1.2) → Judge APPROVE 2.5  | 2       | 8         | 97          |
| HES-105 / #74 | Build 37.6 (3.7) → Verify FAIL 10.8 (1.6) → Fix 16.7 (4.1) → Verify PASS 6.6 (0.8) → Judge COMMENT 10.1 (0.4) → Fix 21.3 (3.3) → Verify PASS 6.7 (1.5) → Judge APPROVE 4.5 | 2       | 8         | 132         |

**O que esses números mostram:**

- **Os três primeiros Verify falharam pelo mesmo motivo:** as provas eram mais fracas que as afirmações.
  - Falhas que o Verifier injetou nos comportamentos sobreviveram aos testes, e alguns arranjos dos mocks ficaram sem check.
  - Os três Fix só adicionaram testes; nenhum mudou o código do app.
  - Exemplos: na HES-104, a linha inteira como link e o filtro `assigned`; na HES-103, a posição e o `flex-wrap` do "Mais filtros"; na HES-105, o `sticky top-0 h-[60px]` e o círculo do passo.
  - O Verifier funcionou como deveria. Um Fix, porém, registrou uma divergência de produto (a HES-104 linka só o nome, não a linha inteira) como linha de Landing (D7), em vez de implementar ou perguntar.
- **Os dois COMMENT da the-judge foram defeitos de CSS computado, que o jsdom não enxerga.**
  - No #73, os selects saíram com 220 px de altura, por causa de `flex-1 basis-[220px]` numa coluna flex.
  - No #74, houve contraste de 1.04:1 e de 2.57:1 nos temas claro e noturno.
  - A the-judge provou os dois com o Chromium headless que existe no sandbox. O repo não tem testes que rodem em navegador, e o `tlc-implement` só usa um navegador quando o próprio repo já usa.
- **36% do tempo dos agentes (cerca de 98 de 275 minutos) foi gasto esperando o CI** dentro das conversas de Build e Fix (por causa da instrução de usar o `iterate`). Depois, a fábrica espera o CI de novo.
- **Mais cerca de 48 minutos foram espera da própria fábrica entre etapas**, por causa do ciclo de 2 minutos, já que uma passagem Build/Fix → Verify consome dois ciclos.
- **O CI leva de 8 a 11 minutos por push**, e só o `pnpm test` leva 9.
  - 8 dos 17 runs foram de commits só de documentação (checklist e relatório).
  - O cache do Turbo nunca acertou.
- **Custo invisível:** o Canvas registra `accumulated_cost: 0.0` e nenhum uso de tokens nas conversas pelo Cursor ACP. Foram 21 conversas e cerca de 4.700 eventos.

## 15. Pontos de melhoria

Quatro investigações independentes, todas só de leitura, levantaram estes pontos em 18/10/2026:

- revisão do código do orquestrador;
- auditoria de segurança e isolamento;
- análise das três primeiras execuções;
- pesquisa de recursos do OpenHands e de práticas de outras fábricas.

Os IDs (S, R, Q, O) servem para referência. Esforço: P = pequeno, M = médio, G = grande. Os itens marcados com ✔︎ foram conferidos de novo, à mão, depois das investigações.

### 15.1 Segurança: fazer antes de ampliar o uso

A fábrica pega texto do Linear e do Slack e entrega a agentes que rodam comandos, sem confirmação. Hoje, quase todas as regras ("não faça merge", "não poste no Slack", "Linear só para leitura") existem só no prompt.

| ID  | Risco                                                                    | Evidência                                                                                                                                                                                                                                                                                                                                                                                                                                                   | Mitigação                                                                                                                                                                                                                  | Esforço              |
| --- | ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------- |
| S1  | **Todos os segredos estão no ambiente do agente,** que pode virar root ✔︎ | O `env_file: .env` vai inteiro para o container `canvas`. O processo do Cursor enxerga `GH_TOKEN`, `LINEAR_API_KEY`, os tokens do Slack e as chaves do Canvas. O usuário `openhands` tem `sudo` sem senha. A rede de saída é livre. Uma issue maliciosa pode mandar o agente enviar tudo para fora, inclusive `~/.config/cursor/auth.json`.                                                                                                                 | Separar o `.env`: o `canvas` recebe só `GH_TOKEN` e um token do Linear reduzido, e os tokens do Slack ficam só no `factory`. Tirar o `sudo` na imagem.                                                                     | P                    |
| S2  | **Nada no GitHub impõe as regras de branch** ✔︎                           | O repositório é privado no plano Free, então não há proteção de branch nem rulesets (a API responde 403 "Upgrade to GitHub Pro"). Com esse token, um agente consegue `git push origin HEAD:main`, `gh pr merge` e apagar branches. Um push na `main` com `supabase/migrations/**` dispara `supabase db push` em staging.                                                                                                                                    | GitHub Pro e um ruleset em `main` e `feat/visual-refresh`: exigir PR, bloquear deleção e force-push e liberar só você.                                                                                                     | P (custa ~US$ 4/mês) |
| S3  | **A descrição da issue escolhe para onde a fábrica faz push**            | `Branch:` e `Base:` aceitam qualquer coisa. Só são recusados um branch igual à base, a `main` e a `master`. `Branch: refs/heads/main` passa na validação. `Branch: feat/visual-refresh` faz a fábrica e o agente enviarem push para o branch compartilhado.                                                                                                                                                                                                 | Validar com um padrão estrito (por exemplo `^feat/[a-z0-9-]+$`, sem `refs/`), recusar branch que já existe e que não foi a fábrica que criou, e permitir só bases de uma lista.                                            | P                    |
| S4  | **Qualquer pessoa do Linear injeta instruções**                          | `ready_issues` não filtra por autor. `issue_block` copia os comentários de todos para os prompts de Plan e Build. O time HES é público, com 2 usuários ativos.                                                                                                                                                                                                                                                                                              | Pegar só issues criadas ou etiquetadas por você e só incluir os seus comentários.                                                                                                                                          | P                    |
| S5  | **O agente consegue cumprir os próprios portões**                        | O agente comenta no Linear com a sua chave. Um comentário sem o prefixo `[factory]` vira "resposta da pessoa" e pode até liberar a parada de schema. O veredito do Verify é um arquivo que o agente escreve, e o da the-judge é uma linha no texto da revisão.                                                                                                                                                                                              | Dar aos agentes uma identidade própria no Linear (app OAuth com só `read` e `comments:create`), aceitar a liberação de schema só pelo Slack, e (O3) publicar os vereditos como commit statuses da própria fábrica.         | M                    |
| S6  | **O sandbox alcança serviços do seu Mac** ✔︎                              | O `host.docker.internal` alcança Postgres local, portas do Cursor e o **ai-memory**. O ai-memory recusa a requisição (403), mas responde **200, sem autenticação**, quando ela traz o cabeçalho `Host: 127.0.0.1:49374`. Um agente injetado lê e envenena a memória de todos os seus projetos, e os agentes do Mac, fora do sandbox, leem essa memória depois. Também havia um `python -m http.server` servindo `.design/visual-refresh` da sua cópia real. | Exigir token no ai-memory, ou servir por socket Unix. Encerrar servidores avulsos. Mais adiante, rede `internal: true` com proxy de saída liberando só GitHub, Linear, Cursor e npm.                                       | P / M                |
| S7  | **Código plantado roda depois, fora da conversa**                        | O wrapper `openhands-state/bin/cursor-acp-params.py` e o `/opt/cursor` podem ser alterados pelo agente. O `.git/config` compartilhado aceita `core.hooksPath` e `core.fsmonitor`, e o orquestrador roda `git status`, `merge` e `push` nos worktrees dos agentes.                                                                                                                                                                                           | Montar `openhands-state/bin` só para leitura, deixar `/opt/cursor` com dono root, e fazer o orquestrador rodar git com `-c core.hooksPath=/dev/null -c core.fsmonitor=false`. Nunca rodar git nesse clone a partir do Mac. | P                    |
| S8  | **A chave de sessão do Canvas vai embutida na página**                   | O `/canvas/` traz a chave no HTML (`AGENT_CANVAS_ALLOW_LAN_SESSION_KEY`) e aceita qualquer cabeçalho `Host`. Um site que você abrir pode usar DNS rebinding para pegar a chave e criar conversas.                                                                                                                                                                                                                                                           | Desligar a injeção e usar o modo com autenticação, ou um proxy que confira o `Host`.                                                                                                                                       | P                    |
| S9  | **Sua conta pessoal do Cursor está exposta**                             | O `auth.json` tem access e refresh token legíveis pelo agente, e o Privacy Mode está desligado.                                                                                                                                                                                                                                                                                                                                                             | Usar uma conta, seat ou API key dedicada ao sandbox, ligar o Privacy Mode e trocar as credenciais em caso de incidente.                                                                                                    | P                    |
| S10 | **O Slack está acessível de dentro do agente**                           | O Canvas ainda carrega um Slack MCP (`npx @zencoderai/slack-mcp-server`, sem versão fixa) com o bot token. O `SLACK_APP_TOKEN` no `canvas` permite abrir uma segunda conexão Socket Mode e roubar eventos da fábrica.                                                                                                                                                                                                                                       | Remover o Slack MCP das configurações do Canvas e deixar os dois tokens só no `factory`.                                                                                                                                   | P                    |
| S11 | **A chave do Linear é de administrador**                                 | `viewer.admin = true`. Ela está no ambiente do agente e no `cursor-mcp.json`.                                                                                                                                                                                                                                                                                                                                                                               | É a mesma solução do S5.                                                                                                                                                                                                   | M                    |
| S12 | **Dependências sem versão fixa na imagem**                               | A imagem base usa tag, não digest. O Cursor é instalado com `curl … \| bash` sem versão e se atualiza sozinho. O Slack MCP vem por `npx -y` sem versão.                                                                                                                                                                                                                                                                                                     | Fixar a imagem por digest, fixar a versão do Cursor e desligar o auto-update, e tirar o MCP via npx.                                                                                                                       | P                    |
| S13 | **A Vercel builda todo push dos agentes**                                | Previews de `hestia-admin-staging` e `hestia-cleaner-web-staging` aparecem nos branches da fábrica.                                                                                                                                                                                                                                                                                                                                                         | Deixar só chaves públicas no ambiente de preview.                                                                                                                                                                          | P                    |
| S14 | **Os commits dos agentes parecem seus**                                  | `GIT_AUTHOR_*` e `GIT_COMMITTER_*` usam o seu nome e e-mail.                                                                                                                                                                                                                                                                                                                                                                                                | Usar um committer distinto, por exemplo "Hestia Factory".                                                                                                                                                                  | P                    |
| S15 | **Telemetria e conversas guardadas por inteiro**                         | A auditoria encontrou `user_consents_to_analytics=true` nas configurações do Canvas do sandbox. As conversas guardam a saída completa das ferramentas, então um `env` vazaria ali. Nenhum segredo foi encontrado nos logs.                                                                                                                                                                                                                                  | Desligar a telemetria. Com o S1 resolvido, o risco do `env` some.                                                                                                                                                          | P                    |

**O que já está bem:**

- O container não tem Docker socket nem privilégios extras.
- A porta 8200 só aceita conexões locais, e a API do Canvas responde 401 sem a chave.
- A sua cópia real não é montada, e as skills e o código da fábrica entram só para leitura.
- O token do GitHub é fine-grained: só este repositório, sem acesso a administração, segredos ou hooks, e com o Actions só para leitura.
- A fábrica nunca faz merge nem force-push.
- O Slack só aceita o seu `SLACK_PERSON_ID`.
- Os segredos do OpenHands ficam cifrados em disco.
- O pnpm usa a lista `allowBuilds`.

### 15.2 Robustez do orquestrador

| ID  | Problema                                                            | Cenário                                                                                                                                                                                                                                                                                             | Correção                                                                                                                                     | Esforço |
| --- | ------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| R1  | **Uma issue é descartada depois de já estar em In Progress** ✔︎      | `begin()` move a issue para In Progress antes de sair de `queued`/`blocked`. Se o tasks file tiver `blocks` e você responder, a próxima tentativa encontra a issue em "In Progress", e não em "Ready for agent", e a marca como `dropped`. O mesmo vale para qualquer erro passageiro depois disso. | Guardar `started=True` antes de mover o status e aceitar In Progress quando ele estiver marcado.                                             | P       |
| R2  | **`start_station` marca a etapa antes de a conversa existir**       | Se o POST ao Canvas falhar, a issue fica presa sem conversa, ocupando a vaga. Ou então o portão da etapa nova roda sobre o resultado da conversa anterior. O `start_fix` já gastou a rodada. Num timeout, pode sobrar um agente órfão rodando.                                                      | Gravar `stage`, `conversation_id` e `events_mark` só depois de receber o id, e contar a rodada depois do POST.                               | P       |
| R3  | **O marcador de eventos não protege depois de enviar uma resposta** | O `events_mark` é lido antes do POST, e o próprio POST cria um evento novo. Se o Canvas ainda mostra `finished`, a resposta antiga é lida de novo e o portão roda enquanto o agente ainda trabalha.                                                                                                 | Ler o marcador depois do POST e sair do `advance` logo depois de entregar a resposta.                                                        | P       |
| R4  | **Respostas se perdem quando a entrega falha**                      | O `inbox` é esvaziado e os comentários são marcados como vistos antes do envio. Se o Linear ou o Canvas falharem, as respostas somem.                                                                                                                                                               | Enviar primeiro e só depois confirmar. Numa falha, devolver ao `inbox`.                                                                      | P       |
| R5  | **Não há prazos, e erros repetidos só vão para o log**              | Uma conversa em `idle`, `paused` ou `running` eterno, um CI `pending` sem fim, um `mergeable` UNKNOWN ou um token revogado viram espera ou tentativa infinita, ocupando a vaga e sem aviso.                                                                                                         | Contar falhas seguidas e definir um prazo por etapa (O4). Ao estourar, parar e chamar você.                                                  | M       |
| R6  | **`git` e `gh` sem timeout, rodando dentro da trava global**        | Um `git fetch` travado congela o ciclo e o listener do Slack.                                                                                                                                                                                                                                       | `subprocess.run(..., timeout=300)` com `GIT_TERMINAL_PROMPT=0`.                                                                              | P       |
| R7  | **O tratamento de parada pode derrubar o processo**                 | Se o Linear falhar dentro do `stop_for_human`, a exceção derruba o `main()`. Depois do restart, as mensagens no Slack se duplicam.                                                                                                                                                                  | Proteger cada efeito colateral com try/except e salvar o estado antes.                                                                       | P       |
| R8  | **Respostas fora de hora**                                          | Uma resposta durante `ci` ou `final` religa o agente anterior, enquanto o Verify começa. Uma resposta em `review`, `blocked` ou `queued` some sem aviso.                                                                                                                                            | Entregar só em etapa de conversa ou em `waiting`. Nos outros casos, guardar e avisar no Slack.                                               | P       |
| R9  | **Loop de auto-resposta no Linear**                                 | Um comentário do agente com `**[factory]**`, ou com uma citação antes do prefixo, vira "resposta da pessoa".                                                                                                                                                                                        | Procurar a marca depois de limpar a formatação, ou fazer a própria fábrica registrar a resposta. O S5 resolve isso de vez.                   | P       |
| R10 | **A etapa final não confere o CI do último commit** ✔︎               | O relatório do Verify cria um commit novo. A the-judge revisa esse commit, e o PR vai para In review sem a fábrica ler o CI dele. Desta vez estava verde por sorte.                                                                                                                                 | Chamar `pr_checks(head)` no `check_final`.                                                                                                   | P       |
| R11 | **Fix que discorda de todos os achados vira parada**                | O `fix.md` permite responder um achado em vez de corrigir. Sem commit novo, o `after_fix` para e chama você.                                                                                                                                                                                        | Quando o motivo foi a the-judge e não houve commit, rodar a Judge de novo.                                                                   | P       |
| R12 | **A Judge recebe o contador errado de rodada**                      | `{{round}}` conta também os Fix vindos de CI e de Verify. A the-judge aplica as regras de "rodada 2" (só bloqueadores) cedo demais.                                                                                                                                                                 | Usar um contador `judge_round` próprio.                                                                                                      | P       |
| R13 | **Mudanças de schema nunca passam no Verify**                       | O `verify.md` manda dar FAIL quando depende do `test:rls`. O resultado que você manda vai para o Build, e não para o Verifier. A checagem de schema também só roda no Build.                                                                                                                        | Guardar o seu resultado no registro, passá-lo ao Verify e checar o schema também no `sync_then_ci`.                                          | P       |
| R14 | **Efeitos duplicados em caso de crash**                             | O estado é salvo depois de trabalho lento. Um restart entre o POST e o save pode abrir uma segunda Judge (duas revisões) ou um segundo Build no mesmo worktree.                                                                                                                                     | Salvar logo depois de gravar o `conversation_id`. Ao iniciar, procurar conversas existentes pelas `tags`.                                    | M       |
| R15 | **Worktrees frágeis**                                               | Vistos do Mac, eles aparecem como `prunable`. Se o diretório existe, o `ensure_worktree` não repara nada, e um "already checked out" vira tentativa infinita.                                                                                                                                       | `git worktree prune` antes do `add`, `git worktree lock` depois, e falha vira parada. Ou montar `projects/` no mesmo caminho dos dois lados. | P       |
| R16 | **`begin()` lê dados velhos**                                       | Sem `fetch` quando o worktree já existe, e o tasks file é lido do worktree, e não da base.                                                                                                                                                                                                          | Sempre fazer `fetch` e ler com `git show origin/<base>:<task>`.                                                                              | P       |
| R17 | **Issues ativas não são conferidas no Linear de novo**              | Se a issue for cancelada ou perder a label, a fábrica continua, e o `check_final` a "ressuscita" em In review.                                                                                                                                                                                      | Conferir o status e a label em cada ciclo (junto com o `linear_replies`) e interromper a conversa.                                           | P       |
| R18 | **Um registro nunca sai do estado**                                 | Uma issue `dropped` ou `review` que volta para Ready for agent, para retrabalho, nunca entra de novo.                                                                                                                                                                                               | Arquivar o registro antigo e criar um novo.                                                                                                  | P       |
| R19 | **Só a primeira página de revisões é lida**                         | O GitHub devolve 30 revisões por página, e cada resposta de Fix numa thread conta como revisão. Depois de 30, o APPROVE novo não é visto.                                                                                                                                                           | `--paginate` ou `per_page=100`.                                                                                                              | P       |
| R20 | **A regex do veredito casa em qualquer lugar do relatório**         | Um relatório FAIL que cite "`**Verdict**: PASS`" do histórico passa no portão.                                                                                                                                                                                                                      | Ler só a primeira linha `**Verdict**:`.                                                                                                      | P       |
| R21 | **Casos de borda do leitor de CI**                                  | Os runs são agrupados só por workflow, e `cancelled` conta como falha. Se só os statuses da Vercel chegaram, o Verify começa sem CI.                                                                                                                                                                | Agrupar por workflow e evento, tratar `cancelled` como não final e configurar a lista de workflows obrigatórios.                             | P       |
| R22 | **Filtros do Slack descartam respostas**                            | Respostas com "Also send to channel" (`thread_broadcast`) ou com arquivo (`file_share`) são ignoradas, e não há deduplicação depois de uma reconexão.                                                                                                                                               | Aceitar esses dois subtipos e guardar os `ts` já vistos.                                                                                     | P       |
| R23 | **Bloqueios e crescimento sem limite**                              | Um bloqueador cancelado segura a issue para sempre. As menções e os worktrees (com `node_modules`) nunca são removidos. Um `Unresolved` com Kind em maiúscula ou entre crases não é detectado.                                                                                                      | Aceitar `canceled`, limpar o que já terminou e normalizar o Kind.                                                                            | P       |
| R24 | **Documentação e código divergem**                                  | O AGENTS.md diz "3 rodadas de Fix" e "avisa uma vez", e o `answer.md` duplica no Linear uma resposta que já veio do Linear.                                                                                                                                                                         | Ajustar os textos.                                                                                                                           | P       |

### 15.3 Qualidade e velocidade do fluxo

| ID  | Melhoria                                                                 | Por quê                                                                                                                                                                                                                                                                                                                                                        | Impacto esperado                                                                          | Esforço |
| --- | ------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- | ------- |
| Q1  | **O Build faz a passada de discriminação do Verifier antes de entregar** | Os 3 primeiros Verify falharam por provas fracas. O Build passaria a: mapear cada elemento e arranjo do mock para um check ou para uma linha de Landing; injetar uma falha por superfície num worktree de rascunho; preferir asserções de DOM e papel a classes do Tailwind; e rodar os testes de novo depois do formatter (a HES-105 perdeu 17 min por isso). | Transforma o primeiro FAIL em PASS. Economiza cerca de 25–30 min e 2 conversas por issue. | P       |
| Q2  | **Parar de esperar o CI dentro das conversas**                           | 36% do tempo dos agentes foi gasto vigiando `gh run watch`. Tirar o `iterate` do Build e do Fix, que passam a só fazer push. A fábrica já espera o CI e, se falhar, manda o log para a mesma conversa, em vez de abrir um Fix do zero.                                                                                                                         | Cerca de 98 min de agente a menos nas três issues, e 8–12 min por rodada.                 | P       |
| Q3  | **Prova de CSS renderizado no perfil `ui`**                              | O repo não tem testes em navegador, então o `tlc-implement` não usa um. Os dois COMMENT da the-judge foram altura e contraste computados. A proposta é um projeto pequeno de Playwright ou Vitest browser mode, com o Chromium que já está no sandbox: telas alteradas em 390 e 1440 px, temas claro e escuro, tamanho das caixas e axe `color-contrast`.      | Teria pego os dois achados antes da Judge, ou seja, uma rodada a menos em 2 de 3 issues.  | M       |
| Q4  | **Judge em paralelo com o Verify**                                       | Os dois rodam sobre o mesmo commit de código, e um único Fix trata os dois relatórios. Exige aceitar a revisão sobre o commit de código, porque o commit do relatório é só documentação.                                                                                                                                                                       | 1 rodada em vez de 2 na HES-103 e na HES-105, cerca de 30 min cada.                       | M       |
| Q5  | **CI mais rápido**                                                       | 10,5 min por push, 17 pushes. Fatiar o vitest do admin (532 s, 38% em setup do jsdom), cachear o `.turbo` e pular os passos pesados quando só `.specs/**` mudou. Isso deve ser feito com um passo de guarda, e não com `paths-ignore`, para o run continuar existindo.                                                                                         | De ~10,5 para ~3–4 min por push.                                                          | M       |
| Q6  | **Fábrica orientada a eventos**                                          | Hoje são 2–4 min de espera por transição, cerca de 48 min nas três issues. Conferir conversas ativas a cada 10–15 s e chamar o `check_ci` logo depois do `sync_then_ci`.                                                                                                                                                                                       | Recupera cerca de 15 min por issue.                                                       | P       |
| Q7  | **Decisão de produto não fica escondida**                                | Um Fix registrou a divergência D7 da HES-104 em vez de perguntar. O `fix.md` deve parar e perguntar, e a mensagem de APPROVE deve listar o "For sign-off" do Verifier.                                                                                                                                                                                         | Você vê o que diverge do mock antes do merge.                                             | P       |
| Q8  | **Ajustes pequenos**                                                     | Instalar o `ripgrep` na imagem (9 chamadas falharam), pôr `{{ci_note}}` no `judge.md` (3 falhas com `gh pr checks`) e guardar as sugestões de "Promote to lint rule" da the-judge.                                                                                                                                                                             | Menos tentativas desperdiçadas.                                                           | P       |
| Q9  | **Slack mais informativo**                                               | Hoje o Slack mostra "Read `<path>`" e um link. Deveria mostrar o motivo do FAIL (checks provados sobre o total, falhas mortas sobre as injetadas), os achados da Judge, se o Fix mudou teste ou código, o tempo da etapa, o estado do CI e um sinal de vida a cada X minutos num Build longo.                                                                  | Você acompanha sem abrir o Canvas.                                                        | P       |
| Q10 | **Limite de PRs em revisão**                                             | A revisão humana é o gargalo, por volta de 3–4 frentes por pessoa. Um `max_in_review` pausaria a fila quando houver N PRs esperando você.                                                                                                                                                                                                                      | Evita acumular trabalho que você não consegue revisar.                                    | P       |

### 15.4 Recursos do OpenHands e práticas de outras fábricas

| ID  | Recomendação                                                     | Fonte                                                                                                                                                                      | Encaixe                                                                                                                                                                                                 | Esforço |
| --- | ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| O1  | **Stop hook por conversa**                                       | `hook_config.stop` no `StartConversationRequest`. O SDK roda o hook quando a conversa termina e, se ele recusar, devolve o motivo ao agente ACP (`local_conversation.py`). | Fazer as checagens de árvore limpa, push, caminho do artefato e `.verified.md` como último commit dentro da conversa. O agente se corrige sozinho, em vez de a fábrica parar e chamar você.             | P–M     |
| O2  | **Encadear dependências em vez de esperar o merge**              | A spec do Symphony (OpenAI) e PRs empilhados, com limite prático de 3–4 camadas.                                                                                           | Quando a bloqueadora já passou pela Judge, a issue dependente nasce do branch dela, com profundidade máxima 2. Depois do merge, a fábrica retarga o PR e sincroniza.                                    | M       |
| O3  | **Vereditos como commit statuses**                               | A skill `github-delivery-watchdog`, do próprio OpenHands, exige `software-factory/tests` e `software-factory/review` no commit exato.                                      | Publicar `factory/verify` e `factory/judge` no SHA e torná-los obrigatórios no ruleset (S2). Uma aprovação velha deixa de passar. Exige Commit statuses com escrita no token.                           | P       |
| O4  | **Orçamento por tempo e detecção de travamento**                 | O Symphony usa `stall_timeout_ms` e `turn_timeout_ms`. O Cursor não reporta tokens, então o custo não aparece no Canvas.                                                   | Um prazo por etapa, desde `station_started_at`, e um "sem eventos novos em N min". Ao estourar, `POST /interrupt` e parar.                                                                              | P       |
| O5  | **Reconciliar a cada ciclo**                                     | O Symphony confere as issues ativas a cada ciclo e para o worker quando uma deixa de ser elegível.                                                                         | É o R17, mais recarregar a config sem restart.                                                                                                                                                          | P       |
| O6  | **Evidência visual** (feito em 18/10 como a etapa Demo, seção 6) | A skill `qa-changes` pede print por estado e GIF da interação. Os vídeos de demonstração do Symphony e os prints de antes e depois são usados como prova de trabalho.      | O Verifier anexa à revisão prints em 1440 e 390 px, comparados com os mocks do `.specs`.                                                                                                                | M–G     |
| O7  | **Modelo diferente para Verify e Judge**                         | Revisores da mesma família têm os mesmos pontos cegos (Addy Osmani, "agentic code review").                                                                                | Um `agent_profile_id` por etapa no `config.json`, com um modelo que não seja Grok na verificação e na revisão.                                                                                          | P       |
| O8  | **A fábrica roda o job instável de novo**                        | A skill `iterate` aceita até 3 reexecuções por SHA antes de considerar a falha real.                                                                                       | Dar ao token Actions com escrita. O `check_ci` roda o job falho de novo uma vez por SHA.                                                                                                                | P       |
| O9  | **Portões humanos por risco**                                    | No "Verification Stack" do OpenHands, a revisão termina com um nível de risco, e o alto vai para uma pessoa.                                                               | Expandir o `schema_paths` para caminhos de risco (RLS, auth, vault). A the-judge emite o nível, e o `check_final` o lê.                                                                                 | P       |
| O10 | **Ciclo de aprendizado**                                         | A skill `learn-from-code-review` transforma comentários de revisão em skills ou no AGENTS.md.                                                                              | Uma automação mensal no Canvas que lê as revisões da the-judge e os FAIL do Verify e abre um PR em rascunho com ajustes no `tlc-implement` e no AGENTS.md.                                              | M       |
| O11 | **Placar de métricas**                                           | Custo por PR, tempo por etapa, taxa de aprovação na primeira tentativa.                                                                                                    | Um resumo semanal no Slack, calculado do `state.json` e da API de conversas: primeiro Verify e primeira Judge, rodadas, minutos por etapa, tempo até a revisão (84–125 min até agora), paradas humanas. | P–M     |
| O12 | **Pequenos ajustes da API**                                      | `parent_conversation_id` agrupa as etapas de uma issue no Canvas. `ask_agent` responde "status?" sem mexer no histórico.                                                   | Tornar a interface do Canvas legível por issue.                                                                                                                                                         | P       |
| O13 | **Cenários "holdout"**                                           | A StrongDM guarda cenários fora do código, longe dos agentes (Simon Willison, "software factory").                                                                         | Cenários e2e feitos a partir dos mocks, rodados depois da Judge, fora do worktree.                                                                                                                      | G       |

**O que não vale a pena com o Cursor via ACP:**

- O critic, o iterative refinement, o condenser e as ferramentas e MCP do agente OpenHands lançam `NotImplementedError` para `ACPAgent`.
- O `/goal` depende de um LLM próprio.
- O security analyzer e a confirmation policy não enxergam as chamadas de ferramenta do Cursor, porque as permissões do ACP são aprovadas automaticamente.
- Os sub-agents do OpenHands não se aplicam, porque o Cursor tem os seus.
- Mover o orquestrador para o serviço de automações não funciona: o Socket Mode precisa de um processo sempre ligado, e webhooks exigem URL pública.
- O runtime Docker por conversa só faz sentido mais adiante.

### 15.5 Ordem sugerida

1. **Agora, antes de liberar mais issues:**
   - S1 (separar segredos e tirar o `sudo`), S10 (Slack MCP), S3 (validar `Branch:`/`Base:`), S6 (token no ai-memory), S7 (git sem hooks no orquestrador);
   - R1, R2, R3, R4, R10.

   Tudo pequeno.

2. **Esta semana:**
   - S2 (GitHub Pro e ruleset) com O3 (vereditos como statuses);
   - S4 e S5/S11 (filtrar autor e dar identidade própria aos agentes no Linear);
   - Q1, Q2 e Q6.

   Esses três últimos devem cortar perto de metade do tempo por issue.

3. **Depois:**
   - Q3 (prova de CSS renderizado), Q4 (Judge em paralelo), Q5 (CI mais rápido);
   - R5 com O4 (prazos);
   - O1 (Stop hook), O7 (outro modelo na revisão) e Q9 (Slack mais informativo).
4. **Mais adiante:** O2 (encadear dependências), O6 (evidência visual), O10 (ciclo de aprendizado), O11 (métricas) e O13 (holdout).

### 15.6 Fontes consultadas pela pesquisa

- OpenHands: [The Verification Stack](https://openhands.dev/blog/20260506-the-verification-stack), [What is an AI dark factory](https://openhands.dev/blog/what-is-an-ai-dark-factory), [ACP agents (SDK)](https://docs.openhands.dev/sdk/guides/agent-acp.md), [ACP agents (Agent Canvas)](https://docs.openhands.dev/openhands/usage/agent-canvas/acp-agents).
- Addy Osmani: [Software factories](https://addyosmani.com/blog/software-factories/), [Agentic code review](https://addyosmani.com/blog/agentic-code-review/), [Cognitive limits of parallel agents](https://addyosmani.com/blog/cognitive-parallel-agents/), [Practical loop engineering](https://addyosmani.com/blog/practical-loop-engineering/), [Human judgment doesn't leave the software](https://addyosmani.com/blog/human-judgment-doesnt-leave-the-software/).
- Outras: [Symphony SPEC (OpenAI)](https://github.com/openai/symphony/blob/main/SPEC.md), [Harness engineering (Google)](https://developers.googleblog.com/the-anatomy-of-harness-engineering-how-to-evaluate-iterate-and-guard-ai-coding-agents/), [Software factory (Simon Willison)](https://simonwillison.net/2026/feb/7/software-factory/).
- Skills locais do OpenHands: `iterate`, `qa-changes`, `github-delivery-watchdog`, `learn-from-code-review`, `openhands-automation`.
