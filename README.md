<h1 align="center">🎧 SpotifyLocalMigrator</h1>

<p align="center">
  <em>Turn that decade-old "local files" playlist into a real Spotify playlist, without letting a single song slip through the cracks.</em>
</p>


> **What this is not:** this tool does not download, copy, convert or store any audio, and it contains no media files or binaries. It only creates ordinary Spotify playlists that reference tracks already in Spotify's catalog, through Spotify's official Web API, on the user's own account.

---

## 😩 The problem

You ripped your CDs in 2011. You dragged the MP3s into Spotify. Spotify syncs them to your phone from the desktop app… sometimes. Then a few hundred of them go grey and unplayable for no reason.

Meanwhile, 95% of those songs are sitting right there in Spotify's catalog.

## 💡 The fix

Rebuild the playlist out of real Spotify tracks, **but only where the match is exact**, and hand everything else to a human to rule on.

```
  local files playlist                        real Spotify playlist
 ┌──────────────────────┐                    ┌──────────────────────┐
 │ Mr. Brightside       │ ── exact match ──> │ Mr. Brightside       │
 │ Car Radio (2011)     │ ── flagged ──────> │ (you decide)         │
 │ Cousin's demo.mp3    │ ── not found ────> │ (stays local)        │
 └──────────────────────┘                    └──────────────────────┘
```

**Exact** means title *and* artist *and* duration (within 2 seconds) all agree. Nothing fuzzier gets added without you saying so.

## ✨ What you get

| | |
|---|---|
| 🎯 **Strict matching** | Same title, same artist, same length. Case, punctuation, accents and leading track numbers are ignored. |
| 🚩 **Every mismatch flagged with a reason** | `duration_mismatch`, `title_variant`, `artist_mismatch`, `no_match`, each with the closest Spotify track shown next to it. |
| ✅ **Approve in bulk or one by one** | By reason, by position, or "keep this one local forever". `build` brings the playlist up to date in place, original order preserved; local files you have pasted or dragged in are kept and moved to their place in the order, never removed. |
| 🔁 **Decisions and matches carry everywhere** | Approve *Three Little Birds* once and it's approved in every other playlist that has it. A song that matched exactly in one playlist (or earlier in the same one) is reused in the next without spending a search. |
| 📅 **Built for the daily grind** | Spotify caps searches per day. `match --limit 400` does a batch and stops; progress is saved every 10 songs. |
| 🧾 **Completeness check** | `verify` proves every source song is in the new playlist (as a Spotify track or a pasted-in local file) or is a collapsed duplicate, lists anything undecided, and calls out non-track items (podcast episodes) it cannot carry over. |
| 🪶 **Zero dependencies** | One Python file, standard library only. |

## 🚀 Setup (one time, ~3 minutes)

1. Open the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) and log in with the account that owns the playlists (Premium required by Spotify).
2. **Create app** → any name → Redirect URI `http://127.0.0.1:8888/callback` → tick **Web API** → Save.
3. Open the app → **Settings** → copy the **Client ID**. The secret isn't needed.
4. Log in once:

   ```bash
   echo YOUR_CLIENT_ID > client_id.txt            # gitignored
   export SPOTIFY_CLIENT_ID=$(cat client_id.txt)
   python3 migrate.py auth                         # browser opens, click Agree
   ```

## 🏃 Run

```bash
export SPOTIFY_CLIENT_ID=$(cat client_id.txt)

python3 migrate.py find pry                         # which of my playlists match "pry"?
python3 migrate.py export "name:Pry"                # pull the source playlist (local files included)
python3 migrate.py match                            # search Spotify for every song
python3 migrate.py build                            # create "Pry (Spotify version)" with exact matches only
```

Small playlist? `python3 migrate.py all "name:Pry"` does export, match and build in one go (`--limit N` caps how many songs are searched). `build --name` picks a different name for the new playlist.

You'll get a breakdown like this:

```
{
 "total": 112,
 "searched": 112,
 "not_searched": 0,
 "in_playlist_after_build": 91,
 "open_flags": 21,
 "counts": {
  "match": 91,
  "duration_mismatch": 6,
  "title_variant": 7,
  "artist_mismatch": 3,
  "no_match": 5
 }
}
Open flags: .../work/pry/flagged.csv; this batch: .../work/pry/flagged_2026-10-07.csv
```

Reasons you can see: `duration_mismatch`, `title_variant`, `artist_mismatch`, `no_match`, plus the rare `no_duration` (your file has no length) and `no_candidates` (search returned nothing; use `research`).

### 🧑‍⚖️ Then you judge

Open `work/<playlist>/flagged.csv`. Each row has your file, the closest Spotify track, the length difference and a `pos` you can reference (0-based, the same number every command prints as `#`).

```bash
python3 migrate.py approve --reasons title_variant          # "(feat. X)" vs "feat. X"? fine, take them all
python3 migrate.py approve --pos 37,134                      # these two specifically
python3 migrate.py approve --keep-local 141                  # never add this one, I like my version
python3 migrate.py approve --drop 98                          # leave this one out entirely (no track, no local file)
python3 migrate.py approve --recheck 211,223                 # wrong track: reopen (not built, standing decision forgotten) until research/approve --pos
python3 migrate.py research --pos 211 --query "I See Fire Kygo remix"   # …like this; stores it if exact
python3 migrate.py research --pos 211 --query "…" --take    # or take the top result anyway
python3 migrate.py research --pos 223 --pick spotify:track:…  # or name the track yourself
python3 migrate.py approve --unkeep 141                      # changed my mind (keep-local or drop), reopen it
python3 migrate.py build                                     # bring the playlist up to date, same URL
python3 migrate.py verify                                    # are we done yet?
python3 migrate.py report --batch 2026-10-07                 # re-print the breakdown; also rewrite that day's flagged_<date>.csv
```

```
Source 'Pry': 112 songs (112 searched so far)
New playlist https://open.spotify.com/playlist/…: 107 Spotify tracks + 5 local files
  Decided so far: 107 Spotify + 5 local + 0 collapsed duplicate(s) = 112 of 112
  Spotify tracks expected from decisions: 107  OK
  Local files to paste in by hand: 5  (present now: 5)
  Still missing: 0
  Unresolved (not yet decided or searched): 0
COMPLETE: every source song is accounted for in the new playlist.
```

`build` has two modes. Until a local file is in the new playlist it rewrites the whole thing, so order is exact. Once a local file is present (the API cannot add those back), it merges instead: missing tracks are inserted at their place in the source order, every item that is out of place (pasted local files land at the end) is moved back to its source position with one reorder call each, and tracks you have since un-approved are listed for you to remove by hand. Don't edit the playlist in the app while `build` runs.

### 📅 Big playlist? Do it in days

For a personal (development-mode) app, Spotify's search endpoint allows roughly 400 to 480 songs per day (each song costs one to three searches), then answers 429 with a Retry-After of up to 24 hours. The window is a rolling 24 hours from the day's first search. `match` stops cleanly instead of sleeping and picks up where it left off; other commands that hit the limit exit with a similar message.

```bash
python3 migrate.py -w iPhone match --limit 400    # today's batch
python3 migrate.py -w iPhone build                # playlist grows a bit
# review work/iphone/flagged_<today>.csv, approve, build, verify… see you tomorrow
```

`-w <name>` selects which exported playlist you're working on, so several can be in flight. Songs already decided or matched exactly in another playlist are not searched again; `match --limit 0` shows how many of a freshly exported playlist still need a search.

## ✋ Two things only a human can do

- **Folders.** The Web API has no concept of playlist folders. Make the folder in the desktop app and drag the new playlist in.
- **Local files.** Paste their URIs into the new playlist in the desktop app: `verify` writes the ones still missing to `work/<playlist>/paste_local.txt` (`spotify:local:…` URIs, one per line); copy its contents, open the new playlist, click an empty spot and press Cmd+V. All of them land in one paste, at the end; the next `build` moves them into place. Dragging them from the old playlist works too. `verify` counts them, and flags any local file in the new playlist whose song has no keep-local decision (for example a file pasted in before its song was approved), since `build` never removes local files.

## 🔬 Matching details

<details>
<summary>How "exact" is decided</summary>

- `norm()`: NFKD-fold accents, lower-case, strip punctuation, collapse spaces, `&` → `and`. `norm_title()` additionally drops a leading `03 - ` track number.
- `core_title()`: additionally drops `(…)` / `[…]` groups, `feat.` tails and ` - Remastered` / ` - Live` / ` - Radio Edit` style suffixes. Used only to *flag* a `title_variant`, never to auto-add.
- Artists match if the local string equals any credited artist, the joined credits, or (after splitting on `,` `;` `/` `&` `+` `feat.` `ft.` `vs.` `x`) one side's set contains the other; as a last resort, the local string minus its `feat.` tail is compared to the first credited artist.
- Duration must be within `DUR_TOL_MS` (2000 ms). Spotify stores local-file lengths rounded to whole seconds, so that's about as tight as it gets.
- Candidates come from up to three searches (`track:"…" artist:"…"`, a plain query, and a parenthesis-stripped title), stopping early once an exact match appears.
</details>

<details>
<summary>Spotify Web API notes (October 2026)</summary>

Several endpoints were renamed and the old ones now return 403:

- `GET /playlists/{id}/tracks` → `GET /playlists/{id}/items`; each entry's track is under `item`, and the count is `items.total`.
- `POST /users/{id}/playlists` → `POST /me/playlists`.
- `public: false` on create is ignored; the tool follows up with a `PUT`, which Spotify may also ignore.
- `market=from_token` needs the `user-read-private` scope; the tool uses `market=US` instead (change the `MARKET` constant if you're elsewhere).
- Local files come back with `is_local: true`, a `spotify:local:…` URI and real title/artist/album/duration metadata, which is the whole reason this works.
</details>

## 🗂️ Files

| Path | What |
|---|---|
| `migrate.py` | the whole tool |
| `client_id.txt`, `token.json` | your credentials (gitignored) |
| `work/approvals.json` | standing decisions, applied across all playlists |
| `work/<playlist>/source.json` | exported source playlist |
| `work/<playlist>/matches.json` | per-song verdict, closest candidate, approvals |
| `work/<playlist>/matches.orphans.json` | entries dropped when a re-export no longer had their song (paid-for searches, kept for reference) |
| `work/<playlist>/flagged.csv` | review list of everything still open |
| `work/<playlist>/flagged_<date>.csv` | just that day's batch |
| `work/<playlist>/result.json` | id, URL and counts of the playlist that was built |
| `work/<playlist>/summary.json` | the breakdown printed after `match`, `approve` and `report` |
| `work/<playlist>/paste_local.txt` | written by `verify`: local files still to paste into the new playlist |
| `work/current` | which playlist commands act on when `-w` is not given |
| `BACKLOG.md` | which playlists are done, in progress and queued |

## 📜 License

MIT. Built with Claude Code doing the typing.
