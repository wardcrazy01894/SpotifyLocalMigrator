# SpotifyLocalMigrator

Rebuild a Spotify **local files** playlist as a regular Spotify playlist, adding
only songs that are an exact catalog match, and get a clear list of everything
that wasn't.

Why: Spotify syncs local files to your phone from the desktop app, and that sync
is flaky. If 95% of a playlist is also in Spotify's catalog, you're better off
with a real playlist plus a handful of local files dragged in by hand.

- Plain Python 3.10+, standard library only. Nothing to install.
- Strict matching: **title + artist + duration (within 2 s)** must all agree.
- Everything else is **flagged with a reason**, never silently added or dropped.
- You approve flagged songs in bulk or one at a time; the playlist is rewritten
  in place, preserving the original order.
- Resumable: a 3,000-song playlist takes ~30 min at Spotify's search rate limit,
  and you can stop and restart without losing progress.

## One-time setup

1. Go to <https://developer.spotify.com/dashboard> and log in with the Spotify
   account that owns the playlists (Spotify requires Premium for the dashboard).
2. **Create app**:
   - App name / description: anything.
   - Redirect URI: `http://127.0.0.1:8888/callback` (exactly), click **Add**.
   - APIs used: tick **Web API**.
3. Open the app → **Settings** → copy the **Client ID** (the secret is not needed).
4. In this folder:

   ```bash
   echo YOUR_CLIENT_ID > client_id.txt         # gitignored
   export SPOTIFY_CLIENT_ID=$(cat client_id.txt)
   python3 migrate.py auth                      # opens a browser, click Agree once
   ```

   A refresh token is saved to `token.json` (gitignored) and renewed automatically.

## Run

```bash
export SPOTIFY_CLIENT_ID=$(cat client_id.txt)

python3 migrate.py find pry                    # list your playlists matching "pry"
python3 migrate.py export "name:Pry"           # -> work/pry/source.json
python3 migrate.py match                       # -> work/pry/matches.json + flagged.csv + breakdown
python3 migrate.py build --name "Pry (spotify version)"   # creates the playlist with exact matches only
```

`export` also accepts a playlist URL, URI, or 22-character id.

### Review and approve

`match` prints a breakdown like:

```
match               91   added
duration_mismatch    6   same title+artist, length differs (closest candidate shown)
title_variant        7   title matches only after stripping "(feat. ...)", "- Live", "(Album Version)", "Taylor's Version"...
artist_mismatch      3   title+length match, artist credited differently ("Pink" vs "P!nk, Nate Ruess")
no_match             5   nothing close (local-only songs, typos, covers)
```

`work/<playlist>/flagged.csv` lists each flagged song with the closest Spotify
track, the length difference, and its `pos` (position in the source playlist).

Approve by reason, by position, or mark songs to keep local, then rewrite the
playlist in place:

```bash
python3 migrate.py approve --reasons duration_mismatch,title_variant
python3 migrate.py approve --pos 42,107 --keep-local 55
python3 migrate.py build                        # rewrites the playlist created above, same URL, original order
python3 migrate.py report                       # re-print the breakdown any time
```

Several playlists can be in flight at once; `-w <name>` picks which one
(default is the last one exported): `python3 migrate.py -w Pry report`.

### Finishing by hand

Two things the Web API cannot do:

- **Playlist folders.** Create the folder in the desktop app and drag the new
  playlist in.
- **Local files.** Drag the leftover local-only songs from the old playlist into
  the new one in the desktop app. Local files can live in any playlist.

## Matching details

Titles and artists are compared after lower-casing, stripping accents and
punctuation, and removing a leading track number (`03 - Song` → `song`).
`&` and `and` are treated as equal. A local artist string like
`Jay-Z feat. Alicia Keys` matches a Spotify credit of `JAY-Z, Alicia Keys`.

Spotify stores local-file durations rounded to whole seconds, so the 2 s
tolerance is the floor of what's meaningful. Change `DUR_TOL_MS` at the top of
`migrate.py` if you want it looser.

Searches use `market=US`. Edit `search_candidates()` if you're elsewhere; with
the `user-read-private` scope you could use `market=from_token` instead.

## Files

| Path | What |
|---|---|
| `migrate.py` | the whole tool |
| `client_id.txt`, `token.json` | your credentials, gitignored |
| `work/<playlist>/source.json` | exported source playlist |
| `work/<playlist>/matches.json` | per-song verdict, closest candidate, approvals |
| `work/<playlist>/flagged.csv` | human-readable review list |
| `work/<playlist>/summary.json` | the counts |
| `work/<playlist>/result.json` | id/URL of the playlist that was built |

## Spotify API notes (October 2026)

Spotify renamed several endpoints and the old ones now return 403:

- `GET /playlists/{id}/tracks` → `GET /playlists/{id}/items`; each entry's track
  is under `item`, and a playlist's count is `items.total`.
- `POST /users/{id}/playlists` → `POST /me/playlists`.
- `public: false` on create was ignored; the tool follows up with a `PUT`.
- Local files come back with `is_local: true`, a `spotify:local:...` URI, and
  real title/artist/album/duration metadata, which is what makes this work.

## License

MIT
