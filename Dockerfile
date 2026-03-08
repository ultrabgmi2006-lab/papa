FROM python:3.10

WORKDIR /app

COPY . .

RUN chmod +x ultra
RUN pip install aiohttp pyTelegramBotAPI pymongo pytz psutil

CMD ./ultra & python ultra.py
