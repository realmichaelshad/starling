# Starling brand assets

The identity is small on purpose: one wordmark, one four-point star, black and cream.

## What is here

| File | Use |
|---|---|
| `web/starling-wordmark.png` | The wordmark, white on transparent, 1561x246. The website header and footer use a copy of it |
| `web/starling-icon-16.png` ... `starling-icon-180.png` | The star on a black rounded square: favicon, touch icon, extension icons |
| `web/starling-icon-192.png`, `starling-icon-512.png`, `starling-icon-maskable-512.png` | The installed web app's icons (the writing page's manifest). The maskable one is full-bleed black with the star inside the 80% safe zone, so Android can crop it to any shape. Drawn from `source/starling-star.svg`; the website serves copies from `web/static/img/` |
| `web/og.png` | Social sharing image, 1200x630. The website serves a copy as `web/static/img/og.png` |
| `source/starling-star.svg` | The star as a vector. The same path is drawn inline by the website and the extension |
| `source/x-banner-1500x500.png`, `source/x-profile-400x400.png` | Header and profile image for the product's X account |
| `source/store-promo-tile-440x280.png` | Chrome Web Store small promo tile |
| `source/social-image.html`, `source/brand-tiles.html`, `source/render.mjs` | Editable sources for every image above except the wordmark and the icons |

The Chrome Web Store icon has its own rule (128x128 with the artwork in the middle 96x96):
`docs/EXTENSION-PUBLISHING.md` explains it. Check the padding of `web/starling-icon-128.png` before
you upload it.

## Colours and type

| | |
|---|---|
| Ground | `#000000`, with a faint radial lift at the top (`#0a0a09`) and 5% film grain (`web/static/img/grain.png`) |
| Ink | `#faf8f3` |
| Ink, dimmed | `#dad7cf` |
| Muted text | `#9a9d9f` |
| Hairline | `#212428` |
| Titles, labels, buttons | Chakra Petch 500/600/700 |
| Reading text | IBM Plex Sans |
| Small technical labels | IBM Plex Mono |

The fonts are self-hosted in `web/static/fonts/` under the SIL Open Font License 1.1; their licence
texts sit beside them and must travel with them. The small uppercase label style is Chakra Petch 600
with letter-spacing `.42em`.

The line under the wordmark is "Your voice. Your drafts. Your Post button." Starling is a hosted
service, so nothing in the brand may say or suggest that it runs on the customer's own computer.

## Changing an image

Edit the HTML source, then:

```sh
node brand/source/render.mjs
```

It uses the Playwright already installed for the browser journeys (`e2e/browser`) and loads only
local files. Copy `brand/web/og.png` to `web/static/img/og.png` afterwards so the website serves the
new one.
