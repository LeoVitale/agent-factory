# Histórico da construção da fábrica agêntica

Este é o registro de como a fábrica foi construída, desde a primeira pergunta até três issues do Visual refresh chegarem a In review com PR aberto. Os horários estão em UTC. O horário local é UTC−3. Os marcados com "≈" foram reconstruídos pela ordem da conversa, e os demais vêm de logs, do GitHub ou do Linear. O que a fábrica é e como funciona está em [factory.md](factory.md).

## Resumo

| Marco                                                          | Quando (UTC)  |
| -------------------------------------------------------------- | ------------- |
| Primeira pergunta: "o que mais eu preciso fazer?"              | 17/10 ≈ 23:30 |
| Sandbox Docker com Cursor, skills e Linear no ar               | 18/10 00:34   |
| Primeira conversa Cursor-ACP aberta pela API do sandbox        | 18/10 ≈ 03:30 |
| Orquestrador ligado, conectado ao Slack                        | 18/10 04:25   |
| Novas regras do AGENTS.md na `main` (PR #68)                   | 18/10 04:44   |
| Primeira issue pega pela fábrica (HES-104)                     | 18/10 05:45   |
| Fábrica com 3 vagas em paralelo                                | 18/10 06:03   |
| Primeiro APPROVE da the-judge e primeiro PR em In review (#72) | 18/10 07:20   |
| Três PRs abertos em In review (#72, #73, #74)                  | 18/10 08:15   |
| Documentação e auditoria da fábrica (`factory.md`)             | 18/10 14:05   |

## 1. Ponto de partida (17/10, noite)

- **O que existia:**
  - O OpenHands **Agent Canvas 1.27.1** instalado pelo npm no Mac, **sem sandbox**, na porta 8100.
  - Dois perfis de agente ACP apontando para o Cursor CLI (`Cursor-ACP` e `Cursor-Grok-xhigh-fast`).
  - MCPs do Linear e do Slack configurados no OpenHands.
- **Primeira resposta:** o que falta para uma fábrica. Os pontos principais:
  - decidir o que automatizar;
  - mudar as regras do `AGENTS.md`, que proibiam um agente de começar quando o status muda;
  - credencial do GitHub;
  - Docker para o `test:rls`;
  - custo.
- **Sem sandbox, o agente teria acesso a tudo** que o seu usuário acessa: CLIs logadas (Supabase, Vercel, `gh`), `~/.ssh`, outros repositórios. A sugestão foi um usuário macOS separado, que você recusou: **"eu preciso então de um sandbox com o Linear, Cursor, todas minhas agents skills globais"**.

## 2. Sandbox Docker (18/10, 00:00–01:30)

- **Investigação:** como a instalação estava montada (`~/.openhands`, perfis, configurações sem segredos, skills públicas `agent-canvas-environment` e `docker`) e quais as opções de sandbox do README.
- **Decisão:** usar a opção "Docker sandbox" (o Canvas inteiro num container) com uma imagem própria.
  - **Imagem:** `Dockerfile` em cima de `ghcr.io/openhands/agent-canvas:1.27.1` com o Cursor CLI.
  - **Montagens:** `compose.yaml` com o estado separado (`openhands-state`), um volume `home` para o login do Cursor e as quatro pastas de skills montadas só para leitura **nos mesmos caminhos do Mac**, para os links simbólicos funcionarem.
  - **Entrada:** `sandbox-entrypoint.sh` liga as skills e o `mcp.json` do Cursor.
  - **Linear:** `cursor-mcp.json` com a chave via `${env:LINEAR_API_KEY}`.
- **Projetos:** você escolheu um **clone dedicado** em `~/agent-sandbox/projects/project-hestia`. A sua cópia de trabalho nunca é montada.
- **00:34:** container no ar em http://127.0.0.1:8200/canvas. Os seus perfis Cursor foram copiados.
- **Login e chave:** você fez o `agent login` com a sua conta pessoal e colocou a chave do Linear no `.env`.
- **Teste:** o Cursor dentro do sandbox listou todas as skills (globais, do repo e plugins da conta) e leu o Linear. Ele enxergou o time **Hestia (HES)**, e não o "Leovitale / Project Hestia" que o `AGENTS.md` citava.
- **Verificação depois de reiniciar:** o login e o Linear continuaram valendo.

## 3. Desenho do fluxo (≈ 01:30–03:00)

- **Explicação do fluxo:** um orquestrador de polling no lugar de webhooks (tudo é local), uma conversa por etapa e a the-judge num contexto separado. Duas limitações do GitHub apareceram: o autor do PR não consegue aprovar o próprio PR, e o `test:rls` não roda no sandbox.
- **Diagrama:** você mandou o diagrama "A fábrica de software agêntica v2", e ele foi lido como uma **light factory**, com pessoas em Research e Review. Pesquisa sobre "agentic factory": BCG Platinion, Factory.ai, a lista awesome-software-factories, Symphony, e a diferença entre dark e light factory.
- **Skills TLC obrigatórias:** você pediu para garantir o uso das skills tlc. Cada etapa passou a ter um portão sobre o artefato da skill:
  - tasks file sem `Unresolved` do tipo `blocks`;
  - checklist;
  - `.verified.md` com `**Verdict**: PASS`;
  - revisão no commit atual.
- **Regras:** proposta da nova seção "Agent cycle" do `AGENTS.md` num worktree `chore/agentic-factory-cycle`, sem mexer no `feat/visual-refresh`. A label `factory` passou a ser o "sim" para push e PR em rascunho.
- **Slack:** canal por projeto, thread por issue, **Slack primeiro** quando a fábrica precisar de você, resposta pelo Slack ou pelo Linear, e registro em inglês no Linear. Você escolheu o **Socket Mode** e aceitou as duas formas de resposta.

## 4. Teste da API e orquestrador (≈ 03:00–04:25)

- **Teste da API:** uma conversa `Cursor-ACP` aberta pela API do sandbox rodou num worktree próprio, enxergou `tlc-plan`, `tlc-implement` e `the-judge`, e devolveu a resposta final. Esse teste validou o plano inteiro.
- **Linear:** os status já existiam (Ready for agent, In Progress, In review). Foram criadas as labels **`factory`** e **`factory:needs-human`** no time HES.
- **Orquestrador:** `factory/factory.py`, `config.json` e os prompts `plan`, `build`, `verify`, `judge`, `fix`, `answer` e `_result`. Ele usa um contrato de saída `FACTORY_RESULT` e é um processo único, sempre ligado, em vez de uma automação de cron, porque o Socket Mode exige uma conexão aberta.
- **Teste sem efeitos:** confirmou a leitura do Linear, o parser do `Unresolved`, a parada sem `FACTORY_RESULT` e os prompts sem campo sem preencher.
- **Tokens:** guias passo a passo para o fine-grained token do GitHub, os tokens do Slack (o app já tinha um "Default OpenHands App Token", e foi recomendado criar um próprio) e o member ID.
- **Verificação:** o GitHub autentica como LeoVitale, o bot do Slack tem todos os escopos e o app token abre conexão.
- **04:25:** serviço `agent-sandbox-factory` ligado, com o Socket Mode conectado.

## 5. Regras no repositório e primeiros ajustes (04:25–05:00)

- **PR #68** (`docs(agents): let the factory run the agent cycle`): criado às 04:27 e **com merge às 04:44**, antes da revisão.
- **Auto-fix** ligado no #68. O "PR checks" falhou num teste do cleaner-web sem relação com a mudança (`auth-provider.test.tsx`), e foi rodado de novo.
  - **PR #70** veio de uma sessão separada, que corrigiu esse teste instável: ele passou a esperar o payload do Sentry (30/30 falhas antes, 0/30 depois). Criado às 04:52 e com merge às 04:56.
- **Sem a permissão Checks:** os tokens fine-grained não oferecem essa permissão. A fábrica passou a ler o CI pelas **execuções do Actions e pelos commit statuses**, e os prompts de Build e Fix ganharam a instrução de ler o CI assim.
- **the-judge no #68:** COMMENT, com 2 should-fix (o Discover não citava a pasta da feature e o time e projeto estavam errados), 2 nits e 1 achado anterior ao PR.
  - **PR #69** trouxe as correções: criado às 04:51, com merge às 04:58.
  - A the-judge no #69 deu APPROVE, com 1 nit e 1 achado anterior ao PR. Os dois foram corrigidos no mesmo PR.
- **Canal do Slack:** a fábrica passou a **convidar você** para os canais que cria.

## 6. Menções no Slack (≈ 05:00–05:13)

- **A automação "Slack Channel Monitor"**, que você tinha criado às 02:11 no Canvas, respondia a menções ao `@openhands` no #visual-refresh por polling de 1 minuto.
- **Você pediu para a fábrica fazer o mesmo pelo Socket Mode.** Ela reage com 👀, abre uma conversa Cursor-ACP com as mesmas regras, posta o link, depois a resposta final, e encaminha as menções seguintes na mesma thread. O monitor foi **pausado**.
- **Nenhum evento chegava.** A fábrica era a única conexão do app (`num_connections: 2`, contando a de teste), e nem as mensagens do próprio bot geravam evento.
  - Causa: faltavam o **Save Changes** na Event Subscriptions e a **reinstalação do app**.
  - Às 05:12, o teste "responda com um ok" funcionou de ponta a ponta.

## 7. Prontidão para o primeiro teste (05:13–05:45)

- **pnpm:** faltava no sandbox. Foi instalado pelo corepack, que respeita o `pnpm@11.17.0` do repo, com `COREPACK_ENABLE_DOWNLOAD_PROMPT=0`. Um `pnpm install --frozen-lockfile` levou 53 s, e os testes de scripts passaram, 28 de 28.
- **Dependências entre issues:** a fábrica não tratava "blocked by", e todas as issues do Visual refresh cairiam no `feat/visual-refresh`, que é **o seu branch de trabalho**.
- **Campos da issue:** você apontou que as issues do Visual refresh têm `Base: feat/visual-refresh`. A fábrica passou a ler da descrição:
  - `Base:`, `Branch:` e `Task:` (que pula o Plan se a task existe na base);
  - `Start only after pull request #N`;
  - além de respeitar o "blocked by" do Linear.
- **PR #71:**
  - o CI passou a rodar em PRs para **`feat/**`** (antes, PRs para `feat/visual-refresh` não rodavam lint, testes nem build);
  - o formato da issue foi documentado no `AGENTS.md`;
  - criado às 05:29, com merge às 05:32;
  - a the-judge deu APPROVE com 1 nit (o branch existente é continuado), corrigido, e a segunda rodada também deu APPROVE.
- **Base em movimento:** você perguntou o que acontece se a base mudar durante o trabalho. A fábrica passou a:
  - fazer **merge da base antes de cada Verify**, sem rebase e sem force-push;
  - transformar **conflito em rodada de Fix**;
  - conferir, antes de In review, se o PR está **`MERGEABLE` e com a base atual**, refazendo Verify e Judge se a base andou.

  Isso foi testado num repositório descartável, nos casos sem mudança, com merge limpo e com conflito.

## 8. Primeira execução real (05:45–08:15)

- **05:45:** você moveu a HES-104 para Ready for agent com a label `factory`.
  - A fábrica criou o canal **#hestia-visual-refresh**, te convidou, leu base, branch e task, viu o PR #67 já com merge, **pulou o Plan** e começou o Build.
  - Você estava olhando o #visual-refresh, e por isso achou que "nada aconteceu".
- **06:03:** você tinha mais 4 issues em Ready for agent paradas pelo `max_active: 1`.
  - Ele foi para **3**, e a fábrica passou a registrar toda issue nova na hora e a avisar uma vez a posição na fila.
  - HES-103 e HES-105 começaram. HES-106 (bloqueada pela 105) e HES-107 (bloqueada pela 106) entraram na fila.
- **06:12–06:23:** PRs em rascunho para `feat/visual-refresh`: **#72** (HES-104, +1721/−363), **#73** (HES-103, +764/−95) e **#74** (HES-105). O CI completo rodou e passou.
- **Os três primeiros Verify deram FAIL:** as provas eram mais fracas que as afirmações. Os três Fix só adicionaram testes.
- **07:20:** a HES-104 recebeu **APPROVE**, e o PR #72 saiu do rascunho.
  - 1 segundo depois, a **automação de PR do GitHub dentro do Linear** devolveu a issue para In Progress.
  - A fábrica passou a recolocar a issue em In review nos ciclos seguintes (`keep_in_review`).
- **HES-103:** COMMENT da the-judge (selects com 220 px de altura), Fix 2 e **APPROVE às 07:40**.
- **HES-105:** COMMENT (contraste de 1.04:1 e 2.57:1), Fix 2 e **APPROVE às 08:15**.
- **Situação ao fim:** PRs **#72, #73 e #74 abertos em In review**. HES-106 espera a HES-105 chegar a Done, e HES-107 espera a HES-106.

## 9. Documentação e auditoria (≈ 13:00–14:05)

- **Quatro subagentes, todos só de leitura:**
  - revisão do código: 24 achados confirmados;
  - segurança: 17 riscos;
  - análise das execuções: linha do tempo, causas das falhas e custo;
  - pesquisa: recursos do OpenHands e práticas de 2026.
- **Achados de risco alto conferidos à mão:**
  - o ai-memory do Mac responde ao sandbox com o cabeçalho `Host` certo;
  - o usuário do container tem `sudo` sem senha;
  - o repositório não tem proteção de branch (plano Free);
  - uma issue que para antes do primeiro agente é descartada quando você responde;
  - a etapa final não confere o CI do último commit.
- **Resultado:** [factory.md](factory.md), com arquitetura, ferramentas, fluxo, resultados e cerca de 60 melhorias priorizadas.

## 10. Lições aprendidas

| Lição                                                                       | Consequência                                                                                                                 |
| --------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| Tokens fine-grained do GitHub não têm a permissão **Checks**                | Ler o CI pelas execuções do Actions e pelos commit statuses. A skill `iterate` (`gh pr checks`) não funciona com esse token. |
| O GitHub não deixa o autor aprovar o próprio PR                             | A the-judge posta COMMENT com "Intended verdict". A aprovação dela não conta para proteção de branch.                        |
| Repositório privado no plano Free não tem proteção de branch nem rulesets   | Hoje, as regras de push e merge existem só no prompt.                                                                        |
| Eventos do Slack só chegam depois de **Save Changes** e reinstalação do app | Uma configuração que aparece certa na tela pode não estar ativa.                                                             |
| Uma menção só notifica quem está no canal                                   | A fábrica convida você para os canais que cria.                                                                              |
| A automação de PR do GitHub no Linear mexe no status                        | Ela desfaz o In review quando o PR sai do rascunho. Ajustar em Team settings → Workflow.                                     |
| Fora de um projeto, o corepack baixa a versão mais nova do pnpm             | Dentro do repo, ele respeita o `packageManager`. Sem TTY, é preciso desligar o prompt de download.                           |
| Vistos do Mac, os worktrees do container aparecem como `prunable`           | Nunca rodar `git worktree prune` nesse clone a partir do Mac.                                                                |
| Workflows de `pull_request` filtram pela **base** do PR                     | Sem `feat/**`, PRs entre branches de feature entram sem CI.                                                                  |
| Um PR pode receber merge antes da revisão                                   | As correções da the-judge viraram um PR novo (#69).                                                                          |
| O jsdom não enxerga CSS computado                                           | Os dois achados da the-judge foram de altura e contraste, e o Verify não pegou nenhum dos dois.                              |
| Agentes gastam muito tempo esperando o CI dentro da conversa                | Foram 36% do tempo de agente nas primeiras três issues.                                                                      |

## 11. Artefatos criados

- **Fora do repo (`~/agent-sandbox`):**
  - `Dockerfile`, `compose.yaml`, `sandbox-entrypoint.sh`, `cursor-mcp.json`, `.env` (apenas os nomes das chaves);
  - `factory/factory.py`, `factory/config.json`, `factory/prompts/*.md`;
  - `factory-state/state.json`, `projects/` (o clone e os worktrees);
  - `factory.md` e este arquivo.
- **Repositório `LeoVitale/project-hestia`:**
  - PRs com merge: **#68** e **#69** (regras da fábrica no `AGENTS.md` e no `discover-linear-issue.mdc`), **#70** (teste instável), **#71** (CI para `feat/**` e campos da issue);
  - PRs abertos da fábrica: **#72**, **#73** e **#74**.
- **Linear:** labels `factory` e `factory:needs-human`. HES-103, 104 e 105 em In review, e HES-106 e 107 esperando.
- **Slack:** canal `#hestia-visual-refresh`, criado pela fábrica, e menções atendidas no `#visual-refresh`. A automação "Slack Channel Monitor" está pausada no Canvas.
