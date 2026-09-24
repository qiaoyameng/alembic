.. change::
    :tags: feature, operations

    Added :paramref:`~.Operations.batch_alter_table.column_migration_expressions`
    to :meth:`.Operations.batch_alter_table`, allowing explicit SQL
    expressions to be stated for individual columns of the recreated table
    when running in "move and copy" mode.  Expressions are evaluated by the
    database against the original table as part of the ``INSERT ... SELECT``
    copy, may refer to columns dropped by the same batch, and take
    precedence over server defaults for newly added columns.  Invalid
    targets, generated columns, and cross-table references are rejected
    before the original table is altered.
