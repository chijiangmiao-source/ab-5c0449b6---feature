FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8000 \
    DATA_DIR=/data \
    STEP_DELAY=0.25

WORKDIR /app

COPY app ./app
COPY tests ./tests
COPY verify ./verify
COPY scripts ./scripts

# 页面构建：校验静态资源并生成 build-manifest.json
RUN python scripts/build_page.py

EXPOSE 8000
VOLUME ["/data"]

CMD ["python", "-m", "app.main"]
