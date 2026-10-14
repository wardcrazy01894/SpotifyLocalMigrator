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
| iPhone | 3262 | https://open.spotify.com/playlist/6RIfz6C47vRfneHYHXO5Sx — day by day. Daily Spotify search budget: 423 songs + ~30 re-searches (2026-10-08), 485 + 23 (2026-10-10), 487 + 8 (2026-10-11), 448 + 13 (2026-10-12), 492 + 11 (2026-10-13) before a 429; rolling 24 h window from the day's first search, currently opens ~17:40. 2026-10-13 end: 3061 searched, 3047 decided (2825 Spotify tracks + 7 collapsed duplicates + 215 keep-local), 0 open flags besides 14 rechecks for tomorrow (pos 2690,2732,2745,2752,2753,2754,2760,2777,2778,2804,2805,2884,2909,2951). 201 left to search. All 11 B2 decided (5 taken, 6 keep-local). Local files are pasted into the live playlist as spotify:local URIs (188 in so far, 27 to paste) and `build` moves them into source order. Late-evening `build` inserted the 5 B2 tracks, then the reorder read and `verify` got a 429 (Retry-After 18 h, all endpoints share the search window), so verify is pending tomorrow |

Daily loop: `python3 migrate.py -w iPhone match --limit 400` → `build` → review `work/iphone/flagged_<date>.csv` →
`approve --pos ... --keep-local ... --recheck ...` → `build` → `verify`.

Standing rules:
- A decision (approve / keep local) made for a song in one playlist applies to the same song in every playlist (`work/approvals.json`).
- A playlist is only done when `verify` says COMPLETE: source count == Spotify tracks in the new playlist + local files dragged in by hand.

## Queued (do not start without approval)

| Playlist | Songs |
|---|---|
| Seven Deadly Beats | 60 |
| Highway Bro Seshin | 85 |
| Volleyball | 5 |
| Clubbing | 53 |
| Songs I like now | 212 |
| Remixes | 18 |
| Cruisin | 68 |
| Pub | 61 |
| Semiformal | 37 |

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
