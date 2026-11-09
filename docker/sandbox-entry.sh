#!/bin/bash
# Seed the writable tmpfs working directory, then run the untrusted command.
#
# $1 is the whole command line, exactly as the agent produced it. It is passed to
# `bash -c` rather than exec'd directly, because the agent legitimately produces
# pipelines and `&&` chains.
set -o pipefail

cp -a /seed/. /work/ 2>/dev/null || true
cd /work || exit 1

exec bash -c "$1"
