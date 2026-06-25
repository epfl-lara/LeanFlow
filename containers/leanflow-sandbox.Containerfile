FROM python:3.12-bookworm

ARG LEANFLOW_SANDBOX_EXTRAS=mcp
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        curl \
        git \
        openssh-client \
        poppler-utils \
        ripgrep \
        unzip \
        xz-utils \
        zstd \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh \
    | sh -s -- -y --no-modify-path --default-toolchain none \
    && cp /root/.elan/bin/* /usr/local/bin/

ENV ELAN_HOME=/leanflow-cache/elan
ENV XDG_CACHE_HOME=/leanflow-cache/xdg
ENV PIP_CACHE_DIR=/leanflow-cache/pip
ENV LEANFLOW_HOME=/leanflow-home
ENV HOME=/leanflow-home
ENV PATH=/opt/leanflow/.venv/bin:/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /opt/leanflow
COPY . /opt/leanflow

RUN python -m venv /opt/leanflow/.venv \
    && /opt/leanflow/.venv/bin/python -m pip install --upgrade pip "setuptools<82" wheel \
    && /opt/leanflow/.venv/bin/python -m pip install -e "/opt/leanflow[${LEANFLOW_SANDBOX_EXTRAS}]" \
    && /opt/leanflow/.venv/bin/leanflow --help >/dev/null

WORKDIR /workspace
ENTRYPOINT []
