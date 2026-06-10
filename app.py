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
    version="2.2.0"
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
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_selector("table", timeout=10000)
            html_content = page.content()
        except Exception as e:
            browser.close()
            raise Exception(f"페이지 로딩 실패: {str(e)}")
        finally:
            browser.close()

    try:
        tables = pd.read_html(io.StringIO(html_content))
        if not tables:
            raise ValueError("페이지에서 <table> 태그를 찾을 수 없습니다.")

        df = tables[0].fillna(0).rename(columns=column_mapping)
        return df
    except Exception as e:
        raise Exception(f"데이터 정제 중 오류: {str(e)}")


def handle_error(e: Exception, sport_name: str):
    print(f"[{sport_name} API Error] {str(e)}")
    return JSONResponse(status_code=500, content={"status": "error", "message": str(e), "data": []})


@app.get("/api/kbo")
def get_kbo_stats(category: str = Query("hitter")):
    category = category.lower()

    if category == "hitter":
        url1 = "https://www.koreabaseball.com/Record/Team/Hitter/Basic1.aspx"
        url2 = "https://www.koreabaseball.com/Record/Team/Hitter/Basic2.aspx"
        map1 = {"순위": "rank", "팀명": "team", "AVG": "avg", "G": "games", "R": "runs", "H": "hits", "HR": "home_runs"}
        map2 = {"팀명": "team", "BB": "walks", "SO": "strikeouts", "SLG": "slg", "OBP": "obp", "OPS": "ops"}

    elif category == "pitcher":
        url1 = "https://www.koreabaseball.com/Record/Team/Pitcher/Basic1.aspx"
        url2 = "https://www.koreabaseball.com/Record/Team/Pitcher/Basic2.aspx"
        map1 = {"순위": "rank", "팀명": "team", "ERA": "era", "G": "games", "W": "wins", "L": "losses", "WPCT": "win_rate",
                "BB": "walks_allowed", "SO": "strikeouts_pitched", "WHIP": "whip"}
        map2 = {"팀명": "team", "R": "runs_allowed", "ER": "earned_runs", "AVG": "opp_avg", "OPS": "opp_ops"}

    elif category == "defense":
        url1 = "https://www.koreabaseball.com/Record/Team/Defense/Basic.aspx"
        url2 = None
        map1 = {"순위": "rank", "팀명": "team", "G": "games", "E": "errors", "DP": "double_plays"}
        map2 = None

    elif category == "runner":
        url1 = "https://www.koreabaseball.com/Record/Team/Runner/Basic.aspx"
        url2 = None
        map1 = {"순위": "rank", "팀명": "team", "G": "games", "SB": "stolen_bases", "CS": "caught_stealing"}
        map2 = None

    else:
        return JSONResponse(status_code=400, content={"status": "error", "message": "잘못된 카테고리입니다.", "data": []})

    try:
        df1 = fetch_and_parse_table(url1, map1)

        if url2:
            df2 = fetch_and_parse_table(url2, map2)
            if "rank" in df2.columns:
                df2 = df2.drop(columns=["rank"])
            merged_df = pd.merge(df1, df2, on="team", how="inner")
        else:
            merged_df = df1

        return {"status": "success", "category": category, "data": merged_df.to_dict(orient="records")}

    except Exception as e:
        return handle_error(e, f"KBO-{category}")


# 🌟 새로 추가된 KBO 일정 크롤링 엔드포인트
@app.get("/api/kbo/schedule")
def get_kbo_schedule():
    """KBO 공식 홈페이지 이번 달 경기 일정 크롤링"""
    url = "https://www.koreabaseball.com/Schedule/Schedule.aspx"

    try:
        html_content = ""
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
            )
            page = context.new_page()

            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_selector("table", timeout=10000)
            html_content = page.content()
            browser.close()

        tables = pd.read_html(io.StringIO(html_content))
        if not tables:
            raise ValueError("일정 표를 찾을 수 없습니다.")

        df = tables[0]

        # 셀 병합(Rowspan)으로 인해 발생한 빈 날짜 채우기 (ffill)
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