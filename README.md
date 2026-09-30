# Ezmanga

Multi-source manga downloader built around [MANGA MILLION](https://mangamillion.shueisha.co.jp). It's a pluggable framework: add a new comic platform as a `source`. Use the `--source` flag to pick one.

> v2.x = multi-source framework. The original single-platform tool lives on the `v1.0` branch.

## ⚠️ Disclaimer

- For **personal offline reading only**. All content © its respective rights holders. **Do not redistribute.**
- MANGA MILLION is a limited-time free service (expected to run until ~Dec 2027). The tool may need updates if the service changes.

## Features

- Pluggable **source** architecture — swap platforms with `--source`
- List all available titles, download a full series or a chapter range
- Resume interrupted downloads (skip already-downloaded pages and complete chapters)
- Bundle downloaded chapters into an `.epub`
- No login required for the default source

## Sources

| Source | Status | Auth | Notes |
|--------|--------|------|-------|
| `mangamillion` | ✅ implemented | none (device token) | Shueisha free service, protobuf + AES decryption |
| `tongli` | ✅ implemented | auto login (`refreshToken`) | Taiwan 東立 e-book, JSON API, Azure SAS image links (no DRM) |
| `bookwalker` | ✅ implemented | logged-in browser | Browser-assisted; needs a local debug Chrome (`--remote-debugging-port`), **not** in Actions |
| `bilibili` | ✅ implemented | logged-in browser | Browser-assisted; canvas extraction, risk-controlled, **not** in Actions |
| `kobo` | ✅ implemented | Kobo web activation · ADE import | **book** track: fetch whole `.kepub` + Obok decrypt. Adobe ADE: `.acsm` fulfill (Auth/InitLicenseService/Fulfill) + ADEPT content-decrypt. **not** in Actions |
| `readmoo` | ⏳ planned | Readmoo desktop app | Planned source |

**Browser-assisted sources** (`bookwalker`, `bilibili`) read the manga from the reader's `<canvas>` (cross-realm `toDataURL` to bypass canvas read-back patching) instead of the HTTP API. This requires your locally logged-in browser started with `--remote-debugging-port=9222 --remote-allow-origins=*`. They can't run in GitHub Actions (no login session there) and need `pip install websocket-client`.

**`kobo` (book track)**: unlike the crawl/capture tracks it fetches the **whole** DRM'd fixed-layout `.kepub`, decrypts it with the Obok scheme (`mmdl/sources/kobo_drm.py`), then extracts pages by OPF spine order (`page_extract.py`). `--source kobo --setup` does a one-time browser-CDP activation (writes `~/.mmdl/kobo.json`, never stores your password). For **Adobe ADE** books (Kobo free samples are often `.acsm`), `--adobe-setup` imports your machine's already-authorized ADE device identity from the registry (`HKCU\Software\Adobe\Adept`), then the tool runs the full ADEPT flow — operator `Auth` → `InitLicenseService` → `Fulfill` → download → decrypt (`kobo_acsm.py` + `adept_drm.py`). Not in Actions.

**Tongli note**: browse endpoints (`/Book`, `/Book/BookVol`) don't need auth, but `/Comic/sas` (which returns the per-page image URLs) requires a Firebase Bearer (`idToken`). The tool logs you in automatically — on first run it prompts for your Tongli email/password (never stored) and caches the Firebase `refreshToken` in `~/.mmdl/tongli_refresh.json`; every later run refreshes it silently, so no manual F12/paste. Use `TONG_LI_EMAIL`/`TONG_LI_PASSWORD` env vars instead of the prompt (also how it runs in GitHub Actions). You can still pin a static token with `TONG_LI_TOKEN` (env), `~/.mmdl/config.ini`, or `--token`. Free-trial pages are subject to the service's session/time limits, so a given volume may return fewer or zero readable pages over time.

All sources emit the same normalized `Title → Chapter → Page` model, so downloads, resume, and EPUB export work identically across platforms.

## Requirements

- Python 3.8+
- [`pycryptodome`](https://pypi.org/project/pycryptodome/)

Install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Or install the required dependency directly:

```bash
python -m pip install pycryptodome
```

## GitHub Actions (one-click download)

No local setup needed — download directly on GitHub:

1. Open the **Actions** tab → select the **Download manga** workflow
2. Click **Run workflow**
3. Fill in the inputs:
   - `source`: `mangamillion` (default) / `tongli`
   - `title_ids`: manga IDs, comma-separated
   - `lang`: language (default `en`)
   - `chapters`: chapter range, leave empty for all chapters
   - `quality`: `middle` / `low`
   - `epub`: `no` / `yes` — bundle into an EPUB
4. Run. When it finishes, download the `manga_million` artifact (tar.gz) from the workflow run page.

Note: `tongli` can authenticate in Actions with `TONG_LI_EMAIL`/`TONG_LI_PASSWORD` secrets (or a static `TONG_LI_TOKEN`), and the runner IP must be reachable by the service. `bookwalker` is intentionally not offered here (it needs a local browser login session).

## Usage

```bash
# List available titles for the default source
python -m mmdl --list --lang en

# Download a full series (e.g. One Piece, id=1)
python -m mmdl --title 1 --lang en

# Download only chapters 1-20
python -m mmdl --title 1 --chapters 1-20 --lang en

# Download and create an EPUB next to the title directory
python -m mmdl --title 1 --chapters 1-20 --lang en --epub

# Create an EPUB from a title that was already downloaded
python -m mmdl --epub-only "manga_million/One Piece" --lang en

# Pick a different source (Tongli — logs in automatically on first run)
python -m mmdl --source tongli --title <volume-guid> --lang zh-TW
# or pin a static token to skip auto-login:
python -m mmdl --source tongli --title <volume-guid> --lang zh-TW --token "$TONG_LI_TOKEN"

# Browser-assisted: BookWalker reader URL (needs logged-in browser on :9222)
python -m mmdl --source bookwalker --url "https://viewer.bookwalker.jp/03/30/viewer.html?cid=<uuid>&cty=1"

# Browser-assisted: Bilibili manga reader URL
python -m mmdl --source bilibili --url "<manga-bilibili-reader-url>"

# Kobo (book track): one-time browser activation, then fetch a book by Kobo content id
python -m mmdl --source kobo --setup
python -m mmdl --source kobo --title "<kobo-content-id>" --output out

# Kobo Adobe ADE (.acsm) books: import your ADE identity, then point --title at the .acsm file
python -m mmdl --source kobo --adobe-setup
python -m mmdl --source kobo --title "/path/to/URLLink.acsm" --output out
```

### Options

| Flag | Description |
|------|-------------|
| `--source` | Content source (default `mangamillion`) |
| `--list` | List all titles, then exit |
| `--title <id>` | Title ID for the selected source |
| `--lang <code>` | Language: `en` / `ja` / `zh-CN` / ... |
| `--chapters <a-b>` | Download only this chapter range |
| `--output <dir>` | Output directory (default `manga_million`) |
| `--quality <q>` | Image quality: `middle` / `low` |
| `--throttle <sec>` | Delay between page downloads (default `0.3`) |
| `--epub` | After downloading, bundle the title into an EPUB |
| `--epub-only <title-dir>` | Build an EPUB from an existing downloaded title directory |
| `--token <t>` | Source auth token (e.g. Tongli Bearer value) |
| `--book-group <g>` | Source optional param (e.g. Tongli BookGroupID) |
| `--setup` | One-time account activation/login for a source (e.g. `kobo`) |
| `--adobe-setup` | Import the machine's ADE device identity from the registry (Kobo `.acsm` fulfill) |

### Output layout

```
manga_million/
  One Piece/
    #001 Chapter 1 Romance Dawn/
      001.webp
      002.webp
      ...
  One Piece.epub
```

## How it works

Every source exposes the `BaseSource` interface (`sources/base.py`) and normalizes its platform's
responses into a shared `Title → Chapter → Page` model, so downloads, resume, and EPUB export work
identically. Sources fall into two capability tracks:

**crawl** — pure HTTP, drive the whole pipeline (`mangamillion`, `tongli`):
1. Resolve `title → chapters → pages` through the platform API
2. Download each page's bytes — MangaMillion decrypts AES-256-CBC, Tongli fetches Azure SAS links
3. Optionally package the images into an EPUB 3 archive

**capture** — browser-assisted, need a locally logged-in reader (`bookwalker`, `bilibili`):
1. Connect to your debug browser on `:9222`
2. Read the manga from the reader's `<canvas>` via cross-realm `toDataURL` (bypasses canvas patching)
3. Page through the reader slowly (≥1.5s) and extract the raw images

Shared `core/` handles transport, resume, and EPUB packaging; each `sources/*.py` only implements its
own API calls, field mapping, and image handling.

## Project structure

```
mmdl/
  core/        # transport, model, naming, resume, epub — platform agnostic
  sources/     # base.BaseSource + one module per platform
cli.py         # --source routing & capability gating
```

## License

[MIT](LICENSE)
