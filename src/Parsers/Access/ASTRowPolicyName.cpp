#include <Parsers/Access/ASTRowPolicyName.h>
#include <Common/quoteString.h>
#include <IO/Operators.h>


namespace DB
{
namespace ErrorCodes
{
    extern const int LOGICAL_ERROR;
}


void ASTRowPolicyName::formatImpl(WriteBuffer & ostr, const FormatSettings & settings, FormatState &, FormatStateStacked) const
{
    const String & database = full_name.database;
    const String & table_name = full_name.table_name;
    const String & short_name = full_name.short_name;
    ostr << backQuoteIfNeed(short_name) << " ON "
                  << (database.empty() ? String{} : backQuoteIfNeed(database) + ".")
                  << backQuoteIfNeed(table_name);

    formatOnCluster(ostr, settings);
}


void ASTRowPolicyName::replaceEmptyDatabase(const String & current_database)
{
    if (full_name.database.empty())
        full_name.database = current_database;
}

String ASTRowPolicyNames::tableOrAsterisk(const String & table_name) const
{
    return table_name == RowPolicyName::ANY_TABLE_MARK ? "*" : backQuoteIfNeed(table_name);
}


void ASTRowPolicyNames::formatImpl(WriteBuffer & ostr, const FormatSettings & settings, FormatState &, FormatStateStacked) const
{
    if (full_names.empty())
        throw Exception(ErrorCodes::LOGICAL_ERROR, "No names of row policies in AST");

    bool same_short_name = true;
    if (full_names.size() > 1)
    {
        for (size_t i = 1; i != full_names.size(); ++i)
            if (full_names[i].short_name != full_names[0].short_name)
            {
                same_short_name = false;
                break;
            }
    }

    bool same_db_and_table_name = true;
    if (full_names.size() > 1)
    {
        for (size_t i = 1; i != full_names.size(); ++i)
            if ((full_names[i].database != full_names[0].database) || (full_names[i].table_name != full_names[0].table_name))
            {
                same_db_and_table_name = false;
                break;
            }
    }

    if (same_short_name)
    {
        const String & short_name = full_names[0].short_name;
        ostr << backQuoteIfNeed(short_name) << " ON "
                     ;

        bool need_comma = false;
        for (const auto & full_name : full_names)
        {
            if (std::exchange(need_comma, true))
                ostr << ", ";
            const String & database = full_name.database;
            const String & table_name = full_name.table_name;
            if (!database.empty())
                ostr << backQuoteIfNeed(database) + ".";
            ostr << tableOrAsterisk(table_name);
        }
    }
    else if (same_db_and_table_name)
    {
        bool need_comma = false;
        for (const auto & full_name : full_names)
        {
            if (std::exchange(need_comma, true))
                ostr << ", ";
            const String & short_name = full_name.short_name;
            ostr << backQuoteIfNeed(short_name);
        }

        const String & database = full_names[0].database;
        const String & table_name = full_names[0].table_name;
        ostr << " ON ";
        if (!database.empty())
            ostr << backQuoteIfNeed(database) + ".";
        ostr << tableOrAsterisk(table_name);
    }
    else
    {
        bool need_comma = false;
        for (const auto & full_name : full_names)
        {
            if (std::exchange(need_comma, true))
                ostr << ", ";
            const String & short_name = full_name.short_name;
            const String & database = full_name.database;
            const String & table_name = full_name.table_name;
            ostr << backQuoteIfNeed(short_name) << " ON "
                         ;
            if (!database.empty())
                ostr << backQuoteIfNeed(database) + ".";
            ostr << tableOrAsterisk(table_name);
        }
    }

    formatOnCluster(ostr, settings);
}


Strings ASTRowPolicyNames::toStrings() const
{
    Strings res;
    res.reserve(full_names.size());
    for (const auto & full_name : full_names)
        res.emplace_back(full_name.toString());
    return res;
}


void ASTRowPolicyNames::replaceEmptyDatabase(const String & current_database)
{
    for (auto & full_name : full_names)
        if (full_name.database.empty())
            full_name.database = current_database;
}

}
