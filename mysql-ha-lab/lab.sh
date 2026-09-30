#!/usr/bin/env bash
# MySQL HA lab driver. Run ./lab.sh with no arguments for help.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
set -a; . ./.env; set +a

NODES="mysql1 mysql2 mysql3"

# ---------------------------------------------------------------- SQL ------

primary_sql() {
cat <<SQL
SET GLOBAL read_only = OFF;
-- Start the binlog from a clean slate so replicas only replay what we do here.
RESET MASTER;

CREATE USER IF NOT EXISTS 'repl'@'%' IDENTIFIED WITH mysql_native_password BY '${REPL_PASSWORD}';
GRANT REPLICATION SLAVE ON *.* TO 'repl'@'%';

CREATE USER IF NOT EXISTS 'orchestrator'@'%' IDENTIFIED WITH mysql_native_password BY '${ORC_PASSWORD}';
GRANT SUPER, PROCESS, REPLICATION SLAVE, REPLICATION CLIENT, RELOAD ON *.* TO 'orchestrator'@'%';
GRANT SELECT ON mysql.slave_master_info TO 'orchestrator'@'%';

CREATE USER IF NOT EXISTS 'monitor'@'%' IDENTIFIED WITH mysql_native_password BY '${MONITOR_PASSWORD}';
GRANT USAGE, REPLICATION CLIENT ON *.* TO 'monitor'@'%';

CREATE USER IF NOT EXISTS 'clone'@'%' IDENTIFIED WITH mysql_native_password BY '${CLONE_PASSWORD}';
GRANT BACKUP_ADMIN, CLONE_ADMIN ON *.* TO 'clone'@'%';

CREATE DATABASE IF NOT EXISTS lab;
CREATE USER IF NOT EXISTS 'app'@'%' IDENTIFIED WITH mysql_native_password BY '${APP_PASSWORD}';
GRANT SELECT, INSERT, UPDATE, DELETE ON lab.* TO 'app'@'%';

CREATE TABLE IF NOT EXISTS lab.heartbeat (
  id         BIGINT AUTO_INCREMENT PRIMARY KEY,
  written_by VARCHAR(32) NOT NULL,
  written_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
);
CREATE TABLE IF NOT EXISTS lab.orders (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  customer_id INT NOT NULL,
  status      VARCHAR(16) NOT NULL,
  amount      DECIMAL(10,2) NOT NULL,
  created_at  DATETIME NOT NULL
);
SET SESSION cte_max_recursion_depth = 10000;   -- default 1000 is too low for 5000 rows
INSERT INTO lab.orders (customer_id, status, amount, created_at)
WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 5000)
SELECT i % 500, ELT(1 + i % 4, 'new', 'paid', 'shipped', 'cancelled'), (i % 997) + 0.99,
       NOW() - INTERVAL (i % 365) DAY
FROM n;

-- Every node, the primary included, comes back READ-ONLY after a restart.
-- A restarted old primary must never start taking writes on its own (split brain);
-- becoming writable is always an explicit promotion.
SET PERSIST_ONLY super_read_only = ON;
SQL
}

# $1 = host the replica should follow, $2 = its port (default 3306)
replica_sql() {
cat <<SQL
STOP REPLICA;
RESET REPLICA ALL;
RESET MASTER;
CHANGE REPLICATION SOURCE TO
  SOURCE_HOST = '$1', SOURCE_PORT = ${2:-3306},
  SOURCE_USER = 'repl', SOURCE_PASSWORD = '${REPL_PASSWORD}',
  SOURCE_AUTO_POSITION = 1, SOURCE_CONNECT_RETRY = 1, SOURCE_RETRY_COUNT = 86400;
SET PERSIST super_read_only = ON;
START REPLICA;
SQL
}

# Re-point a node at a new source WITHOUT wiping its GTID history (used by rejoin).
repoint_sql() {
cat <<SQL
SET PERSIST super_read_only = ON;
STOP REPLICA;
CHANGE REPLICATION SOURCE TO
  SOURCE_HOST = '$1', SOURCE_PORT = ${2:-3306},
  SOURCE_USER = 'repl', SOURCE_PASSWORD = '${REPL_PASSWORD}',
  SOURCE_AUTO_POSITION = 1, SOURCE_CONNECT_RETRY = 1, SOURCE_RETRY_COUNT = 86400;
START REPLICA;
SQL
}

# Make a node the primary: stop replicating and accept writes (not persisted).
promote_sql() {
cat <<SQL
STOP REPLICA;
RESET REPLICA ALL;
SET GLOBAL read_only = OFF;   -- also switches super_read_only off
SQL
}

STATUS_SQL="SELECT @@hostname AS node,
  IF(@@global.read_only, 'replica', 'PRIMARY') AS role,
  @@global.super_read_only AS super_read_only,
  (SELECT COUNT(*) FROM lab.heartbeat) AS heartbeat_rows,
  @@global.gtid_executed AS gtid_executed\\G"

# The lines of SHOW REPLICA STATUS worth looking at.
REPLICA_FIELDS='Source_Host|Replica_IO_Running:|Replica_SQL_Running:|Seconds_Behind_Source|Last_IO_Error:|Last_SQL_Error:|Retrieved_Gtid_Set|Executed_Gtid_Set'

# ------------------------------------------------------------ helpers ------

dc() { docker compose "$@"; }

# Run SQL as root on a node:  echo "SELECT 1" | sql_on mysql1
# MYSQL_PWD keeps the password off the command line (and avoids the warning).
sql_on() { dc exec -T -e MYSQL_PWD="${ROOT_PASSWORD}" "$1" mysql -uroot --protocol=TCP -h127.0.0.1 -N -s; }
sql_on_verbose() { dc exec -T -e MYSQL_PWD="${ROOT_PASSWORD}" "$1" mysql -uroot --protocol=TCP -h127.0.0.1; }

running() { [ -n "$(dc ps -q --status running "$1" 2>/dev/null)" ]; }

wait_mysql() {
  printf "waiting for %s " "$1"
  for _ in $(seq 1 90); do
    if echo "SELECT 1" | sql_on "$1" >/dev/null 2>&1; then echo "ready"; return 0; fi
    printf "."; sleep 2
  done
  echo; echo "$1 did not come up; see: docker compose logs $1" >&2; return 1
}

current_primary() {
  for n in $NODES; do
    running "$n" || continue
    if [ "$(echo "SELECT @@global.read_only" | sql_on "$n")" = "0" ]; then echo "$n"; return 0; fi
  done
  return 1
}

orc_api() { curl -fsS "http://localhost:3000/api/$1"; }

# ----------------------------------------------------------- commands ------

cmd_up() {
  dc up -d mysql1 mysql2 mysql3
  for n in $NODES; do wait_mysql "$n"; done

  echo "configuring mysql1 as primary..."
  primary_sql | sql_on mysql1
  for n in mysql2 mysql3; do
    echo "configuring $n as replica of mysql1..."
    replica_sql mysql1 | sql_on "$n"
  done

  dc up -d proxysql orchestrator
  printf "waiting for orchestrator "
  for _ in $(seq 1 60); do orc_api "clusters" >/dev/null 2>&1 && break; printf "."; sleep 2; done; echo
  for n in $NODES; do orc_api "discover/$n/3306" >/dev/null 2>&1 || true; done
  sleep 3
  cmd_status
  cat <<EOF

Lab is up.
  Orchestrator UI : http://localhost:3000
  App endpoint    : ProxySQL on 127.0.0.1:6033  (user app / ${APP_PASSWORD})
  Next            : ./lab.sh app      (in a second terminal)
                    then open EXERCISES.md
EOF
}

cmd_status() {
  for n in $NODES; do
    if ! running "$n"; then echo "== $n: STOPPED"; continue; fi
    echo "== $n"
    echo "$STATUS_SQL" | sql_on_verbose "$n" | sed -n '2,$p' | sed 's/^ *//'
    echo "SHOW REPLICA STATUS\\G" | sql_on_verbose "$n" | grep -E "$REPLICA_FIELDS" | sed 's/^ *//' || true
  done
  if running proxysql; then
    echo "== proxysql (hostgroup 10 = writer, 20 = readers)"
    proxysql_admin "SELECT hostgroup_id AS hg, hostname, status FROM runtime_mysql_servers ORDER BY hostgroup_id, hostname;" || true
  fi
}

# Admin SQL against ProxySQL, run from whichever MySQL container is still up.
proxysql_admin() {
  for n in $NODES; do
    running "$n" || continue
    dc exec -T -e MYSQL_PWD=radmin "$n" mysql -h proxysql -P6032 -uradmin -t -e "$1"
    return 0
  done
  echo "(no MySQL container running to reach ProxySQL from)"
}

# Writes a heartbeat row through ProxySQL every second and reads from a replica.
# The write runs inside a transaction, so ProxySQL keeps the whole thing on the
# writer and @@hostname shows which node is currently the primary.
cmd_app() {
  local runner=""
  for n in $NODES; do running "$n" && runner="$n" && break; done
  [ -n "$runner" ] || { echo "no MySQL container running"; exit 1; }
  echo "Writing through ProxySQL every second (Ctrl+C to stop)."
  echo "In another terminal run ./lab.sh kill-primary and watch the writer change."
  dc exec -T -e MYSQL_PWD="${APP_PASSWORD}" "$runner" bash -c '
    q() { mysql -h proxysql -P6033 -uapp -N -s lab -e "$1" 2>/dev/null; }
    while true; do
      w=$(q "START TRANSACTION; INSERT INTO heartbeat (written_by) SELECT @@hostname; SELECT @@hostname; COMMIT;")
      r=$(q "SELECT @@hostname")
      echo "$(date +%T)  write -> ${w:-FAILED}   read -> ${r:-FAILED}"
      sleep 1
    done'
}

cmd_kill_primary() {
  local p; p=$(current_primary) || { echo "no primary found"; exit 1; }
  echo "Stopping $p (the current primary). Orchestrator should promote a replica within ~10-20s."
  dc stop "$p"
}

cmd_rejoin() {
  local node="${1:?usage: ./lab.sh rejoin mysqlN}" p
  running "$node" || dc start "$node"
  wait_mysql "$node"
  p=$(current_primary | grep -v "^$node$" || true)
  [ -n "$p" ] || { echo "can't find a primary other than $node"; exit 1; }
  echo "Making $node a replica of $p (keeping its GTID history)..."
  repoint_sql "$p" | sql_on "$node"
  sleep 2
  echo "SHOW REPLICA STATUS\\G" | sql_on_verbose "$node" | grep -E "$REPLICA_FIELDS" | sed 's/^ *//' || true
  orc_api "discover/$node/3306" >/dev/null 2>&1 || true
}

# Manual failover: promote a node and point every other running node at it.
cmd_promote() {
  local node="${1:?usage: ./lab.sh promote mysqlN}"
  running "$node" || { echo "$node is not running"; exit 1; }
  echo "Promoting $node..."
  promote_sql | sql_on "$node"
  for n in $NODES; do
    [ "$n" = "$node" ] && continue
    running "$n" || continue
    echo "Pointing $n at $node..."
    repoint_sql "$node" | sql_on "$n"
  done
  sleep 2; cmd_status
}

# Transactions a replica has that the primary doesn't (they block safe promotion).
cmd_errant() {
  local p pset; p=$(current_primary) || { echo "no primary found"; exit 1; }
  pset=$(echo "SELECT @@global.gtid_executed" | sql_on "$p" | tr -d '\n')
  echo "primary $p: $pset"
  for n in $NODES; do
    [ "$n" = "$p" ] && continue
    running "$n" || continue
    echo "$n errant: $(echo "SELECT IFNULL(NULLIF(GTID_SUBTRACT(@@global.gtid_executed, '$pset'), ''), 'none')" | sql_on "$n")"
  done
}

cmd_sql() { local n="${1:-$(current_primary)}"; dc exec -e MYSQL_PWD="${ROOT_PASSWORD}" "$n" mysql -uroot lab; }
cmd_proxysql() { proxysql_admin "${1:-SELECT hostgroup_id AS hg, hostname, status FROM runtime_mysql_servers;}"; }
cmd_events() { dc exec -T orchestrator cat /tmp/recovery.log 2>/dev/null || echo "(no failovers yet)"; }
cmd_down() { dc down -v; }
cmd_reset() { cmd_down; cmd_up; }

usage() {
  cat <<EOF
MySQL HA lab: 3 MySQL nodes (GTID), ProxySQL, Orchestrator.

  ./lab.sh up              start everything and set up replication
  ./lab.sh status          roles, replication health, ProxySQL routing
  ./lab.sh app             write/read through ProxySQL every second
  ./lab.sh sql [mysqlN]    MySQL shell on a node (default: current primary)
  ./lab.sh proxysql [SQL]  query the ProxySQL admin interface
  ./lab.sh kill-primary    stop the current primary to trigger a failover
  ./lab.sh rejoin mysqlN   bring a node back as a replica of the current primary
  ./lab.sh promote mysqlN  manual failover: make mysqlN the primary
  ./lab.sh errant          GTIDs a replica has that the primary doesn't
  ./lab.sh events          Orchestrator's failover log
  ./lab.sh down            stop and delete everything
  ./lab.sh reset           down + up (fresh lab)
EOF
}

main() {
  local cmd="${1:-help}"; shift || true
  case "$cmd" in
    up) cmd_up ;; status) cmd_status ;; app) cmd_app ;; sql) cmd_sql "$@" ;;
    proxysql) cmd_proxysql "$@" ;; kill-primary) cmd_kill_primary ;;
    rejoin) cmd_rejoin "$@" ;; promote) cmd_promote "$@" ;; events) cmd_events ;; errant) cmd_errant ;; down) cmd_down ;; reset) cmd_reset ;;
    *) usage ;;
  esac
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
