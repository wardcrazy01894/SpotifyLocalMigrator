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
| iPhone | 3262 | https://open.spotify.com/playlist/6RIfz6C47vRfneHYHXO5Sx — day by day, ~300 songs/session (Spotify search quota ≈400/day, resets ~12:43). 2026-10-07 end: 290 searched, 253 on Spotify, 13 keep-local (to drag in by hand), 24 open flags: 14 marked recheck (Kygo remixes, Let It Go, etc. — re-search with `research`), 9 nothing-found pending a decision, 1 other. Group B fully decided. |

Daily loop: `python3 migrate.py -w iPhone match --limit 300` → `build` → review `work/iphone/flagged_<date>.csv` →
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
python3 migrate.py match
python3 migrate.py build --name "<Playlist> (Spotify version)"
# review work/<playlist>/flagged.csv, then:
python3 migrate.py approve --reasons ... --pos ... --keep-local ...
python3 migrate.py build
```
