# Contributing and branching

`main` is protected and always releasable. Nobody pushes to it directly.

## Branches

| Branch | For | Example |
|---|---|---|
| `main` | The released code. Changes arrive only by pull request. | |
| `feat/<topic>` | New features | `feat/instahyre-roles` |
| `fix/<topic>` | Bug fixes | `fix/radio-labels` |
| `security/<topic>` | Security fixes (kept small and quick to review) | `security/host-check` |
| `chore/<topic>` | Tooling, docs, dependencies | `chore/bump-angular` |

Branch from `main`, keep branches short-lived, and delete them after merging.

## Pull requests

1. Open a PR into `main` using the template.
2. CI (`CI/CD` and `Security`) must be green.
3. The owner (see `.github/CODEOWNERS`) reviews and merges.
4. Merge with **squash**, so `main` stays one commit per change.

## Releases and hotfixes

Releases are tags on `main` named `vX.Y.Z`. A hotfix is a normal `fix/` or
`security/` branch off `main`, merged the same way.

## Before you push

```
just check
```
