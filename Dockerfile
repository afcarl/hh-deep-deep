FROM ubuntu:16.04

WORKDIR /opt/hh-deep-deep

RUN apt-get update && \
    apt-get install -y tree docker.io python-pip python3 python3-pip && \
    pip install docker-compose

COPY requirements.txt .
RUN pip3 install -U pip wheel && \
    pip3 install -r requirements.txt

COPY . .
RUN tree
RUN pip3 install -e .
