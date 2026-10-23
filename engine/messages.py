"""What the factory says in Slack, in each language a project can pick with `slack.language`.

Linear comments, GitHub comments and station prompts stay in English; the person's Slack thread is translated.
Each entry is a `str.format` template; a tuple holds the singular and plural of a counted word.
"""

LANGUAGES = ("pt-BR", "en")

MESSAGES = {
    # thread root
    "root.waiting": {"pt-BR": "🙋 Esperando você no {stage}", "en": "🙋 Waiting for you at {stage}"},
    "root.approval": {"pt-BR": "✋ Esperando você aprovar o plano", "en": "✋ Waiting for you to approve the plan"},
    "root.blocked": {"pt-BR": "🚧 Esperando {what}", "en": "🚧 Waiting for {what}"},
    "root.blocked_other": {"pt-BR": "outras entregas", "en": "other work"},
    "root.merged": {"pt-BR": "🎉 Mergeado em `{base}`", "en": "🎉 Merged into `{base}`"},
    "root.queued": {"pt-BR": "⏳ Na fila", "en": "⏳ In the queue"},
    "root.final": {"pt-BR": "✅ Aprovado pela Judge", "en": "✅ Approved by the Judge"},
    "root.review": {"pt-BR": "👀 Pronto para sua revisão", "en": "👀 Ready for your review"},
    "root.dropped": {"pt-BR": "🚪 Saiu da factory", "en": "🚪 Left the factory"},
    "root.refused": {"pt-BR": "⛔ Não começou: falta algo na descrição",
                     "en": "⛔ Not started: the description is missing something"},
    "root.no_pr": {"pt-BR": "🔗 Sem PR ainda", "en": "🔗 No PR yet"},
    "root.rounds": {"pt-BR": "🔧 Correções: {round} de {max}", "en": "🔧 Fix rounds: {round} of {max}"},

    # stations
    "station.started": {"pt-BR": "{name} começou", "en": "{name} started"},
    "station.restarted": {"pt-BR": "{name} recomeçou", "en": "{name} restarted"},
    "station.follow": {"pt-BR": "👀 Acompanhar ao vivo", "en": "👀 Follow it live"},
    "start.plan": {"pt-BR": "transforma a issue em tarefas com critérios que dá para conferir.",
                   "en": "turns the issue into tasks with criteria that can be checked."},
    "start.build": {"pt-BR": "implementa as tarefas, roda as provas do checklist e abre um PR em rascunho.",
                    "en": "builds the tasks, runs the checklist proofs and opens a draft PR."},
    "start.verify": {"pt-BR": "um agente que não escreveu o código refaz cada prova do checklist.",
                     "en": "an agent that did not write the code runs every checklist proof again."},
    "start.judge": {"pt-BR": "um agente revisa o PR com a skill the-judge e dá o veredito.",
                    "en": "an agent reviews the PR with the the-judge skill and gives the verdict."},
    "start.fix": {"pt-BR": "corrige o que a última etapa apontou.", "en": "fixes what the last station found."},
    "start.demo": {"pt-BR": "grava prints de antes e depois e um vídeo por caso de uso.",
                   "en": "records before and after screenshots and one video per use case."},
    "retry.error": {"pt-BR": "a conversa anterior caiu por erro de conexão do Cursor (`{error}`). Uma conversa nova, com um "
                             "processo novo do Cursor, segue com o mesmo pedido (tentativa {attempt} de {max}). Não conta "
                             "como rodada de correção.",
                    "en": "the previous conversation dropped on a Cursor connection error (`{error}`). A new conversation, "
                          "with a new Cursor process, carries on with the same request (attempt {attempt} of {max}). It "
                          "does not count as a fix round."},
    "retry.asked": {"pt-BR": "a seu pedido, uma conversa nova, com um processo novo do Cursor, segue com o mesmo pedido.",
                    "en": "as you asked, a new conversation, with a new Cursor process, carries on with the same request."},
    "build.continues": {"pt-BR": "Build continua", "en": "Build continues"},
    "build.handoff": {"pt-BR": "a conversa anterior encheu o contexto e passou o bastão. Uma conversa nova segue do ponto "
                               "marcado no checklist.",
                      "en": "the previous conversation filled its context and handed off. A new conversation carries on "
                            "from the point marked in the checklist."},
    "fix.round": {"pt-BR": "Correção {round} de {max}", "en": "Fix {round} of {max}"},

    # plan and its approval
    "plan.ready": {"pt-BR": "✅ *Plan pronto*: `{tasks}`.", "en": "✅ *Plan ready*: `{tasks}`."},
    "plan.go_live": {"pt-BR": "⚠️ *Bloqueia o go-live*: {items}", "en": "⚠️ *Blocks go-live*: {items}"},
    "plan.approval": {"pt-BR": "✋ *Aprove o plano antes do Build*\n{summary}\n\n📋 Arquivo de tarefas: `{tasks}`\n"
                               "👉 Responda *aprovo* ou *entendi* aqui na thread para eu seguir. Qualquer outra resposta "
                               "volta para o Plan como pedido de ajuste.",
                      "en": "✋ *Approve the plan before Build*\n{summary}\n\n📋 Tasks file: `{tasks}`\n"
                            "👉 Reply *approve* or *got it* in this thread and I carry on. Any other reply goes back to "
                            "Plan as a change request."},
    "plan.no_summary": {"pt-BR": "_O Plan não mandou um resumo. Leia o arquivo de tarefas abaixo._",
                        "en": "_Plan sent no summary. Read the tasks file below._"},
    "plan.approved": {"pt-BR": "👍 *Plano aprovado*: o Build começa.", "en": "👍 *Plan approved*: Build starts."},
    "plan.revise": {"pt-BR": "📝 *Ajuste pedido no plano*: o Plan refaz com o seu pedido e pede aprovação de novo.",
                    "en": "📝 *Plan change requested*: Plan revises it with your request and asks for approval again."},
    "plan.skipped": {"pt-BR": "⏭️ *Plan pulado*: `{task}` já está planejado no `{base}`.",
                     "en": "⏭️ *Plan skipped*: `{task}` is already planned on `{base}`."},

    # build, verify, judge, fix
    "build.ready": {"pt-BR": "✅ *Build pronto*: PR em rascunho <{pr_url}|#{pr_number}> aberto. <{checklist_url}|📋 Checklist>",
                    "en": "✅ *Build done*: draft PR <{pr_url}|#{pr_number}> opened. <{checklist_url}|📋 Checklist>"},
    "base.updated": {"pt-BR": "🔄 *Base atualizada*: trouxe o `{base}` mais recente para `{branch}`.",
                     "en": "🔄 *Base updated*: merged the latest `{base}` into `{branch}`."},
    "base.conflict": {"pt-BR": "trazer o `{base}` mais recente conflita em {files}.",
                      "en": "merging the latest `{base}` conflicts in {files}."},
    "base.and_more": {"pt-BR": " e outros", "en": " and more"},
    "base.moved": {"pt-BR": "🔄 *O `{base}` andou depois da revisão*: vou trazer as mudanças, e Verify e Judge rodam de novo.",
                   "en": "🔄 *`{base}` moved after the review*: I merge the changes in, and Verify and Judge run again."},
    "final.pr_conflict": {"pt-BR": "o GitHub aponta conflito entre o PR e o `{base}`.",
                          "en": "GitHub reports a conflict between the PR and `{base}`."},
    "final.ci_failed": {"pt-BR": "o CI falhou em {checks} no commit final.",
                        "en": "CI failed in {checks} on the final commit."},
    "demo.queued": {"pt-BR": "⏳ *Demo na fila*: outra demo está gravando agora. Esta começa assim que ela terminar.",
                    "en": "⏳ *Demo queued*: another demo is recording now. This one starts when it ends."},
    "demo.ready": {"pt-BR": "🎬 *Demo pronta*: {shots} de antes e depois e {videos}.\n"
                            "<{linear_url}|▶️ Assistir no Linear> · <{github_url}|🖼️ Ver no PR>",
                   "en": "🎬 *Demo ready*: {shots} and {videos}.\n"
                         "<{linear_url}|▶️ Watch on Linear> · <{github_url}|🖼️ See it on the PR>"},
    "word.comparison": {"pt-BR": ("comparação", "comparações"),
                        "en": ("before/after comparison", "before/after comparisons")},
    "word.video": {"pt-BR": ("vídeo", "vídeos"), "en": ("video", "videos")},
    "review.ready": {"pt-BR": "🎉 *Pronto para sua revisão!* A Judge aprovou o <{pr_url}|{pr_link}>.",
                     "en": "🎉 *Ready for your review!* The Judge approved <{pr_url}|{pr_link}>."},
    "review.demo": {"pt-BR": "\n🎬 Demo: <{linear}|▶️ assistir no Linear> · <{github}|🖼️ ver no PR>",
                    "en": "\n🎬 Demo: <{linear}|▶️ watch on Linear> · <{github}|🖼️ see it on the PR>"},
    "review.next_demo": {"pt-BR": "\n👉 Aprove a feature pela demo e revise o PR. O merge é com você.",
                         "en": "\n👉 Approve the feature from the demo and review the PR. Merging is up to you."},
    "review.next": {"pt-BR": "\n👉 Revise o PR. O merge é com você.", "en": "\n👉 Review the PR. Merging is up to you."},
    "verify.pass": {"pt-BR": "✅ *Verify passou*: todas as provas do checklist conferem. {report}",
                    "en": "✅ *Verify passed*: every checklist proof holds. {report}"},
    "verify.fail": {"pt-BR": "❌ *Verify reprovou*: algumas provas não conferem. {report}",
                    "en": "❌ *Verify failed*: some proofs do not hold. {report}"},
    "verify.report": {"pt-BR": "📄 Relatório", "en": "📄 Report"},
    "verify.fix_why": {"pt-BR": "corrige as provas que o Verify reprovou, a partir do relatório.",
                       "en": "fixes the proofs Verify failed, from its report."},
    "judge.changes": {"pt-BR": "✋ *Judge pediu mudanças*. <{url}|📝 Ver a revisão>",
                      "en": "✋ *Judge requested changes*. <{url}|📝 See the review>"},
    "judge.comment": {"pt-BR": "💬 *Judge deixou comentários*. <{url}|📝 Ver a revisão>",
                      "en": "💬 *Judge left comments*. <{url}|📝 See the review>"},
    "judge.other": {"pt-BR": "💬 *Judge respondeu {verdict}*. <{url}|📝 Ver a revisão>",
                    "en": "💬 *Judge answered {verdict}*. <{url}|📝 See the review>"},
    "judge.fix_why": {"pt-BR": "responde à <{url}|revisão> da Judge.", "en": "answers the Judge's <{url}|review>."},
    "judge.approved": {"pt-BR": "✅ *Judge aprovou*. <{url}|📝 Ver a revisão>\n🚦 Agora confiro o CI do commit aprovado{then}",
                       "en": "✅ *Judge approved*. <{url}|📝 See the review>\n🚦 Now I check CI on the approved commit{then}"},
    "judge.then_demo": {"pt-BR": ", e depois vem a Demo.", "en": ", and then the Demo."},
    "judge.then_end": {"pt-BR": ".", "en": "."},
    "fix.no_commit": {"pt-BR": "📨 *Correção sem commit*: o head continua. O que restava no CI não é defeito deste PR.",
                      "en": "📨 *Fix without a commit*: the head stays. What was left in CI is not a defect of this PR."},
    "fix.sent": {"pt-BR": "📤 *Correção {round} enviada* (`{head}`): Verify e Judge rodam de novo, e o CI roda depois do APPROVE.",
                 "en": "📤 *Fix {round} pushed* (`{head}`): Verify and Judge run again, and CI runs after the APPROVE."},

    # the person
    "human.needed": {"pt-BR": "🙋 *Preciso de você* no {stage}\n{question}\n💬 Responda aqui na thread ou na issue do Linear.",
                     "en": "🙋 *I need you* at {stage}\n{question}\n💬 Reply in this thread or on the Linear issue."},
    "reply.fix_round": {"pt-BR": "📨 *Recebi sua resposta*: abro mais uma rodada de 🔧 Fix com ela, sem voltar ao Verify.",
                        "en": "📨 *Got your answer*: I open one more 🔧 Fix round with it, without going back to Verify."},
    "reply.fix_why": {"pt-BR": "corrige o que o Verify reprovou, seguindo a sua resposta.",
                      "en": "fixes what Verify failed, following your answer."},
    "reply.recheck": {"pt-BR": "📨 *Recebi sua resposta*: vou conferir a issue de novo.",
                      "en": "📨 *Got your answer*: I will check the issue again."},
    "reply.continues": {"pt-BR": "📨 *Recebi sua resposta*: o {stage} continua.",
                        "en": "📨 *Got your answer*: {stage} carries on."},
    "review.closed": {"pt-BR": "🚪 *Não reabro*: o <{pr_url}|PR #{pr_number}> está {state}. Abra uma issue nova para a mudança.",
                      "en": "🚪 *I won't reopen it*: <{pr_url}|PR #{pr_number}> is {state}. Open a new issue for the change."},
    "review.state_merged": {"pt-BR": "com merge", "en": "merged"},
    "review.state_closed": {"pt-BR": "fechado", "en": "closed"},
    "review.reopen": {"pt-BR": "📝 *Pedido recebido na revisão*: o <{pr_url}|PR #{pr_number}> volta para rascunho, a issue "
                               "volta para In Progress e uma rodada de Fix começa com o seu pedido.",
                      "en": "📝 *Request received during review*: <{pr_url}|PR #{pr_number}> goes back to draft, the issue "
                            "goes back to In Progress, and a Fix round starts with your request."},
    "review.reopen_why": {"pt-BR": "faz o que você pediu aqui na thread.", "en": "does what you asked in this thread."},

    # pickup and the queue
    "refuse": {"pt-BR": "⛔ *Não consigo começar*: {why}\n👉 Corrija a descrição e remova a label `{label}` para tentar de novo.",
               "en": "⛔ *I can't start*: {why}\n👉 Fix the description and remove the `{label}` label to try again."},
    "refuse.folder": {"pt-BR": "a descrição precisa citar a pasta da feature, como `{folder}`.",
                      "en": "the description must name the feature folder, such as `{folder}`."},
    "refuse.branch": {"pt-BR": "a `Branch:` `{branch}` é a própria base. Use uma branch só desta issue.",
                      "en": "`Branch:` `{branch}` is the base itself. Use a branch of this issue's own."},
    "dropped": {"pt-BR": "🚪 *Saiu da factory*: a issue não está mais em {ready} com a label `{label}`.",
                "en": "🚪 *Left the factory*: the issue is no longer in {ready} with the `{label}` label."},
    "blocked": {"pt-BR": "🚧 *Esperando*: {reasons}. Começa sozinha quando estiverem Done ou com merge.",
                "en": "🚧 *Waiting*: {reasons}. It starts on its own once they are Done or merged."},
    "picked": {"pt-BR": "📥 *Entrou na factory*: parte do `{base}` na branch `{branch}`.",
               "en": "📥 *Joined the factory*: starts from `{base}` on the branch `{branch}`."},
    "queued": {"pt-BR": "⏳ *Na fila*: posição {position}. Já há {max} issues rodando; esta começa quando uma vaga abrir.",
               "en": "⏳ *Queued*: position {position}. {max} issues are already running; this one starts when a slot frees up."},
    "merged": {"pt-BR": "🎉 *PR mergeado!* <{pr_url}|PR #{pr_number}> entrou em `{base}`{by}. A issue saiu da factory. Valeu! 🙌",
               "en": "🎉 *PR merged!* <{pr_url}|PR #{pr_number}> went into `{base}`{by}. The issue left the factory. Thanks! 🙌"},
    "merged.by": {"pt-BR": " por {who}", "en": " by {who}"},

    # worktree cleanup
    "worktree.kept": {"pt-BR": "🧹 *Worktree mantido*: {why} Ele fica em `{path}` até isso ser resolvido.",
                      "en": "🧹 *Worktree kept*: {why} It stays at `{path}` until that is resolved."},
    "worktree.running": {"pt-BR": "uma conversa da Canvas ainda está rodando nele.",
                         "en": "a Canvas conversation is still running in it."},
    "worktree.dirty": {"pt-BR": "ele tem mudanças fora de commit.", "en": "it has uncommitted changes."},
    "worktree.no_merge": {"pt-BR": "não achei o merge commit do PR #{pr_number}.",
                          "en": "I could not find the merge commit of PR #{pr_number}."},
    "worktree.unmerged": {"pt-BR": "a branch `{branch}` tem commits que não entraram no PR #{pr_number}.",
                          "en": "the branch `{branch}` has commits that did not go into PR #{pr_number}."},

    # Slack mentions
    "mention.taken": {"pt-BR": "👀 Deixa comigo! <{url}|Acompanhar ao vivo>", "en": "👀 On it! <{url}|Follow it live>"},
    "mention.stopped": {"pt-BR": "🛑 O agente parou com status *{status}*.", "en": "🛑 The agent stopped with status *{status}*."},
    "mention.empty": {"pt-BR": "✅ Feito, sem mensagem final.", "en": "✅ Done, with no final message."},
}

# Words the person can reply with to approve a plan, in any language a project picks. The whole reply has to be one.
APPROVALS = {"aprovo", "aprovado", "aprova", "entendi", "ok", "okay", "sim", "pode seguir", "segue", "manda ver",
             "approve", "approved", "got it", "understood", "yes", "lgtm", "go", "go ahead", "👍", "✅"}
