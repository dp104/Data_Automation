FROM python:3.12-slim

WORKDIR /app

# Playwright needs its own browser binary + OS libraries (for headless Chrome, used on JS-heavy
# sites and bot-protected pages) - --with-deps installs both in one step.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

ENV UNISCRAPE_NO_BROWSER=1
EXPOSE 8765

CMD ["python", "webapp.py"]
