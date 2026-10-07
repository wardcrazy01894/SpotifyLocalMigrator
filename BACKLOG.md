# Backlog

Rule for every playlist: new playlist is named `<Original> (Spotify version)`;
first pass adds exact matches only (title + artist + duration), then the owner
reviews the flagged breakdown and approves additions by reason or position.
Finished playlists get dragged into the "Old iTunes playlists" folder by hand.

## Done

| Playlist | Songs | Added | Kept local | Result |
|---|---|---|---|---|
| Pry | 112 | 107 | 5 | https://open.spotify.com/playlist/034PA7UMN27PrxIpRDnI9Q |

## In progress

| Playlist | Songs | Status |
|---|---|---|
| iPhone | 3262 | day-by-day. Each session: `python3 migrate.py -w iPhone match --limit 300` → `build` → review `work/iphone/flagged_<date>.csv` → `approve ...` → `build`. 2026-10-07: 290 searched, 193 added, 97 flagged awaiting answers. Spotify search quota resets ~12:43 daily. |

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
