FROM python:3.12-slim

WORKDIR /app

COPY bot/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY bot/*.py /app/

CMD ["python", "main.py"]
