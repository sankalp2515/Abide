# A three-agent test team (Northwind Books) that uses Abide like an outside company: only httpx, plus the one-file
# client it downloads from the gateway at start. See agents.py.
FROM python:3.11-slim
RUN pip install --no-cache-dir httpx==0.28.1
WORKDIR /team
COPY agents.py .
RUN useradd -m team
USER team
ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
CMD ["python", "agents.py"]
