# Ezmanga

Multi-source manga downloader built around [MANGA MILLION](https://mangamillion.shueisha.co.jp). It's a pluggable framework: add a new comic platform as a `source`. Use the `--source` flag to pick one.

> v2.x = multi-source framework. The original single-platform tool lives on the `v1.0` branch.

## ⚠️ Disclaimer

- For **personal offline reading only**. All content © its respective rights holders. **Do not redistribute.**
- MANGA MILLION is a limited-time free service (expected to run until ~Dec 2027). The tool may need updates if the service changes.

## Features

- Pluggable **source** architecture — swap platforms with `--source`
- List available titles (Keiyoushi lists/searches one page at a time), download a full series or a chapter range
- Resume interrupted downloads (skip already-downloaded pages and complete chapters)
- Bundle downloaded chapters into an `.epub`, or export each chapter/volume as `.zip` / `.cbz`
- No login required for the default source

## Sources

| Source | Status | Auth | Notes |
|--------|--------|------|-------|
| `mangamillion` | ✅ implemented | none (device token) | Shueisha free service, protobuf + AES decryption |
| `tongli` | ✅ implemented | auto login (`refreshToken`) | Taiwan 東立 e-book, JSON API, Azure SAS image links (no DRM) |
| `bookwalker` | ✅ implemented | logged-in browser | Native JPG restoration with ordinary-browser BW helper; optional debug-browser Canvas capture; not in Actions |
| `bilibili` | ✅ implemented | anonymous or QR-login HTTP; browser for Canvas mode | Python/WASM API and image decryption; optional browser Canvas capture |
| `kobo` | ✅ implemented | Kobo web activation · ADE import | **book** track: fetch whole `.kepub` + Obok decrypt. Adobe ADE: `.acsm` fulfill (Auth/InitLicenseService/Fulfill) + ADEPT content-decrypt. **not** in Actions |
| `lightnovel` | ✅ implemented | Light Novel Shelf refresh token | Pure HTTP SignalR LongPolling, paginated comics, WebP images |
| `keiyoushi` | ✅ initial support | extension-dependent | Local JDK 25+, on-demand stdio host, official API 1.6 JARs |
| `readmoo` | ⏳ planned | Readmoo desktop app | Planned source |

**Browser-assisted sources**: `bookwalker` defaults to local JPG restoration, using the BW helper extension in an ordinary logged-in Chrome/Edge browser. `--bw-mode canvas` and `--bili-mode canvas` need a local debug browser (`--remote-debugging-port=9222 --remote-allow-origins=*`) plus `websocket-client`. These browser modes require a local browser session. Bilibili defaults to HTTP/WASM and does not need a browser, Node, or Java.

**`kobo` (book track)**: unlike the crawl/capture tracks it fetches the **whole** DRM'd fixed-layout `.kepub`, decrypts it with the Obok scheme (`mmdl/sources/kobo_drm.py`), then extracts pages by OPF spine order (`page_extract.py`). `--source kobo --setup` does a one-time browser-CDP activation (writes `~/.mmdl/kobo.json`, never stores your password). For **Adobe ADE** books (Kobo free samples are often `.acsm`), `--adobe-setup` imports your machine's already-authorized ADE device identity from the registry (`HKCU\Software\Adobe\Adept`), then the tool runs the full ADEPT flow — operator `Auth` → `InitLicenseService` → `Fulfill` → download → decrypt (`kobo_acsm.py` + `adept_drm.py`). Not in Actions.

**Tongli note**: browse endpoints (`/Book`, `/Book/BookVol`) don't need auth, but `/Comic/sas` (which returns the per-page image URLs) requires a Firebase Bearer (`idToken`). The tool logs you in automatically — on first run it prompts for your Tongli email/password (never stored) and caches the Firebase `refreshToken` in `~/.mmdl/tongli_refresh.json`; every later run refreshes it silently, so no manual F12/paste. Use `TONG_LI_EMAIL`/`TONG_LI_PASSWORD` env vars instead of the prompt (also how it runs in GitHub Actions). You can still pin a static token with `TONG_LI_TOKEN` (env), `~/.mmdl/config.ini`, or `--token`. Free-trial pages are subject to the service's session/time limits, so a given volume may return fewer or zero readable pages over time.

**Light Novel Shelf (`lightnovel`)**: reads comics from [lightnovel.app](https://www.lightnovel.app/) using the authenticated reading API, without a browser during downloads. Set `LIGHTNOVEL_REFRESH_TOKEN`, pass the refresh token with `--token`, or use a private configuration file (see below). It automatically tries the main API and Cloudflare API; `LIGHTNOVEL_API_BASE` can select an HTTPS endpoint explicitly. Server reading permissions apply, and invalid/expired tokens require logging in again. This source never calls the paid book/chapter archive download endpoints. The CDN serves WebP even when URLs end in `.jpg`; downloads validate the actual WebP bytes.

All sources emit the same normalized `Title → Chapter → Page` model, so downloads, resume, and EPUB export work identically across platforms.

## Requirements

- Python 3.8+
- [`pycryptodome`](https://pypi.org/project/pycryptodome/)
- Light Novel Shelf: `curl_cffi` and `msgpack` (included in `requirements.txt`; loaded only for this source)
- Keiyoushi (optional): local JDK 25+, `curl_cffi` for extension installation, and Pillow 11.3+ for AVIF conversion

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
python -m mmdl --epub-only "manga_download/mangamillion/One Piece" --lang en

# Pick a different source (Tongli — logs in automatically on first run)
python -m mmdl --source tongli --title <volume-guid> --lang zh-TW
# or pin a static token to skip auto-login:
python -m mmdl --source tongli --title <volume-guid> --lang zh-TW --token "$TONG_LI_TOKEN"

# BookWalker native: ordinary logged-in browser with the BW helper extension
python -m mmdl --source bookwalker --url "https://viewer.bookwalker.jp/03/30/viewer.html?cid=<uuid>&cty=1" --cbz
# Optional Canvas capture (PNG; needs logged-in debug browser on :9222)
python -m mmdl --source bookwalker --url "https://viewer.bookwalker.jp/03/30/viewer.html?cid=<uuid>&cty=1" --bw-mode canvas --cbz

# Bilibili: optional QR login for chapters your account can access
python -m mmdl --source bilibili --setup
# HTTP/WASM: one reader chapter, no browser required
python -m mmdl --source bilibili --url "https://manga.bilibili.com/mc26731/329893" --cbz
# Or select chapters by comic ID
python -m mmdl --source bilibili --title 26731 --chapters 1 --cbz
# Optional existing Canvas capture
python -m mmdl --source bilibili --url "<manga-bilibili-reader-url>" --bili-mode canvas --cbz

# Kobo (book track): one-time browser activation, then fetch a book by Kobo content id
python -m mmdl --source kobo --setup
python -m mmdl --source kobo --title "<kobo-content-id>" --output out

# Kobo Adobe ADE (.acsm) books: import your ADE identity, then point --title at the .acsm file
python -m mmdl --source kobo --adobe-setup
python -m mmdl --source kobo --title "/path/to/URLLink.acsm" --output out
```

### Light Novel Shelf comics

Use an existing Eznovel config by setting `LIGHTNOVEL_CONFIG` to its `config.json` path. Only `lightnovel.refresh_token` and optional `lightnovel.api_base` are read; the file is never modified. Alternatively, create `~/.mmdl/lightnovel.json` with `{"refresh_token": "<your-refresh-token>"}`. Keep credentials outside this repository. `--token` overrides `LIGHTNOVEL_REFRESH_TOKEN`, which overrides the config token.

```bash
# With LIGHTNOVEL_REFRESH_TOKEN or LIGHTNOVEL_CONFIG already set:
python -m mmdl --source lightnovel --title <manga-id> --lang zh-CN --epub
python -m mmdl --source lightnovel --title "https://www.lightnovel.app/manga/<manga-id>" --chapters 1-2 --lang zh-CN --epub
python -m mmdl --source lightnovel --list
```

`--chapters` selects the site's `SortNum` values (volumes for volume-based comics). Interrupted runs skip saved pages using the existing resume behavior. `--list` follows all server pages and may take time for a large catalog. Images keep the API's page order and CDN resolution; `--quality` has no effect for this source.

### ZIP / CBZ export

Use `--zip` or `--cbz` to export each downloaded chapter/volume separately. Archives are saved inside the manga's title folder, next to the original chapter folders. Each archive contains only the page images at its root, in natural page order. Images are stored without another compression pass; CBZ uses the same ZIP format. You can combine `--zip`, `--cbz`, and `--epub`.

```bash
# Download selected volumes and export one CBZ per volume
python -m mmdl --source lightnovel --title <manga-id> --chapters 1-2 --cbz

# Export existing chapter folders without API requests or login
python -m mmdl --cbz-only "manga_download/mangamillion/One Piece"
python -m mmdl --zip-only "manga_download/mangamillion/One Piece"

# Export both archive formats from an existing manga folder
python -m mmdl --cbz-only "manga_download/mangamillion/One Piece" --zip
```

Original images are retained. Re-exporting replaces archives with the same chapter name and extension. `--epub-only`, `--zip-only`, and `--cbz-only` are mutually exclusive; use the corresponding download/export flags to request additional formats.

### Options

| Flag | Description |
|------|-------------|
| `--source` | Content source (default `mangamillion`) |
| `--list` | List all titles, then exit |
| `--title <id>` | Title ID for the selected source |
| `--lang <code>` | Language: `en` / `ja` / `zh-CN` / ... |
| `--chapters <n or a-b>` | Download one chapter or a range, including fractional chapters such as `10.5-12.5` |
| `--output <dir>` | Output directory (default `manga_download/<source>`; an explicit directory is used directly) |
| `--quality <q>` | Image quality: `middle` / `low` |
| `--throttle <sec>` | Delay between page downloads (default `0.3`) |
| `--epub` | After downloading, bundle the title into an EPUB |
| `--zip` | After downloading, export each chapter/volume as ZIP in the title folder |
| `--cbz` | After downloading, export each chapter/volume as CBZ in the title folder |
| `--epub-only <title-dir>` | Build an EPUB from an existing downloaded title directory |
| `--zip-only <title-dir>` | Export existing chapter/volume folders as ZIP without downloading |
| `--cbz-only <title-dir>` | Export existing chapter/volume folders as CBZ without downloading |
| `--token <t>` | Source auth token (Tongli Bearer or Light Novel Shelf refresh token) |
| `--book-group <g>` | Source optional param (e.g. Tongli BookGroupID) |
| `--setup` | One-time account activation/login for a source (e.g. `kobo`, `bilibili`) |
| `--adobe-setup` | Import the machine's ADE device identity from the registry (Kobo `.acsm` fulfill) |

### Output layout

Default downloads are grouped by source under `manga_download/<source>`. `--output <dir>` uses
the specified directory directly; existing downloads can be moved to the matching source folder
to keep using resume and offline export.

```
manga_download/
  mangamillion/
    One Piece/
      #001 Chapter 1 Romance Dawn/
        001.webp
        002.webp
        ...
      #001 Chapter 1 Romance Dawn.zip
      #001 Chapter 1 Romance Dawn.cbz
    One Piece.epub
  bilibili/
    <title>/
  bookwalker/
    <title>/
```

## How it works

Every source exposes the `BaseSource` interface (`sources/base.py`) and normalizes its platform's
responses into a shared `Title → Chapter → Page` model, so downloads, resume, and EPUB export work
identically. Sources fall into two capability tracks:

**crawl** — HTTP, drive the whole pipeline (`mangamillion`, `tongli`, `lightnovel`, `bilibili`):
1. Resolve `title → chapters → pages` through the platform API
2. Download each page's bytes — MangaMillion decrypts AES-256-CBC, Tongli fetches Azure SAS links
3. Optionally package the images into an EPUB 3 archive

**capture** — optional browser modes (`--bw-mode canvas`, `--bili-mode canvas`):
1. Connect to your debug browser on `:9222`
2. Read the manga from the reader's `<canvas>` via cross-realm `toDataURL` (bypasses canvas patching)
3. Page through the reader slowly (≥1.5s) and extract the raw images

Shared `core/` handles transport, resume, and EPUB packaging; each `sources/*.py` only implements its
own API calls, field mapping, and image handling.

### Bilibili HTTP mode

Log in once with `python -m mmdl --source bilibili --setup`. Scan the printed local PNG path with
the Bilibili app and confirm on your phone. QR generation and polling use HTTP; no browser is started.
The QR image is removed when setup finishes. The account session is saved separately in
`~/.mmdl/bilibili_cookies.txt` and loaded for subsequent HTTP downloads. This file contains private
cookies: keep it local. Expired login sessions require running `--setup` again. Login does not purchase
or unlock chapters; the account must already have access.

`bilibili` defaults to `--bili-mode http`. `--url` accepts a comic or reader URL; a reader URL downloads
only its chapter. `--title` accepts a comic ID or URL and supports `--chapters`, resume, EPUB, and
per-chapter ZIP/CBZ export. It does not request popular/recent listings or buy/unlock chapters.

The first HTTP download caches the pinned public reader script and three official WASM modules in
`~/.mmdl/bilibili`. Python runs these modules through Wasmtime; it does not execute JavaScript or
start a browser/Node process. ECDH uses the existing cryptography dependency. Signed API responses
and encrypted image bytes are decoded before saving the original image format and dimensions.
Optional WASM telemetry is not transmitted.

The current protocol snapshot is reader `550c4c7ca4`. Website protocol changes can require an update.
The Go callback source bundled beside the host is public protocol metadata, not executable JS.
HTTP mode uses the saved login session when available, otherwise an anonymous session. A complete
22-page free chapter, including four encrypted images, was verified. QR login, saved-session reuse,
and an encrypted free page were also verified. Purchased-chapter HTTP download was verified with
a logged-in account; decoded JPEGs and CBZ archives passed validation. The existing Canvas mode
remains available for the reader session. Account cookies, image tokens, and temporary private
keys are not stored in the protocol cache.

## Project structure

```
mmdl/
  core/        # transport, model, naming, resume, epub — platform agnostic
  sources/     # base.BaseSource + one module per platform
cli.py         # --source routing & capability gating
```

## Keiyoushi extensions (optional)

Ezmanga starts its own Java stdio host for the selected extension and closes it when the command
finishes. It does not start a Suwayomi server, HTTP listener, database, or WebUI. Images are fetched
through the extension's client, preserving headers, cookies, and image processing.

Use a local **JDK 25+** (`JAVA_HOME`, or `java` and `javac` on PATH). Setup downloads a pinned
24.8 MiB compatibility library and compiles the small host with `javac`; no Gradle or Android SDK
is required. The host accepts official **extension API 1.4–1.6 JARs** and HTTP image sources.
API 1.6 downloads have been verified. API 1.4/1.5 packages use a dedicated legacy Rx
adapter for search, details, chapter lists, page lists, and image URL resolution.
Details finish before chapter requests; original extension models and clients are retained.
The host calls chapter preparation hooks, recognizes missing chapter numbers with the shared
runtime parser, and uses legacy image hooks when provided. Legacy settings are written
synchronously to the extension's own preferences, including private or lazily accessed stores.
Compilation and local integration tests using the official vomic API 1.4 JAR passed,
including settings persistence, search, downloads, resume, and ZIP/CBZ/EPUB export.
Successful downloads from public API 1.4 websites have not yet been confirmed.
Compatibility depends on each extension and website.

```bash
python -m mmdl --source keiyoushi --setup
python -m mmdl --source keiyoushi --extensions --search xkcd
python -m mmdl --source keiyoushi --install-extension xkcd
python -m mmdl --source keiyoushi --installed-extensions
python -m mmdl --source keiyoushi --extension xkcd --extension-sources
python -m mmdl --source keiyoushi --install-extension mangamillion
python -m mmdl --source keiyoushi --extension mangamillion --lang en --search "One Piece"
python -m mmdl --source keiyoushi --extension xkcd --lang en --title '<ID or URL from search>' --chapters 1-1 --cbz
python -m mmdl --source keiyoushi --update-extensions
```

`--extension` accepts the full package name or its last component. For multiple sources in the
same language, use `--extension-source <ID>` from `--extension-sources`. Searches show one page; use `--page` for the next page.
Keiyoushi provides search to locate a download ID; popular and recent-update browsing are not exposed. `--installed-extensions` lists local packages
without a network request, and supports `--lang` and `--search` filters. Updates are explicit; only installed extensions update,
and adding `--extension` limits the update to one package. Official JAR checksums are verified
at installation and before loading.

Runtime files, extensions, and preferences live under `~/.mmdl/keiyoushi/`. Override this with
`EZMANGA_KEIYOUSHI_HOME`; `EZMANGA_JAVA` and `EZMANGA_JAVAC` can select local Java executables.
Credentials belong in user configuration, never project files. This host currently supports
JPEG, PNG, WebP, and GIF (including animation); static AVIF pages are converted losslessly to PNG using Pillow 11.3+.
Extensions requiring Android WebView, native Android libraries, or interactive
login may need further compatibility work; compatibility is not guaranteed for every source.

To inspect source settings without displaying saved values:

```bash
python -m mmdl --source keiyoushi --extension xkcd --lang en --source-preferences
python -m mmdl --source keiyoushi --extension xkcd --lang en --preferences-file private-settings.json
python -m mmdl --source keiyoushi --extension manhuagui --source-filters
python -m mmdl --source keiyoushi --extension manhuagui --search "" --filters-file filters.json
```

`private-settings.json` uses the extension's setting keys and explicit value types:

```json
{"preferences": [{"key": "organization_method", "type": "String", "value": "BY_YEAR"}]}
```

Supported setting types are `String`, `Boolean`, `Int`, `Long`, `Float`, and `StringSet`.
Keep files containing credentials outside the repository. Settings persist between commands;
the host restarts after changing them so cached authentication headers are refreshed.
Some extensions require a configured website or server URL before listing titles.

Account login is supported through the native [Picacomic](https://github.com/keiyoushi/extensions-source/tree/main/src/zh/picacomic)
and [Zaimanhua](https://github.com/keiyoushi/extensions-source/tree/main/src/zh/zaimanhua) extensions.
Install either extension, then import its account settings from a private JSON file:

```bash
python -m mmdl --source keiyoushi --install-extension picacomic
python -m mmdl --source keiyoushi --install-extension zaimanhua
python -m mmdl --source keiyoushi --extension picacomic --preferences-file private-picacomic.json
python -m mmdl --source keiyoushi --extension zaimanhua --preferences-file private-zaimanhua.json
```

Both files use the same account keys. Replace the placeholders with your own credentials:

```json
{
  "preferences": [
    {"key": "USERNAME", "type": "String", "value": "<username>"},
    {"key": "PASSWORD", "type": "String", "value": "<password>"},
    {"key": "TOKEN", "type": "String", "value": ""}
  ]
}
```

An empty `TOKEN` clears an old session. The extension logs in on the next request and saves
the resulting token. To import an existing session instead, use only a `TOKEN` entry with
its raw value; do not include the `Bearer ` prefix. Picacomic refreshes rejected tokens
using the saved account. Zaimanhua also clears its token when account settings change;
native setting callbacks run before any explicitly supplied token is saved.

Search for a title, then copy its ID from the results into the download command:

```bash
python -m mmdl --source keiyoushi --extension picacomic --search "<title>"
python -m mmdl --source keiyoushi --extension picacomic --title "<ID from search>" --chapters 1 --cbz
python -m mmdl --source keiyoushi --extension zaimanhua --search "<title>"
python -m mmdl --source keiyoushi --extension zaimanhua --title "<ID from search>" --chapters 1 --cbz
```

Authentication stays in the extension; the downloader does not maintain separate login APIs.
These two extensions use account/token authentication. Cookie-file import is not implemented.

`--source-filters` shows filter paths, types, choices, and states. A filters file maps those
paths to new states, for example `{"0": 1, "2.0": true}`; choose paths from the selected
extension's output. Select filters use a zero-based choice index, text filters a string,
checkboxes a boolean, and tri-state filters `0`, `1`, or `2`. Sort filters accept
`{"index": 0, "ascending": true}` or `null`. Group children use dotted paths.
Filters apply only to searches; an empty search string requests the source's filtered catalog.

The host uses the independently published
[Suwayomi-ext-runtime](https://github.com/576576/Suwayomi-ext-runtime) as a compatibility **library**;
its HTTP process entry point is never invoked. The pinned artifact is an alpha release. Source API
and Android compatibility updates remain host maintenance responsibilities.
`--chapters 1` selects only chapter 1; fractional ranges such as `--chapters 10.5-12.5`
use the extension's chapter numbers. Downloaded pages are written to temporary files
and moved into place only when complete, so interrupted writes are retried on resume. See
[runtime/THIRD_PARTY.md](runtime/THIRD_PARTY.md) for version and license information.

## BookWalker Japan download modes

The existing `--source bookwalker --url <reader-url>` command defaults to `--bw-mode native`.
It restores page resources locally to JPG. Native downloads use your ordinary logged-in
Chrome/Edge browser through the helper extension in [browser/bookwalker](browser/bookwalker).
They do not require a debugging port, a separate browser profile, or Java.

One-time setup: open `chrome://extensions` or `edge://extensions`, enable developer mode,
choose **Load unpacked**, and select the `browser/bookwalker` directory. This developer-mode
switch installs the local extension; it does not enable browser remote debugging.

For each download:

1. Open the requested BW manga reader in your already logged-in browser, then click the
   **Ezmanga BW** extension to open its connection page.
2. Run `python -m mmdl --source bookwalker --url "<reader-url>" --cbz`.
3. Paste the command's pairing code into the connection page and click **Connect**.
4. Keep the reader and connection tabs open until the download finishes.

The helper reads cookies applicable to the selected BW reader/API and runs the website's
authorization script in that reader tab. It sends updated sessions every 20 seconds to a
temporary receiver bound to `127.0.0.1`. The receiver closes with the download. Session data
and the generated per-download pairing code stay in memory. The helper verifies the receiver
before sending cookies. The receiver accepts only the requested reader and paired client.
If the default port is occupied, use `--bw-port <port>` and enter that port in the helper.

Python fetches and decodes the Publus configuration, restores image tiles with Pillow,
and refreshes short-lived reading authorization during downloads. The native implementation
covers `viewer.bookwalker.jp`, `viewer-trial.bookwalker.jp`, and `viewer-df.bookwalker.jp`.
The account must have access to the volume. English BookWalker uses a different reader
protocol and is not covered here. Native downloads do not turn pages or extract Canvas pixels.

Use `--bw-mode canvas` to select the existing browser-rendered PNG capture explicitly.
That mode still needs your logged-in debug browser and `websocket-client`.
Native errors do not silently switch modes. Both modes support `--epub`, `--zip`, and `--cbz`.
Native restoration uses the already declared `curl_cffi`, `pycryptodome`, and Pillow dependencies.
Scrambled pages are encoded as JPEG at quality 90; unmodified JPEG pages keep their bytes.

The Python Publus decoder is adapted from the
[Keiyoushi Publus library](https://github.com/keiyoushi/extensions-source/tree/main/lib/publus).
Its adapted code is covered by [Apache-2.0](mmdl/sources/BOOKWALKER_LICENSE.txt).

## License

[MIT](LICENSE)
