# Host for the frozen Lean-IMO-Bench campaign.
# The campaign carries its own python env, LeanFlow runtime and Lean toolchain;
# this image supplies what those bind mounts expect to find on the SYSTEM.
#
# ripgrep is not optional: leanflow_cli/workflows/prover/session_search.py execs
# a bare `rg`, and it backs both search_project and the offline fallback of
# lean_search. Without it the prover has no local library search at all, and the
# failure is swallowed into an ordinary tool error rather than raised.
#
# bwrap is setuid root - bubblewrap's supported mode for hosts without usable
# unprivileged user namespaces.
FROM ubuntu:24.04
RUN apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      bubblewrap python3.12 ca-certificates git curl procps libcap2-bin \
      ripgrep \
 && rm -rf /var/lib/apt/lists/* \
 && chmod u+s /usr/bin/bwrap
