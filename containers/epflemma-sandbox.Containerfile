FROM python:3.12-bookworm

ARG EPFLEMMA_SANDBOX_EXTRAS=mcp
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        curl \
        git \
        openssh-client \
        ripgrep \
        unzip \
        xz-utils \
        zstd \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh \
    | sh -s -- -y --no-modify-path --default-toolchain none \
    && cp /root/.elan/bin/* /usr/local/bin/

ENV ELAN_HOME=/epflemma-cache/elan
ENV XDG_CACHE_HOME=/epflemma-cache/xdg
ENV PIP_CACHE_DIR=/epflemma-cache/pip
ENV EPFLEMMA_HOME=/epflemma-home
ENV OPENGAUSS_HOME=/epflemma-home
ENV GAUSS_HOME=/epflemma-home
ENV HOME=/epflemma-home
ENV PATH=/opt/epflemma/.venv/bin:/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /opt/epflemma
COPY . /opt/epflemma

RUN python -m venv /opt/epflemma/.venv \
    && /opt/epflemma/.venv/bin/python -m pip install --upgrade pip "setuptools<82" wheel \
    && /opt/epflemma/.venv/bin/python -m pip install -e "/opt/epflemma[${EPFLEMMA_SANDBOX_EXTRAS}]" \
    && /opt/epflemma/.venv/bin/epflemma --help >/dev/null

WORKDIR /workspace
ENTRYPOINT []
