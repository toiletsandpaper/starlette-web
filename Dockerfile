FROM python:3.10-slim-buster
WORKDIR /web-project

COPY setup.cfg .
COPY setup.py .

RUN groupadd -r web && useradd -r -g web web
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
		gcc \
		libpq-dev \
		python-dev \
    && pip install .[admin,apispec,auth,postgres,redis,scheduler,testing] \
	&& apt-get purge -y --auto-remove gcc python-dev \
	&& apt-get -y autoremove \
	&& apt-get clean \
	&& rm -rf /var/lib/apt/lists/*

RUN mkdir -p media
RUN mkdir -p filestorage
RUN mkdir -p static

COPY starlette_web ./starlette_web
COPY etc/run_tests.sh .
COPY .coveragerc .
COPY .flake8 .

RUN chown -R web:web /web-project

ENV STARLETTE_SETTINGS_MODULE=starlette_web.tests.settings
COPY command.py .
RUN python command.py collectstatic

ENTRYPOINT ["/bin/sh", "/web-project/run_tests.sh"]
