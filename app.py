import io
import uvicorn
import pandas as pd
import urllib.request  # 🌟 Playwright 대신 사용할 초경량 파이썬 내장 라이브러리
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="Sports Stats Scraper API",
    description="KBO(타자/투수/수비/주루 세부스탯 및 일정) 초경량 스크래핑 API",
    version="3.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def fetch_and_parse_table(url: str, column_mapping: dict, match_text: str) -> pd.DataFrame:
    """Playwright 없이 브라우저 헤더만 모방하여 초고속으로 HTML을 다운로드하고 파싱합니다."""
    try:
        # 브라우저인 것처럼 속이는 헤더 설정
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"}
        )
        # 메모리를 거의 먹지 않는 초경량 HTTP 요청 (타임아웃 15초)
        with urllib.request.urlopen(req, timeout=15) as response:
            html_content = response.read().decode('utf-8')

        # 지정한 텍스트(예: '팀명')가 있는 진짜 테이블만 저격 파싱
        tables = pd.read_html(io.StringIO(html_content), match=match_text)
        if not tables:
            raise ValueError(f"'{match_text}' 테이블을 찾을 수 없습니다.")

        return tables[0].fillna(0).rename(columns=column_mapping)
    except Exception as e:
        raise Exception(f"데이터 다운로드 및 파싱 실패: {str(e)}")


def handle_error(e: Exception, sport_name: str):
    print(f"[{sport_name} API Error] {str(e)}")
    return JSONResponse(status_code=500, content={"status": "error", "message": str(e), "data": []})


@app.get("/api/kbo")
def get_kbo_stats(category: str = Query("hitter")):
    category = category.lower()

    configs = {
        "hitter": (["https://www.koreabaseball.com/Record/Team/Hitter/Basic1.aspx",
                    "https://www.koreabaseball.com/Record/Team/Hitter/Basic2.aspx"],
                   {"순위": "rank", "팀명": "team", "AVG": "avg", "G": "games", "R": "runs", "H": "hits",
                    "HR": "home_runs"},
                   {"팀명": "team", "BB": "walks", "SO": "strikeouts", "SLG": "slg", "OBP": "obp", "OPS": "ops"}),
        "pitcher": (["https://www.koreabaseball.com/Record/Team/Pitcher/Basic1.aspx",
                     "https://www.koreabaseball.com/Record/Team/Pitcher/Basic2.aspx"],
                    {"순위": "rank", "팀명": "team", "ERA": "era", "G": "games", "W": "wins", "L": "losses",
                     "WPCT": "win_rate", "BB": "walks_allowed", "SO": "strikeouts_pitched", "WHIP": "whip"},
                    {"팀명": "team", "R": "runs_allowed", "ER": "earned_runs", "AVG": "opp_avg", "OPS": "opp_ops"}),
        "defense": (["https://www.koreabaseball.com/Record/Team/Defense/Basic.aspx"],
                    {"순위": "rank", "팀명": "team", "G": "games", "E": "errors", "DP": "double_plays"}, None),
        "runner": (["https://www.koreabaseball.com/Record/Team/Runner/Basic.aspx"],
                   {"순위": "rank", "팀명": "team", "G": "games", "SB": "stolen_bases", "CS": "caught_stealing"}, None)
    }

    if category not in configs:
        return JSONResponse(status_code=400, content={"status": "error", "message": "잘못된 카테고리"})

    try:
        urls, m1, m2 = configs[category]
        df1 = fetch_and_parse_table(urls[0], m1, "팀명")
        if m2:
            df2 = fetch_and_parse_table(urls[1], m2, "팀명")
            if "rank" in df2.columns: df2 = df2.drop(columns=["rank"])
            df1 = pd.merge(df1, df2, on="team", how="inner")
        return {"status": "success", "data": df1.to_dict(orient="records")}
    except Exception as e:
        return handle_error(e, f"KBO-{category}")


@app.get("/api/kbo/schedule")
def get_kbo_schedule():
    """KBO 일정 크롤링 (초경량 및 상단 미니 전광판 오인 방지 완벽 반영)"""
    url = "https://www.koreabaseball.com/Schedule/Schedule.aspx"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"}
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            html_content = response.read().decode('utf-8')

        # '구장'이라는 텍스트가 포함된 진짜 한 달 일정 테이블만 선택
        tables = pd.read_html(io.StringIO(html_content), match="구장")
        if not tables:
            raise ValueError("일정 테이블을 찾을 수 없습니다.")

        df = tables[0]
        if "날짜" in df.columns:
            df["날짜"] = df["날짜"].ffill()

        df = df.fillna("")

        col_mapping = {
            "날짜": "date",
            "시간": "time",
            "경기": "game",
            "중계방송": "broadcast",
            "구장": "stadium",
            "비고": "note"
        }
        df = df.rename(columns=col_mapping)
        return {"status": "success", "data": df.to_dict(orient="records")}
    except Exception as e:
        return handle_error(e, "KBO-Schedule")


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)