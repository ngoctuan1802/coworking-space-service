FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .

RUN apt-get install -y gcc libpq-dev && \
    pip install --no-cache-dir -r requirements.txt && \
    apt-get purge -y gcc && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

COPY . .

EXPOSE 5153

CMD ["python", "app.py"]
