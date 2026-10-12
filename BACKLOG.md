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
| iPhone | 3262 | https://open.spotify.com/playlist/6RIfz6C47vRfneHYHXO5Sx — day by day. Daily Spotify search budget: 423 songs + ~30 re-searches (2026-10-08), 485 + 23 (2026-10-10), 487 + 8 (2026-10-11), 448 + 13 (2026-10-12) before a 429; rolling 24 h window from the day's first search, currently opens ~17:40. 2026-10-12 end: 2569 searched, 2370 on Spotify (2363 tracks + 7 collapsed duplicates), 181 keep-local, 7 B2 flags awaiting the user (pos 1639,2156,2241,2311,2405,2513,2548), 11 rechecks for tomorrow (pos 2158,2229,2256,2270,2316,2331,2332,2335,2463,2464,2542). 693 left to search. |

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
