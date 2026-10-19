FROM python:3.12-slim
WORKDIR /app
COPY main.py .
RUN useradd --create-home mio
USER mio
ENV PYTHONUNBUFFERED=1
CMD ["python", "main.py"]
