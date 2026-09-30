# Exercises

Do them in order. If the lab gets into a state you don't understand, `./lab.sh reset`
gives you a fresh one in about a minute.

Commands starting with `./lab.sh` go in your Mac's Terminal. SQL blocks go inside a MySQL
shell opened with `./lab.sh sql mysqlN`. Each exercise ends with the interview question it
prepares you for.

---

## 1. Tour the topology

```bash
./lab.sh status
```
For each node, look at `role`, `super_read_only`, `gtid_executed` and, on replicas,
`Replica_IO_Running` / `Replica_SQL_Running` / `Seconds_Behind_Source`. Open
http://localhost:3000 and find the same three nodes in Orchestrator.

> **Interview:** *"How do you check that replication is healthy?"* Both threads are `Yes`, there are no
> `Last_*_Error`, lag is near 0, and the replica's `Executed_Gtid_Set` keeps up with the primary.

---

## 2. Read/write splitting with ProxySQL

In a **second terminal**:
```bash
./lab.sh app
```
Writes go to `mysql1`, and reads alternate between `mysql2` and `mysql3`. See which queries went where:
```bash
./lab.sh proxysql "SELECT hostgroup, count_star, digest_text FROM stats_mysql_query_digest ORDER BY count_star DESC LIMIT 5"
```

> **Interview:** *"How would you send reads to replicas?"* ProxySQL query rules route
> `^SELECT` to the reader hostgroup. `SELECT ... FOR UPDATE` and anything inside a
> transaction stay on the writer.

---

## 3. Automatic failover

Keep `./lab.sh app` running. In another terminal:
```bash
./lab.sh kill-primary
```
Watch the app terminal: writes fail for a few seconds, then continue on a **new** host.
```bash
./lab.sh status     # which replica was promoted? is the other one following it?
./lab.sh events     # what Orchestrator detected and did
```

> **Interview:** *"Walk me through a failover."* Orchestrator sees the primary is unreachable
> *and* the replicas agree (DeadMaster). It picks the most up-to-date replica, repoints the
> other replica to it using GTID, and sets `read_only=0` on the new primary. ProxySQL sees the
> `read_only` change and moves it into the writer hostgroup. The seconds of failed writes come from
> detection time plus recovery time plus ProxySQL's `read_only` check interval.

---

## 4. Bring the old primary back

```bash
./lab.sh rejoin mysql1
./lab.sh status
```
`mysql1` comes back **read-only** (every node does after a restart) and replicates from
the new primary.

> **Interview:** *"The old primary is back. Can it just rejoin?"* Only if it has no transactions the new
> primary lacks (errant GTIDs, see exercise 6). Otherwise rebuild it (exercise 7).

---

## 5. Break replication, then fix it

Someone writes directly on a replica (it happens):
```bash
./lab.sh sql mysql2
```
```sql
SET GLOBAL super_read_only = OFF;
INSERT INTO lab.heartbeat (id, written_by) VALUES (1000000, 'oops-on-replica');
SET GLOBAL super_read_only = ON;
exit
```
Now the same id arrives from the primary (`./lab.sh sql` opens the current primary):
```sql
INSERT INTO lab.heartbeat (id, written_by) VALUES (1000000, 'primary');
```
`./lab.sh status` shows `Replica_SQL_Running: No` and error **1062** on mysql2.

Find the exact transaction that failed, on mysql2:
```sql
SELECT LAST_ERROR_NUMBER,
       REGEXP_SUBSTR(LAST_ERROR_MESSAGE, '[0-9a-f-]{36}:[0-9]+') AS failed_gtid
FROM performance_schema.replication_applier_status_by_worker
WHERE LAST_ERROR_NUMBER <> 0;
```
Skip it by committing an **empty transaction with that GTID** (paste your `failed_gtid`):
```sql
STOP REPLICA;
SET GTID_NEXT = 'paste-failed_gtid-here';
BEGIN; COMMIT;
SET GTID_NEXT = 'AUTOMATIC';
START REPLICA;
```
Replication runs again, but compare the row on both nodes:
```sql
SELECT * FROM lab.heartbeat WHERE id = 1000000;   -- run on mysql2, then on the primary
```
They're **different**. Skipping hides the problem; it doesn't fix the data.

> **Interview:** *"How do you skip a transaction with GTID?"* You can't use
> `sql_slave_skip_counter`; inject an empty transaction with the failing GTID. Then say
> that skipping causes data drift, and that you'd verify with pt-table-checksum or rebuild the replica.

---

## 6. Find errant GTIDs

```bash
./lab.sh errant
```
mysql2 now has a transaction **only it** has (the direct write). Under the hood that's:
```sql
SELECT GTID_SUBTRACT(@@global.gtid_executed, '<primary gtid_executed>');
```

> **Interview:** *"What's an errant GTID and why does it matter?"* It's a transaction that exists
> on a replica but not the primary. If that replica gets promoted, the other servers either fetch that
> transaction or fail, and Orchestrator avoids promoting such a replica. The fix is to
> commit empty transactions with those GTIDs on the primary (only if the data is truly
> harmless), or rebuild the replica.

---

## 7. Rebuild the broken replica with CLONE

Rebuild mysql2 from mysql3 (a healthy replica, so the primary isn't loaded):
```bash
./lab.sh sql mysql2
```
```sql
STOP REPLICA;
SET GLOBAL super_read_only = OFF;   -- CLONE refuses to run while super_read_only is ON
SET GLOBAL clone_valid_donor_list = 'mysql3:3306';
CLONE INSTANCE FROM 'clone'@'mysql3':3306 IDENTIFIED BY 'clonepass';
```
You'll get **ERROR 3707 "Restart server failed"**. That's expected: the clone is done and
mysqld shuts down to switch to the new data. Docker's restart policy brings the container
back after a few seconds. Then:
```bash
./lab.sh rejoin mysql2
./lab.sh errant                     # mysql2: none
```
Check the row from exercise 5; it matches the primary again.

> **Interview:** *"How do you rebuild a replica?"* Use CLONE on 8.0.17+ (one statement, copies
> data and GTID state), or XtraBackup plus `CHANGE REPLICATION SOURCE ... SOURCE_AUTO_POSITION=1`.
> Mention the `super_read_only` and restart gotchas.

---

## 8. Replication lag, and ProxySQL taking a lagging replica out

Keep `./lab.sh app` running. Make mysql3 deliberately 30 seconds behind:
```bash
./lab.sh sql mysql3
```
```sql
STOP REPLICA;
CHANGE REPLICATION SOURCE TO SOURCE_DELAY = 30;
START REPLICA;
SHOW REPLICA STATUS\G   -- SQL_Delay, SQL_Remaining_Delay, Seconds_Behind_Source
```
After ~15 seconds:
```bash
./lab.sh proxysql "SELECT hostgroup_id, hostname, status FROM runtime_mysql_servers"
```
mysql3 shows **SHUNNED**. It's more than 10 seconds behind (`max_replication_lag` in
`conf/proxysql.cnf`), so reads stop going to it. Undo it:
```sql
STOP REPLICA; CHANGE REPLICATION SOURCE TO SOURCE_DELAY = 0; START REPLICA;
```

> **Interview:** *"A replica is lagging. What do you do?"* Take it out of the read pool (ProxySQL
> does this automatically), then find the cause: long transactions on the primary, missing primary
> keys, a single-threaded applier (`replica_parallel_workers`), or disk I/O. A delayed replica is
> also a real protection against an accidental `DROP TABLE`.

---

## 9. Slow query → index

On the primary (`./lab.sh sql`):
```sql
EXPLAIN FORMAT=TREE SELECT * FROM lab.orders WHERE customer_id = 42 AND status = 'paid';
-- "Table scan on orders"
ALTER TABLE lab.orders ADD INDEX idx_customer_status (customer_id, status);
EXPLAIN FORMAT=TREE SELECT * FROM lab.orders WHERE customer_id = 42 AND status = 'paid';
-- "Index lookup on orders using idx_customer_status"
```
The `ALTER` replicated, so the index exists on the replicas too. The slow query log is on
(`long_query_time = 0.5`):
```bash
docker compose exec mysql1 tail -20 /var/lib/mysql/mysql1-slow.log
```

> **Interview:** *"How do you find and fix slow queries?"* Use the slow log with pt-query-digest,
> then EXPLAIN / EXPLAIN ANALYZE, then an index on the equality columns first. On a big table in
> production, use gh-ost or pt-online-schema-change instead of a plain `ALTER`.

---

## 10. Manual failover without Orchestrator

For when the automation itself is down:
```bash
./lab.sh promote mysql3
./lab.sh status
```
`promote` does exactly what you'd do by hand:
```sql
-- on the new primary
STOP REPLICA; RESET REPLICA ALL; SET GLOBAL read_only = OFF;
-- on every other node
CHANGE REPLICATION SOURCE TO SOURCE_HOST='mysql3', SOURCE_AUTO_POSITION=1, ...; START REPLICA;
```

> **Interview:** *"Orchestrator is down and the primary died. What do you do?"* Pick the replica with
> the most complete `gtid_executed` and no errant GTIDs, promote it, and repoint the others with
> auto-position. Make sure the old primary can't come back writable (fence it, or make it
> start read-only as this lab does), then update ProxySQL or DNS if nothing does it automatically.

---

## After the lab

Add it to your resume and GitHub:
*"Built a MySQL 8.0 HA lab (GTID replication, ProxySQL read/write split, Orchestrator
auto-failover); practised failover, errant-GTID repair, CLONE-based replica rebuilds and
lag handling."* Every exercise above gives you a concrete story to tell in an interview.
