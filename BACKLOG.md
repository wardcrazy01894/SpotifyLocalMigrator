# Backlog

Rule for every playlist: new playlist is named `<Original> (Spotify version)`;
first pass adds exact matches only (title + artist + duration), then the owner
reviews the flagged breakdown and approves additions by reason or position.
Finished playlists get dragged into the "Old iTunes playlists" folder by hand.

## Done

| Playlist | Songs | Added | Kept local | Result |
|---|---|---|---|---|
| Pry | 112 | 107 | 5 | https://open.spotify.com/playlist/034PA7UMN27PrxIpRDnI9Q — `verify` COMPLETE 2026-10-07 |

## In progress

| Playlist | Songs | Status |
|---|---|---|
| iPhone | 3262 | https://open.spotify.com/playlist/6RIfz6C47vRfneHYHXO5Sx — day by day. Daily Spotify search budget: 423 songs + ~30 re-searches (2026-10-08), 485 + 23 (2026-10-10), 487 + 8 (2026-10-11), 448 + 13 (2026-10-12), 492 + 11 (2026-10-13) before a 429; rolling 24 h window from the day's first search. 2026-10-14 end: all 3262 searched and decided (all 18 B2 → keep local): 3006 Spotify tracks + 9 collapsed duplicates + 247 local files, all pasted; the desktop app shows 3253 items. Left: `build` to move the pasted files into source order, then `verify` (after the 429 lifts ~19:05 on 2026-10-15). |

Daily loop: `python3 migrate.py -w iPhone match --limit 400` → `build` → review `work/iphone/flagged_<date>.csv` →
`approve --pos ... --keep-local ... --recheck ...` → `build` → `verify`.

Standing rules:
- A decision (approve / keep local) made for a song in one playlist applies to the same song in every playlist (`work/approvals.json`); exact matches found in any playlist are reused too (PR #8), so only unseen songs cost a search.
- A playlist is only done when `verify` says COMPLETE: source count == Spotify tracks in the new playlist + local files dragged in by hand.

## Queued (approved 2026-10-13 to start on 2026-10-14, after iPhone's searching is finished)

Order for each: `export "name:<Playlist>"` → `match --limit 0` (offline: shows how many songs still need a search after carry-over from iPhone/Pry) → `build --name "<Playlist> (Spotify version)"` with what is known → search the rest within the day's budget → flags → `build` → paste local files → `verify`.

| Playlist | Songs | Status (2026-10-14: all exported, searched, built and decided in one evening; 599 songs needed 106 searches thanks to carry-over; all 23 B2 → keep local; every local file pasted, counts match in the app; left: `build` to reorder the pasted files + `verify`, after the 429 lifts ~19:05 on 2026-10-15) |
|---|---|---|
| Seven Deadly Beats | 60 | https://open.spotify.com/playlist/0o70onbuKAyfpIcfr83fab — 48 Spotify + 12 local (pasted) |
| Highway Bro Seshin | 85 | https://open.spotify.com/playlist/4IoL2tTGwc6bO7MCZy6o9g — 82 Spotify + 3 local (pasted) |
| Volleyball | 5 | https://open.spotify.com/playlist/3mjcko825vaHcWYGumZK54 — 5 Spotify, `verify` COMPLETE 2026-10-14 |
| Clubbing | 53 | https://open.spotify.com/playlist/0IOofgcGmunhMLZ159VhfI — 33 Spotify + 1 dup + 19 local (pasted) |
| Songs I like now | 212 | https://open.spotify.com/playlist/5LbDULeHLazOgbaVAeRFBv — 190 Spotify + 22 local (pasted) |
| Remixes | 18 | https://open.spotify.com/playlist/1eq99yJu0k36crzjzIM4P8 — 2 Spotify + 16 local (pasted) |
| Cruisin | 68 | https://open.spotify.com/playlist/7mD8jFpgtNztHjbuwg6iHT — 56 Spotify + 12 local (pasted) |
| Pub | 61 | https://open.spotify.com/playlist/5BJzO9MJcxTVJh5muz4Oz6 — 52 Spotify + 9 local (pasted) |
| Semiformal | 37 | https://open.spotify.com/playlist/5rXLfE2ZO9nLS8ZhveX5ZF — 37 Spotify, `verify` COMPLETE 2026-10-14 |

Per playlist:

```bash
export SPOTIFY_CLIENT_ID=$(cat client_id.txt)
python3 migrate.py export "name:<Playlist>"
python3 migrate.py -w "<Playlist>" match
python3 migrate.py -w "<Playlist>" build --name "<Playlist> (Spotify version)"
# review work/<playlist>/flagged.csv, then:
python3 migrate.py -w "<Playlist>" approve --pos ... --keep-local ...
python3 migrate.py -w "<Playlist>" build
python3 migrate.py -w "<Playlist>" verify     # done only when it prints COMPLETE
```
