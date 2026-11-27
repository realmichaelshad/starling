# Third-party licences

The Python table below lists every package and version pinned in `backend/requirements.lock`,
because the lock decides what the container image contains. Where a row names a license, it was read
on 2026-09-20 from the installed package's own metadata in the development environment
(`importlib.metadata`), from the version installed on that date. A package that is in the lock but
was not in that inventory has none recorded here, and its row says where to look. This is an
inventory, not legal advice.

## Summary

- **Nothing copyleft among the 37 packages checked.** None of the 37 Python packages checked on
  2026-09-20 is GPL, AGPL or LGPL. They are MIT, BSD, Apache-2.0, PSF-2.0 or similar, except
  `certifi` (MPL-2.0, file-level: it is used unmodified). The other 10 packages in the lock have not
  been checked here: see each package's own metadata.
- **The Chrome extension ships no third-party packages.** `extension/package.json` has no runtime
  dependencies. The build tool compiles a few hundred bytes of its own glue code (`@wxt-dev/browser`,
  MIT) into the bundle; nothing else.
- **The website ships three typefaces** under the SIL Open Font License 1.1, with their licence texts
  beside them. It loads nothing from any other host. The extension bundles three of the same files,
  unmodified, for its onboarding page, with the same licence texts. Its icons and other images are
  covered under [Icons and images](#icons-and-images).
- **The container image carries each Python package's licence file**, because `pip` installs it in
  the package's `.dist-info` folder. That is how the attribution conditions of the BSD, MIT and Apache
  licences are met for the image. If you ever distribute Starling in another form, carry those files.

## Python packages in the running service (47)

| Package | Version | Licence (as the package declares it) |
|---|---|---|
| `annotated-doc` | 0.0.5 | MIT |
| `annotated-types` | 0.8.0 | MIT |
| `anthropic` | 2.3.0 | see the package's own metadata |
| `anyio` | 4.15.1 | MIT |
| `argon2-cffi` | 25.1.0 | MIT |
| `argon2-cffi-bindings` | 26.1.0 | MIT |
| `certifi` | 2026.7.22 | MPL-2.0 |
| `cffi` | 2.1.1 | MIT-0 |
| `click` | 8.5.0 | BSD-3-Clause |
| `cloudpickle` | 3.1.2 | see the package's own metadata |
| `cryptography` | 50.0.1 | Apache-2.0 OR BSD-3-Clause |
| `docstring-parser` | 0.18.0 | see the package's own metadata |
| `fastapi` | 0.141.1 | MIT |
| `h11` | 0.16.0 | MIT |
| `httpcore` | 1.0.9 | BSD-3-Clause |
| `httpcore2` | 2.16.0 | see the package's own metadata |
| `httptools` | 0.8.0 | MIT |
| `httpx` | 0.28.1 | BSD-3-Clause |
| `httpx2` | 2.16.0 | see the package's own metadata |
| `idna` | 3.20 | BSD-3-Clause |
| `jinja2` | 3.1.6 | BSD-3-Clause |
| `jiter` | 0.18.0 | see the package's own metadata |
| `joblib` | 1.6.0 | BSD-3-Clause |
| `markupsafe` | 3.0.3 | BSD-3-Clause |
| `narwhals` | 2.26.0 | MIT |
| `numpy` | 2.4.6 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| `opentelemetry-api` | 1.45.2 | see the package's own metadata |
| `pillow` | 12.3.0 | MIT-CMU |
| `pycparser` | 3.0 | BSD-3-Clause |
| `pydantic` | 2.13.5 | MIT |
| `pydantic-core` | 2.46.5 | MIT |
| `python-dotenv` | 1.2.3 | BSD-3-Clause |
| `python-multipart` | 0.0.32 | Apache-2.0 |
| `pyyaml` | 6.0.3 | MIT |
| `scikit-learn` | 1.9.1 | BSD-3-Clause |
| `scipy` | 1.17.1 | BSD-3-Clause |
| `sniffio` | 1.3.1 | see the package's own metadata |
| `starlette` | 1.6.0 | BSD-3-Clause |
| `threadpoolctl` | 3.7.0 | BSD-3-Clause |
| `truststore` | 0.10.5 | see the package's own metadata |
| `typing-extensions` | 4.16.0 | PSF-2.0 |
| `typing-inspection` | 0.4.4 | MIT |
| `tzdata` | 2026.4 | Apache-2.0 |
| `uvicorn` | 0.52.4 | BSD-3-Clause |
| `uvloop` | 0.22.1 | see the package's own metadata |
| `watchfiles` | 1.2.0 | MIT |
| `websockets` | 17.1 | BSD-3-Clause |

`numpy`, `scipy` and `scikit-learn` wheels bundle compiled numerical libraries (OpenBLAS and
similar) under their own permissive licences; the texts are inside each wheel.

## Tools used to build and test, never shipped

| Tool | Where | Licence |
|---|---|---|
| `wxt` | builds the extension | MIT |
| `typescript` | type check | Apache-2.0 |
| `vitest` | extension unit tests | MIT |
| `@types/chrome` | type definitions | MIT |
| `playwright` | browser journeys | Apache-2.0 |
| `pytest`, `pip-tools`, `pip-audit` | Python tests and the dependency lock | MIT, BSD-3-Clause, Apache-2.0 |

Their own dependencies (about 420 npm packages in the two lockfiles) are development-time only.

## Assets

| Asset | Where | Licence | Condition, and how it is met |
|---|---|---|---|
| Chakra Petch, IBM Plex Sans, IBM Plex Mono (WOFF2, Latin subset) | `web/static/fonts/`; three of the files also in `extension/public/fonts/` | SIL Open Font License 1.1 | the licence text ships with the fonts: `OFL-Chakra-Petch.txt`, `OFL-IBM-Plex-Sans.txt`, `OFL-IBM-Plex-Mono.txt`, unmodified. "Plex" is a reserved font name: do not ship a modified version under it |
| `backend/engine/models/starling-embedding.npy` | engine | none (not third-party) | a fixed random projection generated from a known seed; `backend/engine/tools/build_embedding_projection.py` regenerates it and proves the shipped file is exactly that |
| Logo, wordmark, icons, social images | `brand/`, `web/static/img/`, `extension/public/` | Starling's own | none: not third-party |

The extension's panel on X names X's interface typeface in a CSS `font-family` list so that it matches
the page it sits in, and uses whatever the page has already loaded. Only its own onboarding page uses
the bundled Starling fonts.

## Icons and images

The inline SVG icons in `web/templates` and the image `web/static/img/grain.png` have no recorded
third-party source in this repository. The repository does record three things about them. The
four-point star is Starling's own mark (`brand/source/starling-star.svg`). A comment in
`web/static/css/site.css` describes `grain.png` as a 300x300 tile built by a recipe. A comment in
`web/templates/_macros.html` says the "G" on the Google sign-in button is drawn to Google's sign-in
branding rules, so that mark is Google's and not Starling's.

## Known advisories in development tools

None of these packages is in the running service or inside the extension; they run on a developer's
machine while building and testing. The five found against the OSV database on 2026-09-20 were updated
on 2026-09-22, and `pnpm audit` in `extension/` and `npm audit` in `e2e/browser` then reported no known
vulnerabilities; the extension tests, both builds with the verifier and the browser journeys passed on
the new versions.

| Package | Advisory | Was | Now |
|---|---|---|---|
| `vitest`, `@vitest/mocker` | GHSA-82fw-gwwq-j7x9 (moderate): file read through the test runner's dev server | 4.1.10 | 4.1.11 |
| `adm-zip` (pinned in `extension/pnpm-workspace.yaml`) | GHSA-7q85-xj36-vmfc (high, memory), GHSA-vwc7-r8mq-g2x9 (moderate, symlinks) | 0.6.0 | 0.6.1 |
| `brace-expansion` | GHSA-mh99-v99m-4gvg, GHSA-rgw5-rvv9-x895 (high): memory exhaustion on hostile patterns | 1.1.16 | 1.1.21 |
| `nanoid` | GHSA-2v37-7h3g-55p8 (high): endless loop with a zero size | 3.3.16 | 3.3.19 |
| `postcss` | GHSA-fxqj-rqcc-2cmp (moderate): source-map file read | 8.5.19 | 8.5.28 |

`extension/` is managed with pnpm 10 or newer.

On 2026-09-20 the 71 Python packages of the development environment had no known advisory, apart
from the environment's own `setuptools`, which the deployed image removes together with `pip`. The
lock has changed since that date.
Update the npm tools with `pnpm update` in `extension/` and `npm update` in `e2e/browser`, then run the
extension tests and the browser journeys. `pip-audit` (in `backend/requirements-dev.txt`) repeats the
Python check against the lock.
