# For more information, please refer to https://aka.ms/vscode-docker-python
FROM python:3.10-slim

# RUN apk update && apk add postgresql-dev gcc python3-dev musl-dev
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt requirements.txt
RUN pip3 install --upgrade pip
RUN pip3 install -r requirements.txt
COPY . /app
CMD ["streamlit", "run", "csvpdfOpenAI.py"]

EXPOSE 8000
EXPOSE 8501

#ENTRYPOINT ["streamlit","run"]

#CMD ["csvpdfOpenAI.py"]
