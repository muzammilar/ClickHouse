---
description: 'Learn how to add a custom partitioning key to MergeTree tables.'
sidebar_label: 'Custom Partitioning Key'
sidebar_position: 30
slug: /engines/table-engines/mergetree-family/custom-partitioning-key
title: 'Custom Partitioning Key'
---

# Custom partitioning key

:::note
In most cases you do not need a partition key, and in most other cases you do not need a partition key more granular than by months.

You should never use too granular of partitioning. Don't partition your data by client identifiers or names. Instead, make a client identifier or name the first column in the ORDER BY expression.
:::

Partitioning is available for the [MergeTree family tables](../../../engines/table-engines/mergetree-family/mergetree.md), including [replicated tables](../../../engines/table-engines/mergetree-family/replication.md) and [materialized views](/sql-reference/statements/create/view#materialized-view).

A partition is a logical combination of records in a table by a specified criterion. You can set a partition by an arbitrary criterion, such as by month, by day, or by event type. Each partition is stored separately to simplify manipulations of this data. When accessing the data, ClickHouse uses the smallest subset of partitions possible. Partitions improve performance for queries containing a partitioning key because ClickHouse will filter for that partition before selecting the parts and granules within the partition.

The partition is specified in the `PARTITION BY expr` clause when [creating a table](../../../engines/table-engines/mergetree-family/mergetree.md#table_engine-mergetree-creating-a-table). The partition key can be any expression from the table columns. For example, to specify partitioning by month, use the expression `toYYYYMM(date_column)`:

```sql
CREATE TABLE visits
(
    VisitDate Date,
    Hour UInt8,
    ClientID UUID
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(VisitDate)
ORDER BY Hour;
```

The partition key can also be a tuple of expressions (similar to the [primary key](../../../engines/table-engines/mergetree-family/mergetree.md#primary-keys-and-indexes-in-queries)). For example:

```sql
ENGINE = ReplicatedCollapsingMergeTree('/clickhouse/tables/name', 'replica1', Sign)
PARTITION BY (toMonday(StartDate), EventType)
ORDER BY (CounterID, StartDate, intHash32(UserID));
```

In this example, we set partitioning by the event types that occurred during the current week.

By default, the floating-point partition key is not supported. To use it enable the setting [allow_floating_point_partition_key](../../../operations/settings/merge-tree-settings.md#allow_floating_point_partition_key).

When inserting new data to a table, this data is stored as a separate part (chunk) sorted by the primary key. In 10-15 minutes after inserting, the parts of the same partition are merged into the entire part.

:::info
A merge only works for data parts that have the same value for the partitioning expression. This means **you shouldn't make overly granular partitions** (more than about a thousand partitions). Otherwise, the `SELECT` query performs poorly because of an unreasonably large number of files in the file system and open file descriptors.
:::

Use the [system.parts](../../../operations/system-tables/parts.md) table to view the table parts and partitions. For example, let's assume that we have a `visits` table with partitioning by month. Let's perform the `SELECT` query for the `system.parts` table:

```sql
SELECT
    partition,
    name,
    active
FROM system.parts
WHERE table = 'visits'
```

```text
┌─partition─┬─name──────────────┬─active─┐
│ 201901    │ 201901_1_3_1      │      0 │
│ 201901    │ 201901_1_9_2_11   │      1 │
│ 201901    │ 201901_8_8_0      │      0 │
│ 201901    │ 201901_9_9_0      │      0 │
│ 201902    │ 201902_4_6_1_11   │      1 │
│ 201902    │ 201902_10_10_0_11 │      1 │
│ 201902    │ 201902_11_11_0_11 │      1 │
└───────────┴───────────────────┴────────┘
```

The `partition` column contains the names of the partitions. There are two partitions in this example: `201901` and `201902`. You can use this column value to specify the partition name in [ALTER ... PARTITION](../../../sql-reference/statements/alter/partition.md) queries.

The `name` column contains the names of the partition data parts. You can use this column to specify the name of the part in the [ALTER ATTACH PART](/sql-reference/statements/alter/partition#attach-partitionpart) query.

Let's break down the name of the part: `201901_1_9_2_11`:

- `201901` is the partition name.
- `1` is the minimum number of the data block.
- `9` is the maximum number of the data block.
- `2` is the chunk level (the depth of the merge tree it is formed from).
- `11` is the mutation version (if a part mutated)

:::info
The parts of old-type tables have the name: `20190117_20190123_2_2_0` (minimum date - maximum date - minimum block number - maximum block number - level).
:::

The `active` column shows the status of the part. `1` is active; `0` is inactive. The inactive parts are, for example, source parts remaining after merging to a larger part. The corrupted data parts are also indicated as inactive.

As you can see in the example, there are several separated parts of the same partition (for example, `201901_1_3_1` and `201901_1_9_2`). This means that these parts are not merged yet. ClickHouse merges the inserted parts of data periodically, approximately 15 minutes after inserting. In addition, you can perform a non-scheduled merge using the [OPTIMIZE](../../../sql-reference/statements/optimize.md) query. Example:

```sql
OPTIMIZE TABLE visits PARTITION 201902;
```

```text
┌─partition─┬─name─────────────┬─active─┐
│ 201901    │ 201901_1_3_1     │      0 │
│ 201901    │ 201901_1_9_2_11  │      1 │
│ 201901    │ 201901_8_8_0     │      0 │
│ 201901    │ 201901_9_9_0     │      0 │
│ 201902    │ 201902_4_6_1     │      0 │
│ 201902    │ 201902_4_11_2_11 │      1 │
│ 201902    │ 201902_10_10_0   │      0 │
│ 201902    │ 201902_11_11_0   │      0 │
└───────────┴──────────────────┴────────┘
```

Inactive parts will be deleted approximately 10 minutes after merging.

Another way to view a set of parts and partitions is to go into the directory of the table: `/var/lib/clickhouse/data/<database>/<table>/`. For example:

```bash
/var/lib/clickhouse/data/default/visits$ ls -l
total 40
drwxr-xr-x 2 clickhouse clickhouse 4096 Feb  1 16:48 201901_1_3_1
drwxr-xr-x 2 clickhouse clickhouse 4096 Feb  5 16:17 201901_1_9_2_11
drwxr-xr-x 2 clickhouse clickhouse 4096 Feb  5 15:52 201901_8_8_0
drwxr-xr-x 2 clickhouse clickhouse 4096 Feb  5 15:52 201901_9_9_0
drwxr-xr-x 2 clickhouse clickhouse 4096 Feb  5 16:17 201902_10_10_0
drwxr-xr-x 2 clickhouse clickhouse 4096 Feb  5 16:17 201902_11_11_0
drwxr-xr-x 2 clickhouse clickhouse 4096 Feb  5 16:19 201902_4_11_2_11
drwxr-xr-x 2 clickhouse clickhouse 4096 Feb  5 12:09 201902_4_6_1
drwxr-xr-x 2 clickhouse clickhouse 4096 Feb  1 16:48 detached
```

The folders '201901_1_1_0', '201901_1_7_1' and so on are the directories of the parts. Each part relates to a corresponding partition and contains data just for a certain month (the table in this example has partitioning by month).

The `detached` directory contains parts that were detached from the table using the [DETACH](/sql-reference/statements/detach) query. The corrupted parts are also moved to this directory, instead of being deleted. The server does not use the parts from the `detached` directory. You can add, delete, or modify the data in this directory at any time – the server will not know about this until you run the [ATTACH](/sql-reference/statements/alter/partition#attach-partitionpart) query.

Note that on the operating server, you cannot manually change the set of parts or their data on the file system, since the server will not know about it. For non-replicated tables, you can do this when the server is stopped, but it isn't recommended. For replicated tables, the set of parts cannot be changed in any case.

ClickHouse allows you to perform operations with the partitions: delete them, copy from one table to another, or create a backup. See the list of all operations in the section [Manipulations With Partitions and Parts](/sql-reference/statements/alter/partition).

## Group By optimisation using partition key {#group-by-optimisation-using-partition-key}

For some combinations of table's partition key and query's group by key it might be possible to execute aggregation for each partition independently.
Then we'll not have to merge partially aggregated data from all execution threads at the end,
because we provided with the guarantee that each group by key value cannot appear in working sets of two different threads.

The typical example is:

```sql
CREATE TABLE session_log
(
    UserID UInt64,
    SessionID UUID
)
ENGINE = MergeTree
PARTITION BY sipHash64(UserID) % 16
ORDER BY tuple();

SELECT
    UserID,
    COUNT()
FROM session_log
GROUP BY UserID;
```

:::note
Performance of such a query heavily depends on the table layout. Because of that the optimisation is not enabled by default.
:::

The key factors for a good performance:

- number of partitions involved in the query should be sufficiently large (more than `max_threads / 2`), otherwise query will under-utilize the machine
- partitions shouldn't be too small, so batch processing won't degenerate into row-by-row processing
- partitions should be comparable in size, so all threads will do roughly the same amount of work

:::info
It's recommended to apply some hash function to columns in `partition by` clause in order to distribute data evenly between partitions.
:::

Relevant settings are:

- `allow_aggregate_partitions_independently` - controls if the use of optimisation is enabled
- `force_aggregate_partitions_independently` - forces its use when it's applicable from the correctness standpoint, but getting disabled by internal logic that estimates its expediency
- `max_number_of_partitions_for_independent_aggregation` - hard limit on the maximal number of partitions table could have
