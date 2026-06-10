# 1. 버전을 1.60.0으로 업데이트!
FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

# 2. 작업 폴더 지정
WORKDIR /app

# 3. 라이브러리 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. 내 코드 복사
COPY . .

# 5. 서버 실행
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}