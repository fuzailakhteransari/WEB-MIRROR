FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn[standard] websockets pydantic qrcode

COPY shared ./shared
COPY server ./server
COPY web ./web

ENV WEBDESK_HOST=0.0.0.0
ENV WEBDESK_PORT=8080

EXPOSE 8080

CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips '*'"]
