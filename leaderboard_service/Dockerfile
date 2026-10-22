FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PNUT_LEADERBOARD_BIND=0.0.0.0
WORKDIR /app
COPY leaderboard_service/requirements.txt /app/leaderboard_service/requirements.txt
RUN pip install --no-cache-dir -r /app/leaderboard_service/requirements.txt
COPY mnmparse/__init__.py mnmparse/leaderboard.py /app/mnmparse/
COPY leaderboard_service/__init__.py leaderboard_service/__main__.py leaderboard_service/server.py /app/leaderboard_service/
USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=2).close()"
CMD ["python", "-m", "leaderboard_service"]
