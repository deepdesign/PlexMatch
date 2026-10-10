# PlexMatch - Plex Media Renamer

Scans a media library (movies, TV series, documentaries), matches files against TMDB, and renames/restructures them into Plex-compliant naming.

---

## Requirements

- Python 3.10+ (https://python.org)
- Node.js 18+ (https://nodejs.org)
- A free TMDB API key (https://www.themoviedb.org/settings/api)

---

## First-time setup

Open a terminal in this folder and run:

```
cd backend
pip install flask flask-cors requests

cd ../frontend
npm install
```

---

## Running the app

**Windows:** Double-click `start.bat`

**Mac/Linux:**

Terminal 1:
```
cd backend
python app.py
```

Terminal 2:
```
cd frontend
npm run dev
```

Then open http://localhost:5173 in your browser.

---

## How it works

1. **Configure** - Choose a library folder (Browse, or type a path such as `K:\Media`). Matching, Plex output, API keys, and Tools are sidebar groups; whichever groups you leave open are remembered. A TMDB key is required before a scan.
2. **Scan** - The tool walks every subfolder and parses filenames. A file that is already in the Plex shape is marked organised and skips the network. Everything else is matched against TMDB, then OMDb, then AI or Wikipedia when those are turned on. Progress streams live. **Stop** keeps whatever has already matched and opens Review.
3. **Review** - Opens on **Needs attention** when anything needs a decision: conflicts, missing years, unmatched files, and low-confidence matches. A TV season with two or more episodes is one row; expand it to change a single episode. For each file you can:
   - Approve or reject the proposed rename
   - Expand the row to pick an alternative TMDB match (the proposed path is rebuilt automatically)
   - Type a corrected title (and optional year) and re-search, or use **Use as-is** when the title is not in any database
   - Use **Ask AI** on a single row when an AI key is configured
4. **Rename** - Moves approved files into the Plex layout. Subtitles, artwork, and `.nfo` files beside a video move with it. When output was split into `Movies/` and `TV/`, those folder paths are shown with a copy button so each can be added as its own Plex library. Empty source folders can be removed afterward.
5. **Undo** - One click puts that batch back, including the sidecars that moved with each video.

---

## Plex naming output

> **Important:** Plex needs movies and TV in *separate* libraries. A single library mixing both will ignore whichever type doesn't match its agent. With **Restructure into Plex folders** and **Separate Movies and TV Shows** on (both default), output is split into `Movies/` and `TV/` so you can point one Movie-agent library at `Movies/` and one TV-agent library at `TV/`.

**Films** (point a *Movie* library here):
```
Media/
  Movies/
    Fukushima A Nuclear Nightmare (2026) {tmdb-1234567}/
      Fukushima A Nuclear Nightmare (2026).mkv
```

**TV series** (point a *TV Show* library here):
```
Media/
  TV/
    Making a Murderer (2015) {tmdb-61664}/
      Season 01/
        Making a Murderer (2015) - S01E01 - Plea of Innocence.mkv
```

**Add TMDB/IMDb IDs to folder names** appends the `{tmdb-…}` or `{imdb-…}` tag so Plex matches the exact title. Turn **Restructure into Plex folders** off to leave each file in its current folder and only rename it. The Movies/TV split and the ID tag only apply when files move into new folders.

---

## Notes

- Settings (API keys, folder path, preferences) are saved automatically between sessions in `backend/settings.json`. Keys can be copied from the API keys group. Sidebar groups you leave open are remembered in this browser.
- All renames are shown as a dry run in the Review step before anything is moved
- A rename only moves files. **Clean up empty folders** only removes directories that are already empty after a move
- **Tools** can permanently delete, and only after you confirm a selection:
  - **Clean up orphaned folders** removes folders that contain no video files
  - **Clean up junk files** removes readmes, shortcuts (`.url`, `.lnk`), and release leftovers such as checksums and torrent files. Posters, subtitles, and `.nfo` files stay
- Every rename batch is logged to `backend/rename_log.json` so it can be undone, including sidecars that moved with a video
- **Auto-pick top match** (on by default) approves the top match. Off leaves a weak or close call unchecked so you can confirm it
- If a file has no TMDB match, it is flagged as unmatched. Use the per-row manual search, **Use as-is**, or **Ask AI**
- With **Clean names without a match** enabled, unmatched files still get a best-effort Plex name built from the folder (title/year) and filename (season/episode/episode title). These rows are badged **Local** and are not auto-approved
- An optional **OMDb** key (free, IMDb-backed) is used when TMDB has no confident match. It covers obscure titles, fills the `{imdb-…}` folder tag, and can supply episode names. Get a free key at [omdbapi.com](https://www.omdbapi.com/apikey.aspx)
- Optional **AI** (OpenAI or Anthropic) runs only on weak matches, and from **Ask AI** on a row. Results are cached. Keys stay on this machine. After a key checks out, the model list keeps a short pro/con note on the built-in presets and adds the other chat models on your account. An OpenAI-compatible base URL works for Azure, Groq, or a local proxy.
- **Look up unmatched titles on Wikipedia** (no API key) fills a canonical title, year, and episode name when an article exists. Those rows are badged **Web** with a Wikipedia link. Titles with no article fall back to the **Local** filename cleanup
- Files already in the correct Plex location are flagged **organised**, hidden by default, and are not looked up again. A file whose target name is already taken is flagged a **conflict** so the rename never silently overwrites it
- Rows missing a year are flagged. Expand the row and use **Use as-is** to apply a typed title and year with no database lookup
- Confidence scores: High (85%+), Medium (60-84%), Low (below 60%)

---

## API endpoints

| Method | Path                   | Purpose                                                         |
|--------|------------------------|-----------------------------------------------------------------|
| GET    | `/api/settings`        | Load persisted settings                                         |
| POST   | `/api/settings`        | Save settings                                                   |
| POST   | `/api/scan`            | Blocking scan (returns all proposals at once)                   |
| POST   | `/api/scan/stream`     | Streaming scan via Server-Sent Events (one proposal at a time). Closing the request stops work that has not started |
| POST   | `/api/rematch`         | Re-match one file by TMDB id, manual title, or `ai: true`       |
| GET    | `/api/ai/console-urls` | OpenAI / Anthropic console URLs for signing in to create keys   |
| POST   | `/api/ai/verify-key`   | Check an AI key and return the chat models on that account      |
| POST   | `/api/rename`          | Execute approved moves; logs the batch, optional folder cleanup |
| GET    | `/api/rename-log`      | List logged rename batches (most recent first)                  |
| POST   | `/api/undo`            | Reverse a rename batch (defaults to the most recent)            |
| POST   | `/api/pick-folder`     | Open the native OS folder picker and return the chosen path     |
| POST   | `/api/validate-folder` | Check whether a path exists on disk                             |
| POST   | `/api/orphan-folders`  | Preview folders under the root that contain no video files      |
| POST   | `/api/delete-folders`  | Permanently delete the given video-free folders                 |
| POST   | `/api/junk-files`      | Preview readmes, shortcuts, and release leftovers               |
| POST   | `/api/delete-junk`     | Permanently delete files that still classify as junk            |
| POST   | `/api/delete-files`    | Permanently delete specific files (for example duplicate sources) |
