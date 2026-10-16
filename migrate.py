#!/usr/bin/env python3
"""Rebuild a Spotify "local files" playlist as a proper Spotify playlist.

Pipeline (each step is resumable and writes to ./work/<slug>/):
  auth              one-time browser login (PKCE, no client secret needed)
  find   <name>     locate one of your playlists by name, print its id/url
  export <ref>      dump the source playlist (incl. local files) -> source.json
  match [--limit N] search Spotify for each local track; strict matching -> matches.json
  build             create the new playlist, or bring the existing one up to date -> result.json
  approve           approve flagged songs (by reason or position), keep-local, recheck, unkeep
  research          re-search one song by hand, or pick a specific track for it
  verify            prove every source song is accounted for in the new playlist
  report            re-print the breakdown and rewrite flagged.csv
  all    <ref>      export + match + build in one go (small playlists)

<ref> is a playlist URL/URI/id, or a playlist name prefixed with "name:".
-w <name> selects which exported playlist the command works on.
Only the Python standard library is used. The Web API cannot create playlist
folders or add local files, so those two steps stay manual in the desktop app.
"""
from __future__ import annotations

import argparse
import base64
import bisect
import csv
import datetime
import hashlib
import http.client
import http.server
import json
import os
import re
import secrets
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK_ROOT = HERE / "work"
TOKEN_FILE = HERE / "token.json"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = (
    "playlist-read-private playlist-read-collaborative "
    "playlist-modify-private playlist-modify-public"
)
API = "https://api.spotify.com/v1"
MARKET = "US"              # search/track market; the token's scopes do not allow market=from_token
DUR_TOL_MS = 2000          # title+artist+duration within this => added


# ----------------------------------------------------------------------------
# Auth (Authorization Code with PKCE)
# ----------------------------------------------------------------------------
def _client_id() -> str:
    cid = os.environ.get("SPOTIFY_CLIENT_ID")
    if not cid:
        sys.exit("Set SPOTIFY_CLIENT_ID (from https://developer.spotify.com/dashboard).")
    return cid


def _token_request(data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    hint = "If the refresh token was revoked, run: python3 migrate.py auth"
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        sys.exit(f"Spotify token request failed ({e.code}): {detail}\n{hint}")
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
        sys.exit(f"Spotify token request failed before a reply ({type(e).__name__}: {e}); check the network and retry.")
    try:
        tok = json.loads(raw)
    except ValueError:
        sys.exit(f"Spotify token request returned something that is not JSON: {raw[:200]!r}\n{hint}")
    tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
    if "refresh_token" not in tok and TOKEN_FILE.exists():
        tok["refresh_token"] = _load_json(TOKEN_FILE).get("refresh_token")
    # Holds the access and refresh tokens: owner-only.
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if hasattr(os, "fchmod"):
        os.fchmod(fd, 0o600)  # an existing 0644 file keeps its mode through O_TRUNC
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(tok))
    TOKEN_FILE.chmod(0o600)
    return tok


def cmd_auth(_args) -> None:
    cid = _client_id()
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    state = secrets.token_urlsafe(16)
    got: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Done. You can close this tab.</h2>")

        def log_message(self, *a):
            pass

    try:
        srv = http.server.HTTPServer(("127.0.0.1", 8888), Handler)
    except OSError as e:
        sys.exit(f"Cannot listen on 127.0.0.1:8888 for the login redirect ({e}); close whatever is using that port and rerun auth.")
    threading.Thread(target=srv.handle_request, daemon=True).start()

    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(
        {
            "client_id": cid,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
    )
    print("Opening browser for Spotify login. If it does not open, visit:\n", url)
    webbrowser.open(url)
    deadline = time.time() + 300
    while "code" not in got and "error" not in got and time.time() < deadline:
        time.sleep(0.2)
    srv.server_close()
    if not got:
        sys.exit("Timed out waiting for the browser redirect.")
    if got.get("state") != state:
        sys.exit("State mismatch on the redirect (another request hit the callback port?); run auth again.")
    if "error" in got:
        sys.exit(f"Spotify returned error: {got['error']}")
    if "code" not in got:
        sys.exit("Redirect carried no authorization code.")
    _token_request(
        {
            "grant_type": "authorization_code",
            "code": got["code"],
            "redirect_uri": REDIRECT_URI,
            "client_id": cid,
            "code_verifier": verifier,
        }
    )
    me = api_get("/me")
    print(f"Logged in as {me.get('display_name')} ({me.get('id')}). Token saved to {TOKEN_FILE.name}.")


def access_token() -> str:
    if not TOKEN_FILE.exists():
        sys.exit("Not logged in. Run: python3 migrate.py auth")
    tok = _load_json(TOKEN_FILE)
    if time.time() >= tok.get("expires_at", 0):
        if not tok.get("refresh_token"):
            sys.exit("Token expired and no refresh token saved. Run: python3 migrate.py auth")
        tok = _token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": tok["refresh_token"],
                "client_id": _client_id(),
            }
        )
    return tok["access_token"]


# ----------------------------------------------------------------------------
# HTTP helpers with 429 handling
# ----------------------------------------------------------------------------
def _retry_after(headers) -> int:
    try:
        return int(float(headers.get("Retry-After", "2"))) + 1
    except (TypeError, ValueError):
        return 3


MAX_429_WAIT_S = 600  # total sleeping on 429s for one call before giving up with RateLimited


def _call(method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
    """One API call. GETs are retried on 5xx/network errors; writes are not, because a
    retried POST whose first attempt actually landed would insert tracks twice. Rerunning
    `build` is safe instead: it re-reads the playlist and only adds what is missing.
    Failures raise ApiError (method, URL, status and body) or RateLimited."""
    url = path if path.startswith("http") else API + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    attempts = 8 if method == "GET" else 1
    last_err = ""
    attempt = 0
    waited = 0
    while attempt < attempts:
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", f"Bearer {access_token()}")
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, data=data, timeout=30) as r:
                raw = r.read()
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except ValueError:
                raise ApiError(f"{method} {url} -> {getattr(r, 'status', '2xx')} but the body is not JSON: {raw[:200]!r}") from None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # Rejected before processing, so retrying is safe for writes too; does not count as an attempt.
                wait = _retry_after(e.headers)
                if wait > 120:
                    raise RateLimited(wait, f"{method} {url}") from None
                if waited + wait > MAX_429_WAIT_S:
                    raise RateLimited(wait, f"{method} {url} still throttled after {waited}s of waiting") from None
                waited += wait
                print(f"\n  {method} {url} -> 429; sleeping {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            detail = e.read().decode(errors="replace")
            last_err = f"{e.code}: {detail[:300]}"
            attempt += 1
            if e.code in (500, 502, 503, 504) and attempt < attempts:
                print(f"\n  {method} {url} -> {last_err}; retrying", file=sys.stderr)
                time.sleep(2 * attempt)
                continue
            raise ApiError(f"{method} {url} -> {last_err}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as e:
            last_err = f"{type(e).__name__}: {e}"
            attempt += 1
            if attempt < attempts:
                print(f"\n  {method} {url} -> {last_err}; retrying", file=sys.stderr)
                time.sleep(2 * attempt)
    raise ApiError(f"Giving up on {method} {url} after {attempts} attempt(s); last error: {last_err}")


class ApiError(RuntimeError):
    """A Spotify call failed for good (status, URL and body are in the message). Rerunning the
    command is safe: every step re-reads its state and only does what is still missing."""


class RateLimited(Exception):
    def __init__(self, wait_s: int, note: str = ""):
        # rounded up at every scale, so the message never promises less than the real wait
        when = f"{-(-wait_s // 360) / 10:.1f} h" if wait_s >= 3600 else f"{-(-wait_s // 60)} min" if wait_s >= 60 else f"{wait_s}s"
        super().__init__(f"Spotify rate limit ({note}); retry after {when}" if note else f"Spotify rate limit; retry after {when}")
        self.wait_s = wait_s


def api_get(path: str, **params) -> dict:
    return _call("GET", path, params or None)


def api_post(path: str, body: dict) -> dict:
    return _call("POST", path, body=body)


def paged(url: str, params: dict | None) -> list[dict]:
    out: list[dict] = []
    first = True
    while url:
        page = _call("GET", url, params if first else None)
        first = False
        out.extend(page.get("items", []))
        url = page.get("next")
    return out


# ----------------------------------------------------------------------------
# Locate / export
# ----------------------------------------------------------------------------
def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower() or "playlist"


def workdir(name: str) -> Path:
    d = WORK_ROOT / slug(name)
    d.mkdir(parents=True, exist_ok=True)
    return d


WORK_OVERRIDE: str | None = None


def current_work() -> Path:
    if WORK_OVERRIDE:
        d = WORK_ROOT / slug(WORK_OVERRIDE)
        if not (d / "source.json").exists():
            sys.exit(f"No export found at {d}. Run: python3 migrate.py export \"name:{WORK_OVERRIDE}\"")
        return d
    marker = WORK_ROOT / "current"
    if not marker.exists():
        sys.exit("Nothing exported yet. Run: python3 migrate.py export <ref>")
    return Path(marker.read_text().strip())


def _load_json(path: Path, missing=None):
    """Read a JSON file. A missing file returns `missing()` when a factory is given (else exits);
    a corrupt or unreadable one exits naming the file, so the cause is never a bare traceback."""
    if missing is not None and not path.exists():
        return missing()
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as e:
        sys.exit(f"Cannot read {path}: {e}")


def _total(p: dict) -> int:
    # "items" since the Oct 2026 rename; "tracks" kept for older responses
    return (p.get("items") or p.get("tracks") or {}).get("total", 0)


def my_playlists() -> list[dict]:
    return paged(f"{API}/me/playlists", {"limit": 50})


def resolve_playlist(ref: str) -> dict:
    if ref.startswith("name:"):
        want = ref[5:].strip().lower()
        hits = [p for p in my_playlists() if (p.get("name") or "").strip().lower() == want]
        if not hits:
            sys.exit(f"No playlist named '{ref[5:]}' in your library.")
        if len(hits) > 1:
            print("Multiple playlists share that name; using the first:", file=sys.stderr)
            for p in hits:
                print(f"  {p['name']}  {_total(p)} items  {p['external_urls']['spotify']}", file=sys.stderr)
        return hits[0]
    m = re.search(r"playlist[/:]([A-Za-z0-9]{22})", ref) or re.fullmatch(r"([A-Za-z0-9]{22})", ref)
    if not m:
        sys.exit(f"Could not parse playlist reference: {ref}")
    return api_get(f"/playlists/{m.group(1)}", fields="id,name,external_urls,items.total,tracks.total")


def cmd_find(args) -> None:
    want = args.name.strip().lower()
    for p in my_playlists():
        if want in (p.get("name") or "").lower():
            print(f"{p['name']!r}  {_total(p)} items  {p['external_urls']['spotify']}")


def cmd_export(args) -> Path:
    pl = resolve_playlist(args.playlist)
    pid, name = pl["id"], pl["name"]
    wd = workdir(name)
    (WORK_ROOT / "current").write_text(str(wd))
    print(f"Source playlist: {name} ({_total(pl)} items)")
    raw = paged(
        f"{API}/playlists/{pid}/items",
        {
            "limit": 50,
            "additional_types": "track",
            "fields": "next,items(is_local,item(uri,id,name,duration_ms,artists(name),album(name)))",
        },
    )
    items: list[dict] = []
    skipped = 0
    for it in raw:
        t = it.get("item") or {}
        if not t:
            skipped += 1  # unavailable/removed entry; Spotify returns it as null
            continue
        items.append(
            {
                "pos": len(items),
                "is_local": bool(it.get("is_local")),
                "uri": t.get("uri"),
                "id": t.get("id"),
                "title": t.get("name") or "",
                "artist": ", ".join(a.get("name", "") for a in t.get("artists") or []),
                "album": (t.get("album") or {}).get("name") or "",
                "duration_ms": t.get("duration_ms") or 0,
            }
        )
    local = sum(1 for i in items if i["is_local"])
    (wd / "source.json").write_text(json.dumps({"name": name, "id": pid, "items": items}, indent=1))
    print(f"Exported {len(items)} items ({local} local files, {len(items) - local} Spotify tracks) -> {wd / 'source.json'}")
    if skipped:
        print(f"  {skipped} unavailable entr{'y' if skipped == 1 else 'ies'} skipped; positions are renumbered", file=sys.stderr)
    return wd


# ----------------------------------------------------------------------------
# Matching (strict: title + artist + duration)
# ----------------------------------------------------------------------------
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")
_TRACKNO = re.compile(r"^\s*\d{1,3}\s*[-._)\]]*\s+")
_PAREN = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_FEAT = re.compile(r"\b(feat|ft|featuring)\b.*$", re.IGNORECASE)
_SPLIT_ARTISTS = re.compile(
    r"\s*(?:,|;|/|&|\+|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|\bvs\.?\b|\bx\b)\s*", re.IGNORECASE
)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ").replace("’", "'")
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


def norm_title(s: str) -> str:
    return norm(_TRACKNO.sub("", s or ""))


def core_title(s: str) -> str:
    s = _TRACKNO.sub("", s or "")
    s = _PAREN.sub(" ", s)
    s = _FEAT.sub("", s)
    s = re.sub(
        r"\s*-\s*(remaster(ed)?|album version|single version|explicit|clean|radio edit|live|acoustic|\d{4}\b).*$",
        "", s, flags=re.I,
    )
    return norm(s)


def artist_set(s: str) -> set[str]:
    return {norm(a) for a in _SPLIT_ARTISTS.split(s or "") if norm(a)}


def artists_match(local_artist: str, cand_artists: list[str]) -> bool:
    la = norm(local_artist)
    if not la or not cand_artists:
        return False
    cand = [norm(a) for a in cand_artists]
    if la in cand or la == norm(", ".join(cand_artists)) or la == " ".join(cand):
        return True
    ls, cs = artist_set(local_artist), set(cand)
    if ls and (ls <= cs or cs <= ls):
        return True
    return norm(_FEAT.sub("", local_artist)) == cand[0]


def evaluate(src: dict, c: dict) -> dict:
    """Score one search candidate against the local track.

    verdict: match | duration_mismatch | title_variant | artist_mismatch | no_match
    Only 'match' is added to the playlist.
    """
    cand_artists = [a["name"] for a in c.get("artists", [])]
    title_exact = norm_title(src["title"]) == norm_title(c["name"])
    title_core = core_title(src["title"]) == core_title(c["name"])
    artist_ok = artists_match(src["artist"], cand_artists)
    dur_src, dur_c = src["duration_ms"] or 0, c.get("duration_ms", 0)
    dur_diff = abs(dur_src - dur_c) if dur_src else None
    dur_ok = dur_diff is not None and dur_diff <= DUR_TOL_MS

    score = (40 if title_exact else 20 if title_core else 0) + (30 if artist_ok else 0)
    if dur_diff is not None:
        score += 30 if dur_ok else max(0, 20 - dur_diff // 1000)

    if title_exact and artist_ok and dur_ok:
        verdict = "match"
    elif title_exact and artist_ok:
        verdict = "duration_mismatch" if dur_diff is not None else "no_duration"
    elif title_core and artist_ok:
        verdict = "title_variant"
    elif (title_exact or title_core) and not artist_ok:
        verdict = "artist_mismatch"
    else:
        verdict = "no_match"
    return {
        "verdict": verdict,
        "score": score,
        "uri": c["uri"],
        "title": c["name"],
        "artist": ", ".join(cand_artists),
        "album": c.get("album", {}).get("name", ""),
        "duration_ms": dur_c,
        "dur_diff_ms": dur_diff,
    }


def search_candidates(src: dict) -> list[dict]:
    title = _TRACKNO.sub("", src["title"]).strip()
    primary = next(iter(_SPLIT_ARTISTS.split(src["artist"] or "")), "").strip()
    queries: list[str] = []
    if title and primary:
        queries.append(f'track:"{title}" artist:"{primary}"')
        queries.append(f"{title} {primary}")
    ct = _PAREN.sub("", title).strip()
    if ct and ct != title and primary:
        queries.append(f'track:"{ct}" artist:"{primary}"')
    if title and not primary:
        queries.append(f'track:"{title}"')
    seen: dict[str, dict] = {}
    for q in queries:
        res = api_get("/search", q=q, type="track", limit=10, market=MARKET)
        for t in filter(None, res.get("tracks", {}).get("items", [])):  # Spotify can return null items
            seen.setdefault(t["id"], t)
        if any(evaluate(src, t)["verdict"] == "match" for t in seen.values()):
            break
    return list(seen.values())


def cmd_match(args) -> None:
    wd, src, cache = load_state()  # aligns the cache first: a re-export may have shifted positions
    cache_path = wd / "matches.json"
    items = src["items"]
    batch = datetime.date.today().isoformat()
    todo = [i for i in items if i["is_local"] and str(i["pos"]) not in cache]
    todo, decided, matched = carry_unsearched(src, cache, todo, batch)  # known already: no search needed
    if decided:
        print(f"  {decided} song(s) decided in another playlist; not searching them")
    if matched:
        print(f"  {matched} song(s) matched exactly in another playlist; reusing those tracks")
    remaining = len(todo)
    limit = getattr(args, "limit", None)
    if limit is not None:
        if limit < 0:
            sys.exit("--limit must be 0 or more")
        todo = todo[:limit]
    print(f"{len(items)} items; {remaining} local files still to match; doing {len(todo)} now (batch {batch})")
    t0 = time.time()
    stopped: str | None = None
    failures = 0
    found: dict[str, dict] = {}  # exact matches made in this run, for duplicates later in the list
    try:
        for n, it in enumerate(todo, 1):
            key = song_key(it)
            if has_identity(it) and key in found:
                cache[str(it["pos"])] = carried_match(found[key], src["name"], batch, key)
            else:
                try:
                    cands = search_candidates(it)
                    failures = 0
                except RateLimited as e:
                    stopped = str(e)
                    break
                except RuntimeError as e:
                    failures += 1
                    print(f"\n  search failed for #{it['pos']} {it['title']}: {e}", file=sys.stderr)
                    if failures >= 5:
                        stopped = f"{failures} consecutive search failures; last: {e}"
                        break
                    continue
                _store_best(cache, it, cands, batch)
                if cache[str(it["pos"])]["verdict"] == "match":
                    found.setdefault(key, cache[str(it["pos"])])
                time.sleep(0.05)
            if n % 10 == 0 or n == len(todo):
                cache_path.write_text(json.dumps(cache, indent=1))
                rate = n / max(time.time() - t0, 1e-6)
                print(f"  {n}/{len(todo)}  ({rate:.1f}/s)", end="\r", flush=True)
    finally:
        cache_path.write_text(json.dumps(cache, indent=1))  # never lose paid-for searches
    print()
    carried = apply_global(src, cache)
    if carried:
        print(f"  {carried} decision(s) carried over from earlier playlists")
    cache_path.write_text(json.dumps(cache, indent=1))
    if stopped:
        print(f"STOPPED EARLY: {stopped}. Progress is saved; rerun match later.", file=sys.stderr)
    write_reports(wd, src, cache, batch=batch)


def _store_best(cache: dict, it: dict, cands: list[dict], batch: str) -> None:
    if not cands:
        cache[str(it["pos"])] = {"verdict": "no_candidates", "score": 0, "uri": None, "batch": batch, "key": song_key(it)}
        return
    order = {"match": 5, "duration_mismatch": 4, "no_duration": 4, "title_variant": 3, "artist_mismatch": 2, "no_match": 1}
    best = max((evaluate(it, c) for c in cands), key=lambda r: (order[r["verdict"]], r["score"]))
    best["batch"] = batch
    best["key"] = song_key(it)
    cache[str(it["pos"])] = best


def fmt_dur(ms: int | None) -> str:
    ms = ms or 0
    return f"{ms // 60000}:{(ms // 1000) % 60:02d}"


def write_reports(wd: Path, src: dict, cache: dict, batch: str | None = None) -> dict:
    """Print the breakdown; write flagged.csv (all open flags) and, if batch is
    given, flagged_<batch>.csv with only that batch's open flags."""
    counts: dict[str, int] = {}
    rows: list[dict] = []
    for it in src["items"]:
        if not it["is_local"]:
            counts["already_spotify"] = counts.get("already_spotify", 0) + 1
            continue
        m = cache.get(str(it["pos"]), {"verdict": "not_searched"})
        v = m["verdict"]
        if m.get("keep_local"):
            v = "keep_local"
        elif m.get("approved"):
            v = "approved_" + v
        bucket = "recheck" if m.get("recheck") else v  # a recheck is open whatever the verdict was
        counts[bucket] = counts.get(bucket, 0) + 1
        if bucket in ("match", "not_searched", "keep_local") or bucket.startswith("approved_"):
            continue
        rows.append(
            {
                "batch": m.get("batch", ""),
                "reason": v + (" (recheck)" if m.get("recheck") else ""),
                "pos": it["pos"],
                "title": it["title"],
                "artist": it["artist"],
                "album": it["album"],
                "duration": fmt_dur(it["duration_ms"]),
                "closest_spotify_track": (
                    f"{m.get('title')} — {m.get('artist')} [{m.get('album')}] {fmt_dur(m.get('duration_ms'))}"
                    if m.get("uri") else ""
                ),
                "duration_diff_s": (
                    f"{m['dur_diff_ms'] / 1000:.1f}" if m.get("dur_diff_ms") is not None else ""
                ),
                "closest_uri": m.get("uri") or "",
            }
        )
    rows.sort(key=lambda r: (r["reason"], r["pos"]))
    fields = list(rows[0].keys()) if rows else ["reason"]
    with (wd / "flagged.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    if batch:
        with (wd / f"flagged_{batch}.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows([r for r in rows if r["batch"] == batch])
    searched = sum(v for k, v in counts.items() if k not in ("not_searched", "already_spotify"))
    will_add = counts.get("match", 0) + sum(v for k, v in counts.items() if k.startswith("approved_"))
    summary = {
        "total": len(src["items"]),
        "searched": searched,
        "not_searched": counts.get("not_searched", 0),
        "in_playlist_after_build": will_add + counts.get("already_spotify", 0),
        "open_flags": len(rows),
        "counts": counts,
    }
    (wd / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    print(f"Open flags: {wd / 'flagged.csv'}" + (f"; this batch: {wd / f'flagged_{batch}.csv'}" if batch else ""))
    return summary


# ----------------------------------------------------------------------------
# Approve / report (the human-in-the-loop part)
# ----------------------------------------------------------------------------
GLOBAL_APPROVALS = WORK_ROOT / "approvals.json"


def wanted(m: dict | None) -> bool:
    """Should this local track's candidate go into the new playlist? A recheck is open, not wanted."""
    if not m or not m.get("uri") or m.get("keep_local") or m.get("recheck"):
        return False
    return m["verdict"] == "match" or m.get("approved") is True


def song_key(it: dict) -> str:
    return f"{norm_title(it['title'])}|{norm(it['artist'])}|{(it['duration_ms'] or 0) // 1000}"


def load_global() -> dict:
    return _load_json(GLOBAL_APPROVALS, missing=dict)


def has_identity(it: dict) -> bool:
    """A standing decision needs a title and an artist; otherwise every metadata-less file would share it."""
    return bool(norm_title(it["title"])) and bool(norm(it["artist"]))


def record_global(it: dict, m: dict, decision: str, playlist: str) -> None:
    """Standing rule: a decision made in one playlist applies to the same song everywhere."""
    if not has_identity(it):
        return
    g = load_global()
    g[song_key(it)] = {
        "decision": decision,  # approve | keep_local
        "uri": m.get("uri") if decision == "approve" else None,
        "title": it["title"],
        "artist": it["artist"],
        "from_playlist": playlist,
    }
    GLOBAL_APPROVALS.write_text(json.dumps(g, indent=1))


CARRY_FIELDS = ("verdict", "score", "uri", "title", "artist", "album", "duration_ms", "dur_diff_ms")


def known_matches() -> dict[str, dict]:
    """Exact matches already found while working on any playlist, keyed by song_key.

    An exact match depends only on the song's title, artist and length, so the same file in
    another playlist needs no search. Read from every work dir's source.json + matches.json,
    nothing extra to maintain. Entries the owner has since overridden (keep-local, recheck)
    or that belong to a different song than their position now holds are skipped."""
    out: dict[str, dict] = {}
    if not WORK_ROOT.is_dir():
        return out
    for d in sorted(WORK_ROOT.iterdir()):
        sp, mp = d / "source.json", d / "matches.json"
        if not (sp.is_file() and mp.is_file()):
            continue
        try:
            src, cache = json.loads(sp.read_text()), json.loads(mp.read_text())
        except (OSError, ValueError) as e:
            print(f"  WARNING: skipping {d.name} for match reuse, cannot read its files: {e}", file=sys.stderr)
            continue
        for it in src.get("items", []):
            if not it.get("is_local") or not has_identity(it):
                continue
            m = cache.get(str(it["pos"]))
            if not m or m.get("verdict") != "match" or not m.get("uri") or m.get("keep_local") or m.get("recheck"):
                continue
            if m.get("carried_from"):
                continue  # only real search results seed reuse; a copy may be stale after a recheck/research elsewhere
            if m.get("key") != song_key(it):
                continue  # unkeyed legacy entry, or a re-exported playlist whose positions shifted
            out.setdefault(m["key"], {**m, "from_playlist": src.get("name", d.name)})
    return out


def carried_match(known: dict, from_playlist: str, batch: str, key: str) -> dict:
    """A cache entry for a song whose exact match was found elsewhere (or earlier in this run)."""
    m = {f: known.get(f) for f in CARRY_FIELDS}
    m.update({"verdict": "match", "batch": batch, "key": key, "carried_from": from_playlist})
    return m


def carry_unsearched(src: dict, cache: dict, todo: list[dict], batch: str) -> tuple[list[dict], int, int]:
    """Songs already decided in another playlist, or matched exactly in any playlist, do not
    need a search (and must not spend the daily search budget). Give them a cache entry and
    drop them from the to-do list. Returns (still to search, decisions carried, matches carried).
    A standing decision wins over an exact match found elsewhere."""
    g = load_global()
    known = known_matches()
    rest: list[dict] = []
    decided = matched = 0
    for it in todo:
        key = song_key(it) if has_identity(it) else None
        d = g.get(key) if key else None
        if not d:
            km = known.get(key) if key else None
            if km:
                cache[str(it["pos"])] = carried_match(km, km["from_playlist"], batch, key)
                matched += 1
            else:
                rest.append(it)
            continue
        decided += 1
        m = {
            "verdict": "carried",
            "score": 0,
            "uri": d.get("uri"),
            "title": f"{d.get('title', '')}  (decision carried from {d['from_playlist']})",
            "artist": d.get("artist", ""),
            "album": "",
            "duration_ms": None,
            "dur_diff_ms": None,
            "batch": batch,
            "key": song_key(it),
            "carried_from": d["from_playlist"],
        }
        if d["decision"] == "keep_local":
            m["keep_local"], m["approved"] = True, False
        else:
            m["approved"] = True
        cache[str(it["pos"])] = m
    return rest, decided, matched


def forget_global(it: dict) -> None:
    g = load_global()
    if g.pop(song_key(it), None) is not None:
        GLOBAL_APPROVALS.write_text(json.dumps(g, indent=1))


def apply_global(src: dict, cache: dict) -> int:
    g = load_global()
    n = 0
    for it in src["items"]:
        m = cache.get(str(it["pos"]))
        d = g.get(song_key(it)) if has_identity(it) else None
        if not m or not d or m.get("approved") is not None or m.get("keep_local") or m.get("recheck"):
            continue  # a recheck stays open until research or an explicit approve resolves it
        if m["verdict"] == "match" and d["decision"] != "keep_local":
            continue
        if d["decision"] == "keep_local":
            m["keep_local"] = True
            m["approved"] = False
        else:
            m["approved"] = True
            if d.get("uri") and d["uri"] != m.get("uri"):
                m["uri"] = d["uri"]
                m["title"] = m.get("title", "") + "  (uri carried from " + d["from_playlist"] + ")"
        m["carried_from"] = d["from_playlist"]
        n += 1
    return n


def align_cache(src: dict, cache: dict, orphans: list[dict] | None = None) -> tuple[int, int, int]:
    """matches.json is keyed by source position. Each entry also stores the song's key so that
    a re-export of a playlist that changed (insert/delete/reorder) cannot silently attach old
    verdicts and approvals to different songs.

    Returns (backfilled, rekeyed, dropped). Entries without a key (written before this check
    existed) are assumed aligned and get one. Entries whose key no longer matches the song at
    their position are moved to the position that now holds that song, or dropped (and appended
    to `orphans` if given, so paid-for searches and decisions are never lost silently)."""
    by_pos = {str(it["pos"]): it for it in src["items"]}
    backfilled = rekeyed = dropped = 0
    stale: dict[str, dict] = {}
    for pos, m in list(cache.items()):
        it = by_pos.get(pos)
        if it is None:
            stale[pos] = cache.pop(pos)
            continue
        key = song_key(it)
        if "key" not in m:
            m["key"] = key
            backfilled += 1
        elif m["key"] != key:
            stale[pos] = cache.pop(pos)
    if stale:
        wanted_keys: dict[str, list[str]] = {}
        for it in src["items"]:
            if it["is_local"] and str(it["pos"]) not in cache:
                wanted_keys.setdefault(song_key(it), []).append(str(it["pos"]))
        for pos, m in stale.items():
            targets = wanted_keys.get(m.get("key", ""), [])
            if targets:
                cache[targets.pop(0)] = m
                rekeyed += 1
            else:
                dropped += 1
                if orphans is not None:
                    orphans.append(dict(m, former_pos=pos))
                print(f"  WARNING: dropped cached decision for former #{pos} ({m.get('title')}): song no longer in source",
                      file=sys.stderr)
    return backfilled, rekeyed, dropped


def load_state() -> tuple[Path, dict, dict]:
    wd = current_work()
    src = _load_json(wd / "source.json")
    cache = _load_json(wd / "matches.json", missing=dict)
    orphans: list[dict] = []
    backfilled, rekeyed, dropped = align_cache(src, cache, orphans)
    if rekeyed or dropped:
        print(f"  source changed since matching: {rekeyed} cached decision(s) moved, {dropped} dropped", file=sys.stderr)
    if orphans:
        op = wd / "matches.orphans.json"
        old = _load_json(op, missing=list)
        op.write_text(json.dumps(old + orphans, indent=1))
        print(f"  dropped entries kept in {op}", file=sys.stderr)
    if apply_global(src, cache) or backfilled or rekeyed or dropped:
        (wd / "matches.json").write_text(json.dumps(cache, indent=1))
    return wd, src, cache


def cmd_approve(args) -> None:
    """Mark flagged tracks as approved (added on next build) / keep-local / recheck."""
    wd, src, cache = load_state()
    reasons = set(filter(None, (args.reasons or "").split(",")))
    positions = {int(x) for x in filter(None, (args.pos or "").split(","))}
    keep = {int(x) for x in filter(None, (args.keep_local or "").split(","))}
    recheck = {int(x) for x in filter(None, (args.recheck or "").split(","))}
    unkeep = {int(x) for x in filter(None, (args.unkeep or "").split(","))}
    changed = 0
    overlap = unkeep & (keep | recheck | positions)
    if overlap:
        sys.exit(f"--unkeep overlaps with another option for position(s) {sorted(overlap)}; run it as a separate command first")
    asked = keep | recheck | positions | unkeep
    known = {it["pos"] for it in src["items"] if str(it["pos"]) in cache}
    for pos in sorted(asked - known):
        print(f"  ignored #{pos}: not in the source, or not searched yet", file=sys.stderr)
    for it in src["items"]:
        m = cache.get(str(it["pos"]))
        if not m:
            continue
        if it["pos"] in unkeep:
            if not m.get("keep_local"):
                print(f"  ignored #{it['pos']}: not keep-local", file=sys.stderr)
                continue
            forget_global(it)
            changed += 1
            if m.get("verdict") == "carried":
                # never searched here; drop the entry so the next `match` searches it
                del cache[str(it["pos"])]
                print(f"  #{it['pos']} will be searched on the next match: {it['title']} — {it['artist']}")
            else:
                m.pop("keep_local", None)
                m.pop("approved", None)
                print(f"  #{it['pos']} is open again: {it['title']} — {it['artist']}")
        elif it["pos"] in keep:
            m["approved"] = False
            m["keep_local"] = True
            m.pop("recheck", None)
            record_global(it, m, "keep_local", src["name"])
            changed += 1
        elif it["pos"] in recheck:
            if m.get("keep_local"):
                print(f"  ignored #{it['pos']}: keep-local (use --unkeep first)", file=sys.stderr)
                continue
            m["recheck"] = True
            m.pop("approved", None)  # open again: not built until research or an explicit approve resolves it
            forget_global(it)
            changed += 1
            print(f"  recheck #{it['pos']}: {it['title']} — {it['artist']}  (open; use research or approve --pos)")
            twins = [o["pos"] for o in src["items"] if has_identity(it) and o["pos"] != it["pos"] and o["pos"] not in recheck
                     and o["is_local"] and song_key(o) == song_key(it) and wanted(cache.get(str(o["pos"])))
                     and cache[str(o["pos"])].get("uri") == m.get("uri")]
            if twins:
                print(f"    NOTE: the same song is also at #{','.join(map(str, twins))} with the same track; recheck those too if it is wrong",
                      file=sys.stderr)
        elif (m["verdict"] != "match" or m.get("recheck")) and (m["verdict"] in reasons or it["pos"] in positions):
            explicit = it["pos"] in positions
            if m.get("recheck") and not explicit:
                print(f"  skipped #{it['pos']}: marked recheck (name it with --pos to approve anyway)", file=sys.stderr)
            elif m.get("keep_local"):
                if explicit:
                    print(f"  ignored #{it['pos']}: already keep-local (use --unkeep to reverse)", file=sys.stderr)
            elif not m.get("uri"):
                if explicit:
                    print(f"  ignored #{it['pos']}: no candidate to approve; use research --query/--pick", file=sys.stderr)
            else:
                m["approved"] = True
                m.pop("recheck", None)
                record_global(it, m, "approve", src["name"])
                changed += 1
                print(f"  approve #{it['pos']}: {it['title']} — {it['artist']}  ->  {m['title']} — {m['artist']}")
    (wd / "matches.json").write_text(json.dumps(cache, indent=1))
    print(f"{changed} entries updated. Run 'build' to apply.")
    cmd_report(args)


def track_id(ref: str) -> str | None:
    """Accept spotify:track:<id>, open.spotify.com/.../track/<id>?si=..., or a bare 22-char id."""
    m = re.search(r"track[/:]([A-Za-z0-9]{22})(?![A-Za-z0-9])", ref or "") or re.fullmatch(r"\s*([A-Za-z0-9]{22})\s*", ref or "")
    return m.group(1) if m else None


def cmd_research(args) -> None:
    """Re-search one flagged song with a hand-written query (e.g. 'I See Fire Kygo remix')."""
    wd, src, cache = load_state()
    if not 0 <= args.pos < len(src["items"]):
        sys.exit(f"--pos must be between 0 and {len(src['items']) - 1} (the # in flagged.csv)")
    it = src["items"][args.pos]
    if not it["is_local"]:
        sys.exit(f"#{args.pos} is already a Spotify track; nothing to research.")
    m = cache.get(str(it["pos"]), {})
    print(f"#{it['pos']}: {it['title']} — {it['artist']} {fmt_dur(it['duration_ms'])}")
    if args.pick:
        tid = track_id(args.pick)
        if not tid:
            sys.exit(f"--pick needs a spotify:track:<id> uri, an open.spotify.com/track/<id> link or a bare id, got {args.pick!r}")
        t = api_get(f"/tracks/{tid}", market=MARKET)
        r = evaluate(it, t)
        r["approved"] = True
        r["batch"] = m.get("batch")
        r["key"] = song_key(it)
        cache[str(it["pos"])] = r
        record_global(it, r, "approve", src["name"])
        print(f"  picked: {r['title']} — {r['artist']} {fmt_dur(r['duration_ms'])} ({r['verdict']})")
    else:
        res = api_get("/search", q=args.query, type="track", limit=10, market=MARKET)
        cands = sorted((evaluate(it, t) for t in filter(None, res.get("tracks", {}).get("items", []))), key=lambda r: -r["score"])
        for r in cands[:8]:
            print(f"  {r['verdict']:17} {r['score']:3}  {r['title']} — {r['artist']} [{r['album']}] {fmt_dur(r['duration_ms'])}  {r['uri']}")
        if cands and (args.take or cands[0]["verdict"] == "match"):
            best = cands[0]
            best["batch"] = m.get("batch")
            best["key"] = song_key(it)
            if best["verdict"] != "match":
                best["approved"] = True
                record_global(it, best, "approve", src["name"])
            cache[str(it["pos"])] = best
            print(f"  -> stored {best['title']} ({best['verdict']}{', approved' if best.get('approved') else ''})")
        elif cands:
            print("  -> nothing stored (no exact match; use --take to accept the top result, or --pick <uri>)")
    final = cache.get(str(it["pos"]), {})
    if final.get("verdict") == "match" or final.get("approved"):
        final.pop("recheck", None)  # resolved; no longer open
    (wd / "matches.json").write_text(json.dumps(cache, indent=1))


def verify_summary(src: dict, cache: dict, live: list[str]) -> dict:
    """Pure part of `verify`. `live` is the new playlist's uri list in order (local files included).

    Done means: every source song is either a Spotify track that is present, a local file that
    has been dragged in, or one of the collapsed duplicates; and nothing undecided remains."""
    expected, dupes = desired_uris(src, cache, False)
    live_spotify = [u for u in live if u.startswith("spotify:track:")]
    live_local = {u for u in live if u.startswith("spotify:local:")}
    keep_local = [it for it in src["items"] if cache.get(str(it["pos"]), {}).get("keep_local")]
    unresolved = [
        it for it in src["items"]
        if it["is_local"] and not wanted(cache.get(str(it["pos"]))) and not cache.get(str(it["pos"]), {}).get("keep_local")
    ]
    live_set = set(live_spotify)
    missing_tracks = [u for u in expected if u not in live_set]
    extra_tracks = sorted(live_set - set(expected))
    non_track = [it for it in src["items"] if not it["is_local"] and not (it["uri"] or "").startswith("spotify:track:")]
    missing_local = [it for it in keep_local if it["uri"] not in live_local]
    extra_local = sorted(live_local - {it["uri"] for it in keep_local})  # pasted in without a keep-local decision
    return {
        "source_total": len(src["items"]),
        "searched": sum(1 for it in src["items"] if str(it["pos"]) in cache),
        "expected": len(expected),
        "dupes": dupes,
        "live_spotify": len(live_spotify),
        "live_local": len(live_local),
        "missing_tracks": missing_tracks,
        "extra_tracks": extra_tracks,
        "keep_local": keep_local,
        "missing_local": missing_local,
        "extra_local": extra_local,
        "unresolved": unresolved,
        "non_track": non_track,  # podcast episodes etc.; the API will not add them to a playlist this way
        "complete": not unresolved and not missing_tracks and not extra_tracks and not missing_local and not extra_local,
    }


def cmd_verify(args) -> None:
    """Completeness check: every source song is in the new playlist (as a Spotify track or a
    dragged-in local file) or is a collapsed duplicate, and nothing is undecided."""
    wd, src, cache = load_state()
    rp = wd / "result.json"
    if not rp.exists():
        sys.exit("No playlist built yet for this source.")
    res = _load_json(rp)
    v = verify_summary(src, cache, live_uris(res["playlist_id"]))
    print(f"Source '{src['name']}': {v['source_total']} songs ({v['searched']} searched so far)")
    print(f"New playlist {res['url']}: {v['live_spotify']} Spotify tracks + {v['live_local']} local files")
    accounted = v["expected"] + len(v["keep_local"]) + v["dupes"]
    print(f"  Decided so far: {v['expected']} Spotify + {len(v['keep_local'])} local + {v['dupes']} collapsed duplicate(s) = {accounted} of {v['source_total']}")
    if v["non_track"]:
        print(f"  Not carried over ({len(v['non_track'])} non-track item(s), e.g. podcast episodes):")
        for it in v["non_track"][:10]:
            print(f"    - #{it['pos']} {it['title']} — {it['artist']}")
    ok = not v["missing_tracks"] and not v["extra_tracks"]
    print(f"  Spotify tracks expected from decisions: {v['expected']}  {'OK' if ok else 'MISMATCH'}")
    for u in v["missing_tracks"][:20]:
        print(f"    missing from playlist: {u}")
    for u in v["extra_tracks"][:20]:
        print(f"    in playlist but not wanted: {u}")
    print(f"  Local files to drag in by hand: {len(v['keep_local'])}  (present now: {v['live_local']})")
    if len(v["missing_local"]) != len(v["keep_local"]):
        print(f"  Still missing: {len(v['missing_local'])}")
    for it in v["missing_local"][:20]:
        print(f"    - #{it['pos']} {it['title']} — {it['artist']}")
    if len(v["missing_local"]) > 20:
        print(f"    ... and {len(v['missing_local']) - 20} more")
    if v["extra_local"]:
        print(f"  Local files in the playlist without a keep-local decision ({len(v['extra_local'])}); remove them by hand, or mark the song keep-local and remove its Spotify track by hand:")
        for u in v["extra_local"][:20]:
            print(f"    - {u}")
    if v["missing_local"]:
        paste = wd / "paste_local.txt"
        paste.write_text("".join(it["uri"] + "\n" for it in v["missing_local"]))
        print(f"  Paste list: {paste}  (copy it, open the new playlist in the desktop app, click an empty spot, Cmd+V, then `build`)")
    unresolved = v["unresolved"]
    print(f"  Unresolved (not yet decided or searched): {len(unresolved)}")
    for it in unresolved[:40]:
        m = cache.get(str(it["pos"]), {})
        print(f"    - #{it['pos']} {it['title']} — {it['artist']} [{m.get('verdict', 'not_searched')}{', recheck' if m.get('recheck') else ''}]")
    if len(unresolved) > 40:
        print(f"    ... and {len(unresolved) - 40} more")
    if v["complete"]:
        print("COMPLETE: every source song is accounted for in the new playlist.")
    elif not unresolved and ok and not v["extra_local"]:
        print(f"All decided; drag the {len(v['missing_local'])} remaining local file(s) in to finish.")
    elif not unresolved and ok:
        todo = f"remove the {len(v['extra_local'])} extra local file(s) above by hand"
        if v["missing_local"]:
            todo += f" and paste the {len(v['missing_local'])} missing one(s) in"
        print(f"All decided; {todo} to finish (`build` never removes local files).")
    elif not unresolved and v["extra_tracks"] and v["live_local"]:
        # merge mode (local files present) never removes tracks, so `build` alone cannot fix this
        todo = f"remove the {len(v['extra_tracks'])} unwanted Spotify track(s) above by hand"
        if v["extra_local"]:
            todo += f" and the {len(v['extra_local'])} extra local file(s)"
        if v["missing_local"]:
            todo += f"; paste the {len(v['missing_local'])} missing local file(s) in"
        if v["missing_tracks"]:
            todo += ", then run `build`"
        print(f"All decided; {todo}.")
    elif not unresolved:
        print("All decided, but the playlist does not match the decisions; run `build`.")


def cmd_report(args) -> None:
    wd, src, cache = load_state()
    write_reports(wd, src, cache, batch=getattr(args, "batch", None))


# ----------------------------------------------------------------------------
# Build
# ----------------------------------------------------------------------------
def desired_uris(src: dict, cache: dict, keep_duplicates: bool) -> tuple[list[str], int]:
    uris: list[str] = []
    for it in src["items"]:
        if not it["is_local"]:
            if it["uri"] and it["uri"].startswith("spotify:track:"):
                uris.append(it["uri"])
            continue
        m = cache.get(str(it["pos"]))
        if wanted(m):
            uris.append(m["uri"])
    before = len(uris)
    if not keep_duplicates:
        seen: set[str] = set()
        uris = [u for u in uris if not (u in seen or seen.add(u))]
    return uris, before - len(uris)


def live_uris(playlist_id: str) -> list[str]:
    live = paged(f"{API}/playlists/{playlist_id}/items", {"limit": 50, "fields": "next,items(is_local,item(uri))"})
    return [(x.get("item") or {}).get("uri") or "" for x in live]


def full_order(src: dict, cache: dict) -> list[str]:
    """The finished playlist in source order: wanted Spotify tracks plus the local files the
    owner drags in by hand (their spotify:local: uris). Used as the anchor list when merging."""
    out: list[str] = []
    seen: set[str] = set()
    for it in src["items"]:
        m = cache.get(str(it["pos"]))
        if not it["is_local"]:
            u = it["uri"] if it["uri"] and it["uri"].startswith("spotify:track:") else None
        elif wanted(m):
            u = m["uri"]
        else:
            u = it["uri"]  # local file; present only if the owner dragged it in
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def plan_merge(order: list[str], live: list[str]) -> list[tuple[int, list[str]]]:
    """Pure part of the merge: which Spotify tracks are missing and where each run goes.

    Each missing track is inserted just before the next item in `order` that is already in the
    playlist (a Spotify track or a dragged-in local file), so the source order survives.
    Returns (position, uris) inserts to apply in sequence; positions account for earlier inserts."""
    present = set(live)
    next_present: list[str | None] = [None] * len(order)
    nxt: str | None = None
    for i in range(len(order) - 1, -1, -1):
        next_present[i] = nxt
        if order[i] in present:
            nxt = order[i]
    sim = list(live)
    plan: list[tuple[int, list[str]]] = []
    run: list[str] = []
    run_anchor: str | None = None
    for i, u in enumerate(order):
        if not u.startswith("spotify:track:") or u in present:
            continue
        anchor = next_present[i]
        if run and (anchor != run_anchor or len(run) == 100):
            pos = sim.index(run_anchor) if run_anchor is not None else len(sim)
            plan.append((pos, run))
            sim[pos:pos] = run
            run = []
        run_anchor = anchor
        run.append(u)
    if run:
        pos = sim.index(run_anchor) if run_anchor is not None else len(sim)
        plan.append((pos, run))
    return plan


def merge_playlist(playlist_id: str, order: list[str], live: list[str]) -> None:
    """Bring the playlist in line with `order` without touching local files already in it.
    Spotify tracks that are no longer wanted are reported, not removed."""
    plan = plan_merge(order, live)
    n_new = sum(len(run) for _, run in plan)
    n_local = sum(1 for u in live if u.startswith("spotify:local:"))
    print(f"  {n_local} local file(s) already in the playlist; merging {n_new} new track(s) in {len(plan)} insert(s) instead of replacing.")
    done = 0
    for pos, run in plan:
        api_post(f"/playlists/{playlist_id}/items", {"uris": run, "position": pos})
        done += len(run)
        print(f"  inserted {done}/{n_new}", end="\r", flush=True)
    wanted_set = set(order)
    extra = [u for u in live if u.startswith("spotify:track:") and u not in wanted_set]
    if extra:
        print(f"  NOTE: {len(extra)} Spotify track(s) in the playlist are no longer wanted; remove by hand:")
        for u in extra:
            print(f"    - https://open.spotify.com/track/{u.split(':')[-1]}")


def _lis_members(seq: list[int]) -> set[int]:
    """Indices into `seq` of one longest strictly increasing subsequence (patience sorting)."""
    tails: list[int] = []  # values
    tails_idx: list[int] = []  # index in seq of each tail
    prev: list[int] = [-1] * len(seq)
    for i, v in enumerate(seq):
        k = bisect.bisect_left(tails, v)
        if k == len(tails):
            tails.append(v)
            tails_idx.append(i)
        else:
            tails[k] = v
            tails_idx[k] = i
        prev[i] = tails_idx[k - 1] if k else -1
    out: set[int] = set()
    i = tails_idx[-1] if tails_idx else -1
    while i != -1:
        out.add(i)
        i = prev[i]
    return out


def plan_reorder(order: list[str], live: list[str]) -> list[tuple[int, int]]:
    """Pure part of putting the playlist back in source order after local files were pasted in
    (the app appends them at the end). Returns single-item moves (range_start, insert_before)
    for the reorder endpoint, in sequence; each is expressed against the playlist as it is after
    the earlier moves, with the endpoint's own semantics (indices are taken before the move, so a
    move forward lands at insert_before - 1).

    Minimal: items already in increasing order relative to each other (a longest increasing
    subsequence) stay put; every other item is moved to sit right after its predecessor in
    `order`. `live` may contain duplicates and empty entries; only the first copy of an item is
    positioned. Items not in `order` (strays, extra copies) are never moved, so they can stay
    interleaved; `merge_playlist` lists unwanted Spotify tracks for removal by hand."""
    first_pos: dict[str, int] = {}
    for idx, u in enumerate(live):
        first_pos.setdefault(u, idx)
    target = [u for u in order if u in first_pos]
    keep = _lis_members([first_pos[u] for u in target])
    # Non-first copies get a unique placeholder so sim.index() always finds the tracked copy.
    sim = [u if first_pos.get(u) == i else f"\0dup{i}" for i, u in enumerate(live)]
    moves: list[tuple[int, int]] = []
    for i, u in enumerate(target):
        if i in keep:
            continue
        cur = sim.index(u)
        before = sim.index(target[i - 1]) + 1 if i else 0
        if cur == before:
            continue
        moves.append((cur, before))
        sim.pop(cur)
        sim.insert(before - 1 if cur < before else before, u)
    return moves


def reorder_playlist(playlist_id: str, order: list[str], live: list[str]) -> int:
    """Apply plan_reorder with one reorder call per move, chaining snapshot ids so a playlist
    edited elsewhere mid-run makes Spotify reject the stale move instead of moving the wrong
    item. Returns the number of moves made. Do not edit the playlist while this runs."""
    moves = plan_reorder(order, live)
    snapshot: str | None = api_get(f"/playlists/{playlist_id}", fields="snapshot_id").get("snapshot_id") if moves else None
    for n, (start, before) in enumerate(moves, 1):
        body: dict = {"range_start": start, "insert_before": before, "range_length": 1}
        if snapshot:
            body["snapshot_id"] = snapshot
        snapshot = _call("PUT", f"/playlists/{playlist_id}/items", body=body).get("snapshot_id") or None
        print(f"  moved {n}/{len(moves)}", end="\r", flush=True)
    if moves:
        print(f"  moved {len(moves)} item(s) back into source order.")
    return len(moves)


def cmd_build(args) -> None:
    wd, src, cache = load_state()
    uris, dupes = desired_uris(src, cache, args.keep_duplicates)
    result_path = wd / "result.json"
    update_ref = args.update
    if update_ref is None and result_path.exists() and not args.new:
        update_ref = _load_json(result_path)["playlist_id"]

    if update_ref:
        pl = resolve_playlist(update_ref)
        live = live_uris(pl["id"])
        has_local = any(u.startswith("spotify:local:") for u in live)
        mode = "Merging into" if has_local else "Rewriting"
        print(f"{mode} '{pl['name']}' with {len(uris)} tracks ({dupes} duplicate(s) collapsed) ...")
        if has_local and args.keep_duplicates:
            print("  NOTE: --keep-duplicates is ignored when merging; the merge keeps one copy of each track.", file=sys.stderr)
        if args.dry_run:
            if has_local:
                order = full_order(src, cache)
                plan = plan_merge(order, live)
                sim = list(live)
                for pos, run in plan:
                    sim[pos:pos] = run
                moves = plan_reorder(order, sim)
                print(f"  dry run: would insert {sum(len(r) for _, r in plan)} track(s) in {len(plan)} call(s), then move {len(moves)} item(s)")
            else:
                print(f"  dry run: would replace the {len(live)} item(s) now in the playlist with {len(uris)} track(s)")
            return
        if has_local:
            # Local files have been dragged in by hand; a full replace would drop them
            # (the API cannot add local files). Merge instead, inserting new tracks in order.
            order = full_order(src, cache)
            merge_playlist(pl["id"], order, live)
            # Pasted/dragged local files land at the end; move everything back into source order.
            reorder_playlist(pl["id"], order, live_uris(pl["id"]))
        else:
            _call("PUT", f"/playlists/{pl['id']}/items", body={"uris": uris[:100]})
            for i in range(100, len(uris), 100):
                api_post(f"/playlists/{pl['id']}/items", {"uris": uris[i : i + 100]})
                print(f"  added {min(i + 100, len(uris))}/{len(uris)}", end="\r", flush=True)
    else:
        name = args.name or f"{src['name']} (Spotify version)"
        print(f"Creating playlist '{name}' with {len(uris)} tracks ({dupes} duplicate(s) collapsed) ...")
        if args.dry_run:
            return
        pl = api_post(
            "/me/playlists",
            {
                "name": name,
                "public": False,
                "description": f"Rebuilt from the local-files playlist '{src['name']}'.",
            },
        )
        # Remember the new playlist immediately so a failure while adding tracks does not
        # make the next `build` create a second one.
        result_path.write_text(json.dumps({"playlist_id": pl["id"], "url": pl["external_urls"]["spotify"], "count": 0}, indent=1))
        try:  # Spotify has been ignoring public:false on create; ask again explicitly.
            _call("PUT", f"/playlists/{pl['id']}", body={"public": False})
        except RuntimeError as e:
            print(f"  WARNING: could not make the playlist private ({e}); check it in the app.", file=sys.stderr)
        for i in range(0, len(uris), 100):
            api_post(f"/playlists/{pl['id']}/items", {"uris": uris[i : i + 100]})
            print(f"  added {min(i + 100, len(uris))}/{len(uris)}", end="\r", flush=True)
    print()
    check = api_get(f"/playlists/{pl['id']}", fields="items.total,tracks.total")
    result = {
        "playlist_id": pl["id"],
        "url": pl["external_urls"]["spotify"],
        "count": len(uris),
        "on_spotify": _total(check),
        "duplicates_collapsed": dupes,
    }
    result_path.write_text(json.dumps(result, indent=1))
    print(f"Done: {pl['external_urls']['spotify']}  ({result['on_spotify']} tracks on Spotify)")


def cmd_all(args) -> None:
    global WORK_OVERRIDE
    cmd_export(args)
    WORK_OVERRIDE = None  # operate on what was just exported, whatever -w said
    cmd_match(args)
    cmd_build(args)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-w", "--work", help="which exported playlist to operate on (its name); default: the last one exported")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth").set_defaults(fn=cmd_auth)
    f = sub.add_parser("find", help="list your playlists whose name contains <name>")
    f.add_argument("name")
    f.set_defaults(fn=cmd_find)
    e = sub.add_parser("export", help="dump the source playlist (local files included) to work/<slug>/source.json")
    e.add_argument("playlist", help='playlist URL/URI/id, or "name:<playlist name>"')
    e.set_defaults(fn=cmd_export)
    mt = sub.add_parser("match", help="search Spotify for every local file not yet searched")
    mt.add_argument("--limit", type=int, help="search at most this many new songs (daily budget is roughly 400-480 songs)")
    mt.set_defaults(fn=cmd_match)

    ap = sub.add_parser("approve", help="approve flagged tracks for adding, or mark them keep-local")
    ap.add_argument("--reasons", help="comma list: duration_mismatch,title_variant,artist_mismatch,no_match,no_duration")
    ap.add_argument("--pos", help="comma list of source positions (from flagged.csv)")
    ap.add_argument("--keep-local", help="comma list of positions to never add")
    ap.add_argument("--recheck", help="comma list of positions to reopen for a re-search by hand (not built until resolved)")
    ap.add_argument("--unkeep", help="comma list of keep-local positions to reopen (also forgets the standing decision)")
    ap.set_defaults(fn=cmd_approve)
    rs = sub.add_parser("research", help="re-search one song with a custom query, or pick a specific track uri")
    rs.add_argument("--pos", type=int, required=True, help="source position (the # in flagged.csv)")
    how = rs.add_mutually_exclusive_group(required=True)
    how.add_argument("--query", help="hand-written search, e.g. 'I See Fire Kygo remix'")
    how.add_argument("--pick", help="spotify:track:<id> or track link to accept for this song")
    rs.add_argument("--take", action="store_true", help="accept the top result even if not an exact match")
    rs.set_defaults(fn=cmd_research)
    sub.add_parser("verify", help="check the new playlist accounts for every source song").set_defaults(fn=cmd_verify)
    rp = sub.add_parser("report", help="re-print the breakdown and rewrite flagged.csv")
    rp.add_argument("--batch", help="also write flagged_<batch>.csv for this batch date (YYYY-MM-DD)")
    rp.set_defaults(fn=cmd_report)

    def build_opts(sp):
        sp.add_argument("--name", help="name for the new playlist")
        sp.add_argument("--update", help="rewrite this existing playlist (URL/id/name:...) instead of creating one")
        sp.add_argument("--new", action="store_true", help="force creating a new playlist even if result.json exists")
        sp.add_argument("--keep-duplicates", action="store_true")
        sp.add_argument("--dry-run", action="store_true")

    b = sub.add_parser("build", help="create the new playlist, or bring the existing one up to date")
    build_opts(b)
    b.set_defaults(fn=cmd_build)
    a = sub.add_parser("all", help="export + match + build in one go")
    a.add_argument("playlist")
    a.add_argument("--limit", type=int, help="search at most this many songs before building")
    build_opts(a)
    a.set_defaults(fn=cmd_all)
    args = p.parse_args()
    global WORK_OVERRIDE
    WORK_OVERRIDE = args.work
    try:
        args.fn(args)
    except RateLimited as e:
        sys.exit(f"{e}. Progress so far is saved; try again later.")
    except ApiError as e:
        sys.exit(f"Spotify call failed: {e}\nProgress so far is saved; rerun the same command, it only does what is still missing.")


if __name__ == "__main__":
    main()
