.. change::
    :tags: feature, autogenerate

    Added support for explicit column rename hints in autogenerate via the
    new :paramref:`.EnvironmentContext.configure.column_renames` parameter.
    Hints are given as a dictionary keyed by ``(schema, table_name)`` tuples
    mapping existing column names to new column names, allowing a renamed
    column to be detected as a reversible :meth:`.Operations.alter_column`
    with ``new_column_name`` rather than a ``DROP`` / ``ADD`` pair that
    would lose data.  Hints are validated against the reflected database
    and target metadata before any operations are generated, indexes and
    constraints that refer to the column are matched across the rename,
    and batch mode rendering is supported.  See
    :ref:`autogen_column_renames` for details.
