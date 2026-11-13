import json
import unicodedata
from datetime import datetime, timezone
from curl_cffi import requests
from rapidfuzz import process, fuzz

session = requests.Session(impersonate="chrome")

SOFASCORE_BASE = "https://www.sofascore.com/api/v1"
TOURNAMENT_ID = 52  # Trendyol Süper Lig
FANTASY_API_URL = "https://api.tfffantezilig.com/v1/bootstrap-static"


def normalize_name(text: str) -> str:
    if not text:
        return ""
    tr_map = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    text = text.translate(tr_map)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("utf-8")
    return text.lower().replace(".", "").strip()


def get_active_season_id() -> tuple[int, str]:
    res = session.get(f"{SOFASCORE_BASE}/unique-tournament/{TOURNAMENT_ID}/seasons").json()
    current = res["seasons"][0]
    return current["id"], current["name"]


def fetch_team_lambdas(season_id: int) -> dict:
    url = f"{SOFASCORE_BASE}/unique-tournament/{TOURNAMENT_ID}/season/{season_id}/standings/total"
    res = session.get(url).json()
    rows = res["standings"][0]["rows"]
    teams = {}
    for r in rows:
        matches = max(1, r.get("matches", 1))
        name = r["team"]["name"]
        teams[name] = {
            "id": r["team"]["id"],
            "short": r["team"].get("nameCode") or name[:3].upper(),
            "gf": round(r.get("scoresFor", 0) / matches, 2),
            "ga": round(r.get("scoresAgainst", 0) / matches, 2),
        }
    return teams


def fetch_next_fixtures(season_id: int, teams: dict) -> dict:
    team_id_to_name = {v["id"]: k for k, v in teams.items()}
    fixtures_map = {}
    try:
        rounds_url = f"{SOFASCORE_BASE}/unique-tournament/{TOURNAMENT_ID}/season/{season_id}/rounds"
        rounds_data = session.get(rounds_url).json()
        current_round = rounds_data.get("currentRound", {}).get("round", 1)

        events_url = f"{SOFASCORE_BASE}/unique-tournament/{TOURNAMENT_ID}/season/{season_id}/events/round/{current_round}"
        events = session.get(events_url).json().get("events", [])

        for ev in events:
            home_name = team_id_to_name.get(ev["homeTeam"]["id"], ev["homeTeam"]["name"])
            away_name = team_id_to_name.get(ev["awayTeam"]["id"], ev["awayTeam"]["name"])
            home_gf = teams.get(home_name, {}).get("gf", 1.35)
            away_gf = teams.get(away_name, {}).get("gf", 1.15)

            fixtures_map[home_name] = {
                "oppInfo": f"vs {teams.get(away_name, {}).get('short', away_name[:3])} (E)",
                "oppLambda": round(away_gf * 0.90, 2),
                "matchupMult": 1.15 if teams.get(away_name, {}).get("ga", 1.2) >= 1.4 else 1.0,
            }
            fixtures_map[away_name] = {
                "oppInfo": f"@ {teams.get(home_name, {}).get('short', home_name[:3])} (D)",
                "oppLambda": round(home_gf * 1.12, 2),
                "matchupMult": 1.12 if teams.get(home_name, {}).get("ga", 1.2) >= 1.4 else 0.95,
            }
    except Exception as e:
        print(f"Fikstür uyarısı: {e}")
    return fixtures_map


def fetch_real_pitch_stats(season_id: int) -> list:
    fields = (
        "goals,assists,expectedGoals,expectedAssists,keyPasses,"
        "accurateCrosses,saves,minutesPlayed,appearances,rating"
    )
    all_results = []
    for offset in range(0, 400, 100):
        url = (
            f"{SOFASCORE_BASE}/unique-tournament/{TOURNAMENT_ID}/season/{season_id}/statistics"
            f"?limit=100&offset={offset}&order=-minutesPlayed&accumulation=total&fields={fields}"
        )
        data = session.get(url).json()
        batch = data.get("results", [])
        if not batch:
            break
        all_results.extend(batch)
    return all_results


def fetch_fantasy_official_stats() -> dict:
    fantasy_db = {}
    try:
        res = session.get(FANTASY_API_URL, timeout=10)
        if res.status_code == 200:
            data = res.json()
            for p in data.get("elements", data.get("players", [])):
                norm_key = normalize_name(p.get("web_name") or p.get("name", ""))
                fantasy_db[norm_key] = {
                    "price": float(p.get("now_cost", p.get("price", 55))) / (10.0 if p.get("now_cost", 0) > 25 else 1.0),
                    "ownership": float(p.get("selected_by_percent", p.get("ownership", 5.0))),
                    "fantasy_pos": p.get("position_short", None),
                    "last3_pts": float(p.get("form", p.get("last3Pts", 3.0))),
                    "status": p.get("status", "a"),
                }
    except Exception as e:
        print(f"Bilgi: Fantezi API doğrudan okunamadı ({e}), dinamik fiyat/puan motoru kullanılıyor.")
    return fantasy_db


def merge_and_calculate():
    season_id, season_name = get_active_season_id()
    print(f"[{datetime.now().isoformat()}] Aktif Sezon: {season_name} (ID: {season_id}) çekiliyor...")

    teams = fetch_team_lambdas(season_id)
    fixtures = fetch_next_fixtures(season_id, teams)
    pitch_players = fetch_real_pitch_stats(season_id)
    fantasy_players = fetch_fantasy_official_stats()
    fantasy_keys = list(fantasy_players.keys())

    pos_map = {"G": "GK", "D": "DEF", "M": "MID", "F": "FWD"}
    merged_list = []

    for idx, item in enumerate(pitch_players):
        p_info = item["player"]
        t_name = item["team"]["name"]
        mins = item.get("minutesPlayed", 0)
        apps = max(1, item.get("appearances", 1))
        if mins < 45:
            continue

        per90 = mins / 90.0
        real_pos = pos_map.get(p_info.get("position", "M"), "MID")
        xg90 = round((item.get("expectedGoals") or 0.0) / per90, 2)
        xa90 = round((item.get("expectedAssists") or 0.0) / per90, 2)
        goals = item.get("goals") or 0
        assists = item.get("assists") or 0
        rating = round(float(item.get("rating") or 6.75), 2)

        norm_p_name = normalize_name(p_info["name"])
        matched_fantasy = None
        if fantasy_keys:
            match_res = process.extractOne(norm_p_name, fantasy_keys, scorer=fuzz.token_sort_ratio)
            if match_res and match_res[1] >= 78:
                matched_fantasy = fantasy_players[match_res[0]]

        if matched_fantasy:
            if matched_fantasy["status"] in ("i", "s", "u"):
                continue
            price = matched_fantasy["price"]
            ownership = matched_fantasy["ownership"]
            fantasy_pos = matched_fantasy["fantasy_pos"] or real_pos
            last3_pts = matched_fantasy["last3_pts"]
        else:
            fantasy_pos = real_pos
            price = round(min(13.5, max(4.5, 4.5 + (rating - 6.5) * 4.2 + (goals + assists) * 0.22)), 1)
            ownership = round(min(48.0, max(1.1, (goals + assists) * 2.5 + (rating - 6.65) * 14)), 1)
            last3_pts = round(2.0 + ((goals * 4.5 + assists * 3.0) / apps), 1)

        is_oop = (fantasy_pos == "DEF" and (xg90 + xa90) >= 0.28) or (fantasy_pos == "MID" and xg90 >= 0.38)
        tactical_role = (
            "Hücumcu Kanat Beki (OOP)" if (fantasy_pos == "DEF" and is_oop)
            else ("Gizli Forvet / 10# (OOP)" if (fantasy_pos == "MID" and is_oop) else fantasy_pos)
        )

        fix = fixtures.get(t_name, {"oppInfo": "Fikstür Bekleniyor", "oppLambda": 1.25, "matchupMult": 1.05})

        merged_list.append({
            "id": p_info.get("id", idx + 1),
            "name": p_info["name"],
            "team": teams.get(t_name, {}).get("short", t_name[:3].upper()),
            "teamFull": t_name,
            "pos": fantasy_pos,
            "tacticalRole": tactical_role,
            "isOOP": is_oop,
            "price": price,
            "ownership": ownership,
            "minsProb": round(min(0.98, mins / (apps * 90.0)), 2),
            "last3Pts": last3_pts,
            "xg90": xg90,
            "xa90": xa90,
            "actualGI90": round((goals + assists) / per90, 2),
            "shotsInBox90": round(xg90 * 5.4, 1),
            "keyPasses90": round((item.get("keyPasses") or 0) / per90, 2),
            "saves90": round((item.get("saves") or 0) / per90, 1) if fantasy_pos == "GK" else 0,
            "setPieceShare": round(min(0.85, ((item.get("accurateCrosses") or 0) / per90) * 0.22), 2),
            "matchupMult": fix["matchupMult"],
            "oppLambda": fix["oppLambda"],
            "bonusExp": round(max(0.15, (rating - 6.75) * 2.4), 2),
            "oppInfo": fix["oppInfo"],
            "scoutNote": (
                f"{season_name} güncel verisi: {mins} dk, 90 dk başına {xg90} xG + {xa90} xA. "
                f"Reyting: {rating}."
            ),
        })

    payload = {
        "lastUpdated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "season": season_name,
        "playerCount": len(merged_list),
        "players": merged_list,
    }

    with open("live_data.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"Başarılı! {len(merged_list)} oyuncu birleştirildi ve live_data.json güncellendi.")


if __name__ == "__main__":
    merge_and_calculate()
