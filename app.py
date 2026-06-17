import io
import os
import time
import requests
import uvicorn
import pandas as pd
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from playwright.sync_api import sync_playwright

app = FastAPI(
    title="Sports Stats Scraper API",
    description="KBO(종합 전력 및 일정 통합), NPB, Soccer, Basketball 스크래핑 API",
    version="3.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def handle_error(e: Exception, sport_name: str):
    print(f"[{sport_name} API Error] {str(e)}")
    return JSONResponse(status_code=500, content={"status": "error", "message": str(e), "data": []})


# ──────────────────────────────────────────────────────────────────────────
# API-Football (Pro) — 2026 월드컵 일정/세부지표의 1순위 소스.
# 키는 환경변수 API_FOOTBALL_KEY 로만 주입(클라이언트 노출 금지). 미설정/오류 시 FIFA 폴백.
# ──────────────────────────────────────────────────────────────────────────
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "56b655b93ba83e349f606c0e8d70d72a")
API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
WORLDCUP_AF = {"league": 1, "season": 2026}  # league 1 = FIFA World Cup

# api-sports 야구 API(NPB 일정 프록시). 동일 계정 키를 football/baseball 이 공유하므로
# API_SPORTS_KEY 우선, 없으면 기존 API_FOOTBALL_KEY 재사용(키 회전 시 env 1개만 갱신).
API_SPORTS_KEY = os.getenv("API_SPORTS_KEY") or API_FOOTBALL_KEY
API_BASEBALL_BASE = "https://v1.baseball.api-sports.io"

# 아주 단순한 인메모리 TTL 캐시(인스턴스 단위). 비싼 집계/라이브 호출 재요청 방지.
_AF_CACHE: dict[str, tuple[float, object]] = {}


def _cache_get(key: str, ttl: float):
    hit = _AF_CACHE.get(key)
    if hit and (time.time() - hit[0] < ttl):
        return hit[1]
    return None


def _cache_set(key: str, value):
    _AF_CACHE[key] = (time.time(), value)
    return value


def af_get(path: str, params: dict | None = None, timeout: int = 20) -> dict:
    """API-Football GET. 키 미설정/HTTP 오류/응답 errors 가 있으면 예외를 던져 폴백을 유도한다."""
    if not API_FOOTBALL_KEY:
        raise RuntimeError("API_FOOTBALL_KEY 미설정")
    resp = requests.get(
        f"{API_FOOTBALL_BASE}{path}",
        params=params or {},
        headers={"x-apisports-key": API_FOOTBALL_KEY, "Accept": "application/json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    errors = payload.get("errors")
    # API-Football 은 errors 를 {} 또는 [] 로 주며, 비어있지 않으면 실패(쿼터/파라미터 등)
    if errors and (isinstance(errors, dict) and errors or isinstance(errors, list) and errors):
        raise RuntimeError(f"API-Football errors: {errors}")
    return payload


def _af_stat_value(stat_items: list, *names):
    """fixtures/statistics 의 [{type,value}] 목록에서 주어진 type 이름들 중 첫 매칭 값을 반환."""
    wanted = {n.lower() for n in names}
    for item in stat_items or []:
        if str(item.get("type", "")).lower() in wanted:
            return item.get("value")
    return None


def _pct_to_number(val):
    """'61%' → 61.0, 숫자면 그대로. None/'' → None."""
    if val is None or val == "":
        return None
    if isinstance(val, str) and val.endswith("%"):
        try:
            return float(val.rstrip("%"))
        except ValueError:
            return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


@app.get("/api/kbo/all")
def get_kbo_all_stats():
    """🌟 해결책: 단 1개의 브라우저만 켜서 모든 KBO 데이터를 백엔드에서 통합 병합 (메모리 초절약)"""
    urls = {
        "hitter1": "https://www.koreabaseball.com/Record/Team/Hitter/Basic1.aspx",
        "hitter2": "https://www.koreabaseball.com/Record/Team/Hitter/Basic2.aspx",
        "pitcher1": "https://www.koreabaseball.com/Record/Team/Pitcher/Basic1.aspx",
        "runner": "https://www.koreabaseball.com/Record/Team/Runner/Basic.aspx",
        "defense": "https://www.koreabaseball.com/Record/Team/Defense/Basic.aspx"
    }

    html_contents = {}

    try:
        with sync_playwright() as p:
            # 브라우저를 단 한 번만 실행하여 512MB RAM 한계를 절대 넘지 않도록 방어
            browser = p.chromium.launch(headless=True,
                                        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0")
            page = context.new_page()

            for key, url in urls.items():
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                # '팀명'이 포함된 진짜 테이블 내용이 뜰 때까지 안전하게 대기
                page.wait_for_selector("table:has-text('팀명') tbody tr td", timeout=20000)
                html_contents[key] = page.content()

            browser.close()
    except Exception as e:
        return handle_error(e, "KBO-All-Playwright")

    try:
        # 1. 투수 기록 베이스 (승, 패, ERA, WHIP, 탈삼진, 볼넷 등)
        df_p1 = pd.read_html(io.StringIO(html_contents["pitcher1"]), match="팀명")[0].fillna(0)
        df_p1 = df_p1.rename(columns={
            "팀명": "team", "W": "wins", "L": "losses", "ERA": "era",
            "WHIP": "whip", "SO": "strikeouts_pitched", "BB": "walks_allowed", "WPCT": "win_rate"
        })[["team", "wins", "losses", "era", "whip", "strikeouts_pitched", "walks_allowed", "win_rate"]]

        # 2. 타자1 (홈런, 득점)
        df_h1 = pd.read_html(io.StringIO(html_contents["hitter1"]),
                             match="team" if "team" in html_contents["hitter1"] else "팀명")[0].fillna(0)
        df_h1 = df_h1.rename(columns={"팀명": "team", "HR": "home_runs", "R": "runs"})[["team", "home_runs", "runs"]]

        # 3. 타자2 (OPS)
        df_h2 = pd.read_html(io.StringIO(html_contents["hitter2"]), match="팀명")[0].fillna(0)
        df_h2 = df_h2.rename(columns={"팀명": "team", "OPS": "ops"})[["team", "ops"]]

        # 4. 주루 (도루)
        df_r = pd.read_html(io.StringIO(html_contents["runner"]), match="팀명")[0].fillna(0)
        df_r = df_r.rename(columns={"팀명": "team", "SB": "stolen_bases"})[["team", "stolen_bases"]]

        # 5. 수비 (실책)
        df_d = pd.read_html(io.StringIO(html_contents["defense"]), match="팀명")[0].fillna(0)
        df_d = df_d.rename(columns={"팀명": "team", "E": "errors"})[["team", "errors"]]

        # 백엔드 메모리 상에서 안전하게 가로 병합(Merge)
        merged = pd.merge(df_p1, df_h1, on="team", how="inner")
        merged = pd.merge(merged, df_h2, on="team", how="inner")
        merged = pd.merge(merged, df_r, on="team", how="inner")
        merged = pd.merge(merged, df_d, on="team", how="inner")

        return {"status": "success", "data": merged.to_dict(orient="records")}
    except Exception as e:
        return handle_error(e, "KBO-All-Parsing")


@app.get("/api/kbo/schedule")
def get_kbo_schedule():
    """KBO 일정 크롤링 (독립된 단일 브라우저 구동으로 메모리 부담 최소화)"""
    url = "https://www.koreabaseball.com/Schedule/Schedule.aspx"
    try:
        html_content = ""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True,
                                        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0")
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_selector("table:has-text('구장') tbody tr td", timeout=20000)
                html_content = page.content()
            finally:
                browser.close()

        tables = pd.read_html(io.StringIO(html_content), match="구장")
        if not tables:
            raise ValueError("일정 테이블을 찾을 수 없습니다.")

        df = tables[0]
        if "날짜" in df.columns:
            df["날짜"] = df["날짜"].ffill()

        df = df.fillna("")
        col_mapping = {"날짜": "date", "시간": "time", "경기": "game", "중계방송": "broadcast", "구장": "stadium", "비고": "note"}
        df = df.rename(columns=col_mapping)
        return {"status": "success", "data": df.to_dict(orient="records")}
    except Exception as e:
        return handle_error(e, "KBO-Schedule")


@app.get("/api/npb/schedule")
def get_npb_schedule(date: str):
    """NPB(일본 프로야구) 일정 — api-sports baseball /games 프록시.
    키는 환경변수(API_SPORTS_KEY/API_FOOTBALL_KEY)에서만 주입하여 클라이언트 노출을 막는다.
    프런트가 소비하는 필드(home/away)만 추려서 NPB(일본) 경기로 필터링해 반환한다."""
    try:
        if not API_SPORTS_KEY:
            raise RuntimeError("API_SPORTS_KEY/API_FOOTBALL_KEY 미설정")
        resp = requests.get(
            f"{API_BASEBALL_BASE}/games",
            params={"date": date, "timezone": "Asia/Seoul"},
            headers={"x-apisports-key": API_SPORTS_KEY, "Accept": "application/json"},
            timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json()
        errors = payload.get("errors")
        # api-sports 는 errors 를 {} 또는 [] 로 주며, 비어있지 않으면 실패(쿼터/키 등)
        if errors and (isinstance(errors, dict) and errors or isinstance(errors, list) and errors):
            raise RuntimeError(f"api-sports baseball errors: {errors}")

        games = []
        for g in payload.get("response", []) or []:
            league = g.get("league") or {}
            country = g.get("country") or {}
            lname = str(league.get("name") or "").upper()
            cname = str(country.get("name") or "").upper()
            if "NPB" not in lname and "JAPAN" not in cname:
                continue
            teams = g.get("teams") or {}
            games.append({
                "home": (teams.get("home") or {}).get("name") or "미정",
                "away": (teams.get("away") or {}).get("name") or "미정",
                "league": league.get("name"),
                "country": country.get("name"),
            })
        return {"status": "success", "data": games}
    except Exception as e:
        return handle_error(e, "NPB-Schedule")


# 2026 FIFA 월드컵 식별자 (FIFA 공식 데이터 API 기준: 월드컵=17, 2026 시즌=285023)
WORLDCUP_2026 = {"competition": "17", "season": "285023"}


def _worldcup_schedule_af() -> list:
    """[1순위] API-Football /fixtures 로 2026 월드컵 일정. 실패 시 예외(→ FIFA 폴백)."""
    cached = _cache_get("wc_schedule", ttl=600)  # 10분 캐시
    if cached is not None:
        return cached

    payload = af_get("/fixtures", {"league": WORLDCUP_AF["league"], "season": WORLDCUP_AF["season"]})
    games = []
    for it in payload.get("response", []) or []:
        fx = it.get("fixture") or {}
        lg = it.get("league") or {}
        teams = it.get("teams") or {}
        goals = it.get("goals") or {}
        venue = fx.get("venue") or {}
        st = fx.get("status") or {}
        games.append({
            "match_id": fx.get("id"),                 # 라이브 상세 패널에서 사용
            "date": fx.get("date"),                   # UTC ISO
            "home": (teams.get("home") or {}).get("name"),
            "away": (teams.get("away") or {}).get("name"),
            "group": "",                              # 그룹 문자는 standings에서만 → 빈 값
            "stage": lg.get("round"),                 # 'Group Stage - 1' / 'Round of 16' ...
            "stadium": venue.get("name"),
            "city": venue.get("city"),
            "status": st.get("short"),                # NS/1H/HT/2H/FT/AET ...
            "home_score": goals.get("home"),
            "away_score": goals.get("away"),
        })
    return _cache_set("wc_schedule", games)


def _worldcup_schedule_fifa() -> list:
    """[폴백] FIFA 공식 데이터 API. 실패 시 예외."""
    url = "https://api.fifa.com/api/v3/calendar/matches"
    params = {
        "idCompetition": WORLDCUP_2026["competition"],
        "idSeason": WORLDCUP_2026["season"],
        "count": 500,
        "language": "en",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0",
        "Accept": "application/json",
    }
    resp = requests.get(url, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    results = payload.get("Results") or payload.get("results") or []

    def localized(node):
        try:
            return node[0]["Description"]
        except (KeyError, IndexError, TypeError):
            return ""

    def team_name(side):
        try:
            return side["TeamName"][0]["Description"]
        except (KeyError, IndexError, TypeError):
            return "미정"

    games = []
    for m in results:
        home = m.get("Home") or {}
        away = m.get("Away") or {}
        stadium = m.get("Stadium") or {}
        games.append({
            "match_id": m.get("IdMatch"),
            "date": m.get("Date"),
            "home": team_name(home),
            "away": team_name(away),
            "group": localized(m.get("GroupName")),
            "stage": localized(m.get("StageName")),
            "stadium": localized(stadium.get("Name")),
            "city": "",
            "status": m.get("MatchStatus"),
            "home_score": home.get("Score"),
            "away_score": away.get("Score"),
        })
    return games


@app.get("/api/worldcup/schedule")
def get_worldcup_schedule():
    """2026 월드컵 일정 — API-Football 1순위, 실패 시 FIFA 공식 API 폴백."""
    try:
        return {"status": "success", "source": "api-football", "data": _worldcup_schedule_af()}
    except Exception as e_af:
        print(f"[WorldCup-Schedule] API-Football 실패 → FIFA 폴백: {e_af}")
        try:
            return {"status": "success", "source": "fifa", "data": _worldcup_schedule_fifa()}
        except Exception as e_fifa:
            return handle_error(e_fifa, "WorldCup-Schedule")


def _worldcup_teamstats_af() -> dict:
    """[1순위] API-Football: standings(전적/득실) + fixtures/statistics 집계(점유율·유효슈팅·xG/xGA)."""
    cached = _cache_get("wc_teamstats", ttl=3 * 3600)  # 3시간 캐시
    if cached is not None:
        return cached

    # 1) 순위표 → 전적/득실
    standings = af_get("/standings", {"league": WORLDCUP_AF["league"], "season": WORLDCUP_AF["season"]})
    data: dict = {}
    sresp = standings.get("response", []) or []
    if sresp:
        for group in ((sresp[0].get("league") or {}).get("standings") or []):
            for row in group:
                tname = (row.get("team") or {}).get("name")
                if not tname:
                    continue
                allg = row.get("all") or {}
                goals = allg.get("goals") or {}
                played = allg.get("played") or 0
                gf = goals.get("for") or 0
                ga = goals.get("against") or 0
                gp = played or 1
                data[tname] = {
                    "played": played,
                    "w": allg.get("win") or 0,
                    "d": allg.get("draw") or 0,
                    "l": allg.get("lose") or 0,
                    "gf": gf, "ga": ga,
                    "avg_gf": round(gf / gp, 2),
                    "avg_ga": round(ga / gp, 2),
                    "points": row.get("points"),
                    "group": row.get("group"),
                    # 세부지표(아래 집계에서 채움; 데이터 없으면 None 유지)
                    "avg_possession": None,
                    "avg_shots_on_target": None,
                    "avg_xg": None,
                    "avg_xga": None,
                }

    # 2) 종료(FT) 경기들의 매치 통계 집계 → 팀별 평균 점유율/유효슈팅/xG/xGA
    try:
        fixtures = _worldcup_schedule_af()
    except Exception:
        fixtures = []

    agg: dict = {}  # name -> 누적 {합계, 개수}
    finished = {"FT", "AET", "PEN"}  # 정규시간/연장/승부차기 종료 — 녹아웃 경기도 집계에 포함
    for g in fixtures:
        if g.get("status") not in finished or not g.get("match_id"):
            continue
        mid = g["match_id"]
        # 종료 경기의 통계는 더 이상 바뀌지 않으므로 경기 단위로 길게 캐시(24h) → 리빌드 시 API 재호출 최소화
        fxkey = f"wc_fxstat_{mid}"
        parsed = _cache_get(fxkey, ttl=24 * 3600)
        if parsed is None:
            try:
                stat_payload = af_get("/fixtures/statistics", {"fixture": mid})
            except Exception:
                continue
            sides = stat_payload.get("response", []) or []
            if len(sides) < 2:
                continue
            parsed = []
            for side in sides:
                items = side.get("statistics") or []
                parsed.append({
                    "name": (side.get("team") or {}).get("name"),
                    "pos": _pct_to_number(_af_stat_value(items, "Ball Possession")),
                    "sot": _pct_to_number(_af_stat_value(items, "Shots on Goal")),
                    "xg": _pct_to_number(_af_stat_value(items, "expected_goals")),
                })
            _cache_set(fxkey, parsed)
        if len(parsed) < 2:
            continue
        for i, p in enumerate(parsed):
            name = p["name"]
            if not name:
                continue
            opp = parsed[1 - i]
            a = agg.setdefault(name, {"pos": 0.0, "npos": 0, "sot": 0.0, "nsot": 0,
                                      "xg": 0.0, "nxg": 0, "xga": 0.0, "nxga": 0})
            if p["pos"] is not None:
                a["pos"] += p["pos"]; a["npos"] += 1
            if p["sot"] is not None:
                a["sot"] += p["sot"]; a["nsot"] += 1
            if p["xg"] is not None:
                a["xg"] += p["xg"]; a["nxg"] += 1
            if opp.get("xg") is not None:
                a["xga"] += opp["xg"]; a["nxga"] += 1

    for name, a in agg.items():
        d = data.get(name)
        if not d:
            continue
        if a["npos"]:
            d["avg_possession"] = round(a["pos"] / a["npos"], 1)
        if a["nsot"]:
            d["avg_shots_on_target"] = round(a["sot"] / a["nsot"], 2)
        if a["nxg"]:
            d["avg_xg"] = round(a["xg"] / a["nxg"], 2)
        if a["nxga"]:
            d["avg_xga"] = round(a["xga"] / a["nxga"], 2)

    return _cache_set("wc_teamstats", data)


def _worldcup_teamstats_fifa() -> dict:
    """[폴백] FIFA 경기 결과로부터 전적/득실만 계산(세부지표 없음)."""
    url = "https://api.fifa.com/api/v3/calendar/matches"
    params = {
        "idCompetition": WORLDCUP_2026["competition"],
        "idSeason": WORLDCUP_2026["season"],
        "count": 500,
        "language": "en",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0",
        "Accept": "application/json",
    }
    resp = requests.get(url, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    results = payload.get("Results") or payload.get("results") or []

    def team_name(side):
        try:
            return side["TeamName"][0]["Description"]
        except (KeyError, IndexError, TypeError):
            return None

    stats = {}

    def slot(name):
        if name not in stats:
            stats[name] = {"played": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0}
        return stats[name]

    for m in results:
        if m.get("MatchStatus") != 0:
            continue
        hn = team_name(m.get("Home") or {})
        an = team_name(m.get("Away") or {})
        hs, as_ = m.get("HomeTeamScore"), m.get("AwayTeamScore")
        if not hn or not an or hs is None or as_ is None:
            continue
        try:
            hs, as_ = int(hs), int(as_)
        except (ValueError, TypeError):
            continue
        sh, sa = slot(hn), slot(an)
        sh["played"] += 1; sa["played"] += 1
        sh["gf"] += hs; sh["ga"] += as_
        sa["gf"] += as_; sa["ga"] += hs
        if hs > as_:
            sh["w"] += 1; sa["l"] += 1
        elif hs < as_:
            sh["l"] += 1; sa["w"] += 1
        else:
            sh["d"] += 1; sa["d"] += 1

    data = {}
    for name, s in stats.items():
        gp = s["played"] or 1
        data[name] = {
            "played": s["played"],
            "w": s["w"], "d": s["d"], "l": s["l"],
            "gf": s["gf"], "ga": s["ga"],
            "avg_gf": round(s["gf"] / gp, 2),
            "avg_ga": round(s["ga"] / gp, 2),
            "points": s["w"] * 3 + s["d"],
            "avg_possession": None, "avg_shots_on_target": None,
            "avg_xg": None, "avg_xga": None,
        }
    return data


@app.get("/api/worldcup/teamstats")
def get_worldcup_teamstats():
    """팀별 본선 성적+세부지표 — API-Football 1순위(점유율·유효슈팅·xG 포함), 실패 시 FIFA 폴백."""
    try:
        return {"status": "success", "source": "api-football", "data": _worldcup_teamstats_af()}
    except Exception as e_af:
        print(f"[WorldCup-TeamStats] API-Football 실패 → FIFA 폴백: {e_af}")
        try:
            return {"status": "success", "source": "fifa", "data": _worldcup_teamstats_fifa()}
        except Exception as e_fifa:
            return handle_error(e_fifa, "WorldCup-TeamStats")


@app.get("/api/worldcup/fixture/{fixture_id}")
def get_worldcup_fixture(fixture_id: int):
    """단일 경기 실시간 상세: 스코어/상태 + 매치통계(home/away) + 타임라인 + 라인업. (API-Football)"""
    cache_key = f"wc_fixture_{fixture_id}"
    cached = _cache_get(cache_key, ttl=45)  # 라이브 45초 캐시
    if cached is not None:
        return cached
    try:
        fx_payload = af_get("/fixtures", {"id": fixture_id})
        resp = fx_payload.get("response", []) or []
        if not resp:
            return {"status": "error", "message": "경기를 찾을 수 없습니다.", "data": None}
        item = resp[0]
        fx = item.get("fixture") or {}
        teams = item.get("teams") or {}
        goals = item.get("goals") or {}
        st = fx.get("status") or {}
        home_name = (teams.get("home") or {}).get("name")

        timeline = []
        for ev in item.get("events") or []:
            tm = ev.get("time") or {}
            timeline.append({
                "minute": tm.get("elapsed"),
                "extra": tm.get("extra"),
                "team": (ev.get("team") or {}).get("name"),
                "type": ev.get("type"),       # Goal / Card / subst
                "detail": ev.get("detail"),   # Normal Goal / Yellow Card / ...
                "player": (ev.get("player") or {}).get("name"),
                "assist": (ev.get("assist") or {}).get("name"),
            })

        statistics = {"home": {}, "away": {}}
        try:
            for side in af_get("/fixtures/statistics", {"fixture": fixture_id}).get("response", []) or []:
                key = "home" if (side.get("team") or {}).get("name") == home_name else "away"
                statistics[key] = {it.get("type"): it.get("value") for it in (side.get("statistics") or [])}
        except Exception as se:
            print(f"[WorldCup-Fixture] 통계 생략 fixture={fixture_id}: {se}")

        lineups = []
        try:
            for lu in af_get("/fixtures/lineups", {"fixture": fixture_id}).get("response", []) or []:
                lineups.append({
                    "team": (lu.get("team") or {}).get("name"),
                    "formation": lu.get("formation"),
                    "startXI": [(p.get("player") or {}).get("name") for p in (lu.get("startXI") or [])],
                })
        except Exception as le:
            print(f"[WorldCup-Fixture] 라인업 생략 fixture={fixture_id}: {le}")

        data = {
            "match_id": fixture_id,
            "home": home_name,
            "away": (teams.get("away") or {}).get("name"),
            "home_score": goals.get("home"),
            "away_score": goals.get("away"),
            "status": st.get("short"),
            "status_long": st.get("long"),
            "elapsed": st.get("elapsed"),
            "timeline": timeline,
            "statistics": statistics,
            "lineups": lineups,
        }
        return _cache_set(cache_key, {"status": "success", "data": data})
    except Exception as e:
        return handle_error(e, "WorldCup-Fixture")


def _wc_player_ratings_for_team(team_id) -> dict:
    """대회(league=1, season=2026) 시즌 선수별 평균 평점 {player_id: rating}. 페이지네이션 처리."""
    ratings: dict = {}
    page = 1
    while True:
        payload = af_get("/players", {
            "team": team_id, "league": WORLDCUP_AF["league"], "season": WORLDCUP_AF["season"], "page": page,
        })
        for it in payload.get("response", []) or []:
            pid = (it.get("player") or {}).get("id")
            if pid is None:
                continue
            r = None
            for s in it.get("statistics", []) or []:
                rating = _pct_to_number((s.get("games") or {}).get("rating"))
                if rating is None:
                    continue
                if ((s.get("league") or {}).get("id")) == WORLDCUP_AF["league"]:
                    r = rating  # 월드컵 평점 우선
                    break
                if r is None:
                    r = rating  # 폴백: 첫 유효 평점
            if r is not None:
                ratings[pid] = r
        paging = payload.get("paging") or {}
        if page >= (paging.get("total") or 1):
            break
        page += 1
    return ratings


def _worldcup_lineup_strength(fixture_id: int) -> dict:
    """선발 XI 평균 평점을 팀명 키 dict 로 반환: {teamName: {rating, count, formation}}.
    1) 라인업(startXI) 확보 → 2) 경기 선수평점(fixtures/players)이 있으면 그 선발 평점 평균,
    없으면 대회 시즌 선수 평균평점으로 startXI 매핑. 데이터 없으면 rating=None(프런트는 등급표 폴백)."""
    cache_key = f"wc_lineupstrength_{fixture_id}"
    cached = _cache_get(cache_key, ttl=600)  # 10분 캐시
    if cached is not None:
        return cached

    lineups = af_get("/fixtures/lineups", {"fixture": fixture_id}).get("response", []) or []

    # (있으면) 경기 선수평점에서 '선발(substitute=False)' 평균
    fx_team_rating: dict = {}
    try:
        for side in af_get("/fixtures/players", {"fixture": fixture_id}).get("response", []) or []:
            tid = (side.get("team") or {}).get("id")
            vals = []
            for p in side.get("players", []) or []:
                g = ((p.get("statistics") or [{}])[0] or {}).get("games") or {}
                if g.get("substitute") is False:
                    rr = _pct_to_number(g.get("rating"))
                    if rr is not None:
                        vals.append(rr)
            if vals:
                fx_team_rating[tid] = sum(vals) / len(vals)
    except Exception as pe:
        print(f"[WorldCup-LineupStrength] fixtures/players 생략 fixture={fixture_id}: {pe}")

    data: dict = {}
    for side in lineups:
        team = side.get("team") or {}
        tid, tname = team.get("id"), team.get("name")
        if not tname:
            continue
        rating, count = None, 0
        startxi_ids = [(e.get("player") or {}).get("id") for e in (side.get("startXI") or [])]
        if tid in fx_team_rating:
            rating = round(fx_team_rating[tid], 2)
            count = len(startxi_ids) or 11
        elif startxi_ids:
            tr = _wc_player_ratings_for_team(tid)
            vals = [tr[i] for i in startxi_ids if i in tr]
            if vals:
                rating = round(sum(vals) / len(vals), 2)
                count = len(vals)
        data[tname] = {"rating": rating, "count": count, "formation": side.get("formation")}
    return _cache_set(cache_key, data)


@app.get("/api/worldcup/lineup-strength/{fixture_id}")
def get_worldcup_lineup_strength(fixture_id: int):
    """선택 경기의 실제 선발 XI 평균 평점(라인업 전력 보정용). API-Football 전용 — 데이터 없으면 빈 값."""
    try:
        return {"status": "success", "data": _worldcup_lineup_strength(fixture_id)}
    except Exception as e:
        return handle_error(e, "WorldCup-LineupStrength")


# ──────────────────────────────────────────────────────────────────────────
# 배구 — FIVB 네이션스리그(VNL, 남/여). api-sports volleyball API 프록시.
# 동일 api-sports 계정 키 사용(API_SPORTS_KEY→API_FOOTBALL_KEY). 배구 별도 구독이 필요할 수 있음.
# 주의: api-sports 배구는 일정/세트스코어/순위 중심 — 세부 스킬스탯은 제공하지 않음.
# ──────────────────────────────────────────────────────────────────────────
VOLLEYBALL_BASE = "https://v1.volleyball.api-sports.io"
VNL_SEASON = int(os.getenv("VNL_SEASON", "2026"))


def vb_get(path: str, params: dict | None = None, timeout: int = 20) -> dict:
    """api-sports volleyball GET. 키 미설정/오류 시 예외."""
    if not API_SPORTS_KEY:
        raise RuntimeError("API_SPORTS_KEY/API_FOOTBALL_KEY 미설정")
    resp = requests.get(
        f"{VOLLEYBALL_BASE}{path}",
        params=params or {},
        headers={"x-apisports-key": API_SPORTS_KEY, "Accept": "application/json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    errors = payload.get("errors")
    if errors and (isinstance(errors, dict) and errors or isinstance(errors, list) and errors):
        raise RuntimeError(f"api-sports volleyball errors: {errors}")
    return payload


def _is_vnl(name) -> bool:
    return "nations league" in str(name or "").lower()


def _vnl_gender(name) -> str:
    n = str(name or "").lower()
    if "women" in n:
        return "여자"
    if "men" in n:
        return "남자"
    return ""


def _vb_sets(game: dict):
    """경기에서 (홈 세트, 원정 세트) 추출 — 제공처 필드명이 scores/goals 중 무엇이든 방어적으로 처리."""
    for key in ("scores", "goals"):
        node = game.get(key) or {}
        h, a = node.get("home"), node.get("away")
        if h is not None and a is not None:
            return h, a
    return None, None


@app.get("/api/volleyball/schedule")
def get_volleyball_schedule(date: str):
    """VNL(남/여) 일정 — api-sports volleyball /games?date= 에서 'Nations League'만 필터해 반환."""
    try:
        payload = vb_get("/games", {"date": date, "timezone": "Asia/Seoul"})
        games = []
        for g in payload.get("response", []) or []:
            league = g.get("league") or {}
            if not _is_vnl(league.get("name")):
                continue
            teams = g.get("teams") or {}
            status = g.get("status") or {}
            hs, as_ = _vb_sets(g)
            games.append({
                "match_id": g.get("id"),
                "date": g.get("date"),
                "home": (teams.get("home") or {}).get("name"),
                "away": (teams.get("away") or {}).get("name"),
                "home_sets": hs,
                "away_sets": as_,
                "status": status.get("short") or status.get("long"),
                "league": league.get("name"),
                "gender": _vnl_gender(league.get("name")),
            })
        return {"status": "success", "data": games}
    except Exception as e:
        return handle_error(e, "Volleyball-Schedule")


def _vnl_leagues() -> list:
    """VNL(남/여) league id 목록을 /leagues 검색으로 동적 확보(하드코딩 회피). 24h 캐시."""
    cached = _cache_get("vnl_leagues", ttl=24 * 3600)
    if cached is not None:
        return cached
    out = []
    try:
        payload = vb_get("/leagues", {"search": "Nations League"})
        for it in payload.get("response", []) or []:
            lg = it.get("league") or it  # 구조 방어(중첩/평면 모두 대응)
            name = lg.get("name") or it.get("name")
            lid = lg.get("id") or it.get("id")
            if _is_vnl(name) and lid is not None:
                out.append({"id": lid, "name": name})
    except Exception as e:
        print(f"[Volleyball] VNL 리그 검색 실패: {e}")
    return _cache_set("vnl_leagues", out)


@app.get("/api/volleyball/teamstats")
def get_volleyball_teamstats():
    """VNL 팀별 전적/세트 집계 — 경기 결과(/games?league=&season=)로부터 직접 계산(팀명 키 dict).
    일정과 동일 소스라 팀명이 100% 일치. 세부 스킬스탯은 api-sports 미제공 → 프런트에서 수동 입력."""
    cache_key = "vnl_teamstats"
    cached = _cache_get(cache_key, ttl=3 * 3600)
    if cached is not None:
        return {"status": "success", "data": cached}
    try:
        agg: dict = {}

        def slot(name):
            if name not in agg:
                agg[name] = {"played": 0, "w": 0, "l": 0, "sets_won": 0, "sets_lost": 0}
            return agg[name]

        leagues = _vnl_leagues()
        for lg in leagues:
            try:
                payload = vb_get("/games", {"league": lg["id"], "season": VNL_SEASON})
            except Exception:
                continue
            for g in payload.get("response", []) or []:
                teams = g.get("teams") or {}
                hn = (teams.get("home") or {}).get("name")
                an = (teams.get("away") or {}).get("name")
                hs, as_ = _vb_sets(g)
                if not hn or not an or hs is None or as_ is None:
                    continue
                try:
                    hs, as_ = int(hs), int(as_)
                except (ValueError, TypeError):
                    continue
                if hs == as_:  # 미완료/무효(배구는 무승부 없음)
                    continue
                sh, sa = slot(hn), slot(an)
                sh["played"] += 1; sa["played"] += 1
                sh["sets_won"] += hs; sh["sets_lost"] += as_
                sa["sets_won"] += as_; sa["sets_lost"] += hs
                if hs > as_:
                    sh["w"] += 1; sa["l"] += 1
                else:
                    sh["l"] += 1; sa["w"] += 1

        data = {}
        for name, s in agg.items():
            sw, sl = s["sets_won"], s["sets_lost"]
            data[name] = {
                "played": s["played"], "w": s["w"], "l": s["l"],
                "sets_won": sw, "sets_lost": sl,
                "set_ratio": round(sw / (sw + sl), 3) if (sw + sl) > 0 else None,
                "win_rate": round(s["w"] / s["played"], 3) if s["played"] > 0 else None,
            }
        _cache_set(cache_key, data)
        return {"status": "success", "data": data}
    except Exception as e:
        return handle_error(e, "Volleyball-TeamStats")


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)