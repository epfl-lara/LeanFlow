FROM ubuntu:24.04

SHELL ["/bin/bash", "-lc"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates git python3 python3-venv python3-pip sudo \
    && rm -rf /var/lib/apt/lists/*

CMD ["/bin/bash"]
