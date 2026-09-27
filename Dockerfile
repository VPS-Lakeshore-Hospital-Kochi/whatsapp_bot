FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "psycopg[binary]>=3.1"
COPY app app
COPY knowledge knowledge
RUN useradd --create-home bot && mkdir -p data && chown bot data
USER bot
EXPOSE 8000
# One worker per container keeps things simple; scale by running more containers behind the load balancer.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
