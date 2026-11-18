# Push migration note — 2026-11-18

Pushes to this repo from the Queen B VM no longer use `git push` with a
token embedded in the git remote URL. The fine-grained PAT expired and the
old token in the URL died with it; SSH is blocked by the VM's proxy.

New flow: `scripts/connector-push.sh "<message>" <paths...>` in the
goal workspace pushes the listed files through the GitHub connector
(`push_files` over the API) and then re-syncs the local checkout with
`git fetch` + `git reset --hard origin/main`.

The remote URL is now tokenless (`https://github.com/omnisight27/index.html.git`).
GitHub Actions workflows are untouched — they still use `GITHUB_TOKEN`.
