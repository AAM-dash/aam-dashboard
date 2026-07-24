# Amazing Animal Minds — KPI Dashboard

Private business dashboard for Amazing Animal Minds. The data payload in
`index.html` is AES-256-GCM encrypted; the page asks for a password before
anything is shown. Refreshed hourly by a GitHub Actions workflow
(see `tools/RUNBOOK.md`) — runs entirely on GitHub's servers, no external
service needs to be online for it to fire.
