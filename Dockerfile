FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY botv2.py .

CMD ["python", "-u", "botv2.py"]
