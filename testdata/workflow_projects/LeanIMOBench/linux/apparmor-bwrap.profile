# Grant /usr/bin/bwrap permission to create user namespaces.
#
# Ubuntu 24.04 sets kernel.apparmor_restrict_unprivileged_userns=1, so an
# unconfined process that creates a user namespace transitions into the
# /etc/apparmor.d/unprivileged_userns profile, which carries "audit deny
# capability" - hence bubblewrap's "setting up uid map: Permission denied".
#
# This profile does not confine bwrap; like the shipped rootlesskit, 1password
# and firefox profiles it exists only to name the binary and grant "userns",
# restoring pre-24.04 behaviour for this one root-owned binary rather than
# disabling the protection host-wide via sysctl.

abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,

  # Site-specific additions and overrides. See local/README for details.
  include if exists <local/bwrap>
}
