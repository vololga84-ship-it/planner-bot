window.STATE =
{
  "slug": "night-notes-agent",
  "dir": "2026-09-13-night-notes-agent--wip",
  "title": "Ночной агент обработки заметок планера",
  "mode": "semi",
  "depth": "normal",
  "polish": null,
  "tier": "T0",
  "briefFile": "2026-09-13-brief.md",
  "memoryFile": "AGENTS.md",
  "skillDir": "/c/Users/loyol/.claude/skills/autopilot",
  "startedAt": "2026-09-13T15:55:36+05:00",
  "updatedAt": "2026-09-13T16:47:00+05:00",
  "finishedAt": null,
  "stages": [
    { "id": "preflight", "status": "done", "startedAt": "2026-09-13T15:55:36+05:00", "finishedAt": "2026-09-13T16:05:00+05:00" },
    { "id": "manifest",  "status": "done", "startedAt": "2026-09-13T16:05:00+05:00", "finishedAt": "2026-09-13T16:12:00+05:00" },
    { "id": "briefing",  "status": "done", "startedAt": "2026-09-13T16:12:00+05:00", "finishedAt": "2026-09-13T16:20:00+05:00" },
    { "id": "spec",      "status": "done", "startedAt": "2026-09-13T16:20:00+05:00", "finishedAt": "2026-09-13T16:45:00+05:00" },
    { "id": "plan",      "status": "skipped", "note": "ярус T0 — без разбивки на таски" },
    { "id": "build",     "status": "active", "startedAt": "2026-09-13T16:47:00+05:00" },
    { "id": "review",    "status": "pending" },
    { "id": "final",     "status": "pending" }
  ],
  "requirements": {
    "total": 7, "done": 0, "inTicket": 0, "inSpec": 4,
    "placeholder": 0, "deferred": 3, "dropped": 0
  },
  "tickets": [],
  "singlePass": null,
  "tests": null,
  "debt": { "placeholders": [], "assumptions": [], "emptyEnv": [] },
  "additions": [],
  "coverage": {
    "findings": 5,
    "actions": [
      "R03/R05i — Groq vs Pro-подписка: реальная развилка, задан вопрос пользователю, ответ «Groq сейчас» — записано в бриф и манифест",
      "разбиение длинных сообщений — добавлен жёсткий fallback по символам для абзаца длиннее 4096",
      "лимит 40 заметок — уточнено, что 'по времени' значит сортировку по колонкам Дата/Время листа",
      "конкретные тех.решения (модель, температура, время запуска, число 40, границы модулей) — craft-уровень, парента не требует, оставлено как есть"
    ]
  },
  "concerns": [],
  "reviewers": { "manifestSpec": null, "craft": null },
  "blind": null
}
