# MySQL HA Lab

A free, local version of the setup that "MySQL DBA" and "Database Reliability Engineer"
job postings keep asking for: **GTID replication, ProxySQL read/write splitting, and
automatic failover with Orchestrator.** It runs in Docker on your laptop, so you can break it
and fix it as often as you like.

```
                 your app / ./lab.sh app
                          |
                   ProxySQL :6033           writes -> hostgroup 10 (primary)
                   /      |      \          reads  -> hostgroup 20 (replicas)
             mysql1    mysql2    mysql3     GTID replication, CLONE plugin
            (primary) (replica) (replica)
                   \      |      /
                  Orchestrator :3000        detects a dead primary, promotes a replica
```

ProxySQL decides which node is the writer by watching `@@read_only`. When Orchestrator
promotes a replica (sets `read_only=0`), ProxySQL moves it into the writer group by
itself. No scripts are needed between the two, and it's worth explaining that in interviews.

## What you need

* **Docker Desktop** (free for personal use): https://www.docker.com/products/docker-desktop/
  Give it about 3 GB of memory (Settings → Resources).
* On Apple Silicon Macs, ProxySQL and Orchestrator run as Intel images under emulation.
  That's slower to start but works.

## Start

```bash
cd mysql-ha-lab
./lab.sh up          # first run downloads images (a few minutes)
./lab.sh status      # who is primary, replication health, ProxySQL routing
```

Open **http://localhost:3000** to see the topology in Orchestrator.

Then work through **[EXERCISES.md](EXERCISES.md)**.

## Commands

| Command | What it does |
|---|---|
| `./lab.sh up` | start everything, set up replication, register nodes with Orchestrator |
| `./lab.sh status` | role, read-only state, GTIDs, replication threads/errors, ProxySQL routing |
| `./lab.sh app` | write + read through ProxySQL every second (run in its own terminal) |
| `./lab.sh sql [mysqlN]` | MySQL shell on a node (default: the current primary) |
| `./lab.sh proxysql ["SQL"]` | query ProxySQL's admin interface |
| `./lab.sh kill-primary` | stop the primary to trigger an automatic failover |
| `./lab.sh promote mysqlN` | manual failover: make mysqlN primary, repoint the others |
| `./lab.sh rejoin mysqlN` | bring a node back as a replica of the current primary |
| `./lab.sh errant` | errant GTIDs on each replica (transactions the primary doesn't have) |
| `./lab.sh events` | Orchestrator's detection/recovery log |
| `./lab.sh down` / `reset` | delete everything / start over fresh |

## Design choices (good interview talking points)

* **Every node restarts read-only**, the primary included (`SET PERSIST super_read_only`).
  A crashed primary that comes back must not start accepting writes next to the new
  primary, because that's split brain. Making a node writable is always an explicit promotion.
  After restarting the whole lab, run `./lab.sh promote mysql1` (or whichever node).
* **GTID with auto-positioning**, so any replica can be pointed at any new primary
  without binlog file/position arithmetic.
* **CLONE plugin loaded on every node**, so a broken replica can be rebuilt from a healthy
  one with a single SQL statement.
* Passwords are in `.env` and are for this lab only.

## What was tested

The MySQL side (replication setup and every SQL exercise in EXERCISES.md) was run
end-to-end against three real MySQL 8.0 servers. That covered replication breaking and repair, errant GTIDs,
CLONE rebuilds, delayed replicas, manual failover and rejoin, restart safety and index
tuning. The ProxySQL and Orchestrator containers could not be run in that test
environment. If either misbehaves, `docker compose logs proxysql` / `docker compose logs
orchestrator` will usually say why. If an image tag isn't found, pin a version in `.env`.
