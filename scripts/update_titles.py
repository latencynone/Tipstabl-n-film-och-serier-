#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Uppdaterar titles.json med nya filmer/serier som:
  - finns på Netflix, Apple TV+, HBO Max, Prime Video eller Disney+ i Sverige
  - hade premiär de senaste fem åren
  - har IMDb-betyg 7.0 eller högre

Körs automatiskt en gång i veckan av .github/workflows/update-titles.yml,
men kan också köras manuellt lokalt eller via "Run workflow" på GitHub.

Datakällor (båda gratis):
  - TMDb (themoviedb.org)  -> vad som finns var, releasedatum, genre, handling
  - OMDb (omdbapi.com)     -> IMDb/Rotten Tomatoes/Metacritic-betyg + IMDb-ID

Begränsning värd att känna till: OMDb ger betyg men inga direkta sid-adresser
till Rotten Tomatoes/Metacritic. Nya titlar som hittas automatiskt får därför
en sökningslänk dit (samma säkra fallback appen redan använder för IMDb när
ID saknas), inte en verifierad direktlänk som de 86 ursprungliga titlarna har.
IMDb-länken blir alltid exakt, eftersom OMDb ger det riktiga IMDb-ID:t.
"""

import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

TMDB_KEY = os.environ.get("TMDB_API_KEY", "")
OMDB_KEY = os.environ.get("OMDB_API_KEY", "")
REGION = "SE"
MIN_IMDB = 7.0
MAX_AGE_YEARS = 5
DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "titles.json")

# Namnen måste matcha hur tjänsterna heter i TMDb:s providerlista.
WANTED_SERVICES = {
    "Netflix": "Netflix",
    "Apple TV+": "Apple TV+",
    "HBO Max": "HBO Max",
    "Prime Video": "Amazon Prime Video",
    "Disney+": "Disney Plus",
}

TMDB_GENRE_BUCKET = {
    # Film
    "Action": "Action & Äventyr", "Adventure": "Action & Äventyr",
    "Animation": "Animerat", "Comedy": "Komedi", "Crime": "Kriminal & Mysterium",
    "Documentary": "Biografi & Sport", "Drama": "Drama", "Family": "Drama",
    "Fantasy": "Sci-fi & Fantasy", "History": "Drama", "Horror": "Skräck",
    "Music": "Musik & Musikal", "Mystery": "Kriminal & Mysterium",
    "Romance": "Komedi", "Science Fiction": "Sci-fi & Fantasy",
    "TV Movie": "Drama", "Thriller": "Thriller", "War": "Drama", "Western": "Action & Äventyr",
    # TV-specifika
    "Action & Adventure": "Action & Äventyr", "Kids": "Drama", "News": "Drama",
    "Reality": "Biografi & Sport", "Sci-Fi & Fantasy": "Sci-fi & Fantasy",
    "Soap": "Drama", "Talk": "Drama", "War & Politics": "Drama",
}


def http_get_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "FILMoSERIER-uppdatering/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            print("  HTTP-fel %s för %s" % (e.code, url), file=sys.stderr)
            return None
        except Exception as e:
            print("  Fel vid hämtning: %s" % e, file=sys.stderr)
            if attempt < retries - 1:
                time.sleep(1)
                continue
            return None
    return None


def tmdb_get(path, params):
    params = dict(params)
    params["api_key"] = TMDB_KEY
    url = "https://api.themoviedb.org/3" + path + "?" + urllib.parse.urlencode(params)
    return http_get_json(url)


_GENRE_NAME_CACHE = {}


def get_genre_names(media_type):
    """Hämtar TMDb:s genre-ID->namn en gång per körning (cachat), så vi kan
    slå upp de genre_ids som discover-svaren ger."""
    if media_type in _GENRE_NAME_CACHE:
        return _GENRE_NAME_CACHE[media_type]
    data = tmdb_get("/genre/" + media_type + "/list", {"language": "en-US"})
    names = {g["id"]: g["name"] for g in (data or {}).get("genres", [])}
    _GENRE_NAME_CACHE[media_type] = names
    return names


def get_provider_ids(media_type):
    """Slår upp leverantörs-ID:n dynamiskt via namn, istället för att lita på
    hårdkodade siffror som kan ändras."""
    data = tmdb_get("/watch/providers/" + media_type, {"watch_region": REGION})
    if not data:
        return {}
    by_name = {p["provider_name"]: p["provider_id"] for p in data.get("results", [])}
    ids = {}
    for our_name, tmdb_name in WANTED_SERVICES.items():
        if tmdb_name in by_name:
            ids[our_name] = by_name[tmdb_name]
        else:
            print("  Varning: hittade inte '%s' (%s) i TMDb:s providerlista" % (our_name, tmdb_name), file=sys.stderr)
    return ids


def normalize_length(runtime_min, media_type, seasons=None):
    if media_type == "movie":
        h, m = divmod(runtime_min or 0, 60)
        return ("%d tim %02d min" % (h, m)) if h else ("%d min" % m)
    if seasons and seasons > 1:
        return "Säsong %d" % seasons
    return "1 säsong"


def slugify_fallback(title):
    """Enkel, säker fallback-sökning (ingen gissad sid-ID) om vi inte har
    ett verifierat RT/MC-ID."""
    return ""


def discover_candidates(media_type, provider_id):
    since = (date.today() - timedelta(days=365 * MAX_AGE_YEARS)).isoformat()
    params_base = {
        "watch_region": REGION,
        "with_watch_providers": provider_id,
        "with_watch_monetization_types": "flatrate",
        "sort_by": "popularity.desc",
        "language": "sv-SE",
    }
    if media_type == "movie":
        # En films releasedatum är entydigt - filtrera direkt i sökningen.
        params_base["primary_release_date.gte"] = since
    # För TV filtreras INTE på discover-sökningens first_air_date, eftersom
    # det bara är säsong 1:s premiärdatum. En långkörare som fortfarande
    # sänder nya säsonger (t.ex. The Boys, premiär 2019, sista säsongen 2026)
    # skulle annars felaktigt sorteras bort som "för gammal". Recency
    # kollas istället per kandidat mot seriens FAKTISKA senaste säsong,
    # se get_tv_last_air_date().
    results = []
    for page in (1, 2):
        params = dict(params_base)
        params["page"] = page
        data = tmdb_get("/discover/" + media_type, params)
        if not data:
            break
        results.extend(data.get("results", []))
        if page >= data.get("total_pages", 1):
            break
    return results


_TV_LAST_AIR_CACHE = {}


def get_tv_last_air_date(tv_id):
    """Hämtar seriens FAKTISKA senaste sändningsdatum (senaste säsongen),
    till skillnad från discover-sökningens first_air_date som bara är
    säsong 1:s premiär. Cachas per körning så samma serie (kan dyka upp
    via flera tjänster) bara slås upp en gång."""
    if tv_id in _TV_LAST_AIR_CACHE:
        return _TV_LAST_AIR_CACHE[tv_id]
    data = tmdb_get("/tv/" + str(tv_id), {})
    result = (data or {}).get("last_air_date") or (data or {}).get("first_air_date")
    _TV_LAST_AIR_CACHE[tv_id] = result
    return result


def omdb_lookup(title, year):
    url = "https://www.omdbapi.com/?apikey=%s&t=%s&y=%s" % (
        OMDB_KEY, urllib.parse.quote(title), year or "")
    data = http_get_json(url)
    if not data or data.get("Response") == "False":
        return None
    ratings = {r["Source"]: r["Value"] for r in data.get("Ratings", [])}
    try:
        imdb = float(data.get("imdbRating", "N/A"))
    except ValueError:
        return None
    rt = 0
    if "Rotten Tomatoes" in ratings:
        m = re.search(r"(\d+)%", ratings["Rotten Tomatoes"])
        if m:
            rt = int(m.group(1))
    mc = 0
    if "Metacritic" in ratings:
        m = re.search(r"(\d+)", ratings["Metacritic"])
        if m:
            mc = int(m.group(1))
    runtime_min = 0
    m = re.search(r"(\d+)", data.get("Runtime", "") or "")
    if m:
        runtime_min = int(m.group(1))
    seasons = None
    if data.get("totalSeasons") and data["totalSeasons"] not in ("N/A", None):
        try:
            seasons = int(data["totalSeasons"])
        except ValueError:
            pass
    return {
        "imdb": imdb, "rt": rt, "mc": mc, "imdbID": data.get("imdbID", ""),
        "runtime": runtime_min, "seasons": seasons,
    }


def build_entry(item, media_type, service_name):
    title = item.get("title") or item.get("name")
    date_str = item.get("release_date") or item.get("first_air_date")
    if not title or not date_str:
        return None
    omdb_year = date_str[:4]  # OMDb indexerar TV-serier på ursprungsåret

    if media_type == "tv":
        # Kolla mot seriens FAKTISKA senaste sändningsdatum - inte bara
        # säsong 1:s premiär (se kommentar i discover_candidates ovan).
        last_air = get_tv_last_air_date(item.get("id"))
        if not last_air:
            return None
        cutoff = (date.today() - timedelta(days=365 * MAX_AGE_YEARS)).isoformat()
        if last_air < cutoff:
            return None
        date_str = last_air  # visas/sorteras på senaste säsongen, inte premiären

    omdb = omdb_lookup(title, omdb_year)
    time.sleep(0.15)  # skonsam mot OMDb:s gratisgräns
    if not omdb or omdb["imdb"] < MIN_IMDB:
        return None

    genre_names = get_genre_names(media_type)
    genre_ids = item.get("genre_ids", [])
    first_genre = genre_names.get(genre_ids[0]) if genre_ids else None
    genre = first_genre if first_genre in TMDB_GENRE_BUCKET or first_genre else "Drama"
    if genre not in TMDB_GENRE_BUCKET:
        genre = "Drama"  # okänd/oöversatt genre -> rimlig standard

    kind = "film" if media_type == "movie" else "serie"
    return {
        "title": title,
        "date": date_str,
        "service": service_name,
        "imdb": omdb["imdb"],
        "rt": omdb["rt"],
        "mc": omdb["mc"],
        "genre": genre,
        "length": normalize_length(omdb["runtime"], media_type, omdb["seasons"]),
        "desc": (item.get("overview") or "")[:140] or "Ingen beskrivning tillgänglig.",
        "id": omdb["imdbID"],
        "rtId": "",
        "mcId": "",
        "kind": kind,
    }


def entry_score(x):
    """Grov kvalitetspoäng - används för att avgöra om en nyfunnen post är
    bättre än en redan sparad, så uppenbara luckor kan självläka över tid."""
    s = 0
    if x.get("rtId"): s += 2
    if x.get("mcId"): s += 2
    if x.get("length"): s += 1
    if len(x.get("desc", "")) > 20: s += 1
    return s


def main():
    if not TMDB_KEY or not OMDB_KEY:
        print("TMDB_API_KEY eller OMDB_API_KEY saknas som miljövariabel/secret. Avbryter.", file=sys.stderr)
        sys.exit(1)

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        current = json.load(f)

    by_id = {}
    existing_keys = set()
    for kind in ("film", "serie"):
        for it in current.get(kind, []):
            if it.get("id"):
                by_id[it["id"]] = it
            existing_keys.add(kind + ":" + it["title"] + ":" + it["date"])

    added = {"film": [], "serie": []}
    upgraded = 0

    for media_type in ("movie", "tv"):
        print("== %s ==" % media_type)
        provider_ids = get_provider_ids(media_type)
        for service_name, provider_id in provider_ids.items():
            print(" Söker på %s..." % service_name)
            candidates = discover_candidates(media_type, provider_id)
            for item in candidates:
                entry = build_entry(item, media_type, service_name)
                if not entry:
                    continue

                # IMDb-ID är den pålitliga nyckeln - releasedatum kan skilja
                # sig med några dagar mellan TMDb och det datum en titel
                # faktiskt dök upp på tjänsten, vilket annars gett dubbletter.
                if entry["id"] and entry["id"] in by_id:
                    old = by_id[entry["id"]]
                    if entry_score(entry) > entry_score(old):
                        old["imdb"], old["rt"], old["mc"] = entry["imdb"], entry["rt"], entry["mc"]
                        if entry["length"]:
                            old["length"] = entry["length"]
                        if len(entry["desc"]) > len(old.get("desc", "")):
                            old["desc"] = entry["desc"]
                        upgraded += 1
                        print("  ~ uppdaterade %s med bättre data" % entry["title"])
                    continue

                key = entry["kind"] + ":" + entry["title"] + ":" + entry["date"]
                if key in existing_keys:
                    continue
                if entry["id"]:
                    by_id[entry["id"]] = entry
                existing_keys.add(key)
                added[entry["kind"]].append(entry)
                print("  + %s (%s) IMDb %.1f" % (entry["title"], entry["date"][:4], entry["imdb"]))

    if not added["film"] and not added["serie"] and not upgraded:
        print("Inga nya titlar eller uppdateringar den här veckan.")
        return

    current["film"] = current.get("film", []) + added["film"]
    current["serie"] = current.get("serie", []) + added["serie"]
    current["updated"] = date.today().isoformat()

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, separators=(",", ":"))

    print("Klart: +%d filmer, +%d serier, %d poster uppgraderade." % (
        len(added["film"]), len(added["serie"]), upgraded))


if __name__ == "__main__":
    main()
