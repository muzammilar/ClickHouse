---
description: 'Guide to configuring and using SQL startup scripts in ClickHouse for
  automatic schema creation and migrations'
sidebar_label: 'Startup scripts'
slug: /operations/startup-scripts
title: 'Startup scripts'
---

# Startup scripts

ClickHouse can run arbitrary SQL queries from the server configuration during startup. This can be useful for migrations or automatic schema creation.

```xml
<clickhouse>
    <startup_scripts>
        <throw_on_error>false<throw_on_error>
        <scripts>
            <query>CREATE ROLE OR REPLACE test_role</query>
        </scripts>
        <scripts>
            <query>CREATE TABLE TestTable (id UInt64) ENGINE=TinyLog</query>
            <condition>SELECT 1;</condition>
        </scripts>
        <scripts>
            <query>CREATE DICTIONARY test_dict (...) SOURCE(CLICKHOUSE(...))</query>
            <user>default</user>
        </scripts>
    </startup_scripts>
</clickhouse>
```

ClickHouse executes all queries from the `startup_scripts` sequentially in the specified order. If any of the queries fail, the execution of the following queries won't be interrupted. However, if `throw_on_error` is set to true,
the server will not start if an error occurs during script execution.

You can specify a conditional query in the config. In that case, the corresponding query executes only when the condition query returns the value `1` or `true`.

:::note
If the condition query returns any other value than `1` or `true`, the result will be interpreted as `false`, and the corresponding won't be executed.
:::
