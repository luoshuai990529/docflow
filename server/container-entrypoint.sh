#!/bin/sh
set -eu
umask 077

service_uid=10001
service_gid=10001
state_root=/var/lib/autofill/workflow
state_dir=${AUTOFILL_STATE_DIR:-$state_root/state}
queue_dir=${AUTOFILL_PROCESS_QUEUE:-$state_root/processor-jobs}
results_dir=${AUTOFILL_RESULTS_DIR:-/var/lib/autofill/results}
secrets_dir=${AUTOFILL_SECRET_DIR:-/var/lib/autofill/secrets}

mkdir -p "$state_root" "$state_dir" "$queue_dir" "$results_dir" "$secrets_dir"
chown -R "$service_uid:$service_gid" "$state_root" "$results_dir" "$secrets_dir"
chmod 0700 "$state_root" "$state_dir" "$queue_dir" "$secrets_dir"
chmod 0750 "$results_dir"

exec gosu "$service_uid:$service_gid" "$@"
