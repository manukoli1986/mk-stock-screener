FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Hosts inject PORT; default 7860 suits Hugging Face Spaces.
ENV PORT=7860
EXPOSE 7860

CMD gunicorn app:app --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT
