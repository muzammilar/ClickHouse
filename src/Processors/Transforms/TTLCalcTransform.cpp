#include <Processors/Transforms/TTLCalcTransform.h>

#include <Processors/Port.h>
#include <Processors/TTL/TTLUpdateInfoAlgorithm.h>

namespace DB
{

static TTLExpressions getExpressions(const TTLDescription & ttl_descr, PreparedSets::Subqueries & subqueries_for_sets, const ContextPtr & context)
{
    auto expr = ttl_descr.buildExpression(context);
    auto expr_queries = expr.sets->getSubqueries();
    subqueries_for_sets.insert(subqueries_for_sets.end(), expr_queries.begin(), expr_queries.end());

    auto where_expr = ttl_descr.buildWhereExpression(context);
    if (where_expr.sets)
    {
        auto where_expr_queries = where_expr.sets->getSubqueries();
        subqueries_for_sets.insert(subqueries_for_sets.end(), where_expr_queries.begin(), where_expr_queries.end());
    }

    return {expr.expression, where_expr.expression};
}

TTLCalcTransform::TTLCalcTransform(
    const ContextPtr & context,
    SharedHeader header_,
    const MergeTreeData & storage_,
    const StorageMetadataPtr & metadata_snapshot_,
    const MergeTreeData::MutableDataPartPtr & data_part_,
    time_t current_time_,
    bool force_)
    : IAccumulatingTransform(header_, header_)
    , data_part(data_part_)
    , log(getLogger(storage_.getLogName() + " (TTLCalcTransform)"))
{
    auto old_ttl_infos = data_part->ttl_infos;

    if (metadata_snapshot_->hasRowsTTL())
    {
        const auto & rows_ttl = metadata_snapshot_->getRowsTTL();
        algorithms.emplace_back(std::make_unique<TTLUpdateInfoAlgorithm>(
            getExpressions(rows_ttl, subqueries_for_sets, context), rows_ttl,
            TTLUpdateField::TABLE_TTL, rows_ttl.result_column, old_ttl_infos.table_ttl, current_time_, force_));
    }

    for (const auto & where_ttl : metadata_snapshot_->getRowsWhereTTLs())
        algorithms.emplace_back(std::make_unique<TTLUpdateInfoAlgorithm>(
            getExpressions(where_ttl, subqueries_for_sets, context), where_ttl,
            TTLUpdateField::ROWS_WHERE_TTL, where_ttl.result_column, old_ttl_infos.rows_where_ttl[where_ttl.result_column], current_time_, force_));

    for (const auto & group_by_ttl : metadata_snapshot_->getGroupByTTLs())
        algorithms.emplace_back(std::make_unique<TTLUpdateInfoAlgorithm>(
            getExpressions(group_by_ttl, subqueries_for_sets, context), group_by_ttl,
            TTLUpdateField::GROUP_BY_TTL, group_by_ttl.result_column, old_ttl_infos.group_by_ttl[group_by_ttl.result_column], current_time_, force_));

    if (metadata_snapshot_->hasAnyColumnTTL())
    {
        for (const auto & [name, description] : metadata_snapshot_->getColumnTTLs())
        {
            algorithms.emplace_back(std::make_unique<TTLUpdateInfoAlgorithm>(
                getExpressions(description, subqueries_for_sets, context), description,
                TTLUpdateField::COLUMNS_TTL, name, old_ttl_infos.columns_ttl[name], current_time_, force_));
        }
    }

    for (const auto & move_ttl : metadata_snapshot_->getMoveTTLs())
        algorithms.emplace_back(std::make_unique<TTLUpdateInfoAlgorithm>(
            getExpressions(move_ttl, subqueries_for_sets, context), move_ttl,
            TTLUpdateField::MOVES_TTL, move_ttl.result_column, old_ttl_infos.moves_ttl[move_ttl.result_column], current_time_, force_));

    for (const auto & recompression_ttl : metadata_snapshot_->getRecompressionTTLs())
        algorithms.emplace_back(std::make_unique<TTLUpdateInfoAlgorithm>(
            getExpressions(recompression_ttl, subqueries_for_sets, context), recompression_ttl,
            TTLUpdateField::RECOMPRESSION_TTL, recompression_ttl.result_column, old_ttl_infos.recompression_ttl[recompression_ttl.result_column], current_time_, force_));
}

void TTLCalcTransform::consume(Chunk chunk)
{
    auto block = getInputPort().getHeader().cloneWithColumns(chunk.detachColumns());
    for (const auto & algorithm : algorithms)
        algorithm->execute(block);

    if (block.empty())
        return;

    Chunk res;
    for (const auto & col : getOutputPort().getHeader())
        res.addColumn(block.getByName(col.name).column);

    setReadyChunk(std::move(res));
}

Chunk TTLCalcTransform::generate()
{
    Block block;
    for (const auto & algorithm : algorithms)
        algorithm->execute(block);

    if (block.empty())
        return {};

    Chunk res;
    for (const auto & col : getOutputPort().getHeader())
        res.addColumn(block.getByName(col.name).column);

    return res;
}

void TTLCalcTransform::finalize()
{
    data_part->ttl_infos = {};
    for (const auto & algorithm : algorithms)
        algorithm->finalize(data_part);
}

IProcessor::Status TTLCalcTransform::prepare()
{
    auto status = IAccumulatingTransform::prepare();
    if (status == Status::Finished)
        finalize();

    return status;
}

}
