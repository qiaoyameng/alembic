.. change::
    :tags: feature, sqlite

    Added first-version support for plain SQL views, oriented towards
    SQLite.  New :meth:`.Operations.create_view` and
    :meth:`.Operations.drop_view` directives emit dialect-quoted
    ``CREATE VIEW`` / ``DROP VIEW`` statements, and the new
    :paramref:`.EnvironmentContext.configure.managed_views` parameter
    accepts a collection of :class:`.ViewDefinition` / :class:`.RemovedView`
    declarations for autogenerate.  Autogenerate manages only declared
    views, orders drops and recreates along the declared dependency graph,
    and produces reversible operations; removal of a previously managed
    view is declared explicitly with :class:`.RemovedView`.  Materialized
    views, routines, and SQL dependency parsing are not supported.

