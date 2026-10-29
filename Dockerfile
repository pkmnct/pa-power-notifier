FROM python:3.11-slim

WORKDIR /app

# pdfplumber's dependency chain (pdfminer.six) needs no native libs,
# but cryptography (used by some pdfplumber extras) may. Keep build deps
# minimal and remove them after install to keep the image small.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pa_power_notifier.py .
COPY env_bootstrap.py .
COPY check_report.py .
COPY recommendation_engine.py .
COPY gas_usage_forecast.py .
COPY gas_plan_compare.py .
COPY electric_plan_compare.py .
COPY rate_scrapers.py .
COPY duquesne_light_analyzer.py .
COPY peoples_gas_analyzer.py .
COPY tou_analyzer.py .
COPY usage_archive.py .

RUN mkdir -p /data

CMD ["python", "pa_power_notifier.py"]
