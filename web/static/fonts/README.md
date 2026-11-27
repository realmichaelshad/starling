# Fonts

The three typefaces the Starling website uses are served from this folder, not from anyone else's
server. The Content-Security-Policy forbids third-party requests, so a hosted font service is not
an option, and self-hosting also means a visitor's browser tells no one else which pages they read.

## Files

| File | Family | Weight | Size |
|---|---|---|---|
| `chakra-petch-500.woff2` | Chakra Petch | 500 | headings and buttons |
| `chakra-petch-600.woff2` | Chakra Petch | 600 | headings and buttons |
| `chakra-petch-700.woff2` | Chakra Petch | 700 | headings and buttons |
| `ibm-plex-sans-variable.woff2` | IBM Plex Sans | 400 to 600, variable | body text |
| `ibm-plex-mono-400.woff2` | IBM Plex Mono | 400 | numbers, notes and code |
| `ibm-plex-mono-500.woff2` | IBM Plex Mono | 500 | numbers, notes and code |

Licence texts, one per family: `OFL-Chakra-Petch.txt`, `OFL-IBM-Plex-Sans.txt`,
`OFL-IBM-Plex-Mono.txt`.

## Source

Fetched on 2026-09-18 from Google Fonts' official service, which is the publisher's own
distribution for all three families. Each file is that service's **Latin subset**: the characters
in the `unicode-range` declared in `../css/tokens.css`. A character outside that range (the
four-point star used as the Starling mark, for one) is drawn by the next family in the stack, which
is why every stack in `tokens.css` ends in system faces.

IBM Plex Sans is one variable file covering weights 400 to 600, so it is declared once rather than
once per weight; the two static Chakra Petch and IBM Plex Mono weights each need their own file.

## Licence

All three families are licensed under the **SIL Open Font License, Version 1.1**. The licence
requires its text to travel with the font files, so the three `OFL-*.txt` files above ship
unmodified next to the fonts and are copied into every deliverable. Do not edit them, and do not
remove one while its font files are still here.

IBM Plex Sans and IBM Plex Mono carry the **Reserved Font Name "Plex"**. Under the OFL that name
may not be used for a modified version of the font, so these files must not be renamed to something
that keeps "Plex" if they are ever subsetted or otherwise changed.

Chakra Petch is copyright 2018 The Chakra Petch Project Authors. IBM Plex Sans and IBM Plex Mono
are copyright 2017 IBM Corp. The exact copyright lines are at the top of each licence file.

## Rules the checker enforces

`scripts/check_site.py` fails the build if any of this drifts:

- every `@font-face` in `../css/*.css` declares `font-display: swap`, so text is readable while a
  font is still arriving;
- every `@font-face` points at a relative `../fonts/<name>.woff2` URL that exists here;
- every `.woff2` file in this folder is used by some `@font-face` (no dead weight in the download);
- every family declared in CSS has its `OFL-<Family>.txt` here;
- this README exists;
- every literal `/static/...` URL in a rendered page exists on disk. `base.html` preloads
  `chakra-petch-600.woff2` and `ibm-plex-sans-variable.woff2` with literal paths, because a preload
  URL must match the URL inside the stylesheet exactly or the browser fetches the file twice. That
  is also why those two preloads skip the `static()` helper and its `?v=` cache key.

This file and the `OFL-*.txt` files are the only ones under `static/` exempt from the checker's
third-party-URL rule: the licence text cites the web address of the licence itself, and one
copyright line cites the repository the family is developed in. Both are quotations in a legal
text, not requests the site makes.
