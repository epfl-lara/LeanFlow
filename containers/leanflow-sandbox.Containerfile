ARG LEANFLOW_SANDBOX_BASE=python:3.12-bookworm
FROM ${LEANFLOW_SANDBOX_BASE}

ARG LEANFLOW_SANDBOX_EXTRAS=mcp
ENV DEBIAN_FRONTEND=noninteractive
USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        curl \
        git \
        openssh-client \
        poppler-utils \
        python3 \
        python3-venv \
        ripgrep \
        unzip \
        xz-utils \
        zstd \
    && rm -rf /var/lib/apt/lists/*

RUN if [ -x /home/lean/.elan/bin/elan ]; then \
        cp /home/lean/.elan/bin/* /usr/local/bin/; \
    else \
        curl -fsSL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh \
        | sh -s -- -y --no-modify-path --default-toolchain none \
        && cp /root/.elan/bin/* /usr/local/bin/; \
    fi

ENV ELAN_HOME=/leanflow-cache/elan
ENV XDG_CACHE_HOME=/leanflow-cache/xdg
ENV PIP_CACHE_DIR=/leanflow-cache/pip
ENV LEANFLOW_HOME=/leanflow-home
ENV HOME=/leanflow-home
ENV PATH=/opt/leanflow/.venv/bin:/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /opt/leanflow
COPY pyproject.toml README.md /opt/leanflow/

RUN python3 -c "import sys; assert sys.version_info >= (3, 12), 'LeanFlow sandbox MCP bootstrap requires Python 3.12+'" \
    && python3 -c "import sys,tomllib; p=tomllib.load(open('pyproject.toml','rb'))['project']; o=p.get('optional-dependencies',{}); x=sys.argv[1].split(','); print('\\n'.join([*p.get('dependencies',[]),*sum((o.get(n,[]) for n in x),[])]))" "${LEANFLOW_SANDBOX_EXTRAS}" > /tmp/leanflow-requirements.txt \
    && python3 -m venv /opt/leanflow/.venv \
    && /opt/leanflow/.venv/bin/python -m pip install --upgrade pip "setuptools<82" wheel \
    && /opt/leanflow/.venv/bin/python -m pip install -r /tmp/leanflow-requirements.txt

COPY . /opt/leanflow

RUN /opt/leanflow/.venv/bin/python -m pip install --no-deps -e "/opt/leanflow[${LEANFLOW_SANDBOX_EXTRAS}]" \
    && /opt/leanflow/.venv/bin/leanflow --help >/dev/null

WORKDIR /workspace
ENTRYPOINT []
