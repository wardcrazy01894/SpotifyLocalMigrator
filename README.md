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
  local files playlist                       real Spotify playlist
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
| ✅ **Approve in bulk or one by one** | By reason, by position, or "keep this one local forever". The playlist is rewritten in place, original order preserved. |
| 🔁 **Decisions carry everywhere** | Approve *Three Little Birds* once and it's approved in every other playlist that has it. |
| 📅 **Built for the daily grind** | Spotify caps searches per day. `match --limit 300` does a batch and stops; progress is saved every 10 songs. |
| 🧾 **Completeness check** | `verify` proves source count = Spotify tracks + local files, and lists anything undecided. |
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
python3 migrate.py build --name "Pry (Spotify version)"   # create it with exact matches only
```

You'll get a breakdown like this:

```
{
 "total": 112,
 "searched": 112,
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
Open flags: work/pry/flagged.csv
```

### 🧑‍⚖️ Then you judge

Open `work/<playlist>/flagged.csv`. Each row has your file, the closest Spotify track, the length difference and a `pos` you can reference.

```bash
python3 migrate.py approve --reasons title_variant          # "(feat. X)" vs "feat. X"? fine, take them all
python3 migrate.py approve --pos 37,134                      # these two specifically
python3 migrate.py approve --keep-local 141                  # never add this one, I like my version
python3 migrate.py approve --recheck 211,223                 # the search landed on the wrong track, look again later
python3 migrate.py research --pos 211 --query "I See Fire Kygo remix"   # …like this
python3 migrate.py build                                     # rewrite the playlist, same URL, original order
python3 migrate.py verify                                    # are we done yet?
```

```
Source 'Pry': 112 songs (112 searched so far)
New playlist: 107 Spotify tracks + 5 local files = 112
  Spotify tracks expected from decisions: 107  OK
  Local files to drag in by hand: 5  (present now: 5)
  Unresolved (not yet decided or searched): 0
COMPLETE: every source song is accounted for in the new playlist.
```

### 📅 Big playlist? Do it in days

Spotify's search endpoint allows roughly 400 searches per day for a personal app, then returns a 24-hour timeout. The tool stops cleanly instead of sleeping, and picks up where it left off:

```bash
python3 migrate.py -w iPhone match --limit 300    # today's batch
python3 migrate.py -w iPhone build                # playlist grows a bit
# review work/iphone/flagged_<today>.csv, approve, build, verify… see you tomorrow
```

`-w <name>` selects which exported playlist you're working on, so several can be in flight.

## ✋ Two things only a human can do

- **Folders.** The Web API has no concept of playlist folders. Make the folder in the desktop app and drag the new playlist in.
- **Local files.** Drag the keep-local songs from the old playlist into the new one in the desktop app. `verify` counts them.

## 🔬 Matching details

<details>
<summary>How "exact" is decided</summary>

- `norm()`: NFKD-fold accents, lower-case, strip punctuation, collapse spaces, `&` → `and`, drop a leading `03 - ` track number.
- `core_title()`: additionally drops `(…)` / `[…]` groups, `feat.` tails and ` - Remastered` / ` - Live` / ` - Radio Edit` style suffixes. Used only to *flag* a `title_variant`, never to auto-add.
- Artists match if the local string equals any credited artist, the joined credits, or (after splitting on `,` `;` `&` `feat.` `vs.` `x`) one side's set contains the other.
- Duration must be within `DUR_TOL_MS` (2000 ms). Spotify stores local-file lengths rounded to whole seconds, so that's about as tight as it gets.
- Candidates come from up to three searches (`track:"…" artist:"…"`, a plain query, and a parenthesis-stripped title), stopping early once an exact match appears.
</details>

<details>
<summary>Spotify Web API notes (October 2026)</summary>

Several endpoints were renamed and the old ones now return 403:

- `GET /playlists/{id}/tracks` → `GET /playlists/{id}/items`; each entry's track is under `item`, and the count is `items.total`.
- `POST /users/{id}/playlists` → `POST /me/playlists`.
- `public: false` on create is ignored; the tool follows up with a `PUT`, which Spotify may also ignore.
- `market=from_token` needs the `user-read-private` scope; the tool uses `market=US` instead (edit `search_candidates()` if you're elsewhere).
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
| `work/<playlist>/flagged.csv` | review list of everything still open |
| `work/<playlist>/flagged_<date>.csv` | just that day's batch |
| `work/<playlist>/result.json` | id and URL of the playlist that was built |
| `BACKLOG.md` | which playlists are done, in progress and queued |

## 📜 License

MIT. Built with Claude Code doing the typing.
