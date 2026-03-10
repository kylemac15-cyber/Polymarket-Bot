From python:3.11-slim
WORKDIR /app
COPY requirements.txt
RUN pip install -r requirements.txt
COPY polymarket_scanner.py
CMD ["python3", "-u", "polymarket_scanner.py"]
