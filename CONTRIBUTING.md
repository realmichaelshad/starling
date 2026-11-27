# Contributing to Starling

Thanks for taking the time. Bug reports, fixes and improvements are all welcome.

## Before you start

- For anything larger than a small fix, open an issue first and say what you want to change and why.
  It saves you from building something that does not fit.
- A security problem does not belong in an issue. Report it privately: see [SECURITY.md](SECURITY.md).
- By contributing you agree that your work is released under the project's [MIT license](LICENSE).

## Setting up

You need Python 3.11, Node.js 20.12 or newer and pnpm 10 or newer. The [README](README.md#run-it)
has the full steps. In short:

```sh
python -m venv backend/.venv
backend/.venv/bin/python -m pip install -r backend/requirements.in -r backend/requirements-dev.txt
cd extension && pnpm install
```

On Windows the interpreter is `backend\.venv\Scripts\python.exe`.

## Checks to run

Run the suites that cover what you touched. Continuous integration runs all of them on every pull
request.

| You changed | Run | From |
|---|---|---|
| The gateway, services, supervisor or command line | `python -m pytest tests` | `backend/server` |
| The engine | `python -m pytest tests` | `backend/engine` |
| Templates, static files or `scripts/check_site.py` | `python -m pytest web/tests` | the repository root |
| The extension | `pnpm typecheck`, `pnpm test` and `pnpm build --mode e2e` | `extension` |
| Anything a customer journey crosses | the journeys in [e2e/README.md](e2e/README.md) | the repository root |

Before you push, run the release gate. It fails on anything that looks like a key, a machine path or
a file that should not be committed:

```sh
python scripts/check_release.py --git-tracked-only
```

## What a good change looks like

- **It comes with a test.** A fix has a test that fails without it. A new behavior has a test that
  describes it. Tests never call a real provider: the stand-ins in `e2e/harness` and the fakes in the
  test folders exist for that.
- **It keeps the promises the product makes.** Starling never presses Post, never sends anything
  before consent, and never charges for a draft it held back. A change that weakens one of these
  needs a very good reason.
- **Comments say why.** Most of the code explains the failure that led to it. Keep that habit: write
  down what went wrong and what the code now guarantees, not what the next line does.
- **The contract stays true.** If you change an interface between two components, update
  [docs/architecture/CONTRACTS.md](docs/architecture/CONTRACTS.md) in the same pull request.
- **No new runtime dependency without a reason.** If you add one, add it to
  `backend/requirements.in`, regenerate the lock with `scripts/lock-requirements.sh` (or the `.ps1`
  on Windows) and add it to [docs/THIRD-PARTY-LICENSES.md](docs/THIRD-PARTY-LICENSES.md).
- **Nothing private.** No keys, no real email addresses, no real people's posts in fixtures. Use
  `example.com` addresses and invented handles.

## Pull requests

Keep a pull request to one change. Say what it does, why, and how you checked it. If it changes
what a customer sees, a screenshot helps.
