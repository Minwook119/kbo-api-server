import io
import uvicorn
import pandas as pd
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from playwright.sync_api import sync_playwright

app = FastAPI(
    title="Sports Stats Scraper API",
    description="KBO(타자/투수/수비/주루 세부스탯 및 일정), NPB, Soccer, Basketball 스크래핑 API",
    version="2.4.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def fetch_and_parse_table(url: str, column_mapping: dict) -> pd.DataFrame:
    html_content = ""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,
                                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0")
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            # 🌟 핵심 방어: '팀명'이라는 글자가 포함된 진짜 기록 테이블이 나타날 때까지 대기
            page.wait_for_selector("table:has-text('팀명')", timeout=20000)
            html_content = page.content()
        except Exception as e:
            raise Exception(f"페이지 로딩 또는 테이블 대기 실패: {str(e)}")
        finally:
            browser.close()

    # 🌟 핵심 방어: 상단 미니 전광판을 무시하고 '팀명' 컬럼이 있는 테이블만 골라서 파싱
    tables = pd.read_html(io.StringIO(html_content), match="팀명")
    if not tables:
        raise ValueError("진짜 팀 기록 테이블을 찾을 수 없습니다.")
    return tables[0].fillna(0).rename(columns=column_mapping)


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
        df1 = fetch_and_parse_table(urls[0], m1)
        if m2:
            df2 = fetch_and_parse_table(urls[1], m2)
            if "rank" in df2.columns: df2 = df2.drop(columns=["rank"])
            df1 = pd.merge(df1, df2, on="team", how="inner")
        return {"status": "success", "data": df1.to_dict(orient="records")}
    except Exception as e:
        return handle_error(e, f"KBO-{category}")


@app.get("/api/kbo/schedule")
def get_kbo_schedule():
    """KBO 일정 크롤링 (상단 미니 오인 방지 완벽 보강)"""
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
                # 🌟 핵심 방어: 미니 전광판엔 없고 본문 일정표에만 있는 '구장' 텍스트 테이블 대기
                page.wait_for_selector("table:has-text('구장')", timeout=20000)
                html_content = page.content()
            except Exception as e:
                raise Exception(f"일정 페이지 로딩 실패: {str(e)}")
            finally:
                browser.close()

        # 🌟 핵심 방어: '구장' 텍스트 컬럼이 포함된 진짜 이번 달 전체 일정 테이블만 정확히 타겟팅
        tables = pd.read_html(io.StringIO(html_content), match="구장")
        if not tables:
            raise ValueError("일정 테이블을 찾을 수 없습니다.")

        df = tables[0]
        if "날짜" in df.columns:
            df["날짜"] = df["날짜"].ffill()  # 연속 날짜 셀 병합 채우기

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