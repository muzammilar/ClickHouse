#!/usr/bin/env bash

CURDIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../shell_config.sh
. "$CURDIR"/../shell_config.sh

QUERY_ID=$RANDOM
$CLICKHOUSE_BENCHMARK <<< "SELECT 1" --query_id $QUERY_ID -i 10 2>/dev/null
$CLICKHOUSE_CLIENT -q "SYSTEM FLUSH LOGS query_log"
$CLICKHOUSE_CLIENT -q "SELECT count() FROM system.query_log WHERE current_database = currentDatabase() AND query_id='$QUERY_ID'"
