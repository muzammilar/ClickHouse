#include "config.h"

#if USE_DELTA_KERNEL_RS
#include <Storages/ObjectStorage/DataLakes/DeltaLake/getSchemaFromSnapshot.h>
#include <Storages/ObjectStorage/DataLakes/DeltaLake/KernelUtils.h>
#include <Storages/ObjectStorage/DataLakes/DeltaLake/KernelPointerWrapper.h>

#include <base/scope_guard.h>
#include <Core/TypeId.h>
#include <Common/logger_useful.h>

#include <DataTypes/DataTypeArray.h>
#include <DataTypes/DataTypeTuple.h>
#include <DataTypes/DataTypeFactory.h>
#include <DataTypes/DataTypesDecimal.h>
#include <DataTypes/DataTypeDecimalBase.h>
#include <DataTypes/DataTypeNullable.h>
#include <DataTypes/DataTypeMap.h>

#include "delta_kernel_ffi.hpp"

namespace DB
{
namespace ErrorCodes
{
    extern const int LOGICAL_ERROR;
    extern const int NOT_IMPLEMENTED;
}

namespace
{
    DataTypePtr getSimpleDataTypeFromTypeIndex(TypeIndex type_index)
    {
        std::string_view name = magic_enum::enum_name(type_index);
        return DB::DataTypeFactory::instance().get(std::string(name), nullptr);
    }

    bool isSimpleDataType(TypeIndex type_index)
    {
        switch (type_index)
        {
            case TypeIndex::UInt8: [[fallthrough]];
            case TypeIndex::UInt16: [[fallthrough]];
            case TypeIndex::UInt32: [[fallthrough]];
            case TypeIndex::UInt64: [[fallthrough]];
            case TypeIndex::UInt128: [[fallthrough]];
            case TypeIndex::UInt256: [[fallthrough]];
            case TypeIndex::Int8: [[fallthrough]];
            case TypeIndex::Int16: [[fallthrough]];
            case TypeIndex::Int32: [[fallthrough]];
            case TypeIndex::Int64: [[fallthrough]];
            case TypeIndex::Int128: [[fallthrough]];
            case TypeIndex::Int256: [[fallthrough]];
            case TypeIndex::Float32: [[fallthrough]];
            case TypeIndex::Float64: [[fallthrough]];
            case TypeIndex::Date: [[fallthrough]];
            case TypeIndex::Date32: [[fallthrough]];
            case TypeIndex::DateTime: [[fallthrough]];
            case TypeIndex::DateTime64: [[fallthrough]];
            case TypeIndex::UUID: [[fallthrough]];
            case TypeIndex::String:
                return true;
            default:
                return false;
        }
    }
}
}

namespace DeltaLake
{

/**
 * A helper class for SchemaVisitor.
 * Holds state for SchemaVisitor methods to collect visitor result.
 */
class SchemaVisitorData
{
    friend class SchemaVisitor;

public:
    struct SchemaResult
    {
        DB::NamesAndTypesList names_and_types;
        DB::NameToNameMap physical_names_map;
    };
    SchemaResult getSchemaResult();
    const DB::Names & getPartitionColumns() const { return partition_columns; }

private:
    DB::DataTypes getDataTypesFromTypeList(size_t list_idx);

    struct Field
    {
        Field(
            const std::string & name_,
            const DB::TypeIndex & type_,
            bool nullable_,
            const std::string & physical_name_)
            : name(name_), type(type_), nullable(nullable_), physical_name(physical_name_) {}

        /// Column name.
        const std::string name;
        /// Column type.
        const DB::TypeIndex type;
        /// Column nullability.
        const bool nullable;
        /// In case of columnMapping.mode = 'name',
        /// physical name of the column in parquet metadata
        /// will be different from table schema column name.
        const std::string physical_name;

        /// If type is complex (array, map, struct), whether it can contain nullable values.
        bool value_contains_null;
        /// If type is complex (array, map, struct), list id of the child list.
        size_t child_list_id;

        size_t precision = 0; /// For Decimal.
        size_t scale = 0; /// For Decimal.

        /// There is no TypeIndex::Bool, so we need to tell
        /// when it is int8 and when it is bool.
        bool is_bool = false;
    };
    using Fields = std::vector<Field>;

    /// See delta-kernel-rs/fii/src/schema.rs for type_lists explanation.
    std::unordered_map<size_t, std::unique_ptr<Fields>> type_lists;
    /// Global counter for type lists.
    size_t list_counter = 0;
    /// A list of partition columns.
    /// Partition columns are not shown in global read schema,
    /// because they are not stored in the actual data,
    /// but instead in data paths directories.
    DB::Names partition_columns;

    const LoggerPtr log = getLogger("SchemaVisitor");

    using KernelScan = KernelPointerWrapper<ffi::SharedScan, ffi::free_scan>;
};

/**
 * A schema visitor class.
 * To get table schema, call visitTableSchema().
 * To get read schema, call visitReadSchema().
 * To get partition columns, call visitPartitionColumns().
 */
class SchemaVisitor
{
    using KernelSharedSchema = KernelPointerWrapper<ffi::SharedSchema, ffi::free_schema>;
    using KernelStringSliceIterator = KernelPointerWrapper<ffi::StringSliceIterator, ffi::free_string_slice_data>;
public:
    static void visitTableSchema(ffi::SharedSnapshot * snapshot, SchemaVisitorData & data)
    {
        KernelSharedSchema schema(ffi::logical_schema(snapshot));
        auto visitor = createVisitor(data);
        [[maybe_unused]] size_t result = ffi::visit_schema(schema.get(), &visitor);
        chassert(result == 0, "Unexpected result: " + DB::toString(result));
    }

    static void visitReadSchema(
        ffi::SharedScan * scan,
        SchemaVisitorData & data)
    {
        KernelSharedSchema schema(ffi::scan_physical_schema(scan));
        auto visitor = createVisitor(data);
        [[maybe_unused]] size_t result = ffi::visit_schema(schema.get(), &visitor);
        chassert(result == 0, "Unexpected result: " + DB::toString(result));
    }

    static void visitPartitionColumns(
        ffi::SharedSnapshot * snapshot,
        SchemaVisitorData & data)
    {
        KernelStringSliceIterator partition_columns_iter(ffi::get_partition_columns(snapshot));
        while (ffi::string_slice_next(partition_columns_iter.get(), &data, &visitPartitionColumn)) {}
    }

private:
    static ffi::EngineSchemaVisitor createVisitor(SchemaVisitorData & data)
    {
        ffi::EngineSchemaVisitor visitor;
        visitor.data = &data;
        visitor.make_field_list = &makeFieldList;

        visitor.visit_boolean = &simpleTypeVisitor<DB::TypeIndex::Int8, true>;
        visitor.visit_string = &simpleTypeVisitor<DB::TypeIndex::String>;
        visitor.visit_long = &simpleTypeVisitor<DB::TypeIndex::Int64>;
        visitor.visit_integer = &simpleTypeVisitor<DB::TypeIndex::Int32>;
        visitor.visit_short = &simpleTypeVisitor<DB::TypeIndex::Int16>;
        visitor.visit_byte = &simpleTypeVisitor<DB::TypeIndex::Int8>;
        visitor.visit_float = &simpleTypeVisitor<DB::TypeIndex::Float32>;
        visitor.visit_double = &simpleTypeVisitor<DB::TypeIndex::Float64>;
        visitor.visit_binary = &simpleTypeVisitor<DB::TypeIndex::String>;
        visitor.visit_date = &simpleTypeVisitor<DB::TypeIndex::Date32>;
        visitor.visit_timestamp = &simpleTypeVisitor<DB::TypeIndex::DateTime64>;
        visitor.visit_timestamp_ntz = &simpleTypeVisitor<DB::TypeIndex::DateTime64>;

        visitor.visit_array = &arrayTypeVisitor;
        visitor.visit_struct = &tupleTypeVisitor;
        visitor.visit_map = &mapTypeVisitor;
        visitor.visit_decimal = &decimalTypeVisitor;

        return visitor;
    }

    static void visitPartitionColumn(void * data, ffi::KernelStringSlice slice)
    {
        SchemaVisitorData * state = static_cast<SchemaVisitorData *>(data);
        state->partition_columns.push_back(KernelUtils::fromDeltaString(slice));
    }

    static uintptr_t makeFieldList(void * data, uintptr_t capacity_hint)
    {
        SchemaVisitorData * state = static_cast<SchemaVisitorData *>(data);
        size_t id = state->list_counter++;

        auto list = std::make_unique<SchemaVisitorData::Fields>();
        if (capacity_hint > 0)
            list->reserve(capacity_hint);

        state->type_lists.emplace(id, std::move(list));
        return id;
    }

    static std::unique_ptr<std::string> extractPhysicalName(const ffi::CStringMap * metadata)
    {
        std::string * physical_name = static_cast<std::string *>(ffi::get_from_string_map(
            metadata,
            KernelUtils::toDeltaString("delta.columnMapping.physicalName"),
            KernelUtils::allocateString));
        return physical_name ? std::unique_ptr<std::string>(physical_name) : nullptr;
    }

    template <DB::TypeIndex type, bool is_bool = false>
    static void simpleTypeVisitor(
        void * data,
        uintptr_t sibling_list_id,
        ffi::KernelStringSlice name,
        bool nullable,
        const ffi::CStringMap * metadata)
    {
        SchemaVisitorData * state = static_cast<SchemaVisitorData *>(data);
        auto it = state->type_lists.find(sibling_list_id);
        if (it == state->type_lists.end())
        {
            throw DB::Exception(
                DB::ErrorCodes::LOGICAL_ERROR,
                "List with id {} does not exist", sibling_list_id);
        }

        const std::string column_name(name.ptr, name.len);
        const auto physical_name_ptr = extractPhysicalName(metadata);
        const std::string physical_name = physical_name_ptr ? *physical_name_ptr : "";

        LOG_TEST(
            state->log,
            "List id: {}, column name: {} (physical name: {}), type: {}, nullable: {}",
            sibling_list_id, column_name, physical_name, type, nullable);

        SchemaVisitorData::Field field(column_name, std::move(type), nullable, physical_name);
        field.is_bool = is_bool;
        it->second->push_back(std::move(field));
    }

    static void decimalTypeVisitor(
        void * data,
        uintptr_t sibling_list_id,
        ffi::KernelStringSlice name,
        bool nullable,
        const ffi::CStringMap * metadata,
        uint8_t precision,
        uint8_t scale)
    {
        const auto type = DB::TypeIndex::Decimal32;
        SchemaVisitorData * state = static_cast<SchemaVisitorData *>(data);
        auto it = state->type_lists.find(sibling_list_id);
        if (it == state->type_lists.end())
        {
            throw DB::Exception(
                DB::ErrorCodes::LOGICAL_ERROR,
                "List with id {} does not exist", sibling_list_id);
        }

        const std::string column_name(name.ptr, name.len);
        const auto physical_name_ptr = extractPhysicalName(metadata);
        const std::string physical_name = physical_name_ptr ? *physical_name_ptr : "";

        LOG_TEST(
            state->log,
            "List id: {}, column name: {} (physical name: {}), type: {}, nullable: {}",
            sibling_list_id, column_name, physical_name, type, nullable);

        SchemaVisitorData::Field field(column_name, type, nullable, physical_name);
        field.precision = precision;
        field.scale = scale;
        it->second->push_back(std::move(field));
    }

    static void arrayTypeVisitor(
        void * data,
        uintptr_t sibling_list_id,
        ffi::KernelStringSlice name,
        bool nullable,
        const ffi::CStringMap * metadata,
        uintptr_t child_list_id)
    {
        listBasedTypeVisitor<DB::TypeIndex::Array>(data, sibling_list_id, name, nullable, metadata, child_list_id);
    }

    static void tupleTypeVisitor(
        void * data,
        uintptr_t sibling_list_id,
        ffi::KernelStringSlice name,
        bool nullable,
        const ffi::CStringMap * metadata,
        uintptr_t child_list_id)
    {
        listBasedTypeVisitor<DB::TypeIndex::Tuple>(data, sibling_list_id, name, nullable, metadata, child_list_id);
    }

    static void mapTypeVisitor(
        void *data,
        uintptr_t sibling_list_id,
        ffi::KernelStringSlice name,
        bool nullable,
        const ffi::CStringMap * metadata,
        uintptr_t child_list_id)
    {
        listBasedTypeVisitor<DB::TypeIndex::Map>(data, sibling_list_id, name, nullable, metadata, child_list_id);
    }

    template <DB::TypeIndex type>
    static void listBasedTypeVisitor(
        void * data,
        uintptr_t sibling_list_id,
        ffi::KernelStringSlice name,
        bool nullable,
        const ffi::CStringMap * metadata,
        uintptr_t child_list_id)
    {
        SchemaVisitorData * state = static_cast<SchemaVisitorData *>(data);
        auto it = state->type_lists.find(sibling_list_id);
        if (it == state->type_lists.end())
        {
            throw DB::Exception(
                DB::ErrorCodes::LOGICAL_ERROR,
                "List with id {} does not exist", sibling_list_id);
        }

        const std::string column_name(name.ptr, name.len);
        const auto physical_name_ptr = extractPhysicalName(metadata);
        const std::string physical_name = physical_name_ptr ? *physical_name_ptr : "";

        LOG_TEST(
            state->log,
            "List id: {}, column name: {} (physical name: {}), type: {}, "
            "nullable: {}, child list id: {}",
            sibling_list_id, column_name, physical_name, type, nullable, child_list_id);

        SchemaVisitorData::Field field(column_name, std::move(type), nullable, physical_name);
        field.child_list_id = child_list_id;
        it->second->push_back(field);
    }
};

SchemaVisitorData::SchemaResult SchemaVisitorData::getSchemaResult()
{
    const auto types = getDataTypesFromTypeList(0);
    chassert(types.size() == type_lists[0]->size());

    std::list<DB::NameAndTypePair> names_and_types;
    SchemaResult result;
    for (size_t i = 0; i < types.size(); ++i)
    {
        const auto & field = (*type_lists[0])[i];
        names_and_types.emplace_back(field.name, types[i]);
        if (!field.physical_name.empty())
        {
            [[maybe_unused]] bool inserted = result.physical_names_map.emplace(field.name, field.physical_name).second;
            chassert(inserted);
        }
    }
    result.names_and_types = DB::NamesAndTypesList(names_and_types.begin(), names_and_types.end());
    return result;
}

DB::DataTypes SchemaVisitorData::getDataTypesFromTypeList(size_t list_idx)
{
    DB::DataTypes types;
    for (const auto & field : *type_lists[list_idx])
    {
        if (field.is_bool)
        {
            auto type = DB::DataTypeFactory::instance().get("Bool");
            if (field.nullable)
                type = std::make_shared<DB::DataTypeNullable>(type);

            types.push_back(type);
        }
        else if (field.type == DB::TypeIndex::Decimal32)
        {
            auto type = DB::createDecimal<DB::DataTypeDecimal>(field.precision, field.scale);
            if (field.nullable)
                type = std::make_shared<DB::DataTypeNullable>(type);

            types.push_back(type);
        }
        else if (field.type == DB::TypeIndex::DateTime64)
        {
            DB::DataTypePtr type = std::make_shared<DB::DataTypeDateTime64>(6);
            if (field.nullable)
                type = std::make_shared<DB::DataTypeNullable>(type);

            types.push_back(type);
        }
        else if (DB::isSimpleDataType(field.type))
        {
            auto type = DB::getSimpleDataTypeFromTypeIndex(field.type);
            if (field.nullable)
                type = std::make_shared<DB::DataTypeNullable>(type);

            types.push_back(type);
        }
        else
        {
            if (!field.child_list_id)
            {
                throw DB::Exception(
                    DB::ErrorCodes::NOT_IMPLEMENTED,
                    "Unsupported simple data type: {}", field.type);
            }

            DB::WhichDataType which(field.type);
            if (which.isTuple())
            {
                auto child_types = getDataTypesFromTypeList(field.child_list_id);
                types.push_back(std::make_shared<DB::DataTypeTuple>(child_types));
            }
            else if (which.isArray())
            {
                auto child_types = getDataTypesFromTypeList(field.child_list_id);
                if (child_types.size() != 1)
                {
                    throw DB::Exception(
                        DB::ErrorCodes::LOGICAL_ERROR,
                        "Unexpected number of types in array: {}",
                        child_types.size());
                }

                types.push_back(std::make_shared<DB::DataTypeArray>(child_types[0]));
            }
            else if (which.isMap())
            {
                auto child_types = getDataTypesFromTypeList(field.child_list_id);
                if (child_types.size() != 2)
                {
                    throw DB::Exception(
                        DB::ErrorCodes::LOGICAL_ERROR,
                        "Unexpected number of types in array: {}",
                        child_types.size());
                }
                types.push_back(std::make_shared<DB::DataTypeMap>(child_types[0], child_types[1]));
            }
            else
            {
                throw DB::Exception(
                    DB::ErrorCodes::NOT_IMPLEMENTED,
                    "Column {} has unsupported complex data type: {}", field.name, field.type);
            }
        }
    }
    return types;
}

std::pair<DB::NamesAndTypesList, DB::NameToNameMap> getTableSchemaFromSnapshot(ffi::SharedSnapshot * snapshot)
{
    SchemaVisitorData data;
    SchemaVisitor::visitTableSchema(snapshot, data);
    auto result = data.getSchemaResult();
    return {result.names_and_types, result.physical_names_map};
}

DB::NamesAndTypesList getReadSchemaFromSnapshot(ffi::SharedScan * scan)
{
    SchemaVisitorData data;
    SchemaVisitor::visitReadSchema(scan, data);
    return data.getSchemaResult().names_and_types;
}

DB::Names getPartitionColumnsFromSnapshot(ffi::SharedSnapshot * snapshot)
{
    SchemaVisitorData data;
    SchemaVisitor::visitPartitionColumns(snapshot, data);
    return data.getPartitionColumns();
}

}

#endif
