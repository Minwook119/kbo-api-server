# 1. 마이크로소프트가 제공하는 Playwright 전용 파이썬 환경 (크롬 브라우저가 이미 깔려있음!)
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# 2. 작업 폴더 지정
WORKDIR /app

# 3. 라이브러리 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. 내 코드(app.py) 복사
COPY . .

# 5. 서버 실행 (Render는 환경변수 PORT를 쓰므로 맞춰서 실행)
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}