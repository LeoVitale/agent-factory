## How to finish

Follow `AGENTS.md`, including the Agent cycle section on the factory. Do not merge. Do not move the Linear status. Do not post to Slack. Do not deploy, run `supabase db push`, or touch hosted data.

When you stop, for any reason, end your final message with exactly one line:

FACTORY_RESULT: {"status": "<done | needs_human{{extra_status}}>"{{extra_fields}}, "question": "<only for needs_human: one clear question for the person, in English>"}

Use `needs_human` whenever a skill tells you to stop and ask, a proof is red, or a check turns out wrong or impossible. Never guess to avoid it.
