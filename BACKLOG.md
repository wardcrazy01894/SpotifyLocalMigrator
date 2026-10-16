# Backlog

Rule for every playlist: new playlist is named `<Original> (Spotify version)`;
first pass adds exact matches only (title + artist + duration), then the owner
reviews the flagged breakdown and approves additions by reason or position.
Finished playlists get dragged into the "Old iTunes playlists" folder by hand.

## Done — all 11 playlists COMPLETE (2026-10-15)

| Playlist | Songs | Spotify | Local | Result |
|---|---|---|---|---|
| Pry | 112 | 107 | 5 | https://open.spotify.com/playlist/034PA7UMN27PrxIpRDnI9Q — `verify` COMPLETE 2026-10-07 |
| iPhone | 3262 | 3006 | 247 | https://open.spotify.com/playlist/6RIfz6C47vRfneHYHXO5Sx — 9 collapsed duplicates; `verify` COMPLETE 2026-10-15 |
| Seven Deadly Beats | 60 | 48 | 12 | https://open.spotify.com/playlist/0o70onbuKAyfpIcfr83fab — `verify` COMPLETE 2026-10-15 |
| Highway Bro Seshin | 85 | 82 | 3 | https://open.spotify.com/playlist/4IoL2tTGwc6bO7MCZy6o9g — `verify` COMPLETE 2026-10-15 |
| Volleyball | 5 | 5 | 0 | https://open.spotify.com/playlist/3mjcko825vaHcWYGumZK54 — `verify` COMPLETE 2026-10-14 |
| Clubbing | 53 | 33 | 19 | https://open.spotify.com/playlist/0IOofgcGmunhMLZ159VhfI — 1 collapsed duplicate; `verify` COMPLETE 2026-10-15 |
| Songs I like now | 212 | 190 | 22 | https://open.spotify.com/playlist/5LbDULeHLazOgbaVAeRFBv — `verify` COMPLETE 2026-10-15 |
| Remixes | 18 | 2 | 16 | https://open.spotify.com/playlist/1eq99yJu0k36crzjzIM4P8 — `verify` COMPLETE 2026-10-15 |
| Cruisin | 68 | 56 | 12 | https://open.spotify.com/playlist/7mD8jFpgtNztHjbuwg6iHT — `verify` COMPLETE 2026-10-15 |
| Pub | 61 | 52 | 9 | https://open.spotify.com/playlist/5BJzO9MJcxTVJh5muz4Oz6 — `verify` COMPLETE 2026-10-15 |
| Semiformal | 37 | 37 | 0 | https://open.spotify.com/playlist/5rXLfE2ZO9nLS8ZhveX5ZF — `verify` COMPLETE 2026-10-14 |

Totals: 3973 source songs → 3618 Spotify tracks + 345 local files + 10 collapsed duplicates.
iPhone took 2026-10-08 to 2026-10-14 (daily search budget 423–492 songs plus a few
re-searches before a 429); the other 9 playlists were done on 2026-10-14 in one evening
because decisions and exact matches carry over (599 songs needed 106 searches).
All 23 "different version" (B2) flags were kept local by the owner.

## In progress / Queued

Nothing. Open a new row here if a source playlist changes; `export` → `match` → `build` →
`verify` picks up only the new songs.

Standing rules:
- A decision (approve / keep local) made for a song in one playlist applies to the same song in every playlist (`work/approvals.json`); exact matches found in any playlist are reused too (PR #8), so only unseen songs cost a search.
- A playlist is only done when `verify` says COMPLETE: source count == Spotify tracks in the new playlist + local files pasted in by hand + collapsed duplicates.
- Once the daily search budget is exhausted, every endpoint answers 429 until the window resets, so run `build` / `verify` before the day's last searches.

Per playlist:

```bash
export SPOTIFY_CLIENT_ID=$(cat client_id.txt)
python3 migrate.py export "name:<Playlist>"
python3 migrate.py -w "<Playlist>" match
python3 migrate.py -w "<Playlist>" build          # named "<Playlist> (Spotify version)" by default
# review work/<playlist>/flagged.csv, then:
python3 migrate.py -w "<Playlist>" approve --pos ... --keep-local ...
python3 migrate.py -w "<Playlist>" build
python3 migrate.py -w "<Playlist>" verify     # done only when it prints COMPLETE
# paste work/<playlist>/paste_local.txt into the new playlist, then build + verify again
```
