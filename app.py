import io
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


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)