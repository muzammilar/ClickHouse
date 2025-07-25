import glob
import json
import logging
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone

import pyspark
import pytest
from azure.storage.blob import BlobServiceClient
from minio.deleteobjects import DeleteObject
from pyspark.sql.functions import (
    current_timestamp,
    monotonically_increasing_id,
    row_number,
)
from pyspark.sql.readwriter import DataFrameWriter, DataFrameWriterV2
from pyspark.sql.types import (
    ArrayType,
    BooleanType,
    DateType,
    IntegerType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)
from pyspark.sql.window import Window

import helpers.client
from helpers.cluster import ClickHouseCluster, ClickHouseInstance, is_arm
from helpers.s3_tools import (
    AzureUploader,
    LocalUploader,
    S3Uploader,
    LocalDownloader,
    get_file_contents,
    list_s3_objects,
    prepare_s3_bucket,
)
from helpers.test_tools import TSV

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))


def get_spark():
    builder = (
        pyspark.sql.SparkSession.builder.appName("spark_test")
        .config(
            "spark.sql.catalog.spark_catalog",
            "org.apache.iceberg.spark.SparkSessionCatalog",
        )
        .config("spark.sql.catalog.local", "org.apache.iceberg.spark.SparkCatalog")
        .config("spark.sql.catalog.spark_catalog.type", "hadoop")
        .config("spark.sql.catalog.spark_catalog.warehouse", "/iceberg_data")
        .config(
            "spark.sql.extensions",
            "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        )
        .master("local")
    )
    return builder.master("local").getOrCreate()


@pytest.fixture(scope="module")
def started_cluster():
    try:
        cluster = ClickHouseCluster(__file__, with_spark=True)
        cluster.add_instance(
            "node1",
            main_configs=[
                "configs/config.d/query_log.xml",
                "configs/config.d/cluster.xml",
                "configs/config.d/named_collections.xml",
                "configs/config.d/filesystem_caches.xml",
            ],
            user_configs=["configs/users.d/users.xml"],
            with_minio=True,
            with_azurite=True,
            stay_alive=True,
        )
        cluster.add_instance(
            "node2",
            main_configs=[
                "configs/config.d/query_log.xml",
                "configs/config.d/cluster.xml",
                "configs/config.d/named_collections.xml",
                "configs/config.d/filesystem_caches.xml",
            ],
            user_configs=["configs/users.d/users.xml"],
            stay_alive=True,
        )
        cluster.add_instance(
            "node3",
            main_configs=[
                "configs/config.d/query_log.xml",
                "configs/config.d/cluster.xml",
                "configs/config.d/named_collections.xml",
                "configs/config.d/filesystem_caches.xml",
            ],
            user_configs=["configs/users.d/users.xml"],
            stay_alive=True,
        )

        logging.info("Starting cluster...")
        cluster.start()

        prepare_s3_bucket(cluster)
        logging.info("S3 bucket created")

        cluster.spark_session = get_spark()
        cluster.default_s3_uploader = S3Uploader(
            cluster.minio_client, cluster.minio_bucket
        )

        cluster.azure_container_name = "mycontainer"

        cluster.blob_service_client = cluster.blob_service_client

        container_client = cluster.blob_service_client.create_container(
            cluster.azure_container_name
        )

        cluster.container_client = container_client

        cluster.default_azure_uploader = AzureUploader(
            cluster.blob_service_client, cluster.azure_container_name
        )

        cluster.default_local_uploader = LocalUploader(cluster.instances["node1"])
        cluster.default_local_downloader = LocalDownloader(cluster.instances["node1"])

        yield cluster

    finally:
        cluster.shutdown()


def run_query(instance, query, stdin=None, settings=None):
    # type: (ClickHouseInstance, str, object, dict) -> str

    logging.info("Running query '{}'...".format(query))
    result = instance.query(query, stdin=stdin, settings=settings)
    logging.info("Query finished")

    return result


def write_iceberg_from_file(
    spark, path, table_name, mode="overwrite", format_version="1", partition_by=None
):
    if mode == "overwrite":
        if partition_by is None:
            spark.read.load(f"file://{path}").writeTo(table_name).tableProperty(
                "format-version", format_version
            ).using("iceberg").create()
        else:
            spark.read.load(f"file://{path}").writeTo(table_name).partitionedBy(
                partition_by
            ).tableProperty("format-version", format_version).using("iceberg").create()
    else:
        spark.read.load(f"file://{path}").writeTo(table_name).append()


def write_iceberg_from_df(
    spark, df, table_name, mode="overwrite", format_version="1", partition_by=None
):
    if mode == "overwrite":
        if partition_by is None:
            df.writeTo(table_name).tableProperty(
                "format-version", format_version
            ).using("iceberg").create()
        else:
            df.writeTo(table_name).tableProperty(
                "format-version", format_version
            ).partitionedBy(partition_by).using("iceberg").create()
    else:
        df.writeTo(table_name).append()


def generate_data(spark, start, end):
    a = spark.range(start, end, 1).toDF("a")
    b = spark.range(start + 1, end + 1, 1).toDF("b")
    b = b.withColumn("b", b["b"].cast(StringType()))

    a = a.withColumn(
        "row_index", row_number().over(Window.orderBy(monotonically_increasing_id()))
    )
    b = b.withColumn(
        "row_index", row_number().over(Window.orderBy(monotonically_increasing_id()))
    )

    df = a.join(b, on=["row_index"]).drop("row_index")
    return df


def get_creation_expression(
    storage_type,
    table_name,
    cluster,
    schema="",
    format_version=2,
    partition_by="",
    if_not_exists=False,
    format="Parquet",
    table_function=False,
    allow_dynamic_metadata_for_data_lakes=False,
    use_version_hint=False,
    run_on_cluster=False,
    explicit_metadata_path="",
    **kwargs,
):
    settings_array = []
    if allow_dynamic_metadata_for_data_lakes:
        settings_array.append("allow_dynamic_metadata_for_data_lakes = 1")

    if explicit_metadata_path:
        settings_array.append(f"iceberg_metadata_file_path = '{explicit_metadata_path}'")

    if use_version_hint:
        settings_array.append("iceberg_use_version_hint = true")

    if partition_by:
        partition_by = "PARTITION BY " + partition_by
    settings_array.append(f"iceberg_format_version = {format_version}")

    if settings_array:
        settings_expression = " SETTINGS " + ",".join(settings_array)
    else:
        settings_expression = ""

    if_not_exists_prefix = ""
    if if_not_exists:
        if_not_exists_prefix = "IF NOT EXISTS"        

    if storage_type == "s3":
        if "bucket" in kwargs:
            bucket = kwargs["bucket"]
        else:
            bucket = cluster.minio_bucket

        if run_on_cluster:
            assert table_function
            return f"icebergS3Cluster('cluster_simple', s3, filename = 'iceberg_data/default/{table_name}/', format={format}, url = 'http://minio1:9001/{bucket}/')"
        else:
            if table_function:
                return f"icebergS3(s3, filename = 'iceberg_data/default/{table_name}/', format={format}, url = 'http://minio1:9001/{bucket}/')"
            else:
                return (
                    f"""
                    DROP TABLE IF EXISTS {table_name};
                    CREATE TABLE {if_not_exists_prefix} {table_name} {schema}
                    ENGINE=IcebergS3(s3, filename = 'iceberg_data/default/{table_name}/', format={format}, url = 'http://minio1:9001/{bucket}/')
                    {partition_by}
                    {settings_expression}
                    """
                )

    elif storage_type == "azure":
        if run_on_cluster:
            assert table_function
            return f"""
                icebergAzureCluster('cluster_simple', azure, container = '{cluster.azure_container_name}', storage_account_url = '{cluster.env_variables["AZURITE_STORAGE_ACCOUNT_URL"]}', blob_path = '/iceberg_data/default/{table_name}/', format={format})
            """
        else:
            if table_function:
                return f"""
                    icebergAzure(azure, container = '{cluster.azure_container_name}', storage_account_url = '{cluster.env_variables["AZURITE_STORAGE_ACCOUNT_URL"]}', blob_path = '/iceberg_data/default/{table_name}/', format={format})
                """
            else:
                return (
                    f"""
                    DROP TABLE IF EXISTS {table_name};
                    CREATE TABLE {if_not_exists_prefix} {table_name} {schema}
                    ENGINE=IcebergAzure(azure, container = {cluster.azure_container_name}, storage_account_url = '{cluster.env_variables["AZURITE_STORAGE_ACCOUNT_URL"]}', blob_path = '/iceberg_data/default/{table_name}/', format={format})
                    {partition_by}
                    {settings_expression}
                    """
                )

    elif storage_type == "local":
        assert not run_on_cluster

        if table_function:
            return f"""
                icebergLocal(local, path = '/iceberg_data/default/{table_name}/', format={format})
            """
        else:
            return (
                f"""
                DROP TABLE IF EXISTS {table_name};
                CREATE TABLE {if_not_exists_prefix} {table_name} {schema}
                ENGINE=IcebergLocal(local, path = '/iceberg_data/default/{table_name}/', format={format})
                {partition_by}
                {settings_expression}
                """
            )

    else:
        raise Exception(f"Unknown iceberg storage type: {storage_type}")


def check_schema_and_data(instance, table_expression, expected_schema, expected_data, timestamp_ms=None):
    if timestamp_ms:
        schema = instance.query(f"DESC {table_expression} SETTINGS iceberg_timestamp_ms = {timestamp_ms}")
        data = instance.query(f"SELECT * FROM {table_expression} ORDER BY ALL SETTINGS iceberg_timestamp_ms = {timestamp_ms}")
    else:
        schema = instance.query(f"DESC {table_expression}")
        data = instance.query(f"SELECT * FROM {table_expression} ORDER BY ALL")
    schema = list(
        map(
            lambda x: x.split("\t")[:2],
            filter(lambda x: len(x) > 0, schema.strip().split("\n")),
        )
    )
    data = list(
        map(
            lambda x: x.split("\t"),
            filter(lambda x: len(x) > 0, data.strip().split("\n")),
        )
    )
    assert expected_schema == schema
    assert expected_data == data

def get_uuid_str():
    return str(uuid.uuid4()).replace("-", "_")


def create_iceberg_table(
    storage_type,
    node,
    table_name,
    cluster,
    schema="",
    format_version=2,
    partition_by="",
    if_not_exists=False,
    format="Parquet",
    **kwargs,
):
    node.query(
        get_creation_expression(storage_type, table_name, cluster, schema, format_version, partition_by, if_not_exists, format, **kwargs)
    )


def create_initial_data_file(
    cluster, node, query, table_name, compression_method="none"
):
    node.query(
        f"""
        INSERT INTO TABLE FUNCTION
            file('{table_name}.parquet')
        SETTINGS
            output_format_parquet_compression_method='{compression_method}',
            s3_truncate_on_insert=1 {query}
        FORMAT Parquet"""
    )
    user_files_path = os.path.join(
        SCRIPT_DIR, f"{cluster.instances_dir_name}/node1/database/user_files"
    )
    result_path = f"{user_files_path}/{table_name}.parquet"
    return result_path


def default_upload_directory(
    started_cluster, storage_type, local_path, remote_path, **kwargs
):
    if storage_type == "local":
        return started_cluster.default_local_uploader.upload_directory(
            local_path, remote_path, **kwargs
        )
    elif storage_type == "s3":
        print(kwargs)
        return started_cluster.default_s3_uploader.upload_directory(
            local_path, remote_path, **kwargs
        )
    elif storage_type == "azure":
        return started_cluster.default_azure_uploader.upload_directory(
            local_path, remote_path, **kwargs
        )
    else:
        raise Exception(f"Unknown iceberg storage type: {storage_type}")


def default_download_directory(
    started_cluster, storage_type, remote_path, local_path, **kwargs
):
    if storage_type == "local":
        return started_cluster.default_local_downloader.download_directory(
            local_path, remote_path, **kwargs
        )
    else:
        raise Exception(f"Unknown iceberg storage type for downloading: {storage_type}")

        
def execute_spark_query_general(
    spark, started_cluster, storage_type: str, table_name: str, query: str
):
    spark.sql(query)
    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{table_name}/",
        f"/iceberg_data/default/{table_name}/",
    )
    return


@pytest.mark.parametrize("format_version", ["1", "2"])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_single_iceberg_file(started_cluster, format_version, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = (
        "test_single_iceberg_file_"
        + format_version
        + "_"
        + storage_type
        + "_"
        + get_uuid_str()
    )

    write_iceberg_from_df(spark, generate_data(spark, 0, 100), TABLE_NAME)

    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
    )

    create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster)

    assert instance.query(f"SELECT * FROM {TABLE_NAME}") == instance.query(
        "SELECT number, toString(number + 1) FROM numbers(100)"
    )


@pytest.mark.parametrize("format_version", ["1", "2"])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_partition_by(started_cluster, format_version, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = (
        "test_partition_by_"
        + format_version
        + "_"
        + storage_type
        + "_"
        + get_uuid_str()
    )

    write_iceberg_from_df(
        spark,
        generate_data(spark, 0, 10),
        TABLE_NAME,
        mode="overwrite",
        format_version=format_version,
        partition_by="a",
    )

    files = default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
    )
    assert len(files) == 14  # 10 partitions + 4 metadata files

    create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster)
    assert int(instance.query(f"SELECT count() FROM {TABLE_NAME}")) == 10
    assert int(instance.query(f"SELECT count() FROM system.iceberg_history WHERE table = '{TABLE_NAME}'")) == 1


@pytest.mark.parametrize("format_version", ["1", "2"])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_multiple_iceberg_files(started_cluster, format_version, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = (
        "test_multiple_iceberg_files_"
        + format_version
        + "_"
        + storage_type
        + "_"
        + get_uuid_str()
    )

    write_iceberg_from_df(
        spark,
        generate_data(spark, 0, 100),
        TABLE_NAME,
        mode="overwrite",
        format_version=format_version,
    )

    files = default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
    )

    # ['/iceberg_data/default/test_multiple_iceberg_files/data/00000-1-35302d56-f1ed-494e-a85b-fbf85c05ab39-00001.parquet',
    # '/iceberg_data/default/test_multiple_iceberg_files/metadata/version-hint.text',
    # '/iceberg_data/default/test_multiple_iceberg_files/metadata/3127466b-299d-48ca-a367-6b9b1df1e78c-m0.avro',
    # '/iceberg_data/default/test_multiple_iceberg_files/metadata/snap-5220855582621066285-1-3127466b-299d-48ca-a367-6b9b1df1e78c.avro',
    # '/iceberg_data/default/test_multiple_iceberg_files/metadata/v1.metadata.json']
    assert len(files) == 5

    create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster)
    assert int(instance.query(f"SELECT count() FROM {TABLE_NAME}")) == 100

    write_iceberg_from_df(
        spark,
        generate_data(spark, 100, 200),
        TABLE_NAME,
        mode="append",
        format_version=format_version,
    )
    files = default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        "",
    )
    assert len(files) == 9

    assert int(instance.query(f"SELECT count() FROM {TABLE_NAME}")) == 200
    assert instance.query(f"SELECT * FROM {TABLE_NAME} ORDER BY 1") == instance.query(
        "SELECT number, toString(number + 1) FROM numbers(200)"
    )


@pytest.mark.parametrize("format_version", ["1", "2"])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_types(started_cluster, format_version, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = (
        "test_types_" + format_version + "_" + storage_type + "_" + get_uuid_str()
    )

    data = [
        (
            123,
            "string",
            datetime.strptime("2000-01-01", "%Y-%m-%d"),
            ["str1", "str2"],
            True,
        )
    ]
    schema = StructType(
        [
            StructField("a", IntegerType()),
            StructField("b", StringType()),
            StructField("c", DateType()),
            StructField("d", ArrayType(StringType())),
            StructField("e", BooleanType()),
        ]
    )
    df = spark.createDataFrame(data=data, schema=schema)
    df.printSchema()
    write_iceberg_from_df(
        spark, df, TABLE_NAME, mode="overwrite", format_version=format_version
    )

    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
    )

    create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster)
    assert int(instance.query(f"SELECT count() FROM {TABLE_NAME}")) == 1
    assert (
        instance.query(f"SELECT a, b, c, d, e FROM {TABLE_NAME}").strip()
        == "123\tstring\t2000-01-01\t['str1','str2']\ttrue"
    )

    table_function_expr = get_creation_expression(
        storage_type, TABLE_NAME, started_cluster, table_function=True
    )
    assert (
        instance.query(f"SELECT a, b, c, d, e FROM {table_function_expr}").strip()
        == "123\tstring\t2000-01-01\t['str1','str2']\ttrue"
    )

    assert instance.query(f"DESCRIBE {table_function_expr} FORMAT TSV") == TSV(
        [
            ["a", "Nullable(Int32)"],
            ["b", "Nullable(String)"],
            ["c", "Nullable(Date)"],
            ["d", "Array(Nullable(String))"],
            ["e", "Nullable(Bool)"],
        ]
    )


@pytest.mark.parametrize("format_version", ["1", "2"])
@pytest.mark.parametrize("storage_type", ["s3", "azure"])
def test_cluster_table_function(started_cluster, format_version, storage_type):

    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session

    TABLE_NAME = (
        "test_iceberg_cluster_"
        + format_version
        + "_"
        + storage_type
        + "_"
        + get_uuid_str()
    )

    def add_df(mode):
        write_iceberg_from_df(
            spark,
            generate_data(spark, 0, 100),
            TABLE_NAME,
            mode=mode,
            format_version=format_version,
        )

        files = default_upload_directory(
            started_cluster,
            storage_type,
            f"/iceberg_data/default/{TABLE_NAME}/",
            f"/iceberg_data/default/{TABLE_NAME}/",
        )

        logging.info(f"Adding another dataframe. result files: {files}")

        return files

    files = add_df(mode="overwrite")
    for i in range(1, len(started_cluster.instances)):
        files = add_df(mode="append")

    logging.info(f"Setup complete. files: {files}")
    assert len(files) == 5 + 4 * (len(started_cluster.instances) - 1)

    clusters = instance.query(f"SELECT * FROM system.clusters")
    logging.info(f"Clusters setup: {clusters}")

    # Regular Query only node1
    table_function_expr = get_creation_expression(
        storage_type, TABLE_NAME, started_cluster, table_function=True
    )
    select_regular = (
        instance.query(f"SELECT * FROM {table_function_expr}").strip().split()
    )

    # Cluster Query with node1 as coordinator
    table_function_expr_cluster = get_creation_expression(
        storage_type,
        TABLE_NAME,
        started_cluster,
        table_function=True,
        run_on_cluster=True,
    )
    select_cluster = (
        instance.query(f"SELECT * FROM {table_function_expr_cluster}").strip().split()
    )

    # Simple size check
    assert len(select_regular) == 600
    assert len(select_cluster) == 600

    # Actual check
    assert select_cluster == select_regular

    # Check query_log
    for replica in started_cluster.instances.values():
        replica.query("SYSTEM FLUSH LOGS")

    for node_name, replica in started_cluster.instances.items():
        cluster_secondary_queries = (
            replica.query(
                f"""
                SELECT query, type, is_initial_query, read_rows, read_bytes FROM system.query_log
                WHERE
                    type = 'QueryStart' AND
                    positionCaseInsensitive(query, '{storage_type}Cluster') != 0 AND
                    position(query, '{TABLE_NAME}') != 0 AND
                    position(query, 'system.query_log') = 0 AND
                    NOT is_initial_query
            """
            )
            .strip()
            .split("\n")
        )

        logging.info(
            f"[{node_name}] cluster_secondary_queries: {cluster_secondary_queries}"
        )
        assert len(cluster_secondary_queries) == 1

    # write 3 times
    assert int(instance.query(f"SELECT count() FROM {table_function_expr_cluster}")) == 100 * 3


@pytest.mark.parametrize("format_version", ["1", "2"])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_delete_files(started_cluster, format_version, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = (
        "test_delete_files_"
        + format_version
        + "_"
        + storage_type
        + "_"
        + get_uuid_str()
    )

    write_iceberg_from_df(
        spark,
        generate_data(spark, 0, 100),
        TABLE_NAME,
        mode="overwrite",
        format_version=format_version,
    )

    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
    )
    create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster)

    # Test trivial count with deleted files
    query_id = "test_trivial_count_" + get_uuid_str()
    assert int(instance.query(f"SELECT count() FROM {TABLE_NAME}", query_id=query_id)) == 100
    instance.query("SYSTEM FLUSH LOGS")
    assert instance.query(f"SELECT ProfileEvents['IcebergTrivialCountOptimizationApplied'] FROM system.query_log where query_id = '{query_id}' and type = 'QueryFinish'") == "1\n"

    spark.sql(f"DELETE FROM {TABLE_NAME} WHERE a >= 0")
    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        "",
    )

    query_id = "test_trivial_count_" + get_uuid_str()
    assert int(instance.query(f"SELECT count() FROM {TABLE_NAME}", query_id=query_id)) == 0

    instance.query("SYSTEM FLUSH LOGS")
    assert instance.query(f"SELECT ProfileEvents['IcebergTrivialCountOptimizationApplied'] FROM system.query_log where query_id = '{query_id}' and type = 'QueryFinish'") == "1\n"

    write_iceberg_from_df(
        spark,
        generate_data(spark, 100, 200),
        TABLE_NAME,
        mode="upsert",
        format_version=format_version,
    )

    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        "",
    )

    query_id = "test_trivial_count_" + get_uuid_str()
    assert int(instance.query(f"SELECT count() FROM {TABLE_NAME}", query_id=query_id)) == 100

    instance.query("SYSTEM FLUSH LOGS")
    assert instance.query(f"SELECT ProfileEvents['IcebergTrivialCountOptimizationApplied'] FROM system.query_log where query_id = '{query_id}' and type = 'QueryFinish'") == "1\n"

    spark.sql(f"DELETE FROM {TABLE_NAME} WHERE a >= 150")
    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        "",
    )

    query_id = "test_trivial_count_" + get_uuid_str()
    assert int(instance.query(f"SELECT count() FROM {TABLE_NAME}", query_id=query_id)) == 50

    instance.query("SYSTEM FLUSH LOGS")
    assert instance.query(f"SELECT ProfileEvents['IcebergTrivialCountOptimizationApplied'] FROM system.query_log where query_id = '{query_id}' and type = 'QueryFinish'") == "1\n"


@pytest.mark.parametrize("format_version", ["1", "2"])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
@pytest.mark.parametrize("is_table_function", [False, True])
def test_evolved_schema_simple(
    started_cluster, format_version, storage_type, is_table_function
):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = (
        "test_evolved_schema_simple_"
        + format_version
        + "_"
        + storage_type
        + "_"
        + get_uuid_str()
    )

    def execute_spark_query(query: str):
        return execute_spark_query_general(
            spark,
            started_cluster,
            storage_type,
            TABLE_NAME,
            query,
        )

    execute_spark_query(
        f"""
            DROP TABLE IF EXISTS {TABLE_NAME};
        """
    )

    execute_spark_query(
        f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                a int NOT NULL,
                b float,
                c decimal(9,2) NOT NULL,
                d array<int>
            )
            USING iceberg
            OPTIONS ('format-version'='{format_version}')
        """
    )

    table_creation_expression = get_creation_expression(
        storage_type,
        TABLE_NAME,
        started_cluster,
        table_function=is_table_function,
        allow_dynamic_metadata_for_data_lakes=True,
    )

    table_select_expression = (
        TABLE_NAME if not is_table_function else table_creation_expression
    )

    if not is_table_function:
        instance.query(table_creation_expression)

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["a", "Int32"],
            ["b", "Nullable(Float32)"],
            ["c", "Decimal(9, 2)"],
            ["d", "Array(Nullable(Int32))"],
        ],
        [],
    )

    execute_spark_query(
        f"""
            INSERT INTO {TABLE_NAME} VALUES (4, NULL, 7.12, ARRAY(5, 6, 7));
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["a", "Int32"],
            ["b", "Nullable(Float32)"],
            ["c", "Decimal(9, 2)"],
            ["d", "Array(Nullable(Int32))"],
        ],
        [["4", "\\N", "7.12", "[5,6,7]"]],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN b TYPE double;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["a", "Int32"],
            ["b", "Nullable(Float64)"],
            ["c", "Decimal(9, 2)"],
            ["d", "Array(Nullable(Int32))"],
        ],
        [["4", "\\N", "7.12", "[5,6,7]"]],
    )

    execute_spark_query(
        f"""
            INSERT INTO {TABLE_NAME} VALUES (7, 5.0, 18.1, ARRAY(6, 7, 9));
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["a", "Int32"],
            ["b", "Nullable(Float64)"],
            ["c", "Decimal(9, 2)"],
            ["d", "Array(Nullable(Int32))"],
        ],
        [["4", "\\N", "7.12", "[5,6,7]"], ["7", "5", "18.1", "[6,7,9]"]],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN d FIRST;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["d", "Array(Nullable(Int32))"],
            ["a", "Int32"],
            ["b", "Nullable(Float64)"],
            ["c", "Decimal(9, 2)"],
        ],
        [["[5,6,7]", "4", "\\N", "7.12"], ["[6,7,9]", "7", "5", "18.1"]],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN b AFTER d;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["d", "Array(Nullable(Int32))"],
            ["b", "Nullable(Float64)"],
            ["a", "Int32"],
            ["c", "Decimal(9, 2)"],
        ],
        [["[5,6,7]", "\\N", "4", "7.12"], ["[6,7,9]", "5", "7", "18.1"]],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME}
            ADD COLUMNS (
                e string
            );
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["d", "Array(Nullable(Int32))"],
            ["b", "Nullable(Float64)"],
            ["a", "Int32"],
            ["c", "Decimal(9, 2)"],
            ["e", "Nullable(String)"],
        ],
        [
            ["[5,6,7]", "\\N", "4", "7.12", "\\N"],
            ["[6,7,9]", "5", "7", "18.1", "\\N"],
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN c TYPE decimal(12, 2);
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["d", "Array(Nullable(Int32))"],
            ["b", "Nullable(Float64)"],
            ["a", "Int32"],
            ["c", "Decimal(12, 2)"],
            ["e", "Nullable(String)"],
        ],
        [
            ["[5,6,7]", "\\N", "4", "7.12", "\\N"],
            ["[6,7,9]", "5", "7", "18.1", "\\N"],
        ],
    )

    execute_spark_query(
        f"""
            INSERT INTO {TABLE_NAME} VALUES (ARRAY(5, 6, 7), 3, -30, 7.12, 'AAA');
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["d", "Array(Nullable(Int32))"],
            ["b", "Nullable(Float64)"],
            ["a", "Int32"],
            ["c", "Decimal(12, 2)"],
            ["e", "Nullable(String)"],
        ],
        [
            ["[5,6,7]", "3", "-30", "7.12", "AAA"],
            ["[5,6,7]", "\\N", "4", "7.12", "\\N"],
            ["[6,7,9]", "5", "7", "18.1", "\\N"],
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN a TYPE BIGINT;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["d", "Array(Nullable(Int32))"],
            ["b", "Nullable(Float64)"],
            ["a", "Int64"],
            ["c", "Decimal(12, 2)"],
            ["e", "Nullable(String)"],
        ],
        [
            ["[5,6,7]", "3", "-30", "7.12", "AAA"],
            ["[5,6,7]", "\\N", "4", "7.12", "\\N"],
            ["[6,7,9]", "5", "7", "18.1", "\\N"],
        ],
    )

    execute_spark_query(
        f"""
            INSERT INTO {TABLE_NAME} VALUES (ARRAY(), 3.0, 12, -9.13, 'BBB');
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["d", "Array(Nullable(Int32))"],
            ["b", "Nullable(Float64)"],
            ["a", "Int64"],
            ["c", "Decimal(12, 2)"],
            ["e", "Nullable(String)"],
        ],
        [
            ["[]", "3", "12", "-9.13", "BBB"],
            ["[5,6,7]", "3", "-30", "7.12", "AAA"],
            ["[5,6,7]", "\\N", "4", "7.12", "\\N"],
            ["[6,7,9]", "5", "7", "18.1", "\\N"],
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN a DROP NOT NULL;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["d", "Array(Nullable(Int32))"],
            ["b", "Nullable(Float64)"],
            ["a", "Nullable(Int64)"],
            ["c", "Decimal(12, 2)"],
            ["e", "Nullable(String)"],
        ],
        [
            ["[]", "3", "12", "-9.13", "BBB"],
            ["[5,6,7]", "3", "-30", "7.12", "AAA"],
            ["[5,6,7]", "\\N", "4", "7.12", "\\N"],
            ["[6,7,9]", "5", "7", "18.1", "\\N"],
        ],
    )

    execute_spark_query(
        f"""
            INSERT INTO {TABLE_NAME} VALUES (NULL, 3.4, NULL, -9.13, NULL);
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["d", "Array(Nullable(Int32))"],
            ["b", "Nullable(Float64)"],
            ["a", "Nullable(Int64)"],
            ["c", "Decimal(12, 2)"],
            ["e", "Nullable(String)"],
        ],
        [
            ["[]", "3", "12", "-9.13", "BBB"],
            ["[]", "3.4", "\\N", "-9.13", "\\N"],
            ["[5,6,7]", "3", "-30", "7.12", "AAA"],
            ["[5,6,7]", "\\N", "4", "7.12", "\\N"],
            ["[6,7,9]", "5", "7", "18.1", "\\N"],
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} DROP COLUMN d;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["b", "Nullable(Float64)"],
            ["a", "Nullable(Int64)"],
            ["c", "Decimal(12, 2)"],
            ["e", "Nullable(String)"],
        ],
        [
            ["3", "-30", "7.12", "AAA"],
            ["3", "12", "-9.13", "BBB"],
            ["3.4", "\\N", "-9.13", "\\N"],
            ["5", "7", "18.1", "\\N"],
            ["\\N", "4", "7.12", "\\N"],
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} RENAME COLUMN a TO f;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["b", "Nullable(Float64)"],
            ["f", "Nullable(Int64)"],
            ["c", "Decimal(12, 2)"],
            ["e", "Nullable(String)"],
        ],
        [
            ["3", "-30", "7.12", "AAA"],
            ["3", "12", "-9.13", "BBB"],
            ["3.4", "\\N", "-9.13", "\\N"],
            ["5", "7", "18.1", "\\N"],
            ["\\N", "4", "7.12", "\\N"],
        ],
    )
    if not is_table_function :
        print (instance.query("SELECT * FROM system.iceberg_history"))
        assert int(instance.query(f"SELECT count() FROM system.iceberg_history WHERE table = '{TABLE_NAME}'")) == 5
        assert int(instance.query(f"SELECT count() FROM system.iceberg_history WHERE table = '{TABLE_NAME}' AND made_current_at >= yesterday()")) == 5

    # Do a single check to verify that restarting CH maintains the setting (ATTACH)
    # We are just interested on the setting working after restart, so no need to run it on all combinations
    if format_version == "1" and storage_type == "s3" and not is_table_function:

        instance.restart_clickhouse()

        execute_spark_query(
            f"""
                ALTER TABLE {TABLE_NAME} RENAME COLUMN e TO z;
            """
        )

        check_schema_and_data(
            instance,
            table_select_expression,
            [
                ["b", "Nullable(Float64)"],
                ["f", "Nullable(Int64)"],
                ["c", "Decimal(12, 2)"],
                ["z", "Nullable(String)"],
            ],
            [
                ["3", "-30", "7.12", "AAA"],
                ["3", "12", "-9.13", "BBB"],
                ["3.4", "\\N", "-9.13", "\\N"],
                ["5", "7", "18.1", "\\N"],
                ["\\N", "4", "7.12", "\\N"],
            ],
        )


@pytest.mark.parametrize("format_version", ["1", "2"])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
@pytest.mark.parametrize("is_table_function", [False, True])
def test_tuple_evolved_simple(
    started_cluster, format_version, storage_type, is_table_function
):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = (
        "test_my_evolved_"
        + format_version
        + "_"
        + storage_type
        + "_"
        + get_uuid_str()
    )

    def execute_spark_query(query: str):
        spark.sql(query)
        default_upload_directory(
            started_cluster,
            storage_type,
            f"/iceberg_data/default/{TABLE_NAME}/",
            f"/iceberg_data/default/{TABLE_NAME}/",
        )
        return

    execute_spark_query(f"DROP TABLE IF EXISTS {TABLE_NAME}")
    execute_spark_query(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            a int NOT NULL,
            b struct<a: float, b: string>,
            c struct<c : int, d: int>
        )
        USING iceberg 
        OPTIONS ('format-version'='2')
    """)

    execute_spark_query(f"INSERT INTO {TABLE_NAME} VALUES (1, named_struct('a', 1.23, 'b', 'ABBA'), named_struct('c', 1, 'd', 2))")

    execute_spark_query(f"ALTER TABLE {TABLE_NAME} RENAME COLUMN b.a TO e")

    table_creation_expression = get_creation_expression(
        storage_type,
        TABLE_NAME,
        started_cluster,
        table_function=is_table_function,
        allow_dynamic_metadata_for_data_lakes=True,
    )

    table_select_expression = (
        TABLE_NAME if not is_table_function else table_creation_expression
    )

    if not is_table_function:
        instance.query(table_creation_expression)


    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['a', 'Int32'], 
            ['b', 'Tuple(\\n    e Nullable(Float32),\\n    b Nullable(String))'],
            ['c', 'Tuple(\\n    c Nullable(Int32),\\n    d Nullable(Int32))']
        ],
        [
            ['1', "(1.23,'ABBA')", '(1,2)']
        ],
    )

    execute_spark_query(f"ALTER TABLE {TABLE_NAME} ALTER COLUMN c.d TYPE long;")

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['a', 'Int32'], 
            ['b', 'Tuple(\\n    e Nullable(Float32),\\n    b Nullable(String))'],
            ['c', 'Tuple(\\n    c Nullable(Int32),\\n    d Nullable(Int64))']
        ],
        [
            ['1', "(1.23,'ABBA')", '(1,2)']
        ],
    )

    execute_spark_query(f"ALTER TABLE {TABLE_NAME} DROP COLUMN c.c")

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['a', 'Int32'], 
            ['b', 'Tuple(\\n    e Nullable(Float32),\\n    b Nullable(String))'],
            ['c', 'Tuple(\\n    d Nullable(Int64))']
        ],
        [
            ['1', "(1.23,'ABBA')", '(2)']
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ADD COLUMN b.g int;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['a', 'Int32'], 
            ['b', 'Tuple(\\n    e Nullable(Float32),\\n    b Nullable(String),\\n    g Nullable(Int32))'],
            ['c', 'Tuple(\\n    d Nullable(Int64))']
        ],
        [
            ['1', "(1.23,'ABBA',NULL)", '(2)']
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN b.g FIRST;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['a', 'Int32'], 
            ['b', 'Tuple(\\n    g Nullable(Int32),\\n    e Nullable(Float32),\\n    b Nullable(String))'],
            ['c', 'Tuple(\\n    d Nullable(Int64))']
        ],
        [
            ['1', "(NULL,1.23,'ABBA')", '(2)']
        ],
    )

    execute_spark_query(f"INSERT INTO {TABLE_NAME} VALUES (2, named_struct('g', 5, 'e', 1.23, 'b', 'BACCARA'), named_struct('d', 3))")

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['a', 'Int32'], 
            ['b', 'Tuple(\\n    g Nullable(Int32),\\n    e Nullable(Float32),\\n    b Nullable(String))'],
            ['c', 'Tuple(\\n    d Nullable(Int64))']
        ],
        [
            ['1', "(NULL,1.23,'ABBA')", '(2)'],
            ['2', "(5,1.23,'BACCARA')", '(3)']
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} RENAME COLUMN b.g TO a;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['a', 'Int32'], 
            ['b', 'Tuple(\\n    a Nullable(Int32),\\n    e Nullable(Float32),\\n    b Nullable(String))'],
            ['c', 'Tuple(\\n    d Nullable(Int64))']
        ],
        [
            ['1', "(NULL,1.23,'ABBA')", '(2)'],
            ['2', "(5,1.23,'BACCARA')", '(3)']
        ],
    )

    execute_spark_query(f"ALTER TABLE {TABLE_NAME} DROP COLUMN b.a")

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['a', 'Int32'], 
            ['b', 'Tuple(\\n    e Nullable(Float32),\\n    b Nullable(String))'],
            ['c', 'Tuple(\\n    d Nullable(Int64))']
        ],
        [
            ['1', "(1.23,'ABBA')", '(2)'],
            ['2', "(1.23,'BACCARA')", '(3)']
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} RENAME COLUMN b.b TO a;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['a', 'Int32'], 
            ['b', 'Tuple(\\n    e Nullable(Float32),\\n    a Nullable(String))'],
            ['c', 'Tuple(\\n    d Nullable(Int64))']
        ],
        [
            ['1', "(1.23,'ABBA')", '(2)'],
            ['2', "(1.23,'BACCARA')", '(3)']
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} RENAME COLUMN b.e TO b;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['a', 'Int32'], 
            ['b', 'Tuple(\\n    b Nullable(Float32),\\n    a Nullable(String))'],
            ['c', 'Tuple(\\n    d Nullable(Int64))']
        ],
        [
            ['1', "(1.23,'ABBA')", '(2)'],
            ['2', "(1.23,'BACCARA')", '(3)']
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN b.a FIRST;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['a', 'Int32'], 
            ['b', 'Tuple(\\n    a Nullable(String),\\n    b Nullable(Float32))'],
            ['c', 'Tuple(\\n    d Nullable(Int64))']
        ],
        [
            ['1', "('ABBA',1.23)", '(2)'],
            ['2', "('BACCARA',1.23)", '(3)']
        ],
    )

@pytest.mark.parametrize("format_version", ["1", "2"])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_array_evolved_with_struct(
    started_cluster, format_version, storage_type
):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = (
        "test_array_evolved_with_struct_"
        + format_version
        + "_"
        + storage_type
        + "_"
        + get_uuid_str()
    )

    def execute_spark_query(query: str):
        spark.sql(query)
        default_upload_directory(
            started_cluster,
            storage_type,
            f"/iceberg_data/default/{TABLE_NAME}/",
            f"/iceberg_data/default/{TABLE_NAME}/",
        )
        return

    execute_spark_query(
        f"""
            DROP TABLE IF EXISTS {TABLE_NAME};
        """
    )

    execute_spark_query(
        f"""
            CREATE TABLE {TABLE_NAME}   (
                address ARRAY<STRUCT<
                    city: STRING,
                    zip: INT
                >>,
                values ARRAY<INT>
            )
            USING iceberg
            OPTIONS ('format-version'='{format_version}')
        """
    )

    execute_spark_query(
        f"""
            INSERT INTO {TABLE_NAME} VALUES (ARRAY(named_struct('name', 'Singapore', 'zip', 12345), named_struct('name', 'Moscow', 'zip', 54321)), ARRAY(1,2));
        """
    )

    table_function = get_creation_expression(
        storage_type, TABLE_NAME, started_cluster, table_function=True
    )
    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ADD COLUMNS ( address.element.foo INT );
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['address', 'Array(Tuple(\\n    city Nullable(String),\\n    zip Nullable(Int32),\\n    foo Nullable(Int32)))'],
            ['values', 'Array(Nullable(Int32))']
        ],
        [
            ["[('Singapore',12345,NULL),('Moscow',54321,NULL)]", '[1,2]']
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} DROP COLUMN address.element.city;
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['address', 'Array(Tuple(\\n    zip Nullable(Int32),\\n    foo Nullable(Int32)))'],
            ['values', 'Array(Nullable(Int32))']
        ],
        [
            ["[(12345,NULL),(54321,NULL)]", '[1,2]']
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN address.element.foo FIRST;
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['address', 'Array(Tuple(\\n    foo Nullable(Int32),\\n    zip Nullable(Int32)))'],
            ['values', 'Array(Nullable(Int32))']
        ],
        [
            ["[(NULL,12345),(NULL,54321)]", '[1,2]']
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} RENAME COLUMN address.element.foo TO city;
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['address', 'Array(Tuple(\\n    city Nullable(Int32),\\n    zip Nullable(Int32)))'],
            ['values', 'Array(Nullable(Int32))']
        ],
        [
            ["[(NULL,12345),(NULL,54321)]", '[1,2]']
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} RENAME COLUMN address TO bee;
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['bee', 'Array(Tuple(\\n    city Nullable(Int32),\\n    zip Nullable(Int32)))'],
            ['values', 'Array(Nullable(Int32))']
        ],
        [
            ["[(NULL,12345),(NULL,54321)]", '[1,2]']
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} RENAME COLUMN values TO fee;
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['bee', 'Array(Tuple(\\n    city Nullable(Int32),\\n    zip Nullable(Int32)))'],
            ['fee', 'Array(Nullable(Int32))']
        ],
        [
            ["[(NULL,12345),(NULL,54321)]", '[1,2]']
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN fee.element TYPE long;
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['bee', 'Array(Tuple(\\n    city Nullable(Int32),\\n    zip Nullable(Int32)))'],
            ['fee', 'Array(Nullable(Int64))']
        ],
        [
            ["[(NULL,12345),(NULL,54321)]", '[1,2]']
        ],
    )
    return
    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN fee FIRST;
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['fee', 'Array(Nullable(Int64))'],
            ['bee', 'Array(Tuple(\\n    city Nullable(Int32),\\n    zip Nullable(Int32)))']
        ],
        [
            ['[1,2]', "[(NULL,12345),(NULL,54321)]"]
        ],
    )


@pytest.mark.parametrize("format_version", ["1", "2"])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_array_evolved_nested(
    started_cluster, format_version, storage_type
):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = (
        "test_array_evolved_nested_"
        + format_version
        + "_"
        + storage_type
        + "_"
        + get_uuid_str()
    )

    def execute_spark_query(query: str):
        spark.sql(query)
        default_upload_directory(
            started_cluster,
            storage_type,
            f"/iceberg_data/default/{TABLE_NAME}/",
            f"/iceberg_data/default/{TABLE_NAME}/",
        )
        return

    execute_spark_query(
        f"""
            DROP TABLE IF EXISTS {TABLE_NAME};
        """
    )

    execute_spark_query(
        f"""
            CREATE TABLE {TABLE_NAME}   (
                address ARRAY<STRUCT<
                    city: STRUCT<
                        foo: STRING,
                        bar: INT
                    >,
                    zip: ARRAY<INT>
                >>
            )
            USING iceberg
            OPTIONS ('format-version'='{format_version}')
        """
    )

    execute_spark_query(
        f"""
            INSERT INTO {TABLE_NAME} VALUES (ARRAY(named_struct('city', named_struct('foo', 'some_value', 'bar', 40), 'zip', ARRAY(41,42)), named_struct('city', named_struct('foo', 'some_value2', 'bar', 1), 'zip', ARRAY(2,3,4))));
        """
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ADD COLUMNS ( address.element.zap INT );
        """
    )

    table_function = get_creation_expression(
        storage_type, TABLE_NAME, started_cluster, table_function=True
    )
    check_schema_and_data(
        instance,
        table_function,
        [
            ['address', 'Array(Tuple(\\n    city Tuple(\\n        foo Nullable(String),\\n        bar Nullable(Int32)),\\n    zip Array(Nullable(Int32)),\\n    zap Nullable(Int32)))']
        ],
        [
            ["[(('some_value',40),[41,42],NULL),(('some_value2',1),[2,3,4],NULL)]"]
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN address.element.zip.element TYPE long;
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['address', 'Array(Tuple(\\n    city Tuple(\\n        foo Nullable(String),\\n        bar Nullable(Int32)),\\n    zip Array(Nullable(Int64)),\\n    zap Nullable(Int32)))']
        ],
        [
            ["[(('some_value',40),[41,42],NULL),(('some_value2',1),[2,3,4],NULL)]"]
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN address.element.zip FIRST;
        """
    )

    execute_spark_query(
        f"""
            INSERT INTO {TABLE_NAME} VALUES (ARRAY(named_struct('zip', ARRAY(411,421), 'city', named_struct('foo', 'some_value1', 'bar', 401), 'zap', 3), named_struct('zip', ARRAY(21,31,41), 'city', named_struct('foo', 'some_value21', 'bar', 11), 'zap', 4)));
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['address', 'Array(Tuple(\\n    zip Array(Nullable(Int64)),\\n    city Tuple(\\n        foo Nullable(String),\\n        bar Nullable(Int32)),\\n    zap Nullable(Int32)))']
        ],
        [
            ["[([41,42],('some_value',40),NULL),([2,3,4],('some_value2',1),NULL)]"],
            ["[([411,421],('some_value1',401),3),([21,31,41],('some_value21',11),4)]"]
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} DROP COLUMN address.element.city.foo;
        """
    )

    execute_spark_query(
        f"""
            INSERT INTO {TABLE_NAME} VALUES (ARRAY(named_struct('zip', ARRAY(4111,4211), 'city', named_struct('bar', 4011), 'zap', 31), named_struct('zip', ARRAY(211,311,411), 'city', named_struct('bar', 111), 'zap', 41)));
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['address', 'Array(Tuple(\\n    zip Array(Nullable(Int64)),\\n    city Tuple(\\n        bar Nullable(Int32)),\\n    zap Nullable(Int32)))']
        ],
        [
            ["[([41,42],(40),NULL),([2,3,4],(1),NULL)]"],
            ["[([411,421],(401),3),([21,31,41],(11),4)]"],
            ["[([4111,4211],(4011),31),([211,311,411],(111),41)]"]
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN address.element.zap FIRST;
        """
    )

    execute_spark_query(
        f"""
            INSERT INTO {TABLE_NAME} VALUES (ARRAY(named_struct('zap', 32, 'zip', ARRAY(4112,4212), 'city', named_struct('bar', 4012)), named_struct('zap', 42, 'zip', ARRAY(212,312,412), 'city', named_struct('bar', 112))));
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['address', 'Array(Tuple(\\n    zap Nullable(Int32),\\n    zip Array(Nullable(Int64)),\\n    city Tuple(\\n        bar Nullable(Int32))))']
        ],
        [
            ["[(3,[411,421],(401)),(4,[21,31,41],(11))]"],
            ["[(31,[4111,4211],(4011)),(41,[211,311,411],(111))]"],
            ["[(32,[4112,4212],(4012)),(42,[212,312,412],(112))]"],
            ["[(NULL,[41,42],(40)),(NULL,[2,3,4],(1))]"],
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ADD COLUMNS ( address.element.city.newbar INT );
        """
    )

    execute_spark_query(
        f"""
            INSERT INTO {TABLE_NAME} VALUES (ARRAY(named_struct('zap', 33, 'zip', ARRAY(4113,4213), 'city', named_struct('bar', 4013, 'newbar', 5013)), named_struct('zap', 43, 'zip', ARRAY(213,313,413), 'city', named_struct('bar', 113, 'newbar', 513))));
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['address', 'Array(Tuple(\\n    zap Nullable(Int32),\\n    zip Array(Nullable(Int64)),\\n    city Tuple(\\n        bar Nullable(Int32),\\n        newbar Nullable(Int32))))']
        ],
        [
            ["[(3,[411,421],(401,NULL)),(4,[21,31,41],(11,NULL))]"],
            ["[(31,[4111,4211],(4011,NULL)),(41,[211,311,411],(111,NULL))]"],
            ["[(32,[4112,4212],(4012,NULL)),(42,[212,312,412],(112,NULL))]"],
            ["[(33,[4113,4213],(4013,5013)),(43,[213,313,413],(113,513))]"],
            ["[(NULL,[41,42],(40,NULL)),(NULL,[2,3,4],(1,NULL))]"],
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ADD COLUMNS ( address.element.new_tuple struct<new_tuple_elem:INT, new_tuple_elem2:INT> );
        """
    )

    execute_spark_query(
        f"""
            INSERT INTO {TABLE_NAME} VALUES (ARRAY(named_struct('zap', 34, 'zip', ARRAY(4114,4214), 'city', named_struct('bar', 4014, 'newbar', 5014), 'new_tuple', named_struct('new_tuple_elem',4,'new_tuple_elem2',4)), named_struct('zap', 44, 'zip', ARRAY(214,314,414), 'city', named_struct('bar', 114, 'newbar', 514), 'new_tuple', named_struct('new_tuple_elem',4,'new_tuple_elem2',4))));
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['address', 'Array(Tuple(\\n    zap Nullable(Int32),\\n    zip Array(Nullable(Int64)),\\n    city Tuple(\\n        bar Nullable(Int32),\\n        newbar Nullable(Int32)),\\n    new_tuple Tuple(\\n        new_tuple_elem Nullable(Int32),\\n        new_tuple_elem2 Nullable(Int32))))']
        ],
        [
            ["[(3,[411,421],(401,NULL),(NULL,NULL)),(4,[21,31,41],(11,NULL),(NULL,NULL))]"],
            ["[(31,[4111,4211],(4011,NULL),(NULL,NULL)),(41,[211,311,411],(111,NULL),(NULL,NULL))]"],
            ["[(32,[4112,4212],(4012,NULL),(NULL,NULL)),(42,[212,312,412],(112,NULL),(NULL,NULL))]"],
            ["[(33,[4113,4213],(4013,5013),(NULL,NULL)),(43,[213,313,413],(113,513),(NULL,NULL))]"],
            ["[(34,[4114,4214],(4014,5014),(4,4)),(44,[214,314,414],(114,514),(4,4))]"],
            ["[(NULL,[41,42],(40,NULL),(NULL,NULL)),(NULL,[2,3,4],(1,NULL),(NULL,NULL))]"],
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN address.element.city.newbar FIRST;
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['address', 'Array(Tuple(\\n    zap Nullable(Int32),\\n    zip Array(Nullable(Int64)),\\n    city Tuple(\\n        newbar Nullable(Int32),\\n        bar Nullable(Int32)),\\n    new_tuple Tuple(\\n        new_tuple_elem Nullable(Int32),\\n        new_tuple_elem2 Nullable(Int32))))']
        ],
        [
            ["[(3,[411,421],(NULL,401),(NULL,NULL)),(4,[21,31,41],(NULL,11),(NULL,NULL))]"],
            ["[(31,[4111,4211],(NULL,4011),(NULL,NULL)),(41,[211,311,411],(NULL,111),(NULL,NULL))]"],
            ["[(32,[4112,4212],(NULL,4012),(NULL,NULL)),(42,[212,312,412],(NULL,112),(NULL,NULL))]"],
            ["[(33,[4113,4213],(5013,4013),(NULL,NULL)),(43,[213,313,413],(513,113),(NULL,NULL))]"],
            ["[(34,[4114,4214],(5014,4014),(4,4)),(44,[214,314,414],(514,114),(4,4))]"],
            ["[(NULL,[41,42],(NULL,40),(NULL,NULL)),(NULL,[2,3,4],(NULL,1),(NULL,NULL))]"],
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN address.element.city FIRST;
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['address', 'Array(Tuple(\\n    city Tuple(\\n        newbar Nullable(Int32),\\n        bar Nullable(Int32)),\\n    zap Nullable(Int32),\\n    zip Array(Nullable(Int64)),\\n    new_tuple Tuple(\\n        new_tuple_elem Nullable(Int32),\\n        new_tuple_elem2 Nullable(Int32))))']
        ],
        [
            ["[((5013,4013),33,[4113,4213],(NULL,NULL)),((513,113),43,[213,313,413],(NULL,NULL))]"],
            ["[((5014,4014),34,[4114,4214],(4,4)),((514,114),44,[214,314,414],(4,4))]"],
            ["[((NULL,40),NULL,[41,42],(NULL,NULL)),((NULL,1),NULL,[2,3,4],(NULL,NULL))]"],
            ["[((NULL,401),3,[411,421],(NULL,NULL)),((NULL,11),4,[21,31,41],(NULL,NULL))]"],
            ["[((NULL,4011),31,[4111,4211],(NULL,NULL)),((NULL,111),41,[211,311,411],(NULL,NULL))]"],
            ["[((NULL,4012),32,[4112,4212],(NULL,NULL)),((NULL,112),42,[212,312,412],(NULL,NULL))]"],
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} DROP COLUMN address.element.city.bar;
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['address', 'Array(Tuple(\\n    city Tuple(\\n        newbar Nullable(Int32)),\\n    zap Nullable(Int32),\\n    zip Array(Nullable(Int64)),\\n    new_tuple Tuple(\\n        new_tuple_elem Nullable(Int32),\\n        new_tuple_elem2 Nullable(Int32))))']
        ],
        [
            ["[((5013),33,[4113,4213],(NULL,NULL)),((513),43,[213,313,413],(NULL,NULL))]"],
            ["[((5014),34,[4114,4214],(4,4)),((514),44,[214,314,414],(4,4))]"],
            ["[((NULL),3,[411,421],(NULL,NULL)),((NULL),4,[21,31,41],(NULL,NULL))]"],
            ["[((NULL),31,[4111,4211],(NULL,NULL)),((NULL),41,[211,311,411],(NULL,NULL))]"],
            ["[((NULL),32,[4112,4212],(NULL,NULL)),((NULL),42,[212,312,412],(NULL,NULL))]"],
            ["[((NULL),NULL,[41,42],(NULL,NULL)),((NULL),NULL,[2,3,4],(NULL,NULL))]"],
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} DROP COLUMN address.element.zip;
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['address', 'Array(Tuple(\\n    city Tuple(\\n        newbar Nullable(Int32)),\\n    zap Nullable(Int32),\\n    new_tuple Tuple(\\n        new_tuple_elem Nullable(Int32),\\n        new_tuple_elem2 Nullable(Int32))))']
        ],
        [
            ["[((5013),33,(NULL,NULL)),((513),43,(NULL,NULL))]"],
            ["[((5014),34,(4,4)),((514),44,(4,4))]"],
            ["[((NULL),3,(NULL,NULL)),((NULL),4,(NULL,NULL))]"],
            ["[((NULL),31,(NULL,NULL)),((NULL),41,(NULL,NULL))]"],
            ["[((NULL),32,(NULL,NULL)),((NULL),42,(NULL,NULL))]"],
            ["[((NULL),NULL,(NULL,NULL)),((NULL),NULL,(NULL,NULL))]"],
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} DROP COLUMN address.element.city;
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['address', 'Array(Tuple(\\n    zap Nullable(Int32),\\n    new_tuple Tuple(\\n        new_tuple_elem Nullable(Int32),\\n        new_tuple_elem2 Nullable(Int32))))']
        ],
        [
            ["[(3,(NULL,NULL)),(4,(NULL,NULL))]"],
            ["[(31,(NULL,NULL)),(41,(NULL,NULL))]"],
            ["[(32,(NULL,NULL)),(42,(NULL,NULL))]"],
            ["[(33,(NULL,NULL)),(43,(NULL,NULL))]"],
            ["[(34,(4,4)),(44,(4,4))]"],
            ["[(NULL,(NULL,NULL)),(NULL,(NULL,NULL))]"],
        ],
    )


@pytest.mark.parametrize("format_version", ["1", "2"])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
@pytest.mark.parametrize("is_table_function", [False, True])
def test_tuple_evolved_nested(
    started_cluster, format_version, storage_type, is_table_function
):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = (
        "test_tuple_evolved_nested_"
        + format_version
        + "_"
        + storage_type
        + "_"
        + get_uuid_str()
    )

    def execute_spark_query(query: str):
        spark.sql(query)
        default_upload_directory(
            started_cluster,
            storage_type,
            f"/iceberg_data/default/{TABLE_NAME}/",
            f"/iceberg_data/default/{TABLE_NAME}/",
        )
        return

    execute_spark_query(f"DROP TABLE IF EXISTS {TABLE_NAME}")
    execute_spark_query(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            a int NOT NULL,
            b struct<a: float, b: struct<na: float, nb: string>>,
            c struct<c : int, d: int>
        )
        USING iceberg 
        OPTIONS ('format-version'='2')
    """)

    execute_spark_query(f"INSERT INTO {TABLE_NAME} VALUES (1, named_struct('a', 1.23, 'b', named_struct('na', 4.56, 'nb', 'BACCARA')), named_struct('c', 1, 'd', 2))")

    table_creation_expression = get_creation_expression(
        storage_type,
        TABLE_NAME,
        started_cluster,
        table_function=is_table_function,
        allow_dynamic_metadata_for_data_lakes=True,
    )

    table_select_expression = (
        TABLE_NAME if not is_table_function else table_creation_expression
    )

    if not is_table_function:
        instance.query(table_creation_expression)


    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['a', 'Int32'], 
            ['b', 'Tuple(\\n    a Nullable(Float32),\\n    b Tuple(\\n        na Nullable(Float32),\\n        nb Nullable(String)))'],
            ['c', 'Tuple(\\n    c Nullable(Int32),\\n    d Nullable(Int32))']
        ],
        [
            ['1', "(1.23,(4.56,'BACCARA'))", '(1,2)']
        ],
    )

    execute_spark_query(f"ALTER TABLE {TABLE_NAME} RENAME COLUMN b.b.na TO e")

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['a', 'Int32'], 
            ['b', 'Tuple(\\n    a Nullable(Float32),\\n    b Tuple(\\n        e Nullable(Float32),\\n        nb Nullable(String)))'],
            ['c', 'Tuple(\\n    c Nullable(Int32),\\n    d Nullable(Int32))']
        ],
        [
            ['1', "(1.23,(4.56,'BACCARA'))", '(1,2)']
        ],
    )

    execute_spark_query(f"ALTER TABLE {TABLE_NAME} ALTER COLUMN b.b.e TYPE double;")

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['a', 'Int32'], 
            ['b', 'Tuple(\\n    a Nullable(Float32),\\n    b Tuple(\\n        e Nullable(Float64),\\n        nb Nullable(String)))'],
            ['c', 'Tuple(\\n    c Nullable(Int32),\\n    d Nullable(Int32))']
        ],
        [
            ['1', "(1.23,(4.559999942779541,'BACCARA'))", '(1,2)']
        ],
    )
    execute_spark_query(f"ALTER TABLE {TABLE_NAME} DROP COLUMN b.b.nb")

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['a', 'Int32'], 
            ['b', 'Tuple(\\n    a Nullable(Float32),\\n    b Tuple(\\n        e Nullable(Float64)))'],
            ['c', 'Tuple(\\n    c Nullable(Int32),\\n    d Nullable(Int32))']
        ],
        [
            ['1', "(1.23,(4.559999942779541))", '(1,2)']
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ADD COLUMN b.b.nc int;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['a', 'Int32'], 
            ['b', 'Tuple(\\n    a Nullable(Float32),\\n    b Tuple(\\n        e Nullable(Float64),\\n        nc Nullable(Int32)))'],
            ['c', 'Tuple(\\n    c Nullable(Int32),\\n    d Nullable(Int32))']
        ],
        [
            ['1', "(1.23,(4.559999942779541,NULL))", '(1,2)']
        ],
    )
    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN b.b.nc FIRST;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['a', 'Int32'], 
            ['b', 'Tuple(\\n    a Nullable(Float32),\\n    b Tuple(\\n        nc Nullable(Int32),\\n        e Nullable(Float64)))'],
            ['c', 'Tuple(\\n    c Nullable(Int32),\\n    d Nullable(Int32))']
        ],
        [
            ['1', "(1.23,(NULL,4.559999942779541))", '(1,2)']
        ],
    )

@pytest.mark.parametrize("format_version", ["2"])
@pytest.mark.parametrize("storage_type", ["local"])
@pytest.mark.parametrize("is_table_function", [False])
def test_map_evolved_nested(
    started_cluster, format_version, storage_type, is_table_function
):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = (
        "test_tuple_evolved_nested_"
        + format_version
        + "_"
        + storage_type
        + "_"
        + get_uuid_str()
    )

    def execute_spark_query(query: str):
        spark.sql(query)
        default_upload_directory(
            started_cluster,
            storage_type,
            f"/iceberg_data/default/{TABLE_NAME}/",
            f"/iceberg_data/default/{TABLE_NAME}/",
        )
        return

    execute_spark_query(f"DROP TABLE IF EXISTS {TABLE_NAME}")
    execute_spark_query(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            b Map<INT, INT>,
            a Map<INT, Struct<
                c : INT,
                d : String
            >>,
            c Struct <
                e : Map<Int, String>
            >
        )
        USING iceberg 
        OPTIONS ('format-version'='2')
    """)

    execute_spark_query(f"INSERT INTO {TABLE_NAME} VALUES (MAP(1, 2), Map(3, named_struct('c', 4, 'd', 'ABBA')), named_struct('e', MAP(5, 'foo')))")

    table_creation_expression = get_creation_expression(
        storage_type,
        TABLE_NAME,
        started_cluster,
        table_function=is_table_function,
        allow_dynamic_metadata_for_data_lakes=True,
    )

    table_select_expression = (
        TABLE_NAME if not is_table_function else table_creation_expression
    )

    if not is_table_function:
        instance.query(table_creation_expression)

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN b.value TYPE long;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['b', 'Map(Int32, Nullable(Int64))'], 
            ['a', 'Map(Int32, Tuple(\\n    c Nullable(Int32),\\n    d Nullable(String)))'],
            ['c', 'Tuple(\\n    e Map(Int32, Nullable(String)))']
        ],
        [
            ['{1:2}', "{3:(4,'ABBA')}", "({5:'foo'})"]
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} RENAME COLUMN c.e TO f;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['b', 'Map(Int32, Nullable(Int64))'], 
            ['a', 'Map(Int32, Tuple(\\n    c Nullable(Int32),\\n    d Nullable(String)))'],
            ['c', 'Tuple(\\n    f Map(Int32, Nullable(String)))']
        ],
        [
            ['{1:2}', "{3:(4,'ABBA')}", "({5:'foo'})"]
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN a.value.d FIRST;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['b', 'Map(Int32, Nullable(Int64))'], 
            ['a', 'Map(Int32, Tuple(\\n    d Nullable(String),\\n    c Nullable(Int32)))'],
            ['c', 'Tuple(\\n    f Map(Int32, Nullable(String)))']
        ],
        [
            ['{1:2}', "{3:('ABBA',4)}", "({5:'foo'})"]
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ADD COLUMN a.value.g int;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['b', 'Map(Int32, Nullable(Int64))'], 
            ['a', 'Map(Int32, Tuple(\\n    d Nullable(String),\\n    c Nullable(Int32),\\n    g Nullable(Int32)))'],
            ['c', 'Tuple(\\n    f Map(Int32, Nullable(String)))']
        ],
        [
            ['{1:2}', "{3:('ABBA',4,NULL)}", "({5:'foo'})"]
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN a.value.g FIRST;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['b', 'Map(Int32, Nullable(Int64))'], 
            ['a', 'Map(Int32, Tuple(\\n    g Nullable(Int32),\\n    d Nullable(String),\\n    c Nullable(Int32)))'],
            ['c', 'Tuple(\\n    f Map(Int32, Nullable(String)))']
        ],
        [
            ['{1:2}', "{3:(NULL,'ABBA',4)}", "({5:'foo'})"]
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} DROP COLUMN a.value.c;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['b', 'Map(Int32, Nullable(Int64))'], 
            ['a', 'Map(Int32, Tuple(\\n    g Nullable(Int32),\\n    d Nullable(String)))'],
            ['c', 'Tuple(\\n    f Map(Int32, Nullable(String)))']
        ],
        [
            ['{1:2}', "{3:(NULL,'ABBA')}", "({5:'foo'})"]
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} RENAME COLUMN a.value.g TO c;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['b', 'Map(Int32, Nullable(Int64))'], 
            ['a', 'Map(Int32, Tuple(\\n    c Nullable(Int32),\\n    d Nullable(String)))'],
            ['c', 'Tuple(\\n    f Map(Int32, Nullable(String)))']
        ],
        [
            ['{1:2}', "{3:(NULL,'ABBA')}", "({5:'foo'})"]
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN c FIRST;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['c', 'Tuple(\\n    f Map(Int32, Nullable(String)))'],
            ['b', 'Map(Int32, Nullable(Int64))'], 
            ['a', 'Map(Int32, Tuple(\\n    c Nullable(Int32),\\n    d Nullable(String)))']
        ],
        [
            ["({5:'foo'})", '{1:2}', "{3:(NULL,'ABBA')}"]
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ADD COLUMN c.g int;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['c', 'Tuple(\\n    f Map(Int32, Nullable(String)),\\n    g Nullable(Int32))'],
            ['b', 'Map(Int32, Nullable(Int64))'], 
            ['a', 'Map(Int32, Tuple(\\n    c Nullable(Int32),\\n    d Nullable(String)))']
        ],
        [
            ["({5:'foo'},NULL)", '{1:2}', "{3:(NULL,'ABBA')}"]
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN c.g FIRST;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['c', 'Tuple(\\n    g Nullable(Int32),\\n    f Map(Int32, Nullable(String)))'],
            ['b', 'Map(Int32, Nullable(Int64))'], 
            ['a', 'Map(Int32, Tuple(\\n    c Nullable(Int32),\\n    d Nullable(String)))']
        ],
        [
            ["(NULL,{5:'foo'})", '{1:2}', "{3:(NULL,'ABBA')}"]
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} DROP COLUMN c.f;
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ['c', 'Tuple(\\n    g Nullable(Int32))'],
            ['b', 'Map(Int32, Nullable(Int64))'], 
            ['a', 'Map(Int32, Tuple(\\n    c Nullable(Int32),\\n    d Nullable(String)))']
        ],
        [
            ["(NULL)", '{1:2}', "{3:(NULL,'ABBA')}"]
        ],
    )



@pytest.mark.parametrize("format_version", ["1", "2"])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_not_evolved_schema(started_cluster, format_version, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = (
        "test_evolved_schema_simple_"
        + format_version
        + "_"
        + storage_type
        + "_"
        + get_uuid_str()
    )

    def execute_spark_query(query: str):
        return execute_spark_query_general(
            spark,
            started_cluster,
            storage_type,
            TABLE_NAME,
            query,
        )

    execute_spark_query(
        f"""
            DROP TABLE IF EXISTS {TABLE_NAME};
        """
    )

    execute_spark_query(
        f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                a int NOT NULL,
                b float,
                c decimal(9,2) NOT NULL,
                d array<int>
            )
            USING iceberg
            OPTIONS ('format-version'='{format_version}')
        """
    )

    instance.query(
        get_creation_expression(
            storage_type,
            TABLE_NAME,
            started_cluster,
            table_function=False,
            allow_dynamic_metadata_for_data_lakes=False,
        )
    )

    check_schema_and_data(
        instance,
        TABLE_NAME,
        [
            ["a", "Int32"],
            ["b", "Nullable(Float32)"],
            ["c", "Decimal(9, 2)"],
            ["d", "Array(Nullable(Int32))"],
        ],
        [],
    )

    execute_spark_query(
        f"""
            INSERT INTO {TABLE_NAME} VALUES (4, 3.0, 7.12, ARRAY(5, 6, 7));
        """
    )

    check_schema_and_data(
        instance,
        TABLE_NAME,
        [
            ["a", "Int32"],
            ["b", "Nullable(Float32)"],
            ["c", "Decimal(9, 2)"],
            ["d", "Array(Nullable(Int32))"],
        ],
        [["4", "3", "7.12", "[5,6,7]"]],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN b TYPE double;
        """
    )

    check_schema_and_data(
        instance,
        TABLE_NAME,
        [
            ["a", "Int32"],
            ["b", "Nullable(Float32)"],
            ["c", "Decimal(9, 2)"],
            ["d", "Array(Nullable(Int32))"],
        ],
        [["4", "3", "7.12", "[5,6,7]"]],
    )

    execute_spark_query(
        f"""
            INSERT INTO {TABLE_NAME} VALUES (7, 5.0, 18.1, ARRAY(6, 7, 9));
        """
    )

    check_schema_and_data(
        instance,
        TABLE_NAME,
        [
            ["a", "Int32"],
            ["b", "Nullable(Float32)"],
            ["c", "Decimal(9, 2)"],
            ["d", "Array(Nullable(Int32))"],
        ],
        [["4", "3", "7.12", "[5,6,7]"], ["7", "5", "18.1", "[6,7,9]"]],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN d FIRST;
        """
    )

    check_schema_and_data(
        instance,
        TABLE_NAME,
        [
            ["a", "Int32"],
            ["b", "Nullable(Float32)"],
            ["c", "Decimal(9, 2)"],
            ["d", "Array(Nullable(Int32))"],
        ],
        [["4", "3", "7.12", "[5,6,7]"], ["7", "5", "18.1", "[6,7,9]"]],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN b AFTER d;
        """
    )

    check_schema_and_data(
        instance,
        TABLE_NAME,
        [
            ["a", "Int32"],
            ["b", "Nullable(Float32)"],
            ["c", "Decimal(9, 2)"],
            ["d", "Array(Nullable(Int32))"],
        ],
        [["4", "3", "7.12", "[5,6,7]"], ["7", "5", "18.1", "[6,7,9]"]],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME}
            ADD COLUMNS (
                e string
            );
        """
    )

    check_schema_and_data(
        instance,
        TABLE_NAME,
        [
            ["a", "Int32"],
            ["b", "Nullable(Float32)"],
            ["c", "Decimal(9, 2)"],
            ["d", "Array(Nullable(Int32))"],
        ],
        [["4", "3", "7.12", "[5,6,7]"], ["7", "5", "18.1", "[6,7,9]"]],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN c TYPE decimal(12, 2);
        """
    )

    check_schema_and_data(
        instance,
        TABLE_NAME,
        [
            ["a", "Int32"],
            ["b", "Nullable(Float32)"],
            ["c", "Decimal(9, 2)"],
            ["d", "Array(Nullable(Int32))"],
        ],
        [["4", "3", "7.12", "[5,6,7]"], ["7", "5", "18.1", "[6,7,9]"]],
    )

    execute_spark_query(
        f"""
            INSERT INTO {TABLE_NAME} VALUES (ARRAY(5, 6, 7), 3, -30, 7.12, 'AAA');
        """
    )

    check_schema_and_data(
        instance,
        TABLE_NAME,
        [
            ["a", "Int32"],
            ["b", "Nullable(Float32)"],
            ["c", "Decimal(9, 2)"],
            ["d", "Array(Nullable(Int32))"],
        ],
        [
            ["-30", "3", "7.12", "[5,6,7]"],
            ["4", "3", "7.12", "[5,6,7]"],
            ["7", "5", "18.1", "[6,7,9]"],
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN a TYPE BIGINT;
        """
    )

    check_schema_and_data(
        instance,
        TABLE_NAME,
        [
            ["a", "Int32"],
            ["b", "Nullable(Float32)"],
            ["c", "Decimal(9, 2)"],
            ["d", "Array(Nullable(Int32))"],
        ],
        [
            ["-30", "3", "7.12", "[5,6,7]"],
            ["4", "3", "7.12", "[5,6,7]"],
            ["7", "5", "18.1", "[6,7,9]"],
        ],
    )

    execute_spark_query(
        f"""
            INSERT INTO {TABLE_NAME} VALUES (ARRAY(), 3.0, 12, -9.13, 'BBB');
        """
    )

    check_schema_and_data(
        instance,
        TABLE_NAME,
        [
            ["a", "Int32"],
            ["b", "Nullable(Float32)"],
            ["c", "Decimal(9, 2)"],
            ["d", "Array(Nullable(Int32))"],
        ],
        [
            ["-30", "3", "7.12", "[5,6,7]"],
            ["4", "3", "7.12", "[5,6,7]"],
            ["7", "5", "18.1", "[6,7,9]"],
            ["12", "3", "-9.13", "[]"],
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN a DROP NOT NULL;
        """
    )

    check_schema_and_data(
        instance,
        TABLE_NAME,
        [
            ["a", "Int32"],
            ["b", "Nullable(Float32)"],
            ["c", "Decimal(9, 2)"],
            ["d", "Array(Nullable(Int32))"],
        ],
        [
            ["-30", "3", "7.12", "[5,6,7]"],
            ["4", "3", "7.12", "[5,6,7]"],
            ["7", "5", "18.1", "[6,7,9]"],
            ["12", "3", "-9.13", "[]"],
        ],
    )

    execute_spark_query(
        f"""
            INSERT INTO {TABLE_NAME} VALUES (NULL, 3.4, NULL, -9.13, NULL);
        """
    )

    check_schema_and_data(
        instance,
        TABLE_NAME,
        [
            ["a", "Int32"],
            ["b", "Nullable(Float32)"],
            ["c", "Decimal(9, 2)"],
            ["d", "Array(Nullable(Int32))"],
        ],
        [
            ["-30", "3", "7.12", "[5,6,7]"],
            ["0", "3.4", "-9.13", "[]"],
            ["4", "3", "7.12", "[5,6,7]"],
            ["7", "5", "18.1", "[6,7,9]"],
            ["12", "3", "-9.13", "[]"],
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} DROP COLUMN d;
        """
    )

    error = instance.query_and_get_error(f"SELECT * FROM {TABLE_NAME} ORDER BY ALL")

    assert "Not found column" in error


@pytest.mark.parametrize("format_version", ["1", "2"])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_evolved_schema_complex(started_cluster, format_version, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = (
        "test_evolved_schema_complex_"
        + format_version
        + "_"
        + storage_type
        + "_"
        + get_uuid_str()
    )

    def execute_spark_query(query: str):
        return execute_spark_query_general(
            spark,
            started_cluster,
            storage_type,
            TABLE_NAME,
            query,
        )

    execute_spark_query(
        f"""
            DROP TABLE IF EXISTS {TABLE_NAME};
        """
    )

    execute_spark_query(
        f"""
            CREATE TABLE {TABLE_NAME}   (
                address STRUCT<
                    house_number : DOUBLE,
                    city: STRUCT<
                        name: STRING,
                        zip: INT
                    >
                >,
                animals ARRAY<INT>
            )
            USING iceberg
            OPTIONS ('format-version'='{format_version}')
        """
    )

    execute_spark_query(
        f"""
            INSERT INTO {TABLE_NAME} VALUES (named_struct('house_number', 3, 'city', named_struct('name', 'Singapore', 'zip', 12345)), ARRAY(4, 7));
        """
    )

    table_function = get_creation_expression(
        storage_type, TABLE_NAME, started_cluster, table_function=True
    )
    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ADD COLUMNS ( address.appartment INT );
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['address', 
             'Tuple(\\n    house_number Nullable(Float64),\\n    city Tuple(\\n        name Nullable(String),\\n        zip Nullable(Int32)),\\n    appartment Nullable(Int32))'],
            ['animals',
                'Array(Nullable(Int32))'],
        ],
        [
            ["(3,('Singapore',12345),NULL)", '[4,7]']
        ],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} DROP COLUMN address.appartment;
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['address', 
             'Tuple(\\n    house_number Nullable(Float64),\\n    city Tuple(\\n        name Nullable(String),\\n        zip Nullable(Int32)))'],
            ["animals", "Array(Nullable(Int32))"],
        ],
        [["(3,('Singapore',12345))", "[4,7]"]],
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ALTER COLUMN animals.element TYPE BIGINT
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['address', 'Tuple(\\n    house_number Nullable(Float64),\\n    city Tuple(\\n        name Nullable(String),\\n        zip Nullable(Int32)))'],
            ['animals',
                'Array(Nullable(Int64))'],
        ],
        [
           ["(3,('Singapore',12345))", '[4,7]']
        ]
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ADD COLUMNS ( map_column Map<INT, INT> );
        """
    )

    execute_spark_query(
        f"""
            INSERT INTO {TABLE_NAME} VALUES (named_struct('house_number', 4, 'city', named_struct('name', 'Moscow', 'zip', 54321)), ARRAY(4, 7), MAP(1, 2));
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['address', 'Tuple(\\n    house_number Nullable(Float64),\\n    city Tuple(\\n        name Nullable(String),\\n        zip Nullable(Int32)))'],
            ['animals',
                'Array(Nullable(Int64))'],
            ['map_column', 'Map(Int32, Nullable(Int32))']
        ],
        [
           ["(3,('Singapore',12345))", '[4,7]', '{}'],
           ["(4,('Moscow',54321))", '[4,7]', '{1:2}'],
        ]
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} RENAME COLUMN map_column TO col_to_del;
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['address', 'Tuple(\\n    house_number Nullable(Float64),\\n    city Tuple(\\n        name Nullable(String),\\n        zip Nullable(Int32)))'],
            ['animals',
                'Array(Nullable(Int64))'],
            ['col_to_del', 'Map(Int32, Nullable(Int32))']
        ],
        [
           ["(3,('Singapore',12345))", '[4,7]', '{}'],
           ["(4,('Moscow',54321))", '[4,7]', '{1:2}'],
        ]
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} DROP COLUMN col_to_del;
        """
    )

    check_schema_and_data(
        instance,
        table_function,
        [
            ['address', 'Tuple(\\n    house_number Nullable(Float64),\\n    city Tuple(\\n        name Nullable(String),\\n        zip Nullable(Int32)))'],
            ['animals',
                'Array(Nullable(Int64))']
        ],
        [
           ["(3,('Singapore',12345))", '[4,7]'],
           ["(4,('Moscow',54321))", '[4,7]'],
        ]
    )


@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_row_based_deletes(started_cluster, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = "test_row_based_deletes_" + storage_type + "_" + get_uuid_str()

    spark.sql(
        f"CREATE TABLE {TABLE_NAME} (id bigint, data string) USING iceberg TBLPROPERTIES ('format-version' = '2', 'write.update.mode'='merge-on-read', 'write.delete.mode'='merge-on-read', 'write.merge.mode'='merge-on-read')"
    )
    spark.sql(
        f"INSERT INTO {TABLE_NAME} select id, char(id + ascii('a')) from range(100)"
    )

    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
    )

    create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster)

    assert int(instance.query(f"SELECT count() FROM {TABLE_NAME}")) == 100

    spark.sql(f"DELETE FROM {TABLE_NAME} WHERE id < 10")
    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        "",
    )

    error = instance.query_and_get_error(f"SELECT * FROM {TABLE_NAME}")
    assert "UNSUPPORTED_METHOD" in error
    instance.query(f"DROP TABLE {TABLE_NAME}")


@pytest.mark.parametrize("format_version", ["1", "2"])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_schema_inference(started_cluster, format_version, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    for format in ["Parquet", "ORC", "Avro"]:
        TABLE_NAME = (
            "test_schema_inference_"
            + format
            + "_"
            + format_version
            + "_"
            + storage_type
            + "_"
            + get_uuid_str()
        )

        # Types time, timestamptz, fixed are not supported in Spark.
        spark.sql(
            f"CREATE TABLE {TABLE_NAME} (intC int, longC long, floatC float, doubleC double, decimalC1 decimal(10, 3), decimalC2 decimal(20, 10), decimalC3 decimal(38, 30), dateC date,  timestampC timestamp, stringC string, binaryC binary, arrayC1 array<int>, mapC1 map<string, string>, structC1 struct<field1: int, field2: string>, complexC array<struct<field1: map<string, array<map<string, int>>>, field2: struct<field3: int, field4: string>>>) USING iceberg TBLPROPERTIES ('format-version' = '{format_version}', 'write.format.default' = '{format}')"
        )

        spark.sql(
            f"insert into {TABLE_NAME} select 42, 4242, 42.42, 4242.4242, decimal(42.42), decimal(42.42), decimal(42.42), date('2020-01-01'), timestamp('2020-01-01 20:00:00'), 'hello', binary('hello'), array(1,2,3), map('key', 'value'), struct(42, 'hello'), array(struct(map('key', array(map('key', 42))), struct(42, 'hello')))"
        )
        default_upload_directory(
            started_cluster,
            storage_type,
            f"/iceberg_data/default/{TABLE_NAME}/",
            f"/iceberg_data/default/{TABLE_NAME}/",
        )

        create_iceberg_table(
            storage_type, instance, TABLE_NAME, started_cluster, format=format
        )

        res = instance.query(
            f"DESC {TABLE_NAME} FORMAT TSVRaw", settings={"print_pretty_type_names": 0}
        )
        expected = TSV(
            [
                ["intC", "Nullable(Int32)"],
                ["longC", "Nullable(Int64)"],
                ["floatC", "Nullable(Float32)"],
                ["doubleC", "Nullable(Float64)"],
                ["decimalC1", "Nullable(Decimal(10, 3))"],
                ["decimalC2", "Nullable(Decimal(20, 10))"],
                ["decimalC3", "Nullable(Decimal(38, 30))"],
                ["dateC", "Nullable(Date)"],
                ["timestampC", "Nullable(DateTime64(6, 'UTC'))"],
                ["stringC", "Nullable(String)"],
                ["binaryC", "Nullable(String)"],
                ["arrayC1", "Array(Nullable(Int32))"],
                ["mapC1", "Map(String, Nullable(String))"],
                ["structC1", "Tuple(field1 Nullable(Int32), field2 Nullable(String))"],
                [
                    "complexC",
                    "Array(Tuple(field1 Map(String, Array(Map(String, Nullable(Int32)))), field2 Tuple(field3 Nullable(Int32), field4 Nullable(String))))",
                ],
            ]
        )

        assert res == expected

        # Check that we can parse data
        instance.query(f"SELECT * FROM {TABLE_NAME}")


@pytest.mark.parametrize("format_version", ["1", "2"])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_explanation(started_cluster, format_version, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    for format in ["Parquet", "ORC", "Avro"]:
        TABLE_NAME = (
            "test_explanation_"
            + format
            + "_"
            + format_version
            + "_"
            + storage_type
            + "_"
            + get_uuid_str()
        )

        # Types time, timestamptz, fixed are not supported in Spark.
        spark.sql(
            f"CREATE TABLE {TABLE_NAME} (x int) USING iceberg TBLPROPERTIES ('format-version' = '{format_version}', 'write.format.default' = '{format}')"
        )

        spark.sql(f"insert into {TABLE_NAME} select 42")
        default_upload_directory(
            started_cluster,
            storage_type,
            f"/iceberg_data/default/{TABLE_NAME}/",
            f"/iceberg_data/default/{TABLE_NAME}/",
        )

        create_iceberg_table(
            storage_type, instance, TABLE_NAME, started_cluster, format=format
        )

        res = instance.query(f"EXPLAIN SELECT * FROM {TABLE_NAME}")
        res = list(
            map(
                lambda x: x.split("\t"),
                filter(lambda x: len(x) > 0, res.strip().split("\n")),
            )
        )

        expected = [
            [
                "Expression ((Project names + (Projection + Change column names to column identifiers)))"
            ],
            [f"  ReadFromObjectStorage"],
        ]

        assert res == expected

        # Check that we can parse data
        instance.query(f"SELECT * FROM {TABLE_NAME}")


@pytest.mark.parametrize("format_version", ["1", "2"])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_metadata_file_selection(started_cluster, format_version, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = (
        "test_metadata_selection_"
        + format_version
        + "_"
        + storage_type
        + "_"
        + get_uuid_str()
    )

    spark.sql(
        f"CREATE TABLE {TABLE_NAME} (id bigint, data string) USING iceberg TBLPROPERTIES ('format-version' = '2', 'write.update.mode'='merge-on-read', 'write.delete.mode'='merge-on-read', 'write.merge.mode'='merge-on-read')"
    )

    for i in range(50):
        spark.sql(
            f"INSERT INTO {TABLE_NAME} select id, char(id + ascii('a')) from range(10)"
        )

    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
    )

    create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster)

    assert int(instance.query(f"SELECT count() FROM {TABLE_NAME}")) == 500

@pytest.mark.parametrize("format_version", ["1", "2"])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_metadata_file_format_with_uuid(started_cluster, format_version, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = (
        "test_metadata_selection_with_uuid_"
        + format_version
        + "_"
        + storage_type
        + "_"
        + get_uuid_str()
    )

    spark.sql(
        f"CREATE TABLE {TABLE_NAME} (id bigint, data string) USING iceberg TBLPROPERTIES ('format-version' = '2', 'write.update.mode'='merge-on-read', 'write.delete.mode'='merge-on-read', 'write.merge.mode'='merge-on-read')"
    )

    for i in range(50):
        spark.sql(
            f"INSERT INTO {TABLE_NAME} select id, char(id + ascii('a')) from range(10)"
        )

    for i in range(50):
        os.rename(
            f"/iceberg_data/default/{TABLE_NAME}/metadata/v{i + 1}.metadata.json",
            f"/iceberg_data/default/{TABLE_NAME}/metadata/{str(i).zfill(5)}-{get_uuid_str()}.metadata.json",
        )

    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
    )

    create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster)

    assert int(instance.query(f"SELECT count() FROM {TABLE_NAME}")) == 500


@pytest.mark.parametrize("format_version", ["1", "2"])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_metadata_file_selection_from_version_hint(started_cluster, format_version, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = (
        "test_metadata_file_selection_from_version_hint_"
        + format_version
        + "_"
        + storage_type
        + "_"
        + get_uuid_str()
    )

    spark.sql(
        f"CREATE TABLE {TABLE_NAME} (id bigint, data string) USING iceberg TBLPROPERTIES ('format-version' = '2', 'write.update.mode'='merge-on-read', 'write.delete.mode'='merge-on-read', 'write.merge.mode'='merge-on-read')"
    )

    for i in range(10):
        spark.sql(
            f"INSERT INTO {TABLE_NAME} select id, char(id + ascii('a')) from range(10)"
        )
        
    # test the case where version_hint.text file contains just the version number
    with open(f"/iceberg_data/default/{TABLE_NAME}/metadata/version-hint.text", "w") as f:
        f.write('5')

    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
    )

    create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster, use_version_hint=True)

    assert int(instance.query(f"SELECT count() FROM {TABLE_NAME}")) == 40

    # test the case where version_hint.text file contains the whole metadata file name
    with open(f"/iceberg_data/default/{TABLE_NAME}/metadata/version-hint.text", "w") as f:
        f.write('v3.metadata.json')

    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
    )

    create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster, use_version_hint=True)

    assert int(instance.query(f"SELECT count() FROM {TABLE_NAME}")) == 20


def test_restart_broken_s3(started_cluster):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = "test_restart_broken_table_function_s3" + "_" + get_uuid_str()

    minio_client = started_cluster.minio_client
    bucket = "broken2"

    if not minio_client.bucket_exists(bucket):
        minio_client.make_bucket(bucket)

    write_iceberg_from_df(
        spark,
        generate_data(spark, 0, 100),
        TABLE_NAME,
        mode="overwrite",
        format_version="1",
    )

    files = default_upload_directory(
        started_cluster,
        "s3",
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
        bucket=bucket,
    )
    create_iceberg_table("s3", instance, TABLE_NAME, started_cluster, bucket=bucket)
    assert int(instance.query(f"SELECT count() FROM {TABLE_NAME}")) == 100

    s3_objects = list_s3_objects(minio_client, bucket, prefix="")
    assert (
        len(
            list(
                minio_client.remove_objects(
                    bucket,
                    [DeleteObject(obj) for obj in s3_objects],
                )
            )
        )
        == 0
    )
    minio_client.remove_bucket(bucket)

    instance.restart_clickhouse()

    assert "NoSuchBucket" in instance.query_and_get_error(
        f"SELECT count() FROM {TABLE_NAME}"
    )

    minio_client.make_bucket(bucket)

    files = default_upload_directory(
        started_cluster,
        "s3",
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
        bucket=bucket,
    )

    assert int(instance.query(f"SELECT count() FROM {TABLE_NAME}")) == 100
    instance.query(f"DROP TABLE {TABLE_NAME}")


@pytest.mark.parametrize("storage_type", ["s3"])
def test_filesystem_cache(started_cluster, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = "test_filesystem_cache_" + storage_type + "_" + get_uuid_str()

    write_iceberg_from_df(
        spark,
        generate_data(spark, 0, 10),
        TABLE_NAME,
        mode="overwrite",
        format_version="1",
        partition_by="a",
    )

    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
    )

    create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster)

    query_id = f"{TABLE_NAME}-{uuid.uuid4()}"
    instance.query(
        f"SELECT * FROM {TABLE_NAME} SETTINGS filesystem_cache_name = 'cache1'",
        query_id=query_id,
    )

    instance.query("SYSTEM FLUSH LOGS")

    written_to_cache_first_select = int(
        instance.query(
            f"SELECT ProfileEvents['CachedReadBufferCacheWriteBytes'] FROM system.query_log WHERE query_id = '{query_id}' AND type = 'QueryFinish'"
        )
    )

    read_from_cache_first_select = int(
        instance.query(
            f"SELECT ProfileEvents['CachedReadBufferReadFromCacheBytes'] FROM system.query_log WHERE query_id = '{query_id}' AND type = 'QueryFinish'"
        )
    )

    assert 0 < int(
        instance.query(
            f"SELECT ProfileEvents['S3GetObject'] FROM system.query_log WHERE query_id = '{query_id}' AND type = 'QueryFinish'"
        )
    )

    query_id = f"{TABLE_NAME}-{uuid.uuid4()}"
    instance.query(
        f"SELECT * FROM {TABLE_NAME} SETTINGS filesystem_cache_name = 'cache1'",
        query_id=query_id,
    )

    instance.query("SYSTEM FLUSH LOGS")

    read_from_cache_second_select = int(
        instance.query(
            f"SELECT ProfileEvents['CachedReadBufferReadFromCacheBytes'] FROM system.query_log WHERE query_id = '{query_id}' AND type = 'QueryFinish'"
        )
    )

    assert (
        read_from_cache_second_select
        == read_from_cache_first_select + written_to_cache_first_select
    )

    assert 0 == int(
        instance.query(
            f"SELECT ProfileEvents['S3GetObject'] FROM system.query_log WHERE query_id = '{query_id}' AND type = 'QueryFinish'"
        )
    )

def check_validity_and_get_prunned_files_general(instance, table_name, settings1, settings2, profile_event_name, select_expression):
    query_id1 = f"{table_name}-{uuid.uuid4()}"
    query_id2 = f"{table_name}-{uuid.uuid4()}"

    data1 = instance.query(
        select_expression,
        query_id=query_id1,
        settings=settings1
    )
    data1 = list(
        map(
            lambda x: x.split("\t"),
            filter(lambda x: len(x) > 0, data1.strip().split("\n")),
        )
    )

    data2 = instance.query(
        select_expression,
        query_id=query_id2,
        settings=settings2
    )
    data2 = list(
        map(
            lambda x: x.split("\t"),
            filter(lambda x: len(x) > 0, data2.strip().split("\n")),
        )
    )

    assert data1 == data2

    instance.query("SYSTEM FLUSH LOGS")

    assert 0 == int(
        instance.query(
            f"SELECT ProfileEvents['{profile_event_name}'] FROM system.query_log WHERE query_id = '{query_id1}' AND type = 'QueryFinish'"
        )
    )
    return int(
        instance.query(
            f"SELECT ProfileEvents['{profile_event_name}'] FROM system.query_log WHERE query_id = '{query_id2}' AND type = 'QueryFinish'"
        )
    )


@pytest.mark.parametrize(
    "storage_type, run_on_cluster",
    [("s3", False), ("s3", True), ("azure", False), ("local", False)],
)
def test_partition_pruning(started_cluster, storage_type, run_on_cluster):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = "test_partition_pruning_" + storage_type + "_" + get_uuid_str()

    def execute_spark_query(query: str):
        return execute_spark_query_general(
            spark,
            started_cluster,
            storage_type,
            TABLE_NAME,
            query,
        )

    execute_spark_query(
        f"""
            CREATE TABLE {TABLE_NAME} (
                tag INT,
                date DATE,
                date2 DATE,
                ts TIMESTAMP,
                ts2 TIMESTAMP,
                time_struct struct<a : DATE, b : TIMESTAMP>,
                name VARCHAR(50),
                number BIGINT
            )
            USING iceberg
            PARTITIONED BY (identity(tag), days(date), years(date2), hours(ts), months(ts2), TRUNCATE(3, name), TRUNCATE(3, number))
            OPTIONS('format-version'='2')
        """
    )

    execute_spark_query(
        f"""
        INSERT INTO {TABLE_NAME} VALUES
        (1, DATE '2024-01-20', DATE '2024-01-20',
        TIMESTAMP '2024-02-20 10:00:00', TIMESTAMP '2024-02-20 10:00:00', named_struct('a', DATE '2024-01-20', 'b', TIMESTAMP '2024-02-20 10:00:00'), 'vasya', 5),
        (2, DATE '2024-01-30', DATE '2024-01-30',
        TIMESTAMP '2024-03-20 15:00:00', TIMESTAMP '2024-03-20 15:00:00', named_struct('a', DATE '2024-03-20', 'b', TIMESTAMP '2024-03-20 14:00:00'), 'vasilisa', 6),
        (1, DATE '2024-02-20', DATE '2024-02-20',
        TIMESTAMP '2024-03-20 20:00:00', TIMESTAMP '2024-03-20 20:00:00', named_struct('a', DATE '2024-02-20', 'b', TIMESTAMP '2024-02-20 10:00:00'), 'iceberg', 7),
        (2, DATE '2025-01-20', DATE '2025-01-20',
        TIMESTAMP '2024-04-30 14:00:00', TIMESTAMP '2024-04-30 14:00:00', named_struct('a', DATE '2024-04-30', 'b', TIMESTAMP '2024-04-30 14:00:00'), 'icebreaker', 8);
    """
    )

    creation_expression = get_creation_expression(
        storage_type, TABLE_NAME, started_cluster, table_function=True, run_on_cluster=run_on_cluster
    )

    def check_validity_and_get_prunned_files(select_expression):
        settings1 = {
            "use_iceberg_partition_pruning": 0
        }
        settings2 = {
            "use_iceberg_partition_pruning": 1
        }
        return check_validity_and_get_prunned_files_general(
            instance, TABLE_NAME, settings1, settings2, 'IcebergPartitionPrunedFiles', select_expression
        )

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} ORDER BY ALL"
        )
        == 0
    )
    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE date <= '2024-01-25' ORDER BY ALL"
        )
        == 3
    )
    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE date2 <= '2024-01-25' ORDER BY ALL"
        )
        == 1
    )
    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE ts <= timestamp('2024-03-20 14:00:00.000000') ORDER BY ALL"
        )
        == 3
    )
    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE ts2 <= timestamp('2024-03-20 14:00:00.000000') ORDER BY ALL"
        )
        == 1
    )

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE tag == 1 ORDER BY ALL"
        )
        == 2
    )

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE tag <= 1 ORDER BY ALL"
        )
        == 2
    )

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE name == 'vasilisa' ORDER BY ALL"
        )
        == 2
    )

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE name < 'kek' ORDER BY ALL"
        )
        == 2
    )

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE number == 8 ORDER BY ALL"
        )
        == 1
    )

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE number <= 5 ORDER BY ALL"
        )
        == 3
    )

    execute_spark_query(f"ALTER TABLE {TABLE_NAME} RENAME COLUMN date TO date3")

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE date3 <= '2024-01-25' ORDER BY ALL"
        )
        == 3
    )

    execute_spark_query(f"ALTER TABLE {TABLE_NAME} ALTER COLUMN tag TYPE BIGINT")

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE tag <= 1 ORDER BY ALL"
        )
        == 2
    )

    execute_spark_query(f"ALTER TABLE {TABLE_NAME} ADD PARTITION FIELD time_struct.a")

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE time_struct.a <= '2024-02-01' ORDER BY ALL"
        )
        == 0
    )

    execute_spark_query(
        f"INSERT INTO {TABLE_NAME} VALUES (1, DATE '2024-01-20', DATE '2024-01-20', TIMESTAMP '2024-02-20 10:00:00', TIMESTAMP '2024-02-20 10:00:00', named_struct('a', DATE '2024-03-15', 'b', TIMESTAMP '2024-02-20 10:00:00'), 'kek', 10)"
    )

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE time_struct.a <= '2024-02-01' ORDER BY ALL"
        )
        == 1
    )


@pytest.mark.parametrize("format_version", ["1", "2"])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_schema_evolution_with_time_travel(
    started_cluster, format_version, storage_type
):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = (
        "test_schema_evolution_with_time_travel_"
        + format_version
        + "_"
        + storage_type
        + "_"
        + get_uuid_str()
    )

    def execute_spark_query(query: str):
        return execute_spark_query_general(
            spark,
            started_cluster,
            storage_type,
            TABLE_NAME,
            query,
        )

    execute_spark_query(
        f"""
            DROP TABLE IF EXISTS {TABLE_NAME};
        """
    )

    execute_spark_query(
        f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                a int NOT NULL
            )
            USING iceberg
            OPTIONS ('format-version'='{format_version}')
        """
    )

    table_creation_expression = get_creation_expression(
        storage_type,
        TABLE_NAME,
        started_cluster,
        table_function=True,
        allow_dynamic_metadata_for_data_lakes=True,
    )

    table_select_expression =  table_creation_expression

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["a", "Int32"]
        ],
        [],
    )

    first_timestamp_ms = int(datetime.now().timestamp() * 1000)

    time.sleep(0.5)

    execute_spark_query(
        f"""
            INSERT INTO {TABLE_NAME} VALUES (4);
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["a", "Int32"],
        ],
        [["4"]],
    )

    error_message = instance.query_and_get_error(f"SELECT * FROM {table_select_expression} ORDER BY ALL SETTINGS iceberg_timestamp_ms = {first_timestamp_ms}")
    assert "No snapshot found in snapshot log before requested timestamp" in error_message

    second_timestamp_ms = int(datetime.now().timestamp() * 1000)

    time.sleep(0.5)

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ADD COLUMNS (
                b double
            );
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["a", "Int32"],
            ["b", "Nullable(Float64)"]
        ],
        [["4", "\\N"]],
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["a", "Int32"],
        ],
        [["4"]],
        timestamp_ms=second_timestamp_ms,
    )

    third_timestamp_ms = int(datetime.now().timestamp() * 1000)

    time.sleep(0.5)

    execute_spark_query(
        f"""
            INSERT INTO {TABLE_NAME} VALUES (7, 5.0);
        """
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["a", "Int32"],
            ["b", "Nullable(Float64)"]
        ],
        [["4", "\\N"], ["7", "5"]],
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["a", "Int32"],
        ],
        [["4"]],
        timestamp_ms=second_timestamp_ms,
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["a", "Int32"],        ],
        [["4"]],
        timestamp_ms=third_timestamp_ms,
    )

    execute_spark_query(
        f"""
            ALTER TABLE {TABLE_NAME} ADD COLUMNS (
                c double
            );
        """
    )

    time.sleep(0.5)
    fourth_timestamp_ms = int(datetime.now().timestamp() * 1000)

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["a", "Int32"],
            ["b", "Nullable(Float64)"]
        ],
        [["4", "\\N"], ["7", "5"]],
        timestamp_ms=fourth_timestamp_ms,
    )

    check_schema_and_data(
        instance,
        table_select_expression,
        [
            ["a", "Int32"],
            ["b", "Nullable(Float64)"],
            ["c", "Nullable(Float64)"]
        ],
        [["4", "\\N", "\\N"], ["7", "5", "\\N"]],
    )

def get_last_snapshot(path_to_table):
    import json
    import os

    metadata_dir = f"{path_to_table}/metadata/"
    last_timestamp = 0
    last_snapshot_id = -1
    for filename in os.listdir(metadata_dir):
        if filename.endswith('.json'):
            filepath = os.path.join(metadata_dir, filename)
            with open(filepath, 'r') as f:
                data = json.load(f)
                print(data)
                timestamp = data.get('last-updated-ms')
                if (timestamp > last_timestamp):
                    last_timestamp = timestamp
                    last_snapshot_id = data.get('current-snapshot-id')
    return last_snapshot_id


@pytest.mark.parametrize("format_version", ["1", "2"])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_iceberg_snapshot_reads(started_cluster, format_version, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = (
        "test_iceberg_snapshot_reads"
        + format_version
        + "_"
        + storage_type
        + "_"
        + get_uuid_str()
    )

    write_iceberg_from_df(
        spark,
        generate_data(spark, 0, 100),
        TABLE_NAME,
        mode="overwrite",
        format_version=format_version,
    )
    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        "",
    )

    create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster)
    assert int(instance.query(f"SELECT count() FROM {TABLE_NAME}")) == 100
    snapshot1_timestamp = datetime.now(timezone.utc)
    snapshot1_id = get_last_snapshot(f"/iceberg_data/default/{TABLE_NAME}/")
    time.sleep(0.1)

    write_iceberg_from_df(
        spark,
        generate_data(spark, 100, 200),
        TABLE_NAME,
        mode="append",
        format_version=format_version,
    )
    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        "",
    )
    snapshot2_timestamp = datetime.now(timezone.utc)
    snapshot2_id = get_last_snapshot(f"/iceberg_data/default/{TABLE_NAME}/")
    time.sleep(0.1)

    write_iceberg_from_df(
        spark,
        generate_data(spark, 200, 300),
        TABLE_NAME,
        mode="append",
        format_version=format_version,
    )
    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        "",
    )
    snapshot3_timestamp = datetime.now(timezone.utc)
    snapshot3_id = get_last_snapshot(f"/iceberg_data/default/{TABLE_NAME}/")
    assert int(instance.query(f"SELECT count() FROM {TABLE_NAME}")) == 300
    assert instance.query(f"SELECT * FROM {TABLE_NAME} ORDER BY 1") == instance.query(
        "SELECT number, toString(number + 1) FROM numbers(300)"
    )

    # Validate that each snapshot timestamp only sees the data inserted by that time.
    assert (
        instance.query(
            f"""
                          SELECT * FROM {TABLE_NAME} ORDER BY 1
                          SETTINGS iceberg_timestamp_ms = {int(snapshot1_timestamp.timestamp() * 1000)}"""
        )
        == instance.query("SELECT number, toString(number + 1) FROM numbers(100)")
    )

    assert (
        instance.query(
            f"""
                          SELECT * FROM {TABLE_NAME} ORDER BY 1
                          SETTINGS iceberg_snapshot_id = {snapshot1_id}"""
        )
        == instance.query("SELECT number, toString(number + 1) FROM numbers(100)")
    )


    assert (
        instance.query(
            f"""
                          SELECT * FROM {TABLE_NAME} ORDER BY 1
                          SETTINGS iceberg_timestamp_ms = {int(snapshot2_timestamp.timestamp() * 1000)}"""
        )
        == instance.query("SELECT number, toString(number + 1) FROM numbers(200)")
    )

    assert (
        instance.query(
            f"""
                          SELECT * FROM {TABLE_NAME} ORDER BY 1
                          SETTINGS iceberg_snapshot_id = {snapshot2_id}"""
        )
        == instance.query("SELECT number, toString(number + 1) FROM numbers(200)")
    )


    assert (
        instance.query(
            f"""SELECT * FROM {TABLE_NAME} ORDER BY 1
                          SETTINGS iceberg_timestamp_ms = {int(snapshot3_timestamp.timestamp() * 1000)}"""
        )
        == instance.query("SELECT number, toString(number + 1) FROM numbers(300)")
    )

    assert (
        instance.query(
            f"""
                          SELECT * FROM {TABLE_NAME} ORDER BY 1
                          SETTINGS iceberg_snapshot_id = {snapshot3_id}"""
        )
        == instance.query("SELECT number, toString(number + 1) FROM numbers(300)")
    )


@pytest.mark.parametrize("storage_type", ["s3", "azure"])
def test_metadata_cache(started_cluster, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = "test_metadata_cache_" + storage_type + "_" + get_uuid_str()

    write_iceberg_from_df(
        spark,
        generate_data(spark, 0, 10),
        TABLE_NAME,
        mode="overwrite",
        format_version="1",
        partition_by="a",
    )

    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
    )

    table_expr = get_creation_expression(storage_type, TABLE_NAME, started_cluster, table_function=True)

    query_id = f"{TABLE_NAME}-{uuid.uuid4()}"
    instance.query(
        f"SELECT * FROM {table_expr}", query_id=query_id,
    )

    instance.query("SYSTEM FLUSH LOGS")

    assert 0 < int(
        instance.query(
            f"SELECT ProfileEvents['IcebergMetadataFilesCacheMisses'] FROM system.query_log WHERE query_id = '{query_id}' AND type = 'QueryFinish'"
        )
    )

    query_id = f"{TABLE_NAME}-{uuid.uuid4()}"
    instance.query(
        f"SELECT * FROM {table_expr}",
        query_id=query_id,
    )

    instance.query("SYSTEM FLUSH LOGS")

    assert 0 == int(
        instance.query(
            f"SELECT ProfileEvents['IcebergMetadataFilesCacheMisses'] FROM system.query_log WHERE query_id = '{query_id}' AND type = 'QueryFinish'"
        )
    )

    assert 0 < int(
        instance.query(
            f"SELECT ProfileEvents['IcebergMetadataFilesCacheHits'] FROM system.query_log WHERE query_id = '{query_id}' AND type = 'QueryFinish'"
        )
    )

    instance.query("SYSTEM DROP ICEBERG METADATA CACHE")

    query_id = f"{TABLE_NAME}-{uuid.uuid4()}"
    instance.query(
        f"SELECT * FROM {table_expr}", query_id=query_id,
    )

    instance.query("SYSTEM FLUSH LOGS")

    assert 0 < int(
        instance.query(
            f"SELECT ProfileEvents['IcebergMetadataFilesCacheMisses'] FROM system.query_log WHERE query_id = '{query_id}' AND type = 'QueryFinish'"
        )
    )

    query_id = f"{TABLE_NAME}-{uuid.uuid4()}"
    instance.query(
        f"SELECT * FROM {table_expr}",
        query_id=query_id,
        settings={"use_iceberg_metadata_files_cache":"0"},
    )

    instance.query("SYSTEM FLUSH LOGS")
    assert "0\t0\n" == instance.query(
            f"SELECT ProfileEvents['IcebergMetadataFilesCacheHits'], ProfileEvents['IcebergMetadataFilesCacheMisses'] FROM system.query_log WHERE query_id = '{query_id}' AND type = 'QueryFinish'",
        )


@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
@pytest.mark.parametrize("is_table_function", [False, True])
def test_minmax_pruning(started_cluster, storage_type, is_table_function):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = "test_minmax_pruning_" + storage_type + "_" + get_uuid_str()

    def execute_spark_query(query: str):
        return execute_spark_query_general(
            spark,
            started_cluster,
            storage_type,
            TABLE_NAME,
            query,
        )

    execute_spark_query(
        f"""
            CREATE TABLE {TABLE_NAME} (
                tag INT,
                date DATE,
                ts TIMESTAMP,
                time_struct struct<a : DATE, b : TIMESTAMP>,
                name VARCHAR(50),
                number BIGINT
            )
            USING iceberg
            OPTIONS('format-version'='2')
        """
    )

    execute_spark_query(
        f"""
        INSERT INTO {TABLE_NAME} VALUES
        (1, DATE '2024-01-20',
        TIMESTAMP '2024-02-20 10:00:00', named_struct('a', DATE '2024-01-20', 'b', TIMESTAMP '2024-02-20 10:00:00'), 'vasya', 5)
    """
    )

    execute_spark_query(
        f"""
        INSERT INTO {TABLE_NAME} VALUES
        (2, DATE '2024-02-20',
        TIMESTAMP '2024-03-20 15:00:00', named_struct('a', DATE '2024-02-20', 'b', TIMESTAMP '2024-03-20 14:00:00'), 'vasilisa', 6)
    """
    )

    execute_spark_query(
        f"""
        INSERT INTO {TABLE_NAME} VALUES
        (3, DATE '2025-03-20',
        TIMESTAMP '2024-04-30 14:00:00', named_struct('a', DATE '2024-03-20', 'b', TIMESTAMP '2024-04-30 14:00:00'), 'icebreaker', 7)
    """
    )
    execute_spark_query(
        f"""
        INSERT INTO {TABLE_NAME} VALUES
        (4, DATE '2025-04-20',
        TIMESTAMP '2024-05-30 14:00:00', named_struct('a', DATE '2024-04-20', 'b', TIMESTAMP '2024-05-30 14:00:00'), 'iceberg', 8)
    """
    )

    if is_table_function:
        creation_expression = get_creation_expression(
        storage_type, TABLE_NAME, started_cluster, table_function=True
    )
    else:
        instance.query(get_creation_expression(
            storage_type, TABLE_NAME, started_cluster, table_function=False
        ))
        creation_expression = TABLE_NAME

    def check_validity_and_get_prunned_files(select_expression):
        settings1 = {
            "use_iceberg_partition_pruning": 0,
            "input_format_parquet_bloom_filter_push_down": 0,
            "input_format_parquet_filter_push_down": 0,
        }
        settings2 = {
            "use_iceberg_partition_pruning": 1,
            "input_format_parquet_bloom_filter_push_down": 0,
            "input_format_parquet_filter_push_down": 0,
        }
        return check_validity_and_get_prunned_files_general(
            instance, TABLE_NAME, settings1, settings2, 'IcebergMinMaxIndexPrunedFiles', select_expression
        )

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} ORDER BY ALL"
        )
        == 0
    )
    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE date <= '2024-01-25' ORDER BY ALL"
        )
        == 3
    )
    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE ts <= timestamp('2024-03-20 14:00:00.000000') ORDER BY ALL"
        )
        == 3
    )

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE tag == 1 ORDER BY ALL"
        )
        == 3
    )

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE tag <= 1 ORDER BY ALL"
        )
        == 3
    )

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE name == 'vasilisa' ORDER BY ALL"
        )
        == 3
    )

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE name < 'kek' ORDER BY ALL"
        )
        == 2
    )

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE number == 8 ORDER BY ALL"
        )
        == 3
    )

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE number <= 5 ORDER BY ALL"
        )
        == 3
    )

    if not is_table_function:
        return

    execute_spark_query(f"ALTER TABLE {TABLE_NAME} RENAME COLUMN date TO date3")

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE date3 <= '2024-01-25' ORDER BY ALL"
        )
        == 3
    )

    execute_spark_query(f"ALTER TABLE {TABLE_NAME} ALTER COLUMN tag TYPE BIGINT")

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE tag <= 1 ORDER BY ALL"
        )
        == 3
    )

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE time_struct.a <= '2024-02-01' ORDER BY ALL"
        )
        == 3
    )

    execute_spark_query(
        f"INSERT INTO {TABLE_NAME} VALUES (1, DATE '2024-01-20', TIMESTAMP '2024-02-20 10:00:00', named_struct('a', DATE '2024-03-15', 'b', TIMESTAMP '2024-02-20 10:00:00'), 'kek', 10)"
    )

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE time_struct.a <= '2024-02-01' ORDER BY ALL"
        )
        == 4
    )

    execute_spark_query(f"ALTER TABLE {TABLE_NAME} ADD COLUMNS (ddd decimal(10, 3))")

    execute_spark_query(
        f"INSERT INTO {TABLE_NAME} VALUES (1, DATE '2024-01-20', TIMESTAMP '2024-02-20 10:00:00', named_struct('a', DATE '2024-03-15', 'b', TIMESTAMP '2024-02-20 10:00:00'), 'kek', 30, decimal(17.22))"
    )

    execute_spark_query(
        f"INSERT INTO {TABLE_NAME} VALUES (1, DATE '2024-01-20', TIMESTAMP '2024-02-20 10:00:00', named_struct('a', DATE '2024-03-15', 'b', TIMESTAMP '2024-02-20 10:00:00'), 'kek', 10, decimal(14311.772))"
    )

    execute_spark_query(
        f"INSERT INTO {TABLE_NAME} VALUES (1, DATE '2024-01-20', TIMESTAMP '2024-02-20 10:00:00', named_struct('a', DATE '2024-03-15', 'b', TIMESTAMP '2024-02-20 10:00:00'), 'kek', 10, decimal(-8888.999))"
    )

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE ddd >= 100 ORDER BY ALL"
        )
        == 2
    )
    # Spark store rounded values of decimals, this query checks that we work it around.
    # Please check the code where we parse lower bounds and upper bounds
    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE ddd >= toDecimal64('17.21', 3) ORDER BY ALL"
        )
        == 1
    )

@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_explicit_metadata_file(started_cluster, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = (
        "test_explicit_metadata_file_"
        + storage_type
        + "_"
        + get_uuid_str()
    )

    spark.sql(
        f"CREATE TABLE {TABLE_NAME} (id bigint, data string) USING iceberg TBLPROPERTIES ('format-version' = '2', 'write.update.mode'='merge-on-read', 'write.delete.mode'='merge-on-read', 'write.merge.mode'='merge-on-read')"
    )

    for i in range(50):
        spark.sql(
            f"INSERT INTO {TABLE_NAME} select id, char(id + ascii('a')) from range(10)"
        )

    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
    )

    create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster, explicit_metadata_path="")

    assert int(instance.query(f"SELECT count() FROM {TABLE_NAME}")) == 500

    create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster, explicit_metadata_path="metadata/v31.metadata.json")

    assert int(instance.query(f"SELECT count() FROM {TABLE_NAME}")) == 300

    create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster, explicit_metadata_path="metadata/v11.metadata.json")

    assert int(instance.query(f"SELECT count() FROM {TABLE_NAME}")) == 100

    with pytest.raises(Exception):
        create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster, explicit_metadata_path=chr(0) + chr(1))
    with pytest.raises(Exception):
        create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster, explicit_metadata_path="../metadata/v11.metadata.json")

@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_minmax_pruning_with_null(started_cluster, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = "test_minmax_pruning_with_null" + storage_type + "_" + get_uuid_str()

    def execute_spark_query(query: str):
        return execute_spark_query_general(
            spark,
            started_cluster,
            storage_type,
            TABLE_NAME,
            query,
        )

    execute_spark_query(
        f"""
            CREATE TABLE {TABLE_NAME} (
                tag INT,
                date DATE,
                ts TIMESTAMP,
                time_struct struct<a : DATE, b : TIMESTAMP>,
                name VARCHAR(50),
                number BIGINT
            )
            USING iceberg
            OPTIONS('format-version'='2')
        """
    )

    # min-max value of time_struct in manifest file is null.
    execute_spark_query(
        f"""
        INSERT INTO {TABLE_NAME} VALUES
        (1, DATE '2024-01-20',
        TIMESTAMP '2024-02-20 10:00:00', null, 'vasya', 5)
    """
    )

    execute_spark_query(
        f"""
        INSERT INTO {TABLE_NAME} VALUES
        (2, DATE '2024-02-20',
        TIMESTAMP '2024-03-20 15:00:00', null, 'vasilisa', 6)
    """
    )

    execute_spark_query(
        f"""
        INSERT INTO {TABLE_NAME} VALUES
        (3, DATE '2025-03-20',
        TIMESTAMP '2024-04-30 14:00:00', null, 'icebreaker', 7)
    """
    )
    execute_spark_query(
        f"""
        INSERT INTO {TABLE_NAME} VALUES
        (4, DATE '2025-04-20',
        TIMESTAMP '2024-05-30 14:00:00', null, 'iceberg', 8)
    """
    )

    execute_spark_query(
        f"""
        INSERT INTO {TABLE_NAME} VALUES
        (1, DATE '2024-01-20',
        TIMESTAMP '2024-02-20 10:00:00', named_struct('a', DATE '2024-02-20', 'b', TIMESTAMP '2024-02-20 10:00:00'), 'vasya', 5)
    """
    )

    creation_expression = get_creation_expression(
        storage_type, TABLE_NAME, started_cluster, table_function=True
    )

    def check_validity_and_get_prunned_files(select_expression):
        settings1 = {
            "use_iceberg_partition_pruning": 0,
            "input_format_parquet_bloom_filter_push_down": 0,
            "input_format_parquet_filter_push_down": 0,
        }
        settings2 = {
            "use_iceberg_partition_pruning": 1,
            "input_format_parquet_bloom_filter_push_down": 0,
            "input_format_parquet_filter_push_down": 0,
        }
        return check_validity_and_get_prunned_files_general(
            instance, TABLE_NAME, settings1, settings2, 'IcebergMinMaxIndexPrunedFiles', select_expression
        )

    assert (
        check_validity_and_get_prunned_files(
            f"SELECT * FROM {creation_expression} WHERE time_struct.a <= '2024-02-01' ORDER BY ALL"
        )
        == 1
    )


@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_bucket_partition_pruning(started_cluster, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = "test_bucket_partition_pruning_" + storage_type + "_" + get_uuid_str()

    def execute_spark_query(query: str):
        return execute_spark_query_general(
            spark,
            started_cluster,
            storage_type,
            TABLE_NAME,
            query,
        )

    execute_spark_query(
        f"""
            CREATE TABLE {TABLE_NAME} (
                id INT,
                name STRING,
                value DECIMAL(10, 2),
                created_at DATE,
                event_time TIMESTAMP
            )
            USING iceberg
            PARTITIONED BY (bucket(3, id), bucket(2, name), bucket(4, value), bucket(5, created_at), bucket(3, event_time))
            OPTIONS('format-version'='2')
        """
    )

    execute_spark_query(
        f"""
        INSERT INTO {TABLE_NAME} VALUES
        (1, 'Alice', 10.50, DATE '2024-01-20', TIMESTAMP '2024-01-20 10:00:00'),
        (2, 'Bob', 20.00, DATE '2024-01-21', TIMESTAMP '2024-01-21 11:00:00'),
        (3, 'Charlie', 30.50, DATE '2024-01-22', TIMESTAMP '2024-01-22 12:00:00'),
        (4, 'Diana', 40.00, DATE '2024-01-23', TIMESTAMP '2024-01-23 13:00:00'),
        (5, 'Eve', 50.50, DATE '2024-01-24', TIMESTAMP '2024-01-24 14:00:00');
        """
    )

    def check_validity_and_get_prunned_files(select_expression):
        settings1 = {
            "use_iceberg_partition_pruning": 0
        }
        settings2 = {
            "use_iceberg_partition_pruning": 1
        }
        return check_validity_and_get_prunned_files_general(
            instance,
            TABLE_NAME,
            settings1,
            settings2,
            "IcebergPartitionPrunedFiles",
            select_expression,
        )

    creation_expression = get_creation_expression(
        storage_type, TABLE_NAME, started_cluster, table_function=True
    )

    queries = [
        f"SELECT * FROM {creation_expression} WHERE id == 1 ORDER BY ALL",
        f"SELECT * FROM {creation_expression} WHERE value == 20.00 OR event_time == '2024-01-24 14:00:00' ORDER BY ALL",
        f"SELECT * FROM {creation_expression} WHERE id == 3 AND name == 'Charlie' ORDER BY ALL",
        f"SELECT * FROM {creation_expression} WHERE (event_time == TIMESTAMP '2024-01-21 11:00:00' AND name == 'Bob') OR (name == 'Eve' AND id == 5) ORDER BY ALL",
    ]

    for query in queries:
        assert check_validity_and_get_prunned_files(query) > 0


@pytest.mark.parametrize("format_version", ["2"])
@pytest.mark.parametrize("storage_type", ["s3"])
def test_cluster_table_function_with_partition_pruning(
    started_cluster, format_version, storage_type
):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session

    TABLE_NAME = (
        "test_cluster_table_function_with_partition_pruning_"
        + format_version
        + "_"
        + storage_type
        + "_"
        + get_uuid_str()
    )

    def execute_spark_query(query: str):
        return execute_spark_query_general(
            spark,
            started_cluster,
            storage_type,
            TABLE_NAME,
            query,
        )

    execute_spark_query(
        f"""
            DROP TABLE IF EXISTS {TABLE_NAME};
        """
    )

    execute_spark_query(
        f"""
            CREATE TABLE {TABLE_NAME} (
                a int,
                b float
            )
            USING iceberg
            PARTITIONED BY (identity(a))
            OPTIONS ('format-version'='{format_version}')
        """
    )

    execute_spark_query(f"INSERT INTO {TABLE_NAME} VALUES (1, 1.0), (2, 2.0), (3, 3.0)")

    table_function_expr_cluster = get_creation_expression(
        storage_type,
        TABLE_NAME,
        started_cluster,
        table_function=True,
        run_on_cluster=True,
    )

    instance.query(f"SELECT * FROM {table_function_expr_cluster} WHERE a = 1")

@pytest.mark.parametrize("storage_type", ["local", "s3"])
def test_compressed_metadata(started_cluster, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = "test_compressed_metadata_" + storage_type + "_" + get_uuid_str()

    table_properties = {
        "write.metadata.compression": "gzip"
    }

    df = spark.createDataFrame([
        (1, "Alice"),
        (2, "Bob")
    ], ["id", "name"])

    # for some reason write.metadata.compression is not working :(
    df.writeTo(TABLE_NAME) \
        .tableProperty("write.metadata.compression", "gzip") \
        .using("iceberg") \
        .create()

    # manual compression of metadata file before upload, still test some scenarios
    subprocess.check_output(f"gzip /iceberg_data/default/{TABLE_NAME}/metadata/v1.metadata.json", shell=True)

    # Weird but compression extension is really in the middle of the file name, not in the end...
    subprocess.check_output(f"mv /iceberg_data/default/{TABLE_NAME}/metadata/v1.metadata.json.gz /iceberg_data/default/{TABLE_NAME}/metadata/v1.gz.metadata.json", shell=True)

    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
    )

    create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster, explicit_metadata_path="")

    assert instance.query(f"SELECT * FROM {TABLE_NAME} WHERE not ignore(*)") == "1\tAlice\n2\tBob\n"


@pytest.mark.parametrize("format_version", ["1", "2"])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_writes(started_cluster, format_version, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session

    TABLE_NAME = "test_row_based_deletes_" + storage_type + "_" + get_uuid_str()

    spark.sql(
        f"CREATE TABLE {TABLE_NAME} (id int) USING iceberg TBLPROPERTIES ('format-version' = '{format_version}')")

    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
    )

    create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster)
    spark.sql(f"INSERT INTO {TABLE_NAME} VALUES (42);")

    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
    )

    instance.query(f"INSERT INTO {TABLE_NAME} VALUES (123);", settings={"allow_experimental_insert_into_iceberg": 1})
    assert instance.query(f"SELECT * FROM {TABLE_NAME} ORDER BY ALL") == '42\n123\n'
    instance.query(f"INSERT INTO {TABLE_NAME} VALUES (456);", settings={"allow_experimental_insert_into_iceberg": 1})
    assert instance.query(f"SELECT * FROM {TABLE_NAME} ORDER BY ALL") == '42\n123\n456\n'

    if storage_type != "local":
        return

    default_download_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
    )

    with open(f"/iceberg_data/default/{TABLE_NAME}/metadata/version-hint.text", "wb") as f:
        f.write(b"4")

    df = spark.read.format("iceberg").load(f"/iceberg_data/default/{TABLE_NAME}").collect()
    assert len(df) == 3


@pytest.mark.parametrize("format_version", ["1", "2"])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_writes_from_zero(started_cluster, format_version, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session

    TABLE_NAME = "test_row_based_deletes_" + storage_type + "_" + get_uuid_str()

    spark.sql(
        f"CREATE TABLE {TABLE_NAME} (id int) USING iceberg TBLPROPERTIES ('format-version' = '{format_version}')")
    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
    )

    create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster)

    instance.query(f"INSERT INTO {TABLE_NAME} VALUES (123);", settings={"allow_experimental_insert_into_iceberg": 1})
    assert instance.query(f"SELECT * FROM {TABLE_NAME} ORDER BY ALL") == '123\n'
    instance.query(f"INSERT INTO {TABLE_NAME} VALUES (456);", settings={"allow_experimental_insert_into_iceberg": 1})
    assert instance.query(f"SELECT * FROM {TABLE_NAME} ORDER BY ALL") == '123\n456\n'

    if storage_type != "local":
        return

    default_download_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
    )

    with open(f"/iceberg_data/default/{TABLE_NAME}/metadata/version-hint.text", "wb") as f:
        f.write(b"3")

    df = spark.read.format("iceberg").load(f"/iceberg_data/default/{TABLE_NAME}").collect()
    assert len(df) == 2


@pytest.mark.parametrize("format_version", ["1", "2"])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_writes_with_partitioned_table(started_cluster, format_version, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = "test_bucket_partition_pruning_" + storage_type + "_" + get_uuid_str()

    def execute_spark_query(query: str):
        spark.sql(query)
        default_upload_directory(
            started_cluster,
            storage_type,
            f"/iceberg_data/default/{TABLE_NAME}/",
            f"/iceberg_data/default/{TABLE_NAME}/",
        )
        return

    execute_spark_query(
        f"""
            CREATE TABLE {TABLE_NAME} (
                id INT,
                name STRING,
                value DECIMAL(10, 2),
                created_at DATE,
                event_time TIMESTAMP
            )
            USING iceberg
            PARTITIONED BY (bucket(3, id), bucket(2, name), bucket(5, created_at), bucket(3, event_time))
            OPTIONS('format-version'='{format_version}')
        """
    )
    create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster)

    instance.query(
        f"""
        INSERT INTO {TABLE_NAME} VALUES
        (1, 'Alice', 10.50, DATE '2024-01-20', TIMESTAMP '2024-01-20 10:00:00'),
        (2, 'Bob', 20.00, DATE '2024-01-21', TIMESTAMP '2024-01-21 11:00:00'),
        (3, 'Charlie', 30.50, DATE '2024-01-22', TIMESTAMP '2024-01-22 12:00:00'),
        (4, 'Diana', 40.00, DATE '2024-01-23', TIMESTAMP '2024-01-23 13:00:00'),
        (5, 'Eve', 50.50, DATE '2024-01-24', TIMESTAMP '2024-01-24 14:00:00');
        """,
        settings={"allow_experimental_insert_into_iceberg": 1}
    )

    assert instance.query(f"SELECT * FROM {TABLE_NAME} ORDER BY ALL") == '1\tAlice\t10.5\t2024-01-20\t2024-01-20 10:00:00.000000\n2\tBob\t20\t2024-01-21\t2024-01-21 11:00:00.000000\n3\tCharlie\t30.5\t2024-01-22\t2024-01-22 12:00:00.000000\n4\tDiana\t40\t2024-01-23\t2024-01-23 13:00:00.000000\n5\tEve\t50.5\t2024-01-24\t2024-01-24 14:00:00.000000\n'

    instance.query(
        f"""
        INSERT INTO {TABLE_NAME} VALUES
        (10, 'Alice', 10.50, DATE '2024-01-20', TIMESTAMP '2024-01-20 10:00:00'),
        (20, 'Bob', 20.00, DATE '2024-01-21', TIMESTAMP '2024-01-21 11:00:00'),
        (30, 'Charlie', 30.50, DATE '2024-01-22', TIMESTAMP '2024-01-22 12:00:00'),
        (40, 'Diana', 40.00, DATE '2024-01-23', TIMESTAMP '2024-01-23 13:00:00'),
        (50, 'Eve', 50.50, DATE '2024-01-24', TIMESTAMP '2024-01-24 14:00:00');
        """,
        settings={"allow_experimental_insert_into_iceberg": 1}
    )

    assert instance.query(f"SELECT * FROM {TABLE_NAME} ORDER BY ALL") == '1\tAlice\t10.5\t2024-01-20\t2024-01-20 10:00:00.000000\n2\tBob\t20\t2024-01-21\t2024-01-21 11:00:00.000000\n3\tCharlie\t30.5\t2024-01-22\t2024-01-22 12:00:00.000000\n4\tDiana\t40\t2024-01-23\t2024-01-23 13:00:00.000000\n5\tEve\t50.5\t2024-01-24\t2024-01-24 14:00:00.000000\n10\tAlice\t10.5\t2024-01-20\t2024-01-20 10:00:00.000000\n20\tBob\t20\t2024-01-21\t2024-01-21 11:00:00.000000\n30\tCharlie\t30.5\t2024-01-22\t2024-01-22 12:00:00.000000\n40\tDiana\t40\t2024-01-23\t2024-01-23 13:00:00.000000\n50\tEve\t50.5\t2024-01-24\t2024-01-24 14:00:00.000000\n'

    if storage_type != "local":
        return

    default_download_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
    )

    with open(f"/iceberg_data/default/{TABLE_NAME}/metadata/version-hint.text", "wb") as f:
        f.write(b"3")

    df = spark.read.format("iceberg").load(f"/iceberg_data/default/{TABLE_NAME}").collect()
    assert len(df) == 10

@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_minmax_pruning_for_arrays_and_maps_subfields_disabled(started_cluster, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = (
        "test_disable_minmax_pruning_for_arrays_and_maps_subfields_"
        + storage_type
        + "_"
        + get_uuid_str()
    )

    def execute_spark_query(query: str):
        return execute_spark_query_general(
            spark,
            started_cluster,
            storage_type,
            TABLE_NAME,
            query,
        )

    execute_spark_query(
        f"""
            DROP TABLE IF EXISTS {TABLE_NAME};
        """
    )

    execute_spark_query(
        f"""
            CREATE TABLE {TABLE_NAME} (
            id BIGINT,
            measurements ARRAY<DOUBLE>
            ) USING iceberg
            TBLPROPERTIES (
            'write.metadata.metrics.max' = 'measurements.element',
            'write.metadata.metrics.min' = 'measurements.element'
            );
        """
    )

    execute_spark_query(
        f"""
            INSERT INTO {TABLE_NAME} VALUES
            (1, array(23.5, 24.1, 22.8, 25.3, 23.9)),
            (2, array(18.2, 19.5, 17.8, 20.1, 19.3, 18.7)),
            (3, array(30.0, 31.2, 29.8, 32.1, 30.5, 29.9, 31.0)),
            (4, array(15.5, 16.2, 14.8, 17.1, 16.5)),
            (5, array(27.3, 28.1, 26.9, 29.2, 28.5, 27.8, 28.3, 27.6));
        """
    )

    default_upload_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
    )

    table_creation_expression = get_creation_expression(
        storage_type,
        TABLE_NAME,
        started_cluster,
        table_function=True,
        allow_dynamic_metadata_for_data_lakes=True,
    )

    table_select_expression = table_creation_expression

    instance.query(f"SELECT * FROM {table_select_expression} ORDER BY ALL")


@pytest.mark.parametrize("format_version", [1, 2])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
def test_writes_create_table(started_cluster, format_version, storage_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = "test_bucket_partition_pruning_" + storage_type + "_" + get_uuid_str()

    create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster, "(x String)", format_version)

    with pytest.raises(Exception):
        create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster, "(x String)", format_version)

    create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster, "(x String)", format_version, "", True)    

    assert instance.query(f"SELECT * FROM {TABLE_NAME} ORDER BY ALL") == ''

    instance.query(f"INSERT INTO {TABLE_NAME} VALUES (123);", settings={"allow_experimental_insert_into_iceberg": 1})
    assert instance.query(f"SELECT * FROM {TABLE_NAME} ORDER BY ALL") == '123\n'
    instance.query(f"INSERT INTO {TABLE_NAME} VALUES (456);", settings={"allow_experimental_insert_into_iceberg": 1})
    assert instance.query(f"SELECT * FROM {TABLE_NAME} ORDER BY ALL") == '123\n456\n'

    if storage_type != "local":
        return

    default_download_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
    )

    with open(f"/iceberg_data/default/{TABLE_NAME}/metadata/version-hint.text", "wb") as f:
        f.write(b"2")

    df = spark.read.format("iceberg").load(f"/iceberg_data/default/{TABLE_NAME}").collect()
    assert len(df) == 2


@pytest.mark.parametrize("format_version", [1, 2])
@pytest.mark.parametrize("storage_type", ["s3", "azure", "local"])
@pytest.mark.parametrize("partition_type", ["identity(y)", "(identity(y))", "icebergTruncate(3, y)", "(identity(y), icebergBucket(3, x))"])
def test_writes_create_partitioned_table(started_cluster, format_version, storage_type, partition_type):
    instance = started_cluster.instances["node1"]
    spark = started_cluster.spark_session
    TABLE_NAME = "test_bucket_partition_pruning_" + storage_type + "_" + get_uuid_str()

    create_iceberg_table(storage_type, instance, TABLE_NAME, started_cluster, "(x String, y Int64)", format_version, partition_type)

    assert instance.query(f"SELECT * FROM {TABLE_NAME} ORDER BY ALL") == ''

    instance.query(f"INSERT INTO {TABLE_NAME} VALUES ('123', 1);", settings={"allow_experimental_insert_into_iceberg": 1})
    assert instance.query(f"SELECT * FROM {TABLE_NAME} ORDER BY ALL") == '123\t1\n'

    if storage_type != "local":
        return

    default_download_directory(
        started_cluster,
        storage_type,
        f"/iceberg_data/default/{TABLE_NAME}/",
        f"/iceberg_data/default/{TABLE_NAME}/",
    )

    with open(f"/iceberg_data/default/{TABLE_NAME}/metadata/version-hint.text", "wb") as f:
        f.write(b"2")

    df = spark.read.format("iceberg").load(f"/iceberg_data/default/{TABLE_NAME}").collect()
    assert len(df) == 1
