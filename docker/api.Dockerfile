FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY coupled_backend ./coupled_backend
ENV DATA_DIR=/data
CMD ["uvicorn", "coupled_backend.api:app", "--host", "0.0.0.0", "--port", "8000"]
